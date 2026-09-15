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
PROCESSING_LEASE_SEC = float(os.environ.get("PROCESSING_LEASE_SEC", "30"))

# 连接池大小：API / worker 都是多线程；SQLite 是单写者，池子开太大只会增加锁竞争。
POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "8"))
POOL_TIMEOUT = float(os.environ.get("DB_POOL_TIMEOUT", "10"))

UPDATABLE = {
    "status", "retry_count", "result_msg", "detected_version", "required_version", "next_attempt_at",
    "object_key", "object_path", "file_hash", "upload_lease_until",
    "processing_owner", "processing_generation", "processing_lease_until",
}


class ConnectionPool:
    """SQLite 连接池（纯标准库实现）。

    为什么要池化：API 和 worker 都是多线程，反复 sqlite3.connect() 会付出建连接和
    打开 WAL 的开销，而且连接数不可控。池化之后连接复用、总数有上限。

    并发安全：借出期间该连接只被一个线程持有，用完归还；写冲突由 SQLite 的
    busy_timeout 排队。
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
        con.execute("PRAGMA busy_timeout=30000")
        return con

    @contextmanager
    def connection(self):
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
    global POOL, DB_PATH
    if path:
        DB_PATH = path
    POOL.close_all()
    POOL = ConnectionPool(DB_PATH)


def connect():
    """一次性直连。业务热路径优先使用连接池。"""
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("""CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                token_version INTEGER NOT NULL DEFAULT 1)""")
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
                processing_owner TEXT,
                processing_generation INTEGER NOT NULL DEFAULT 0,
                processing_lease_until REAL NOT NULL DEFAULT 0,
                object_key TEXT, object_path TEXT, file_hash TEXT,
                idempotency_key TEXT, result_msg TEXT,
                detected_version TEXT,
                required_version TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        # 向后兼容已有 app.db。
        cols = {row["name"] for row in cur.execute("PRAGMA table_info(submissions)").fetchall()}
        if "next_attempt_at" not in cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")
        if "upload_lease_until" not in cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN upload_lease_until REAL NOT NULL DEFAULT 0")
        if "processing_owner" not in cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN processing_owner TEXT")
        if "processing_generation" not in cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN processing_generation INTEGER NOT NULL DEFAULT 0")
        if "processing_lease_until" not in cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN processing_lease_until REAL NOT NULL DEFAULT 0")

        cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
                user_id INTEGER PRIMARY KEY,
                balance REAL NOT NULL DEFAULT 100.0)""")
        # 兼容旧库不能直接补 CHECK constraint，因此用 trigger 把 balance >= 0 作为数据库 invariant。
        cur.execute("""CREATE TRIGGER IF NOT EXISTS accounts_nonnegative_insert
                       BEFORE INSERT ON accounts
                       WHEN NEW.balance < 0
                       BEGIN
                           SELECT RAISE(ABORT, 'account balance cannot be negative');
                       END""")
        cur.execute("""CREATE TRIGGER IF NOT EXISTS accounts_nonnegative_update
                       BEFORE UPDATE OF balance ON accounts
                       WHEN NEW.balance < 0
                       BEGIN
                           SELECT RAISE(ABORT, 'account balance cannot be negative');
                       END""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uniq_user_idem ON submissions(user_id, idempotency_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_worker_claim ON submissions(status, next_attempt_at, processing_lease_until, id)")
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
    with POOL.connection() as con:
        row = con.execute(
            "SELECT id, is_active, token_version FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def set_user_active(user_id: int, is_active: bool) -> bool:
    """disable 时同时 bump token_version，避免旧 token 在重新启用后复活。"""
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
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (user_id,))
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
    """先持久化 uploading reservation，再允许对象存储副作用发生。"""
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
    """兼容测试/监控的只读 helper；worker 正常路径必须使用 claim_next_task。"""
    with POOL.connection() as con:
        row = con.execute(
            "SELECT id FROM submissions WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()
    return row["id"] if row else None


def claim_next_task(owner: str, lease_sec: float = PROCESSING_LEASE_SEC):
    """原子领取 pending，或接管 lease 已过期的 processing。

    correctness 由条件 UPDATE + processing_generation 保证，而不是先 SELECT 的结果。
    每次成功领取（包括 crash 后接管）都会 generation+1，作为 fencing token。
    """
    now_epoch = time.time()
    lease_until = now_epoch + max(0.1, float(lease_sec))
    now_text = _now()

    with POOL.connection() as con:
        cur = con.cursor()
        row = cur.execute(
            """SELECT id, status, processing_generation
               FROM submissions
               WHERE (status='pending' AND next_attempt_at<=?)
                  OR (status='processing' AND processing_lease_until<=?)
               ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END, id
               LIMIT 1""",
            (now_epoch, now_epoch),
        ).fetchone()
        if row is None:
            return None

        old_generation = int(row["processing_generation"] or 0)
        new_generation = old_generation + 1
        if row["status"] == "pending":
            cur.execute(
                """UPDATE submissions
                   SET status='processing', processing_owner=?, processing_generation=?,
                       processing_lease_until=?, next_attempt_at=0, updated_at=?
                   WHERE id=? AND status='pending' AND next_attempt_at<=?
                     AND processing_generation=?""",
                (owner, new_generation, lease_until, now_text,
                 row["id"], now_epoch, old_generation),
            )
        else:
            cur.execute(
                """UPDATE submissions
                   SET processing_owner=?, processing_generation=?,
                       processing_lease_until=?, updated_at=?
                   WHERE id=? AND status='processing' AND processing_lease_until<=?
                     AND processing_generation=?""",
                (owner, new_generation, lease_until, now_text,
                 row["id"], now_epoch, old_generation),
            )

        if cur.rowcount != 1:
            con.rollback()
            return None
        con.commit()
        claimed = cur.execute("SELECT * FROM submissions WHERE id=?", (row["id"],)).fetchone()
        return dict(claimed)


def renew_processing_lease(sid: int, owner: str, generation: int,
                           lease_sec: float = PROCESSING_LEASE_SEC) -> bool:
    """只有当前且尚未过期的 owner 可以 heartbeat 延长 lease。"""
    now_epoch = time.time()
    new_until = now_epoch + max(0.1, float(lease_sec))
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute(
            """UPDATE submissions
               SET processing_lease_until=?, updated_at=?
               WHERE id=? AND status='processing' AND processing_owner=?
                 AND processing_generation=? AND processing_lease_until>?""",
            (new_until, _now(), sid, owner, generation, now_epoch),
        )
        ok = cur.rowcount == 1
        con.commit()
    return ok


def update_processing_owned(sid: int, owner: str, generation: int, fields: dict) -> bool:
    """fenced worker write：只有当前 generation 且 lease 未过期的 owner 才能改 processing。

    离开 processing 时自动清 owner/lease；generation 保留，下一次 claim 再递增。
    """
    keys = [k for k in fields if k in UPDATABLE]
    if not keys:
        return False
    data = dict(fields)
    leaving = data.get("status") not in (None, "processing")
    if leaving:
        data["processing_owner"] = None
        data["processing_lease_until"] = 0
    keys = [k for k in data if k in UPDATABLE]
    sets = ", ".join(f"{k}=?" for k in keys) + ", updated_at=?"
    values = [data[k] for k in keys] + [_now(), sid, owner, generation, time.time()]
    with POOL.connection() as con:
        cur = con.cursor()
        cur.execute(
            f"""UPDATE submissions SET {sets}
                WHERE id=? AND status='processing' AND processing_owner=?
                  AND processing_generation=? AND processing_lease_until>?""",
            values,
        )
        ok = cur.rowcount == 1
        con.commit()
    return ok


def finalize_processing_and_charge(sid: int, owner: str, generation: int,
                                   cost: float, fields: dict):
    """把 fenced processing -> done 与外部 API 模拟扣费放在同一 SQLite 事务。

    `cost=0`（内部模型）时不访问 accounts；外部 API 模式则使用条件扣费
    `balance >= cost`，因此两个任务即使都基于旧余额通过预检查，最终也只能有一个
    消耗最后一份额度。任务状态更新与扣费在同一事务，扣费失败会回滚 done。

    stale worker 的 generation/owner/lease 任一不匹配时也不会产生副作用。
    返回 (ok, balance_after)；额度不足时 ok=False，balance_after 返回当前额度（若有）。
    """
    allowed = {"detected_version", "required_version", "result_msg"}
    data = {k: fields[k] for k in fields if k in allowed}
    data.update({"status": "done", "processing_owner": None, "processing_lease_until": 0})
    keys = list(data)
    sets = ", ".join(f"{k}=?" for k in keys) + ", updated_at=?"
    now_epoch = time.time()

    with POOL.connection() as con:
        cur = con.cursor()
        owner_row = cur.execute(
            """SELECT user_id FROM submissions
               WHERE id=? AND status='processing' AND processing_owner=?
                 AND processing_generation=? AND processing_lease_until>?""",
            (sid, owner, generation, now_epoch),
        ).fetchone()
        if owner_row is None:
            con.rollback()
            return False, None

        cur.execute(
            f"""UPDATE submissions SET {sets}
                WHERE id=? AND status='processing' AND processing_owner=?
                  AND processing_generation=? AND processing_lease_until>?""",
            [data[k] for k in keys] + [_now(), sid, owner, generation, now_epoch],
        )
        if cur.rowcount != 1:
            con.rollback()
            return False, None

        balance_after = None
        if cost > 0:
            cur.execute(
                """UPDATE accounts
                   SET balance=balance-?
                   WHERE user_id=? AND balance>=?""",
                (cost, owner_row["user_id"], cost),
            )
            if cur.rowcount != 1:
                # 回滚 processing -> done；然后读取当前额度给调用方判断 arrears。
                con.rollback()
                row = con.execute(
                    "SELECT balance FROM accounts WHERE user_id=?",
                    (owner_row["user_id"],),
                ).fetchone()
                return False, (float(row["balance"]) if row else None)
            balance_row = cur.execute(
                "SELECT balance FROM accounts WHERE user_id=?",
                (owner_row["user_id"],),
            ).fetchone()
            balance_after = float(balance_row["balance"])

        con.commit()
        return True, balance_after


def find_arrears_id():
    with POOL.connection() as con:
        row = con.execute("SELECT id FROM submissions WHERE status='arrears' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def update_task(sid, expect_status, fields: dict) -> bool:
    """普通乐观锁更新。processing worker 写入请使用 fenced helper。"""
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
