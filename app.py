import json, cgi, os, time, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import db, auth_token, validate, object_store, logutil, billing, metrics
import state_machine as sm
from cache import TTLCache

HOST = os.environ.get("HOST", "127.0.0.1")  # 容器里需设为 0.0.0.0，生产默认只监听本机
PORT = int(os.environ.get("PORT", "8001"))
NAME = f"api-{PORT}"
log = logutil.setup(NAME)

# ===== 无状态约定 =====
# 这个 API 实例不保存任何会话状态，任何请求落到任何一个实例结果都一样：
#   1. 认证：Bearer token 自包含（uid + 过期时间 + HMAC 签名），服务端不存 session
#   2. 业务状态：一律在 DB（结构化状态）和对象存储（大文件），进程重启即丢的东西不算状态
#   3. 幂等：先在 DB 持久化 (user_id, idempotency_key, file_hash, uploading)，再做对象存储副作用；
#            同一 submission 使用稳定 object key，跨实例重试可恢复，不靠进程内 session
# 下面两样是可随时丢弃的本地加速件，丢了只影响性能不影响正确性：
#   - CACHE：DB 的只读副本，可用 CACHE_ENABLED=0 整个关掉
#   - metrics 的请求延时滑动窗口：进程内观测值，不是权威指标（权威指标由 DB 派生）
STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "1") == "1"
TERMINAL_STATES = {"done", "dlq", "rejected"}   # 只缓存终态；中间态每次回源，避免各实例看到不同中间状态

CACHE = TTLCache(hard_ttl=10.0, soft_ttl=3.0)


def _payload(row):
    payload = {k: row[k] for k in ("id", "user_id", "status", "retry_count", "object_key",
                                   "result_msg", "detected_version", "required_version", "updated_at")}
    payload["status_text"] = sm.STATES.get(payload["status"], payload["status"])
    return payload


def err(action, http_status, msg):
    """统一错误出口（GET / POST 共用）
    action: relogin(400/401/403) | arrears(402) | pause(404/部分5xx) | wait(429/部分5xx) | retry_later(timeout)
    """
    return http_status, {"ok": False, "action": action, "msg": msg}


def read_task(sid):
    """读任务。CACHE_ENABLED=0 时完全不碰本地缓存（纯无状态模式）。"""
    if not CACHE_ENABLED:
        row = db.get_task(sid)
        if row is None:
            return None, "miss"
        return _payload(row), "db(无缓存模式)"

    cached, need_verify = CACHE.get(sid)
    if cached is not None and not need_verify:
        log.info(f"缓存读 sid={sid} 命中（未过期）")
        return cached, "cache"
    log.info(f"缓存读 sid={sid} {'未命中' if cached is None else '已过软TTL，回源核对'}")
    row = db.get_task(sid)
    if row is None:
        CACHE.invalidate(sid)
        return None, "miss"
    payload = _payload(row)
    if cached is not None:
        conflict = cached.get("updated_at") != payload["updated_at"]
        if conflict:
            log.warning(f"缓存与DB冲突 sid={sid} 以DB为准（缓存 updated_at={cached.get('updated_at')} DB={payload['updated_at']}）")
        CACHE.set(sid, payload)
        return payload, "db(冲突以DB为准)" if conflict else "cache(已核对)"

    # 只把终态放进缓存：uploading/pending/processing 每次回源，
    # 否则同一个任务在不同实例上可能返回不同的中间状态，破坏无状态假设
    if payload["status"] in TERMINAL_STATES:
        CACHE.set(sid, payload)
    else:
        CACHE.invalidate(sid)
    return payload, "db"


