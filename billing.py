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
    """余额 <= 0 即视为欠费。"""
    return get_balance(user_id) <= 0


def charge(user_id, cost: float = COST_PER_CALL) -> float:
    """独立扣费 helper，返回扣后余额。

    worker 的成功路径不直接调用这里：它使用 db.finalize_processing_and_charge()，
    把 fencing 校验、processing -> done 和本地扣费放在同一事务，避免 stale worker 重复扣费。
    """
    import db
    con = db.connect()
    cur = con.cursor()
    cur.execute("UPDATE accounts SET balance = balance - ? WHERE user_id=?", (cost, user_id))
    con.commit()
    con.close()
    return get_balance(user_id)
