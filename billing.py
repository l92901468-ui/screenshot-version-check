# 账户余额 / 欠费判定（模拟，没有真实计费系统）

COST_PER_CALL = 1.0        # 每次成功识别扣 1
DEFAULT_BALANCE = 100.0


def get_balance(user_id) -> float:
    import db
    con = db.connect()
    cur = con.cursor()
    cur.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row["balance"] if row else 0.0


def is_arrears(user_id) -> bool:
    """余额 <= 0 即视为欠费"""
    return get_balance(user_id) <= 0


def charge(user_id, cost: float = COST_PER_CALL) -> float:
    """扣费，返回扣后余额"""
    import db
    con = db.connect()
    cur = con.cursor()
    cur.execute("UPDATE accounts SET balance = balance - ? WHERE user_id=?", (cost, user_id))
    con.commit()
    con.close()
    return get_balance(user_id)
