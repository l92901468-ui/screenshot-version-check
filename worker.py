import os
import random
import threading
import time

import billing
import db
import logutil
import recognize
import state_machine as sm

WORKER_ID = os.environ.get("WORKER_ID", "1")
NAME = f"worker-{WORKER_ID}"
THREADS = int(os.environ.get("WORKER_THREADS", "4"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "0.5"))
RETRY_BASE_SEC = float(os.environ.get("RETRY_BASE_SEC", "1.0"))
RETRY_MAX_SEC = float(os.environ.get("RETRY_MAX_SEC", "30.0"))
PROCESSING_LEASE_SEC = max(0.5, float(os.environ.get("PROCESSING_LEASE_SEC", str(db.PROCESSING_LEASE_SEC))))
PROCESSING_HEARTBEAT_SEC = max(
    0.1,
    min(
        float(os.environ.get("PROCESSING_HEARTBEAT_SEC", str(PROCESSING_LEASE_SEC / 3.0))),
        PROCESSING_LEASE_SEC / 2.0,
    ),
)

log = logutil.setup(NAME)


def retry_delay(retry_count: int) -> float:
    """指数退避 + full jitter：随机落在 [0, cap]，避免 retry storm。"""
    cap = min(RETRY_MAX_SEC, RETRY_BASE_SEC * (2 ** max(0, retry_count - 1)))
    return random.uniform(0.0, cap)


class ProcessingLeaseHeartbeat:
    """独立 heartbeat，防止长时间模型调用期间 processing lease 自然过期。

    DB 明确返回 False 才代表 ownership 确定丢失；DB 异常只标记 uncertain，继续重试。
    最终副作用仍由数据库里的 owner + generation + live lease fencing 做权威裁决。
    """

    def __init__(self, sid: int, owner: str, generation: int):
        self.sid = sid
        self.owner = owner
        self.generation = generation
        self._stop = threading.Event()
        self.lost = threading.Event()
        self.uncertain = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-{sid}-g{generation}",
            daemon=True,
        )

    def start(self):
        self._thread.start()

    def renew_once(self):
        try:
            renewed = db.renew_processing_lease(
                self.sid,
                self.owner,
                self.generation,
                PROCESSING_LEASE_SEC,
            )
        except Exception:
            self.uncertain.set()
            log.exception(
                f"lease heartbeat 异常 sid={self.sid} gen={self.generation} owner={self.owner}；"
                "ownership 暂时不确定，将继续重试并让 fenced DB write 做最终裁决"
            )
            return "uncertain"

        if not renewed:
            self.lost.set()
            self.uncertain.clear()
            log.warning(
                f"lease 续租被拒绝 sid={self.sid} gen={self.generation} owner={self.owner}，"
                "当前 worker 已确定失去 ownership"
            )
            return "lost"

        if self.uncertain.is_set():
            log.info(
                f"lease heartbeat 恢复 sid={self.sid} gen={self.generation} owner={self.owner}，"
                "ownership 已重新确认"
            )
        self.uncertain.clear()
        return "renewed"

    def _run(self):
        while not self._stop.wait(PROCESSING_HEARTBEAT_SEC):
            if self.renew_once() == "lost":
                return

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.2, PROCESSING_HEARTBEAT_SEC + 0.1))


def _stale_result(tid: str, sid: int, generation: int, where: str):
    log.warning(
        f"[{tid}] 丢弃 stale worker 结果 sid={sid} gen={generation} stage={where} "
        "（lease 已失效或任务已被新 generation 接管）"
    )


def _log_uncertain_commit(tid: str, sid: int, generation: int, where: str):
    log.warning(
        f"[{tid}] ownership 暂时不确定 sid={sid} gen={generation} stage={where}；"
        "不根据本地 heartbeat 状态直接丢结果，交给 fenced DB write 最终裁决"
    )


