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
#   3. 幂等：靠 DB 唯一索引 (user_id, idempotency_key)，跨实例共享
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

    # 只把终态放进缓存：pending/processing 每次回源，
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

    # ---------- 提交：建任务，返回等待中 ----------
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
        existing = db.find_by_idempotency(uid, idem_key)
        if existing:
            log.info(f"幂等命中 uid={uid} key={idem_key} -> 重放 sid={existing['id']} 状态={existing['status']}")
            if existing["status"] == "rejected":
                return self._send(400, {"ok": False, "action": "relogin", "msg": existing["result_msg"] or "提交被拒绝"},
                                  {"Idempotency-Replayed": "true"})
            return self._send(200, {"ok": True, "action": "success",
                                    "submission_id": existing["id"],
                                    "status": existing["status"],
                                    "status_text": sm.STATES.get(existing["status"], existing["status"])},
                              {"Idempotency-Replayed": "true"})
        log.info(f"幂等未命中 uid={uid} key={idem_key} -> 走正常流程")

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
        log.info(f"收到文件 uid={uid} 文件名={filename} 大小={len(file_bytes)}字节")

        ok, msg = validate.validate_file(file_bytes, filename)
        log.info(f"文件校验 uid={uid} 结果={'通过' if ok else '不通过'} {msg}")
        if not ok:
            db.insert_rejected(uid, idem_key, msg)
            log.warning(f"文件校验失败 uid={uid} -> 记为 rejected（{msg}）")
            return self._send(*err("relogin", 400, msg))

        try:
            meta = object_store.put_object(file_bytes, filename)
            log.info(f"对象存储落盘 uid={uid} key={meta['key']} hash={meta['hash'][:12]}...")
            sid = db.insert_task(uid, idem_key, meta, status="pending")
        except sqlite3.IntegrityError:
            row = db.find_by_idempotency(uid, idem_key)
            log.warning(f"并发幂等冲突 uid={uid} key={idem_key} -> 重放")
            if row:
                return self._send(200, {"ok": True, "action": "success", "submission_id": row["id"],
                                        "status": row["status"],
                                        "status_text": sm.STATES.get(row["status"], row["status"])},
                                  {"Idempotency-Replayed": "true"})
            return self._send(*err("pause", 500, "并发幂等冲突"))
        except Exception as ex:
            log.exception(f"提交处理异常 uid={uid}")
            return self._send(*err("pause", 500, f"服务端错误: {ex}"))

        log.info(f"任务已创建 sid={sid} uid={uid} status=pending（等待 worker 识别）")
        self._send(200, {"ok": True, "action": "success", "submission_id": sid,
                         "status": "pending", "status_text": "等待中"})

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