class Handler(BaseHTTPRequestHandler):
    def _start(self):
        self._t0 = time.time()
        log.info(f"收到请求 {self.command} {self.path} 来源={self.client_address[0]}")

    def _send(self, code, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        cost = (time.time() - getattr(self, "_t0", time.time())) * 1000
        metrics.record_request(cost)
        log.info(f"请求结果 {self.command} {self.path} -> {code} | 耗时 {cost:.1f}ms")

    def _auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            log.warning(f"权限拒绝 缺少 Bearer Token {self.command} {self.path}")
            return None, err("relogin", 401, "缺少 Bearer Token")
        try:
            uid = auth_token.verify_token(auth[7:].strip())
            log.info(f"权限通过 uid={uid} {self.command} {self.path}")
            return uid, None
        except ValueError as e:
            log.warning(f"权限拒绝 token 无效（{e}）{self.command} {self.path}")
            return None, err("relogin", 401, str(e))

    def _send_idempotency_conflict(self, sid=None):
        msg = "同一个 Idempotency-Key 已用于不同文件；请为新的业务请求使用新的 key"
        log.warning(f"幂等键复用但请求内容不同 sid={sid}")
        return self._send(409, {"ok": False, "action": "new_idempotency_key", "msg": msg})

    def _replay_submission(self, row):
        """重放一个已经持久化的 submission，不依赖当前 API 实例内存。"""
        headers = {"Idempotency-Replayed": "true"}
        if row["status"] == "rejected":
            return self._send(
                400,
                {"ok": False, "action": "relogin", "msg": row.get("result_msg") or "提交被拒绝"},
                headers,
            )
        return self._send(
            200,
            {"ok": True, "action": "success",
             "submission_id": row["id"],
             "status": row["status"],
             "status_text": sm.STATES.get(row["status"], row["status"])},
            headers,
        )

    def _complete_or_recover_upload(self, row, file_bytes, filename, file_hash, created):
        """把 uploading submission 推进到 pending；crash 后由相同请求恢复。

        created=True 表示本请求刚刚成功 reserve，持有当前 upload lease。
        created=False 时：
          1. 如果稳定 object key 已存在且 hash 一致，说明之前已经完整落盘，只差 DB 状态推进；
          2. 如果对象不存在，只允许 lease 过期后的一个请求原子 claim 后重写。
        """
        sid = row["id"]
        object_key = object_store.make_object_key(sid)

        if not created:
            stored_hash = object_store.object_hash(object_key)
            if stored_hash is not None:
                if stored_hash != file_hash:
                    log.error(f"对象内容与幂等指纹冲突 sid={sid} key={object_key}")
                    return self._send(*err("pause", 500, "对象存储内容与任务指纹不一致，请人工检查"))
                meta = {"key": object_key, "path": object_store.object_path(object_key), "hash": stored_hash}
                if sm.transition(sid, "pending", object_key=meta["key"], object_path=meta["path"],
                                 file_hash=meta["hash"], upload_lease_until=0, result_msg=None):
                    log.info(f"上传恢复 sid={sid}: 对象已完整存在，仅补推进 uploading -> pending")
                return self._replay_submission(db.get_task(sid))

            if not db.claim_stale_upload(sid, file_hash):
                # 原请求仍在 lease 内，避免两个 API 实例同时写同一个对象。
                log.info(f"幂等重放 sid={sid}: 上传仍进行中，等待当前 lease 完成")
                return self._replay_submission(db.get_task(sid))
            log.warning(f"上传恢复 sid={sid}: lease 已过期且对象不存在 -> 当前实例接管重写")

        try:
            meta = object_store.put_object(file_bytes, filename, key=object_key)
            log.info(f"对象存储落盘 sid={sid} key={meta['key']} hash={meta['hash'][:12]}...")
            if meta["hash"] != file_hash:
                raise RuntimeError("对象写入后的 hash 与请求指纹不一致")
            if not sm.transition(sid, "pending", object_key=meta["key"], object_path=meta["path"],
                                 file_hash=meta["hash"], upload_lease_until=0, result_msg=None):
                # 可能被另一个恢复请求先推进；读取 DB 后按权威状态重放。
                current = db.get_task(sid)
                if current and current["status"] != "uploading":
                    return self._replay_submission(current)
                raise RuntimeError("上传完成但状态无法推进到 pending")
        except Exception:
            # 不删除 reservation：它正是 crash/retry 的持久化恢复点。
            # 释放 lease 让下一次相同请求可以立即接管；对象使用稳定 key，不会制造新的 orphan final object。
            db.update_task(sid, "uploading",
                           {"upload_lease_until": 0, "result_msg": "对象写入未完成，等待相同请求重试恢复"})
            raise

        current = db.get_task(sid)
        log.info(f"任务已创建 sid={sid} status=pending（等待 worker 识别）")
        headers = {"Idempotency-Replayed": "true"} if not created else None
        return self._send(
            200,
            {"ok": True, "action": "success", "submission_id": sid,
             "status": current["status"], "status_text": sm.STATES.get(current["status"], current["status"])},
            headers,
        )

    def do_POST(self):
        self._start()
        path = urlparse(self.path).path
        if path == "/api/login":
            return self.handle_login()
        if path == "/api/submit":
            return self.handle_submit()
        self._send(*err("pause", 404, "接口不存在"))

    def do_GET(self):
        self._start()
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.handle_health()
        if path.startswith("/api/status/"):
            return self.handle_status(path)
        self._send(*err("pause", 404, "接口不存在"))

    # ---------- 健康检查（供 LB 探活 / 运维查看，不需鉴权） ----------
    def handle_health(self):
        m = metrics.collect()
        log.info(f"健康检查被查询 CPU={m['cpu_percent']}% 内存={m['memory_percent']}% "
                 f"队列={m['queue_depth']} DLQ={m['dlq_count']} 通过率={m['pass_rate_percent']}%")
        # instance 段：证明这个实例只是"无状态的副本"，杀掉任何一个都不丢状态
        inst = {"name": NAME, "pid": os.getpid(), "started_at": STARTED_AT,
                "cache_enabled": CACHE_ENABLED, "cache_keys": CACHE.size() if CACHE_ENABLED else 0,
                "db_pool": db.pool_stats()}
        self._send(200, {"ok": True, "action": "success", "health": m, "instance": inst})

    # ---------- 登录 ----------
    def handle_login(self):
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(raw)
        except Exception:
            log.warning("账号登录失败 请求体非法")
            return self._send(*err("relogin", 400, "请求体非法"))
        username = data.get("username", "")
        uid = db.verify_user(username, data.get("password", ""))
        if not uid:
            log.warning(f"账号登录失败 username={username}（用户名或密码错误）")
            return self._send(*err("relogin", 401, "用户名或密码错误"))
        log.info(f"账号登录成功 uid={uid} username={username} -> 签发 token")
        self._send(200, {"ok": True, "token": auth_token.issue_token(uid)})

    # ---------- 提交：先持久化幂等 reservation，再写对象，最后进入 pending ----------
    def handle_submit(self):
        uid, e = self._auth()
        if e:
            return self._send(*e)

        balance = billing.get_balance(uid)
        log.info(f"欠费检查 uid={uid} 余额={balance}")
        if billing.is_arrears(uid):
            log.warning(f"账户欠费 uid={uid} 余额={balance} -> 拒绝提交")
            return self._send(*err("arrears", 402, "账户欠费，请充值后重试"))

        idem_key = self.headers.get("Idempotency-Key", "").strip()
        log.info(f"幂等检查 uid={uid} key={idem_key or '(缺失)'}")
        if not idem_key:
            return self._send(*err("relogin", 400, "缺少 Idempotency-Key 头（重复提交请用同一个 key）"))

        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._send(*err("relogin", 400, "需以 multipart/form-data 上传"))
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype})
        if "file" not in form:
            return self._send(*err("relogin", 400, "缺少文件字段"))
        item = form["file"]
        file_bytes = item.file.read()
        filename = item.filename or "upload.bin"
        file_hash = object_store.hash_bytes(file_bytes)
        log.info(f"收到文件 uid={uid} 文件名={filename} 大小={len(file_bytes)}字节 hash={file_hash[:12]}...")

        # 必须拿到 body fingerprint 后再做幂等重放判断：
        # 同一个 key 如果带了不同文件，不能悄悄把旧结果当成新请求成功返回。
        existing = db.find_by_idempotency(uid, idem_key)
        if existing:
            if existing.get("file_hash") and existing["file_hash"] != file_hash:
                return self._send_idempotency_conflict(existing["id"])
            log.info(f"幂等命中 uid={uid} key={idem_key} -> sid={existing['id']} 状态={existing['status']}")
            if existing["status"] == "uploading":
                try:
                    return self._complete_or_recover_upload(
                        existing, file_bytes, filename, file_hash, created=False
                    )
                except Exception as ex:
                    log.exception(f"上传恢复异常 uid={uid} sid={existing['id']}")
                    return self._send(*err("pause", 500, f"服务端错误: {ex}"))
            return self._replay_submission(existing)

        ok, msg = validate.validate_file(file_bytes, filename)
        log.info(f"文件校验 uid={uid} 结果={'通过' if ok else '不通过'} {msg}")
        if not ok:
            try:
                db.insert_rejected(uid, idem_key, msg, file_hash=file_hash)
            except sqlite3.IntegrityError:
                row = db.find_by_idempotency(uid, idem_key)
                if row and row.get("file_hash") and row["file_hash"] != file_hash:
                    return self._send_idempotency_conflict(row["id"])
            log.warning(f"文件校验失败 uid={uid} -> 记为 rejected（{msg}）")
            return self._send(*err("relogin", 400, msg))

        try:
            row, created = db.reserve_upload(uid, idem_key, file_hash)
            if row.get("file_hash") and row["file_hash"] != file_hash:
                return self._send_idempotency_conflict(row["id"])
            if not created and row["status"] != "uploading":
                return self._replay_submission(row)
            return self._complete_or_recover_upload(row, file_bytes, filename, file_hash, created=created)
        except Exception as ex:
            log.exception(f"提交处理异常 uid={uid}")
            return self._send(*err("pause", 500, f"服务端错误: {ex}"))

    # ---------- 查询任务状态 ----------
    def handle_status(self, path):
        uid, e = self._auth()
        if e:
            return self._send(*e)

        tail = path[len("/api/status/"):].strip("/")
        if not tail.isdigit():
            return self._send(*err("relogin", 400, "任务 id 非法"))
        sid = int(tail)

        task, source = read_task(sid)
        if task is None:
            log.info(f"查询结果 sid={sid} 不存在")
            return self._send(*err("pause", 404, "任务不存在"))
        if task["user_id"] != uid:
            log.warning(f"权限拒绝 uid={uid} 试图查看他人任务 sid={sid}（属 uid={task['user_id']}）")
            return self._send(*err("relogin", 403, "无权查看他人任务"))

        log.info(f"查询结果 sid={sid} uid={uid} -> {task['status']}({task['status_text']}) "
                 f"retry={task['retry_count']} 来源={source}")
        self._send(200, {"ok": True, "action": "success",
                         "submission_id": sid,
                         "status": task["status"],
                         "status_text": task["status_text"],
                         "retry_count": task["retry_count"],
                         "detected_version": task["detected_version"],
                         "required_version": task["required_version"],
                         "result_msg": task["result_msg"],
                         "source": source})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    db.init_db()
    log.info(f"{NAME} 启动 http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
