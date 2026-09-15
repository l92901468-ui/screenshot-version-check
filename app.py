import cgi
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import auth_token
import billing
import db
import logutil
import metrics
import object_store
import state_machine as sm
import validate
from cache import TTLCache

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8001"))
NAME = f"api-{PORT}"
log = logutil.setup(NAME)

# ===== 无状态约定 =====
# API 实例不保存业务 session：认证信息在 Bearer token，业务状态在 DB / 对象存储。
# 幂等：先在 DB 持久化 (user_id, idempotency_key, file_hash, uploading)，再做对象存储副作用。
# 上传：文件以 bounded chunks 扫描和写盘，不再 item.file.read() 整体复制进 Python 内存。
STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "1") == "1"
TERMINAL_STATES = {"done", "dlq", "rejected"}

# ThreadingHTTPServer 仍会为连接创建线程，因此这里只做一个简单 admission control：
# 同一 API 实例最多让固定数量的 /api/submit 真正进入 multipart 解析 / 文件扫描路径；
# 超出的请求立即 429，让客户端退避重试，而不是把所有上传同时压进进程。
MAX_INFLIGHT_UPLOADS = max(1, int(os.environ.get("MAX_INFLIGHT_UPLOADS", "16")))
UPLOAD_SLOTS = threading.BoundedSemaphore(MAX_INFLIGHT_UPLOADS)

CACHE = TTLCache(hard_ttl=10.0, soft_ttl=3.0)


def _payload(row):
    payload = {k: row[k] for k in (
        "id", "user_id", "status", "retry_count", "object_key",
        "result_msg", "detected_version", "required_version", "updated_at",
    )}
    payload["status_text"] = sm.STATES.get(payload["status"], payload["status"])
    return payload


def err(action, http_status, msg):
    """统一错误出口。"""
    return http_status, {"ok": False, "action": action, "msg": msg}


