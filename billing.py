"""第三方识图 API 调用额度（模拟）。

这里的 `balance` 不是“员工自己掏钱”，只是为了演示外部 provider 可能有按次计费 / 配额边界。
内部模型模式不使用这份额度。
"""

COST_PER_CALL = 1.0
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
    """外部 API credits <= 0 视为额度不足。"""
    return get_balance(user_id) <= 0


def charge(user_id, cost: float = COST_PER_CALL) -> float:
    """独立测试 helper：只在 credits 足够时扣减，绝不允许变成负数。

    worker 成功路径不直接调用这里；它使用 db.finalize_processing_and_charge()，
    把 fencing、processing -> done 和条件扣费放在同一事务。
    """
    import db
    con = db.connect()
    cur = con.cursor()
    cur.execute(
        "UPDATE accounts SET balance=balance-? WHERE user_id=? AND balance>=?",
        (cost, user_id, cost),
    )
    if cur.rowcount != 1:
        con.rollback()
        row = cur.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
        con.close()
        if row is None:
            raise ValueError("external API credit account not found")
        raise ValueError("insufficient external API credits")
    con.commit()
    row = cur.execute("SELECT balance FROM accounts WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    return row["balance"]
