#!/usr/bin/env python3
"""健康检查异常时的自动回滚。

流程：健康检查异常 -> 看任务、看状态 -> 判定是否需要回滚 -> 回滚到上一个健康版本 -> 复检
判据（可环境变量覆盖）：
  PASS_RATE_MIN        通过率下限(%)
  ERROR_RATE_MAX       错误率上限(%)
  P95_TASK_MS_MAX      任务延时 p95 上限(ms)
  QUEUE_DEPTH_MAX      队列深度上限
  DLQ_MAX              DLQ 累计上限
  CPU_MAX / MEM_MAX    资源上限(%)
  UNHEALTHY_STREAK     连续异常达到几次才触发回滚（防抖动）
  AUTO_ROLLBACK        1 时才真正执行回滚，0 只记录并告警（默认 1）
"""
import argparse
import json
import os
import subprocess
import sys
import time

import db
import logutil
import metrics

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSIONS = os.path.join(ROOT, "ci", "versions.json")
HISTORY = os.path.join(ROOT, "logs", "health_history.json")

THRESHOLDS = {
    "PASS_RATE_MIN": float(os.environ.get("PASS_RATE_MIN", "50")),
    "ERROR_RATE_MAX": float(os.environ.get("ERROR_RATE_MAX", "50")),
    "P95_TASK_MS_MAX": float(os.environ.get("P95_TASK_MS_MAX", "15000")),
    "QUEUE_DEPTH_MAX": int(os.environ.get("QUEUE_DEPTH_MAX", "200")),
    "DLQ_MAX": int(os.environ.get("DLQ_MAX", "30")),
    "CPU_MAX": float(os.environ.get("CPU_MAX", "90")),
    "MEM_MAX": float(os.environ.get("MEM_MAX", "90")),
}
UNHEALTHY_STREAK = int(os.environ.get("UNHEALTHY_STREAK", "2"))
AUTO_ROLLBACK = os.environ.get("AUTO_ROLLBACK", "1") == "1"
UNITS = ["screenshot-api@8001", "screenshot-api@8002", "screenshot-api@8003",
         "screenshot-worker@1", "screenshot-worker@2", "screenshot-worker@3"]

log = logutil.setup("rollback")


def sh(cmd, cwd=ROOT):
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=120)  # nosec
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as exc:
        return -1, "", str(exc)


def current_commit():
    code, out, _ = sh("git rev-parse --short HEAD")
    return out if code == 0 else "unknown"


# ---------------- 判定：健康是否异常 ----------------
def evaluate(m):
    """返回 (是否异常, 违规项列表)；违规项带 kind 用于后续决策。"""
    bad = []
    if m["pass_rate_percent"] < THRESHOLDS["PASS_RATE_MIN"] and m["queue_depth"] + m["dlq_count"] > 0:
        bad.append(("quality", "通过率 %.1f%% < %.1f%%" % (m["pass_rate_percent"], THRESHOLDS["PASS_RATE_MIN"])))
    if m["error_rate_percent"] > THRESHOLDS["ERROR_RATE_MAX"]:
        bad.append(("quality", "错误率 %.1f%% > %.1f%%" % (m["error_rate_percent"], THRESHOLDS["ERROR_RATE_MAX"])))
    if m["task_latency"]["p95_ms"] > THRESHOLDS["P95_TASK_MS_MAX"]:
        bad.append(("quality", "任务 p95 %sms > %sms" % (m["task_latency"]["p95_ms"], THRESHOLDS["P95_TASK_MS_MAX"])))
    if m["queue_depth"] > THRESHOLDS["QUEUE_DEPTH_MAX"]:
        bad.append(("capacity", "队列深度 %s > %s" % (m["queue_depth"], THRESHOLDS["QUEUE_DEPTH_MAX"])))
    if m["dlq_count"] > THRESHOLDS["DLQ_MAX"]:
        bad.append(("quality", "DLQ 累计 %s > %s" % (m["dlq_count"], THRESHOLDS["DLQ_MAX"])))
    if m["cpu_percent"] > THRESHOLDS["CPU_MAX"]:
        bad.append(("resource", "CPU %.1f%% > %.1f%%" % (m["cpu_percent"], THRESHOLDS["CPU_MAX"])))
    if m["memory_percent"] > THRESHOLDS["MEM_MAX"]:
        bad.append(("resource", "内存 %.1f%% > %.1f%%" % (m["memory_percent"], THRESHOLDS["MEM_MAX"])))
    return (len(bad) > 0), bad


