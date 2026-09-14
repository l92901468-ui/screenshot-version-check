import sqlite3, time, os, hashlib

DB_PATH = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(__file__), "app.db")
MAX_RETRY = 3  # retry 次数上限：达到即进 DLQ 待人工审核

UPDATABLE = {"status", "retry_count", "result_msg", "detected_version", "required_version"}


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)  # 并发写排队，避免 database is locked
    con.row_factory = sqlite3.Row
    return con


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    con = connect()
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # 多 worker 并发写需要 WAL
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            object_key TEXT, object_path TEXT, file_hash TEXT,
            idempotency_key TEXT, result_msg TEXT,
            detected_version TEXT,
            required_version TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
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
    con.close()


def verify_user(username: str, password: str):
    con = connect()
    row = con.execute("SELECT id, password_hash FROM users WHERE username=?", (username,)).fetchone()
    con.close()
    if row and row["password_hash"] == hashlib.sha256(password.encode()).hexdigest():
        return row["id"]
    return None


def find_by_idempotency(user_id, key):
    con = connect()
    row = con.execute("SELECT id, status, object_key, result_msg FROM submissions WHERE user_id=? AND idempotency_key=?",
                      (user_id, key)).fetchone()
    con.close()
    return dict(row) if row else None


def insert_task(user_id, idem_key, meta, status="pending") -> int:
    now = _now()
    con = connect()
    cur = con.cursor()
    cur.execute("""INSERT INTO submissions(user_id, status, retry_count, object_key, object_path,
                   file_hash, idempotency_key, created_at, updated_at)
                   VALUES(?,?,0,?,?,?,?,?,?)""",
                (user_id, status, meta["key"], meta["path"], meta["hash"], idem_key, now, now))
    sid = cur.lastrowid
    con.commit()
    con.close()
    return sid


def insert_rejected(user_id, idem_key, msg) -> int:
    now = _now()
    con = connect()
    cur = con.cursor()
    cur.execute("""INSERT INTO submissions(user_id, status, retry_count, idempotency_key, result_msg,
                   created_at, updated_at) VALUES(?, 'rejected', 0, ?, ?, ?, ?)""",
                (user_id, idem_key, msg, now, now))
    sid = cur.lastrowid
    con.commit()
    con.close()
    return sid


def get_task(sid):
    con = connect()
    row = con.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
    con.close()
    return dict(row) if row else None


def find_pending_id():
    """只挑 id，不改状态（状态变更统一走 state_machine）"""
    con = connect()
    row = con.execute("SELECT id FROM submissions WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
    con.close()
    return row["id"] if row else None


def find_arrears_id():
    con = connect()
    row = con.execute("SELECT id FROM submissions WHERE status='arrears' ORDER BY id LIMIT 1").fetchone()
    con.close()
    return row["id"] if row else None


def update_task(sid, expect_status, fields: dict) -> bool:
    """乐观锁更新：只有当前状态等于 expect_status 才写入，返回是否成功"""
    keys = [k for k in fields if k in UPDATABLE]
    if not keys:
        return False
    sets = ", ".join(f"{k}=?" for k in keys) + ", updated_at=?"
    values = [fields[k] for k in keys] + [_now(), sid, expect_status]
    con = connect()
    cur = con.cursor()
    cur.execute(f"UPDATE submissions SET {sets} WHERE id=? AND status=?", values)
    ok = cur.rowcount == 1
    con.commit()
    con.close()
    return ok
