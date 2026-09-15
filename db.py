import hashlib
import os
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(__file__), "app.db")
MAX_RETRY = 3  # retry 次数上限：达到即进 DLQ 待人工审核
UPLOAD_LEASE_SEC = float(os.environ.get("UPLOAD_LEASE_SEC", "30"))

# 连接池大小：API 每实例 4 线程 + worker 每实例 4 线程，默认 8 够用；
# SQLite 是单写者，池子开太大只会增加锁竞争，不会提高吞吐。
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))
POOL_TIMEOUT = float(os.environ.get("DB_POOL_TIMEOUT", "10"))

UPDATABLE = {
    "status", "retry_count", "result_msg", "detected_version", "required_version", "next_attempt_at",
    "object_key", "object_path", "file_hash", "upload_lease_until",
}


class ConnectionPool:
    """SQLite 连接池（纯标准库实现）。

    为什么要池化：API 是多线程的（ThreadingHTTPServer），worker 也是多线程，
    每次请求都 sqlite3.connect() 会反复付出"建连接 + 打开 WAL"的开销，且连接数不可控。
    池化之后：连接复用、总数有上限、借不到时快速失败而不是无限堆积。

    并发安全：借出期间该连接只被一个线程持有，用完归还，
    所以 check_same_thread=False 是安全的；写冲突由 SQLite 的 busy_timeout 排队。
    """

    def __init__(self, path, size=POOL_SIZE, timeout=POOL_TIMEOUT):
        self._path = path
        self._size = size
        self._timeout = timeout
        self._pool = queue.Queue(maxsize=size)
        self._lock = threading.Lock()
        self._created = 0
        self._in_use = 0
        self._borrows = 0
        self._waits = 0
        self._timeouts = 0

    def _new(self):
        con = sqlite3.connect(self._path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")   # 写锁等待 30s，避免 database is locked
        return con

    @contextmanager
    def connection(self):
        """借出一个连接，用完自动归还。"""
        con = self._acquire()
        try:
            yield con
        finally:
            self._release(con)

    def _acquire(self):
        try:
            con = self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    con = self._new()
                else:
                    con = None
            if con is None:
                # 池满：等别人归还，超时就报错，不让请求无限堆积
                self._waits += 1
                try:
                    con = self._pool.get(timeout=self._timeout)
                except queue.Empty:
                    self._timeouts += 1
                    raise RuntimeError("数据库连接池耗尽（DB_POOL_SIZE=%d）" % self._size)
        with self._lock:
            self._borrows += 1
            self._in_use += 1
        return con

    def _release(self, con):
        with self._lock:
            self._in_use -= 1
        try:
            self._pool.put_nowait(con)
        except queue.Full:
            con.close()
            with self._lock:
                self._created -= 1

    def close_all(self):
        """关闭池中空闲连接（切换 DB / 测试时用）"""
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break

    def stats(self):
        return {"size": self._size, "created": self._created, "in_use": self._in_use,
                "idle": self._pool.qsize(), "borrows": self._borrows,
                "waits": self._waits, "timeouts": self._timeouts}


POOL = ConnectionPool(DB_PATH)


def pool_stats():
    return POOL.stats()


def reset_pool(path=None):
    """切换数据库（测试/迁移）时重建池，否则旧连接仍指向旧文件。"""
    global POOL, DB_PATH
    if path:
        DB_PATH = path
    POOL.close_all()
    POOL = ConnectionPool(DB_PATH)


def connect():
    """直接拿一个新连接（不走池）。只给一次性场景用，业务路径请用 POOL.connection()。"""
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # 多 worker 并发写需要 WAL
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                token_version INTEGER NOT NULL DEFAULT 1)""")
        # 兼容旧 app.db：账号状态和 token version 是后加的列。
        user_cols = {row["name"] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
        if "is_active" not in user_cols:
            cur.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "token_version" not in user_cols:
            cur.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1")

        cur.execute("""CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                upload_lease_until REAL NOT NULL DEFAULT 0,
                object_key TEXT, object_path TEXT, file_hash TEXT,
                idempotency_key TEXT, result_msg TEXT,
                detected_version TEXT,
                required_version TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        # 兼容已有 app.db：CREATE TABLE IF NOT EXISTS 不会自动补新列。
        cols = {row["name"] for row in cur.execute("PRAGMA table_info(submissions)").fetchall()}
        if "next_attempt_at" not in cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")
        if "upload_lease_until" not in cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN upload_lease_until REAL NOT NULL DEFAULT 0")
        cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 100.0)""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_user_idem ON submissions(user_id, idempotency_key)")
        cur.execute("INSERT OR IGNORE INTO users(username, password_hash) VALUES(?, ?)",
                    ("demo", hashlib.sha256(b"demo123").hexdigest()))
        uid = cur.execute("SELECT id FROM users WHERE username='demo'").fetchone()
        if uid:
            cur.execute("INSERT OR IGNORE INTO accounts(user_id, balance) VALUES(?, ?)", (uid["id"], 100.0))
        con.commit()


def verify_user(username: str, password: str):
    """校验登录凭据；disabled account 即使密码正确也不能重新登录。"""
    with POOL.connection() as con:
        row = con.execute(
            "SELECT id, password_hash, is_active FROM users WHERE username=?",
            (username,),
        ).fetchone()
    if (
        row
        and row["is_active"]
        and row["password_hash"] == hashlib.sha256(password.encode()).hexdigest()
    ):
        return row["id"]
    return None


