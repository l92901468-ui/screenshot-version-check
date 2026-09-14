import os

import db, logutil

# 进程名：worker 用 WORKER_ID，api 用 PORT，保证日志落到本进程日志文件里
_NAME = f"worker-{os.environ.get('WORKER_ID')}" if os.environ.get("WORKER_ID") else f"api-{os.environ.get('PORT', '0')}"
log = logutil.setup(_NAME)

# 全部状态
STATES = {
    "pending": "等待中",
    "processing": "处理中",
    "done": "完成",
    "dlq": "待人工审核",
    "rejected": "提交被拒绝",
    "arrears": "欠费待充值",
}

# 转换表：只有这里列出的转换才被允许（这就是统筹整个流程的那张表）
ALLOWED = {
    ("pending", "processing"),   # worker 领取任务
    ("processing", "done"),      # 识别成功 + 版本核验有结论（通过/未通过都算完成）
    ("processing", "pending"),   # 识别失败，回队列重试（retry 未达上限）
    ("processing", "dlq"),       # retry 用尽，转人工
    ("processing", "arrears"),   # 账户欠费，暂停处理（不消耗 retry）
    ("arrears", "pending"),      # 充值后恢复，重新入队
}


def can_transition(curr: str, nxt: str) -> bool:
    return (curr, nxt) in ALLOWED


def transition(sid, to: str, **fields) -> bool:
    """状态变更唯一入口：先校验转换是否合法，再用乐观锁写入（防止并发抢占）"""
    row = db.get_task(sid)
    if row is None:
        log.warning(f"状态转换失败：任务不存在 sid={sid}")
        return False

    curr = row["status"]
    if curr == to:
        return True
    if not can_transition(curr, to):
        log.error(f"非法状态转换已拒绝 sid={sid} {curr}({STATES.get(curr)}) -> {to}({STATES.get(to)})")
        return False

    fields["status"] = to
    ok = db.update_task(sid, expect_status=curr, fields=fields)
    if ok:
        extra = " ".join(f"{k}={v}" for k, v in fields.items() if k != "status")
        log.info(f"状态转换 sid={sid} {curr} -> {to} {extra}".rstrip())
    else:
        log.warning(f"状态转换未生效（被并发抢占）sid={sid} {curr} -> {to}")
    return ok