def read_task(sid):
    """读任务；缓存只是可丢弃加速器，DB 永远是权威来源。"""
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
            log.warning(
                f"缓存与DB冲突 sid={sid} 以DB为准（缓存 updated_at={cached.get('updated_at')} "
                f"DB={payload['updated_at']}）"
            )
        CACHE.set(sid, payload)
        return payload, "db(冲突以DB为准)" if conflict else "cache(已核对)"

    # uploading/pending/processing 都是中间态，不进本地缓存。
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
        """重放持久化 submission，不依赖当前 API 实例内存。"""
        headers = {"Idempotency-Replayed": "true"}
        if row["status"] == "rejected":
            return self._send(
                400,
                {"ok": False, "action": "relogin", "msg": row.get("result_msg") or "提交被拒绝"},
                headers,
            )
        return self._send(
            200,
            {
                "ok": True,
                "action": "success",
                "submission_id": row["id"],
                "status": row["status"],
                "status_text": sm.STATES.get(row["status"], row["status"]),
            },
            headers,
        )

    def _complete_or_recover_upload(self, row, file_obj, filename, file_hash, created):
        """把 uploading submission 推进到 pending；crash 后由相同请求恢复。

        file_obj 已在第一遍扫描中完成 hash / size / magic 校验并回绕。
        第二遍由 object_store.put_object_stream 以固定 chunk 流式写入。
        """
        sid = row["id"]
        object_key = object_store.make_object_key(sid)

        if not created:
            stored_hash = object_store.object_hash(object_key)
            if stored_hash is not None:
                if stored_hash != file_hash:
                    log.error(f"对象内容与幂等指纹冲突 sid={sid} key={object_key}")
                    return self._send(*err("pause", 500, "对象存储内容与任务指纹不一致，请人工检查"))
                meta = {
                    "key": object_key,
                    "path": object_store.object_path(object_key),
                    "hash": stored_hash,
                }
                if sm.transition(
                    sid,
                    "pending",
                    object_key=meta["key"],
                    object_path=meta["path"],
                    file_hash=meta["hash"],
                    upload_lease_until=0,
                    result_msg=None,
                ):
                    log.info(f"上传恢复 sid={sid}: 对象已完整存在，仅补推进 uploading -> pending")
                return self._replay_submission(db.get_task(sid))

            if not db.claim_stale_upload(sid, file_hash):
                log.info(f"幂等重放 sid={sid}: 上传仍进行中，等待当前 lease 完成")
                return self._replay_submission(db.get_task(sid))
            log.warning(f"上传恢复 sid={sid}: lease 已过期且对象不存在 -> 当前实例接管重写")

        try:
            meta = object_store.put_object_stream(
                file_obj,
                filename,
                key=object_key,
                expected_hash=file_hash,
            )
            log.info(
                f"对象存储落盘 sid={sid} key={meta['key']} size={meta['size']} "
                f"hash={meta['hash'][:12]}..."
            )
            if not sm.transition(
                sid,
                "pending",
                object_key=meta["key"],
                object_path=meta["path"],
                file_hash=meta["hash"],
                upload_lease_until=0,
                result_msg=None,
            ):
                current = db.get_task(sid)
                if current and current["status"] != "uploading":
                    return self._replay_submission(current)
                raise RuntimeError("上传完成但状态无法推进到 pending")
        except Exception:
            # 不删除 reservation：它是 crash/retry 的持久化恢复点。
            db.update_task(
                sid,
                "uploading",
                {"upload_lease_until": 0, "result_msg": "对象写入未完成，等待相同请求重试恢复"},
            )
            raise

        current = db.get_task(sid)
        log.info(f"任务已创建 sid={sid} status=pending（等待 worker 识别）")
        headers = {"Idempotency-Replayed": "true"} if not created else None
        return self._send(
            200,
            {
                "ok": True,
                "action": "success",
                "submission_id": sid,
                "status": current["status"],
                "status_text": sm.STATES.get(current["status"], current["status"]),
            },
            headers,
        )

    def do_POST(self):
        self._start()
        path = urlparse(self.path).path
        if path == "/api/login":
            return self.handle_login()
        if path == "/api/submit":
            # 不排队堆积无限上传：没有 slot 就快速失败，客户端按 Retry-After 重试。
            if not UPLOAD_SLOTS.acquire(blocking=False):
                self.close_connection = True
                log.warning(
                    f"上传入口繁忙：本实例已达到 MAX_INFLIGHT_UPLOADS={MAX_INFLIGHT_UPLOADS}"
                )
                return self._send(
                    429,
                    {"ok": False, "action": "wait", "msg": "上传入口繁忙，请稍后重试"},
                    {"Retry-After": "1", "Connection": "close"},
                )
            try:
                return self.handle_submit()
            finally:
                UPLOAD_SLOTS.release()
        return self._send(*err("pause", 404, "接口不存在"))

    def do_GET(self):
        self._start()
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.handle_health()
        if path.startswith("/api/status/"):
            return self.handle_status(path)
        return self._send(*err("pause", 404, "接口不存在"))

    def handle_health(self):
        m = metrics.collect()
        log.info(
            f"健康检查被查询 CPU={m['cpu_percent']}% 内存={m['memory_percent']}% "
            f"队列={m['queue_depth']} DLQ={m['dlq_count']} 通过率={m['pass_rate_percent']}%"
        )
        inst = {
            "name": NAME,
            "pid": os.getpid(),
            "started_at": STARTED_AT,
            "cache_enabled": CACHE_ENABLED,
            "cache_keys": CACHE.size() if CACHE_ENABLED else 0,
            "db_pool": db.pool_stats(),
            "max_inflight_uploads": MAX_INFLIGHT_UPLOADS,
            "upload_chunk_size": validate.STREAM_CHUNK_SIZE,
        }
        return self._send(200, {"ok": True, "action": "success", "health": m, "instance": inst})

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
        return self._send(200, {"ok": True, "token": auth_token.issue_token(uid)})

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

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype},
        )
        if "file" not in form:
            return self._send(*err("relogin", 400, "缺少文件字段"))

        item = form["file"]
        filename = item.filename or "upload.bin"

        # 第一遍：bounded-chunk 扫描。只在内存保留一个 chunk，同时得到 size/hash/magic。
        try:
            ok, msg, file_hash, file_size = validate.inspect_file_stream(item.file, filename)
        except Exception as ex:
            log.exception(f"文件流扫描失败 uid={uid}")
            return self._send(*err("pause", 500, f"文件流处理失败: {ex}"))

        log.info(
            f"收到文件 uid={uid} 文件名={filename} 大小={file_size}字节 "
            f"hash={file_hash[:12]}... 校验={'通过' if ok else '不通过'}"
        )

        # 同一个 key 如果对应不同 fingerprint，必须 409，而不是把旧 submission 当新请求成功重放。
        existing = db.find_by_idempotency(uid, idem_key)
        if existing:
            if existing.get("file_hash") and existing["file_hash"] != file_hash:
                return self._send_idempotency_conflict(existing["id"])
            log.info(
                f"幂等命中 uid={uid} key={idem_key} -> sid={existing['id']} 状态={existing['status']}"
            )
            if existing["status"] == "uploading":
                try:
                    return self._complete_or_recover_upload(
                        existing,
                        item.file,
                        filename,
                        file_hash,
                        created=False,
                    )
                except Exception as ex:
                    log.exception(f"上传恢复异常 uid={uid} sid={existing['id']}")
                    return self._send(*err("pause", 500, f"服务端错误: {ex}"))
            return self._replay_submission(existing)

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
            return self._complete_or_recover_upload(
                row,
                item.file,
                filename,
                file_hash,
                created=created,
            )
        except Exception as ex:
            log.exception(f"提交处理异常 uid={uid}")
            return self._send(*err("pause", 500, f"服务端错误: {ex}"))

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

        log.info(
            f"查询结果 sid={sid} uid={uid} -> {task['status']}({task['status_text']}) "
            f"retry={task['retry_count']} 来源={source}"
        )
        return self._send(
            200,
            {
                "ok": True,
                "action": "success",
                "submission_id": sid,
                "status": task["status"],
                "status_text": task["status_text"],
                "retry_count": task["retry_count"],
                "detected_version": task["detected_version"],
                "required_version": task["required_version"],
                "result_msg": task["result_msg"],
                "source": source,
            },
        )

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    db.init_db()
    log.info(
        f"{NAME} 启动 http://{HOST}:{PORT} | "
        f"max_inflight_uploads={MAX_INFLIGHT_UPLOADS} | chunk={validate.STREAM_CHUNK_SIZE}B"
    )
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