def get_user_auth_state(user_id: int):
    """共享认证状态；每次 token 验证直接读这里，不做本地 TTL 缓存。"""
    with POOL.connection() as con:
        row = con.execute(
            "SELECT id, is_active, token_version FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def set_user_active(user_id: int, is_active: bool) -> bool:
    """启用/禁用账号。

    disable 时同时 bump token_version，让禁用前签发的 token 在之后重新启用账号时也不会复活。
    """
    with POOL.connection() as con:
        cur = con.cursor()
        if is_active:
            cur.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
        else:
            cur.execute(
                """UPDATE users
                   SET is_active=0,
                       token_version=CASE WHEN is_active!=0 THEN token_version+1 ELSE token_version END
                   WHERE id=?""",
                (user_id,),
            )
        changed = cur.rowcount == 1
        con.commit()
    return changed


def revoke_user_tokens(user_id: int) -> bool:
    """账号保持启用，但让此前签发的所有 token 立即失效。"""
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute(
            "UPDATE users SET token_version=token_version+1 WHERE id=?",
            (user_id,),
        )
        changed = cur.rowcount == 1
        con.commit()
    return changed


def find_by_idempotency(user_id, key):
    with POOL.connection() as con:
        row = con.execute(
            """SELECT id, status, object_key, object_path, file_hash, upload_lease_until,
                      result_msg, updated_at
               FROM submissions WHERE user_id=? AND idempotency_key=?""",
            (user_id, key),
        ).fetchone()
    return dict(row) if row else None


def reserve_upload(user_id, idem_key, file_hash):
    """先在 DB 中持久化幂等记录，再允许对象存储副作用发生。

    返回 (row, created):
      - created=True：当前请求赢得首次上传权，row.status=uploading。
      - created=False：同一个 (user_id, idempotency_key) 已存在，调用方应重放/恢复，
        并先核对 row.file_hash 是否与本次请求一致。

    upload_lease_until 防止两个 API 实例同时恢复同一个中断上传。
    """
    now_text = _now()
    lease_until = time.time() + UPLOAD_LEASE_SEC
    with POOL.connection() as con:
        cur = con.cursor()
        try:
            cur.execute(
                """INSERT INTO submissions(
                       user_id, status, retry_count, next_attempt_at, upload_lease_until,
                       file_hash, idempotency_key, created_at, updated_at)
                   VALUES(?, 'uploading', 0, 0, ?, ?, ?, ?, ?)""",
                (user_id, lease_until, file_hash, idem_key, now_text, now_text),
            )
            sid = cur.lastrowid
            con.commit()
            row = cur.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
            return dict(row), True
        except sqlite3.IntegrityError:
            con.rollback()
            row = cur.execute(
                "SELECT * FROM submissions WHERE user_id=? AND idempotency_key=?",
                (user_id, idem_key),
            ).fetchone()
            if row is None:
                raise
            return dict(row), False


def claim_stale_upload(sid, file_hash):
    """只有 upload lease 已过期的 uploading 任务才能被一个重试请求重新领取。"""
    now_epoch = time.time()
    new_lease = now_epoch + UPLOAD_LEASE_SEC
    now_text = _now()
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute(
            """UPDATE submissions
               SET upload_lease_until=?, updated_at=?
               WHERE id=? AND status='uploading' AND file_hash=? AND upload_lease_until<=?""",
            (new_lease, now_text, sid, file_hash, now_epoch),
        )
        ok = cur.rowcount == 1
        con.commit()
    return ok


def insert_task(user_id, idem_key, meta, status="pending") -> int:
    """测试/内部辅助入口。正常 /api/submit 使用 reserve_upload -> uploading -> pending。"""
    now = _now()
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute("""INSERT INTO submissions(user_id, status, retry_count, object_key, object_path,
                       file_hash, idempotency_key, created_at, updated_at)
                       VALUES(?,?,0,?,?,?,?,?,?)""",
                    (user_id, status, meta["key"], meta["path"], meta["hash"], idem_key, now, now))
        sid = cur.lastrowid
        con.commit()
    return sid


def insert_rejected(user_id, idem_key, msg, file_hash=None) -> int:
    now = _now()
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute("""INSERT INTO submissions(user_id, status, retry_count, file_hash,
                       idempotency_key, result_msg, created_at, updated_at)
                       VALUES(?, 'rejected', 0, ?, ?, ?, ?, ?)""",
                    (user_id, file_hash, idem_key, msg, now, now))
        sid = cur.lastrowid
        con.commit()
    return sid


def get_task(sid):
    with POOL.connection() as con:
        row = con.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    return dict(row) if row else None


def find_pending_id():
    """只挑已到重试时间的 pending id，不改状态（状态变更统一走 state_machine）。"""
    with POOL.connection() as con:
        row = con.execute(
            "SELECT id FROM submissions WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()
    return row["id"] if row else None


def find_arrears_id():
    with POOL.connection() as con:
        row = con.execute("SELECT id FROM submissions WHERE status='arrears' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def update_task(sid, expect_status, fields: dict) -> bool:
    """乐观锁更新：只有当前状态等于 expect_status 才写入，返回是否成功"""
    keys = [k for k in fields if k in UPDATABLE]
    if not keys:
        return False
    sets = ", ".join(f"{k}=?" for k in keys) + ", updated_at=?"
    values = [fields[k] for k in keys] + [_now(), sid, expect_status]
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute(f"UPDATE submissions SET {sets} WHERE id=? AND status=?", values)
        ok = cur.rowcount == 1
        con.commit()
    return ok