# ---------------- 看任务、看状态 ----------------
def inspect_tasks(limit=200):
    """读取任务表，给出状态分布、卡住的任务、最近失败样例。"""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM submissions GROUP BY status").fetchall()
        dist = {r["status"]: r["c"] for r in rows}

        stuck = conn.execute(
            "SELECT COUNT(*) AS c FROM submissions "
            "WHERE status IN ('pending','processing') "
            "AND created_at < datetime('now','-5 minutes')").fetchone()["c"]

        recent = conn.execute(
            "SELECT id, status, retry_count, detected_version, result_msg, updated_at "
            "FROM submissions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()

    failed = [dict(r) for r in recent if r["status"] in ("dlq", "rejected")][:5]
    return {
        "dist": dist,
        "stuck_over_5min": stuck,
        "recent_failed": failed,
    }


def decide(bad, tasks):
    """看任务 + 看状态后决定动作：rollback / observe / alert。"""
    kinds = {k for k, _ in bad}
    dist = tasks["dist"]
    if "quality" in kinds:
        return "rollback", "质量指标越界（通过率/错误率/延时/DLQ），判定为本次发布引入，执行回滚"
    if "capacity" in kinds:
        if tasks["stuck_over_5min"] > 0 and dist.get("done", 0) == 0:
            return "rollback", "队列堆积且无任务完成、有任务卡死超过5分钟，判定 worker 异常，执行回滚"
        return "observe", "仅队列深度越界但仍有任务完成，属容量问题，先观察（应扩容而非回滚）"
    if "resource" in kinds:
        return "alert", "仅资源水位越界，属容量/资源问题，告警不回滚"
    return "observe", "无明确判据，保持观察"


# ---------------- 版本记录 ----------------
def load_versions():
    if os.path.exists(VERSIONS):
        try:
            with open(VERSIONS, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"current": None, "history": []}


def save_versions(v):
    os.makedirs(os.path.dirname(VERSIONS), exist_ok=True)
    with open(VERSIONS, "w", encoding="utf-8") as fh:
        json.dump(v, fh, ensure_ascii=False, indent=2)


def record(status, commit=None, note=""):
    v = load_versions()
    c = commit or current_commit()
    for h in v["history"]:
        if h["commit"] == c:
            h["status"] = status
            h["note"] = note
            h["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            break
    else:
        v["history"].append({"commit": c, "status": status,
                             "time": time.strftime("%Y-%m-%d %H:%M:%S"), "note": note})
    if v.get("current") is None or status == "good":
        v["current"] = {"commit": c, "status": status,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_versions(v)
    log.info("版本记录 | commit=%s 状态=%s %s" % (c, status, note))
    return v


def last_good(exclude):
    v = load_versions()
    for h in reversed(v["history"]):
        if h["status"] == "good" and h["commit"] != exclude:
            return h["commit"]
    return None


# ---------------- 历史窗口（防抖） ----------------
def push_history(unhealthy, reasons):
    hist = []
    if os.path.exists(HISTORY):
        try:
            with open(HISTORY, "r", encoding="utf-8") as fh:
                hist = json.load(fh)
        except Exception:
            hist = []
    hist.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "unhealthy": unhealthy, "reasons": reasons})
    hist = hist[-20:]
    with open(HISTORY, "w", encoding="utf-8") as fh:
        json.dump(hist, fh, ensure_ascii=False, indent=2)
    streak = 0
    for h in reversed(hist):
        if h["unhealthy"]:
            streak += 1
        else:
            break
    return streak


# ---------------- 回滚执行 ----------------
def do_rollback(target):
    log.warning("开始回滚 | 目标版本=%s" % target)
    code, out, err = sh("git checkout %s -- ." % target)
    if code != 0:
        log.error("回滚失败：切换版本出错 | %s %s" % (out, err))
        return False
    log.info("代码已切换到 %s" % target)

    code, out, err = sh("sudo systemctl restart " + " ".join(UNITS))
    if code != 0:
        log.error("回滚失败：重启服务出错 | %s %s" % (out, err))
        return False
    log.info("服务已重启 | %s" % " ".join(UNITS))

    time.sleep(20)  # 给新版本一个预热 + 至少一轮健康检查的时间
    m = metrics.collect()
    unhealthy, bad = evaluate(m)
    if unhealthy:
        log.error("回滚后复检仍异常 | %s" % "; ".join(r for _, r in bad))
        record("bad", note="回滚后仍异常")
        return False
    log.info("回滚后复检正常 | 通过率=%s%% 错误率=%s%% 队列=%s"
             % (m["pass_rate_percent"], m["error_rate_percent"], m["queue_depth"]))
    record("good", commit=target, note="回滚后验证通过")
    return True


def on_health_sample(m):
    """healthd 每次采集后调用：判定 -> 看任务 -> 必要时回滚。"""
    unhealthy, bad = evaluate(m)
    reasons = [r for _, r in bad]
    streak = push_history(unhealthy, reasons)

    if not unhealthy:
        if streak == 0:
            record("good", note="健康检查通过")
        return "healthy"

    log.warning("健康检查异常(连续%s次) | %s" % (streak, "; ".join(reasons)))
    tasks = inspect_tasks()
    log.warning("查看任务状态 | 分布=%s 卡死超5分钟=%s 最近失败样例=%s"
                % (json.dumps(tasks["dist"], ensure_ascii=False),
                   tasks["stuck_over_5min"],
                   json.dumps(tasks["recent_failed"], ensure_ascii=False)))

    action, why = decide(bad, tasks)
    log.warning("回滚判定 | 动作=%s 原因=%s" % (action, why))

    if action != "rollback":
        return action
    if streak < UNHEALTHY_STREAK:
        log.warning("连续异常 %s 次未达阈值 %s，暂不回滚" % (streak, UNHEALTHY_STREAK))
        return "pending_rollback"
    if not AUTO_ROLLBACK:
        log.warning("AUTO_ROLLBACK=0，仅告警不执行回滚")
        return "alert_only"

    cur = current_commit()
    target = last_good(cur)
    if not target:
        log.error("无可用健康版本，无法回滚（请用 python3 rollback.py --record good 标记基线）")
        return "no_target"
    record("bad", commit=cur, note="; ".join(reasons))
    return "rolled_back" if do_rollback(target) else "rollback_failed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="按当前健康指标判定并处理（含回滚）")
    ap.add_argument("--inspect", action="store_true", help="只看任务与状态，不做处理")
    ap.add_argument("--record", choices=["good", "bad"], help="把当前 commit 标记为健康/异常版本")
    ap.add_argument("--to", help="手动回滚到指定 commit")
    ap.add_argument("--show", action="store_true", help="打印当前状态、阈值与最近健康历史")
    args = ap.parse_args()

    db.init_db()

    if args.record:
        record(args.record, note="手工标记")
        print("已标记当前版本 %s 为 %s" % (current_commit(), args.record))
        return 0
    if args.to:
        ok = do_rollback(args.to)
        print("回滚%s" % ("成功" if ok else "失败"))
        return 0 if ok else 1
    if args.inspect or args.show:
        m = metrics.collect()
        unhealthy, bad = evaluate(m)
        tasks = inspect_tasks()
        print("当前 commit :", current_commit())
        print("健康指标    : 通过率=%s%% 错误率=%s%% 任务p95=%sms 队列=%s DLQ=%s CPU=%s%% 内存=%s%%"
              % (m["pass_rate_percent"], m["error_rate_percent"], m["task_latency"]["p95_ms"],
                 m["queue_depth"], m["dlq_count"], m["cpu_percent"], m["memory_percent"]))
        print("是否异常    :", unhealthy, bad)
        print("任务分布    :", json.dumps(tasks["dist"], ensure_ascii=False))
        print("卡死>5min   :", tasks["stuck_over_5min"])
        print("失败样例    :", json.dumps(tasks["recent_failed"], ensure_ascii=False))
        print("阈值        :", json.dumps(THRESHOLDS, ensure_ascii=False))
        v = load_versions()
        print("版本记录    :", json.dumps(v, ensure_ascii=False))
        return 0 if not unhealthy else 2
    if args.check:
        m = metrics.collect()
        result = on_health_sample(m)
        print("处理结果:", result)
        return 0 if result in ("healthy", "observe", "alert") else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
