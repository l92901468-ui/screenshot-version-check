import os

import db, logutil

_NAME = f"worker-{os.environ.get('WORKER_ID')}" if os.environ.get("WORKER_ID") else f"api-{os.environ.get('PORT', '0')}"
log = logutil.setup(_NAME)

STATES = {
    "uploading": "上传中",
    "pending": "等待中",
    "processing": "处理中",
    "done": "完成",
    "dlq": "待人工审核",
    "rejected": "提交被拒绝",
    # 历史状态名保留 arrears，语义现在是“外部 API 调用额度不足”；内部模型模式不会进入它。
    "arrears": "外部 API 额度不足",
}

ALLOWED = {
    ("uploading", "pending"),
    ("pending", "processing"),
    ("processing", "done"),
    ("processing", "pending"),
    ("processing", "dlq"),
    ("processing", "arrears"),
    ("arrears", "pending"),
}


def can_transition(curr: str, nxt: str) -> bool:
    return (curr, nxt) in ALLOWED


def transition(sid, to: str, **fields) -> bool:
    """非 worker-ownership 状态转换入口。

    pending -> processing 只能通过 db.claim_next_task() 原子领取；processing 出边只能走
    transition_owned/finalize_done_owned。same-state 不再返回成功，避免把“别人已经完成的
    状态”误认成“我自己的操作成功”。
    """
    row = db.get_task(sid)
    if row is None:
        log.warning(f"状态转换失败：任务不存在 sid={sid}")
        return False

    curr = row["status"]
    if curr == to:
        log.warning(f"状态转换拒绝 same-state sid={sid} status={curr}")
        return False
    if curr == "processing" or to == "processing":
        log.error(
            f"processing ownership 必须使用 claim/fenced API sid={sid} {curr} -> {to}"
        )
        return False
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


def transition_owned(sid: int, to: str, owner: str, generation: int, **fields) -> bool:
    """worker processing 出边：owner + generation + 未过期 lease 三重 fencing。"""
    if to == "done":
        log.error(f"processing -> done 必须使用 finalize_done_owned sid={sid}")
        return False
    if not can_transition("processing", to):
        log.error(f"非法 worker 状态转换 sid={sid} processing -> {to}")
        return False
    fields["status"] = to
    ok = db.update_processing_owned(sid, owner, generation, fields)
    if ok:
        extra = " ".join(f"{k}={v}" for k, v in fields.items() if k != "status")
        log.info(
            f"fenced 状态转换 sid={sid} gen={generation} owner={owner} "
            f"processing -> {to} {extra}".rstrip()
        )
    else:
        log.warning(
            f"fenced 状态转换被拒绝 sid={sid} gen={generation} owner={owner} -> {to} "
            f"（lease 过期、已被接管或 generation 不匹配）"
        )
    return ok


def finalize_done_owned(sid: int, owner: str, generation: int, cost: float,
                        detected_version: str, required_version: str, result_msg: str):
    """processing -> done 的 fenced 原子提交。

    internal backend 传 cost=0，不做额度扣减；external backend 传正数 cost，任务完成与
    条件扣费在同一个 SQLite transaction。stale worker 或额度不足都不能留下半套结果。
    """
    ok, balance = db.finalize_processing_and_charge(
        sid,
        owner,
        generation,
        cost,
        {
            "detected_version": detected_version,
            "required_version": required_version,
            "result_msg": result_msg,
        },
    )
    if ok:
        if cost > 0:
            log.info(
                f"fenced 完成 sid={sid} gen={generation} owner={owner} processing -> done "
                f"external_credits={balance}"
            )
        else:
            log.info(
                f"fenced 完成 sid={sid} gen={generation} owner={owner} processing -> done "
                "internal_backend_no_charge"
            )
    else:
        log.warning(
            f"fenced 完成被拒绝 sid={sid} gen={generation} owner={owner}，"
            "可能是 stale ownership 或外部 API 额度不足"
        )
    return ok, balance
