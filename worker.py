import os, time, threading

import db, logutil, billing, recognize
import state_machine as sm

WORKER_ID = os.environ.get("WORKER_ID", "1")
NAME = f"worker-{WORKER_ID}"
THREADS = int(os.environ.get("WORKER_THREADS", "4"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "0.5"))

log = logutil.setup(NAME)


def handle_one(tid: str):
    """领取并处理一个任务；返回 False 表示当前没有待处理任务"""
    sid = db.find_pending_id()
    if sid is None:
        return False

    if not sm.transition(sid, "processing"):
        log.info(f"[{tid}] 领取失败 sid={sid}（已被其他 worker 抢走）")
        return True

    row = db.get_task(sid)
    uid, rc = row["user_id"], row["retry_count"] or 0
    t0 = time.time()
    log.info(f"[{tid}] 开始处理 sid={sid} uid={uid} 第 {rc + 1} 次尝试")

    # 欠费：不消耗 retry，转 arrears 等充值
    balance = billing.get_balance(uid)
    if billing.is_arrears(uid):
        sm.transition(sid, "arrears", result_msg="账户欠费，识图服务不可用")
        log.error(f"[{tid}] sid={sid} 欠费 uid={uid} 余额={balance} -> 转 arrears（待充值，不消耗 retry）")
        return True
    log.info(f"[{tid}] sid={sid} 计费检查通过 余额={balance}，调用识图模型...")

    # 调识图模型拿版本号
    t_model = time.time()
    ok, version, msg = recognize.call_vision_model(sid, rc)
    cost_model = (time.time() - t_model) * 1000
    log.info(f"[{tid}] sid={sid} 识图模型返回 ok={ok} 版本={version} 耗时={cost_model:.1f}ms（{msg}）")

    if not ok:
        rc += 1
        if rc >= db.MAX_RETRY:
            sm.transition(sid, "dlq", retry_count=rc, result_msg=msg)
            log.error(f"[{tid}] sid={sid} 识别失败 retry={rc}/{db.MAX_RETRY} -> DLQ 待人工审核（{msg}）")
        else:
            sm.transition(sid, "pending", retry_count=rc, result_msg=msg)
            log.warning(f"[{tid}] sid={sid} 识别失败 retry={rc}/{db.MAX_RETRY} -> 回队列等待重试（{msg}）")
        return True

    # 识别成功：扣费 + 机械核验版本
    before = billing.get_balance(uid)
    balance = billing.charge(uid)
    log.info(f"[{tid}] sid={sid} 扣费 1 -> 余额 {before} => {balance}")

    passed = recognize.meets_version(version, recognize.REQUIRED_VERSION)
    verdict = (f"核验通过：识别版本 {version} >= 要求 {recognize.REQUIRED_VERSION}" if passed
               else f"核验未通过：识别版本 {version} < 要求 {recognize.REQUIRED_VERSION}")
    sm.transition(sid, "done", detected_version=version,
                  required_version=recognize.REQUIRED_VERSION, result_msg=verdict)
    log.info(f"[{tid}] sid={sid} 版本核验 {'通过' if passed else '未通过'} -> {verdict} "
             f"| 本次总耗时 {(time.time() - t0) * 1000:.1f}ms")
    return True


def recover_arrears(tid: str):
    """余额恢复后，把欠费任务重新放回队列"""
    sid = db.find_arrears_id()
    if sid is None:
        return
    row = db.get_task(sid)
    if row and not billing.is_arrears(row["user_id"]):
        sm.transition(sid, "pending", result_msg="余额已恢复，重新入队")
        log.info(f"[{tid}] sid={sid} 余额已恢复 -> 重新入队")


def loop(tid: str):
    while True:
        try:
            recover_arrears(tid)
            if not handle_one(tid):
                time.sleep(POLL_INTERVAL)
        except Exception:
            log.exception(f"[{tid}] 处理任务异常")
            time.sleep(POLL_INTERVAL)


def main():
    db.init_db()
    log.info(f"{NAME} 启动 | 并发线程={THREADS} | retry上限={db.MAX_RETRY} | 要求版本={recognize.REQUIRED_VERSION}")
    threads = [threading.Thread(target=loop, args=(f"{NAME}-t{i}",), daemon=True) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
