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

    heartbeat 一旦无法续租，就把当前 worker 标记为 lost owner；后续结果必须丢弃。
    即使 lost 标志因竞态没及时看到，最终数据库写仍有 generation/owner/lease fencing。
    """

    def __init__(self, sid: int, owner: str, generation: int):
        self.sid = sid
        self.owner = owner
        self.generation = generation
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-{sid}-g{generation}",
            daemon=True,
        )

    def start(self):
        self._thread.start()

    def _run(self):
        while not self._stop.wait(PROCESSING_HEARTBEAT_SEC):
            try:
                if not db.renew_processing_lease(
                    self.sid,
                    self.owner,
                    self.generation,
                    PROCESSING_LEASE_SEC,
                ):
                    self.lost.set()
                    log.warning(
                        f"lease 续租失败 sid={self.sid} gen={self.generation} owner={self.owner}，"
                        "当前 worker 已失去 ownership"
                    )
                    return
            except Exception:
                # 无法证明 ownership 仍然有效时按 fail-closed 处理：不允许本 worker 提交结果。
                self.lost.set()
                log.exception(
                    f"lease heartbeat 异常 sid={self.sid} gen={self.generation} owner={self.owner}"
                )
                return

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(0.2, PROCESSING_HEARTBEAT_SEC + 0.1))


def _stale_result(tid: str, sid: int, generation: int, where: str):
    log.warning(
        f"[{tid}] 丢弃 stale worker 结果 sid={sid} gen={generation} stage={where} "
        "（lease 已丢失或任务已被新 generation 接管）"
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
    t0 = time.time()
    log.info(
        f"[{tid}] {'接管/重新领取' if reclaimed else '领取'} sid={sid} uid={uid} "
        f"gen={generation} lease={PROCESSING_LEASE_SEC:.1f}s 第 {rc + 1} 次业务尝试"
    )

    heartbeat = ProcessingLeaseHeartbeat(sid, tid, generation)
    heartbeat.start()
    try:
        # 欠费：不消耗 retry，fenced 地转 arrears。
        balance = billing.get_balance(uid)
        if billing.is_arrears(uid):
            if not sm.transition_owned(
                sid,
                "arrears",
                tid,
                generation,
                result_msg="账户欠费，识图服务不可用",
            ):
                _stale_result(tid, sid, generation, "arrears")
            else:
                log.error(
                    f"[{tid}] sid={sid} gen={generation} 欠费 uid={uid} 余额={balance} "
                    "-> 转 arrears（待充值，不消耗 retry）"
                )
            return True

        log.info(
            f"[{tid}] sid={sid} gen={generation} 计费检查通过 余额={balance}，调用识图模型..."
        )

        t_model = time.time()
        ok, version, msg = recognize.call_vision_model(sid, rc)
        cost_model = (time.time() - t_model) * 1000
        log.info(
            f"[{tid}] sid={sid} gen={generation} 识图模型返回 ok={ok} 版本={version} "
            f"耗时={cost_model:.1f}ms（{msg}）"
        )

        if heartbeat.lost.is_set():
            _stale_result(tid, sid, generation, "after_model")
            return True

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

        # 成功结果：版本核验 + fenced finalize。任务状态和本地计费在同一个 DB transaction。
        passed = recognize.meets_version(version, recognize.REQUIRED_VERSION)
        verdict = (
            f"核验通过：识别版本 {version} >= 要求 {recognize.REQUIRED_VERSION}"
            if passed
            else f"核验未通过：识别版本 {version} < 要求 {recognize.REQUIRED_VERSION}"
        )
        before = billing.get_balance(uid)
        finalized, after = sm.finalize_done_owned(
            sid,
            tid,
            generation,
            billing.COST_PER_CALL,
            version,
            recognize.REQUIRED_VERSION,
            verdict,
        )
        if not finalized:
            _stale_result(tid, sid, generation, "finalize")
            return True

        log.info(
            f"[{tid}] sid={sid} gen={generation} 扣费 {billing.COST_PER_CALL} -> 余额 {before} => {after}"
        )
        log.info(
            f"[{tid}] sid={sid} gen={generation} 版本核验 {'通过' if passed else '未通过'} -> {verdict} "
            f"| 本次总耗时 {(time.time() - t0) * 1000:.1f}ms"
        )
        return True
    finally:
        heartbeat.stop()


def recover_arrears(tid: str):
    """余额恢复后，把欠费任务重新放回队列。arrears 不属于 processing ownership。"""
    sid = db.find_arrears_id()
    if sid is None:
        return
    row = db.get_task(sid)
    if row and not billing.is_arrears(row["user_id"]):
        if sm.transition(sid, "pending", next_attempt_at=0, result_msg="余额已恢复，重新入队"):
            log.info(f"[{tid}] sid={sid} 余额已恢复 -> 重新入队")


def loop(tid: str):
    while True:
        try:
            recover_arrears(tid)
            if not handle_one(tid):
                time.sleep(POLL_INTERVAL)
        except Exception:
            # processing 期间异常不会手工改回 pending；停止 heartbeat 后 lease 到期即可由其他 worker 接管。
            log.exception(f"[{tid}] 处理任务异常；若已领取，等待 processing lease 到期后自动恢复")
            time.sleep(POLL_INTERVAL)


def main():
    db.init_db()
    log.info(
        f"{NAME} 启动 | 并发线程={THREADS} | retry上限={db.MAX_RETRY} | "
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