def handle_one(tid: str):
    """原子领取并处理一个任务；返回 False 表示当前没有可领取任务。"""
    row = db.claim_next_task(tid, PROCESSING_LEASE_SEC)
    if row is None:
        return False

    sid = row["id"]
    generation = int(row["processing_generation"])
    uid, rc = row["user_id"], row["retry_count"] or 0
    reclaimed = bool(row.get("processing_generation", 0) > 1)
    external = recognize.uses_external_api()
    backend = recognize.backend_name()
    t0 = time.time()
    log.info(
        f"[{tid}] {'接管/重新领取' if reclaimed else '领取'} sid={sid} uid={uid} "
        f"gen={generation} lease={PROCESSING_LEASE_SEC:.1f}s backend={backend} "
        f"第 {rc + 1} 次业务尝试"
    )

    heartbeat = ProcessingLeaseHeartbeat(sid, tid, generation)
    heartbeat.start()
    try:
        # 只有第三方 API 模式才检查模拟调用额度；内部模型不让员工“为截图识别付费”。
        if external:
            balance = billing.get_balance(uid)
            if billing.is_arrears(uid):
                if heartbeat.lost.is_set():
                    _stale_result(tid, sid, generation, "external_quota")
                    return True
                if heartbeat.uncertain.is_set():
                    _log_uncertain_commit(tid, sid, generation, "external_quota")
                if not sm.transition_owned(
                    sid,
                    "arrears",
                    tid,
                    generation,
                    result_msg="外部识图 API 调用额度不足",
                ):
                    _stale_result(tid, sid, generation, "external_quota")
                else:
                    log.error(
                        f"[{tid}] sid={sid} gen={generation} 外部 API 额度不足 uid={uid} "
                        f"credits={balance} -> 转 arrears（待额度恢复，不消耗 retry）"
                    )
                return True
            log.info(
                f"[{tid}] sid={sid} gen={generation} 外部 API 额度检查通过 credits={balance}"
            )

        log.info(f"[{tid}] sid={sid} gen={generation} 调用 {backend} 识图后端...")
        t_model = time.time()
        ok, version, msg = recognize.call_vision_model(sid, rc)
        cost_model = (time.time() - t_model) * 1000
        log.info(
            f"[{tid}] sid={sid} gen={generation} backend={backend} 返回 ok={ok} 版本={version} "
            f"耗时={cost_model:.1f}ms（{msg}）"
        )

        if heartbeat.lost.is_set():
            _stale_result(tid, sid, generation, "after_model")
            return True
        if heartbeat.uncertain.is_set():
            _log_uncertain_commit(tid, sid, generation, "after_model")

        if not ok:
            new_rc = rc + 1
            if new_rc >= db.MAX_RETRY:
                if sm.transition_owned(
                    sid,
                    "dlq",
                    tid,
                    generation,
                    retry_count=new_rc,
                    result_msg=msg,
                ):
                    log.error(
                        f"[{tid}] sid={sid} gen={generation} 识别失败 retry={new_rc}/{db.MAX_RETRY} "
                        f"-> DLQ 待人工审核（{msg}）"
                    )
                else:
                    _stale_result(tid, sid, generation, "dlq")
            else:
                delay = retry_delay(new_rc)
                next_attempt_at = time.time() + delay
                if sm.transition_owned(
                    sid,
                    "pending",
                    tid,
                    generation,
                    retry_count=new_rc,
                    next_attempt_at=next_attempt_at,
                    result_msg=msg,
                ):
                    log.warning(
                        f"[{tid}] sid={sid} gen={generation} 识别失败 retry={new_rc}/{db.MAX_RETRY} -> "
                        f"指数退避+full jitter {delay:.2f}s 后重试（{msg}）"
                    )
                else:
                    _stale_result(tid, sid, generation, "retry")
            return True

        passed = recognize.meets_version(version, recognize.REQUIRED_VERSION)
        verdict = (
            f"核验通过：识别版本 {version} >= 要求 {recognize.REQUIRED_VERSION}"
            if passed
            else f"核验未通过：识别版本 {version} < 要求 {recognize.REQUIRED_VERSION}"
        )

        # 内部模型 cost=0；外部 API 才消耗模拟 credits。finalize + charge 在同一事务里。
        charge_cost = billing.COST_PER_CALL if external else 0.0
        finalized, after = sm.finalize_done_owned(
            sid,
            tid,
            generation,
            charge_cost,
            version,
            recognize.REQUIRED_VERSION,
            verdict,
        )
        if not finalized:
            # 对外部模式，可能是两个不同任务都通过了旧余额检查，最终只有一个能原子扣费。
            # 如果额度已耗尽且 ownership 仍有效，就转 arrears；若已经 stale，fenced transition 会失败。
            if external and billing.is_arrears(uid):
                if sm.transition_owned(
                    sid,
                    "arrears",
                    tid,
                    generation,
                    result_msg="外部 API 额度在并发提交时被其他任务耗尽",
                ):
                    log.warning(
                        f"[{tid}] sid={sid} gen={generation} finalize 时额度不足 -> arrears；"
                        "数据库条件扣费保证 credits 不会变成负数"
                    )
                    return True
            _stale_result(tid, sid, generation, "finalize")
            return True

        if external:
            log.info(
                f"[{tid}] sid={sid} gen={generation} 外部 API 模拟扣费 {charge_cost} -> credits {after}"
            )
        else:
            log.info(f"[{tid}] sid={sid} gen={generation} 内部模型模式：不做外部 API 扣费")

        log.info(
            f"[{tid}] sid={sid} gen={generation} 版本核验 {'通过' if passed else '未通过'} -> {verdict} "
            f"| 本次总耗时 {(time.time() - t0) * 1000:.1f}ms"
        )
        return True
    finally:
        heartbeat.stop()


def recover_arrears(tid: str):
    """外部 API 额度恢复，或切到 internal backend 后，把 arrears 任务重新放回队列。"""
    sid = db.find_arrears_id()
    if sid is None:
        return
    row = db.get_task(sid)
    if not row:
        return
    external = recognize.uses_external_api()
    if (not external) or (not billing.is_arrears(row["user_id"])):
        reason = "已切换内部模型，重新入队" if not external else "外部 API 额度已恢复，重新入队"
        if sm.transition(sid, "pending", next_attempt_at=0, result_msg=reason):
            log.info(f"[{tid}] sid={sid} {reason}")


def loop(tid: str):
    while True:
        try:
            recover_arrears(tid)
            if not handle_one(tid):
                time.sleep(POLL_INTERVAL)
        except Exception:
            log.exception(f"[{tid}] 处理任务异常；若已领取，等待 processing lease 到期后自动恢复")
            time.sleep(POLL_INTERVAL)


def main():
    db.init_db()
    log.info(
        f"{NAME} 启动 | 并发线程={THREADS} | retry上限={db.MAX_RETRY} | "
        f"backend={recognize.backend_name()} | "
        f"退避基数={RETRY_BASE_SEC}s 上限={RETRY_MAX_SEC}s | "
        f"processing lease={PROCESSING_LEASE_SEC}s heartbeat={PROCESSING_HEARTBEAT_SEC}s | "
        f"要求版本={recognize.REQUIRED_VERSION}"
    )
    threads = [
        threading.Thread(target=loop, args=(f"{NAME}-t{i}",), daemon=True)
        for i in range(THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
