#!/usr/bin/env python3
"""资源预算与并发容量规划：回答"开不开多线程/多任务、开多少"。

思路（两步取小）：
  A. 需求侧：按 1 万用户在提交窗口内的到达率 + 单任务耗时，用排队论算需要多少并发
  B. 资源侧：按这台机器的 CPU / 内存 / SQLite 写串行能力，算最多能撑多少并发
  最终取 min(A, B)，再拆成 "worker 实例数 × 每实例线程数"

所有参数可环境变量覆盖；单任务耗时默认从 DB 实测读取，不拍脑袋。
"""
import argparse
import json
import re
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db          # noqa: E402
import metrics     # noqa: E402

PLAN_FILE = os.path.join(HERE, "capacity.json")


def envf(name, default):
    return float(os.environ.get(name, default))


def envi(name, default):
    return int(os.environ.get(name, default))


# ---------------- 可调参数 ----------------
USERS = envi("USERS", 10000)                  # 目标用户数（题目给的 1 万）
WINDOW_MIN = envf("WINDOW_MIN", 60)           # 假定这些提交集中在多少分钟内完成
SAFETY = envf("SAFETY", 2.0)                  # 安全系数：吸收突发 + 重试 + 抖动
TARGET_UTIL = envf("TARGET_UTIL", 0.7)        # 排队论：服务台利用率上限（>0.7 排队会陡增）
THREADS_PER_WORKER = envi("THREADS_PER_WORKER", 4)
MAX_WORKERS = envi("MAX_WORKERS", 6)
MIN_WORKERS = envi("MIN_WORKERS", 1)
MEM_HEADROOM = envf("MEM_HEADROOM", 0.75)     # 内存只用到预算的 75%，留突发
CPU_TARGET = envf("CPU_TARGET", 0.7)          # CPU 目标水位
CPU_PER_TASK_RATIO = envf("CPU_PER_TASK_RATIO", 0.05)  # 单任务 CPU 时间 / 墙钟时间（I/O 密集估算）
DB_MAX_CONCURRENCY = envi("DB_MAX_CONCURRENCY", 32)    # SQLite 单写串行，经验上限

# 实测值（2026-09-14 在本机 ps 采样）：worker 单线程 18.9MB、4 线程 19.7MB、API 25MB、healthd 21MB
RSS_PROC_MB = envf("RSS_PROC_MB", 18.6)       # worker 进程基础 RSS
RSS_THREAD_MB = envf("RSS_THREAD_MB", 1.0)    # 每线程 RSS（实测 0.3MB，保守取 1MB）
RSS_API_MB = envf("RSS_API_MB", 25.0)
RSS_HEALTHD_MB = envf("RSS_HEALTHD_MB", 21.0)
API_INSTANCES = envi("API_INSTANCES", 3)
RESERVED_MB = envf("RESERVED_MB", 512)        # OS + 页缓存 + nginx(39MB) + 余量
DEFAULT_TASK_MS = envf("DEFAULT_TASK_MS", 500.0)


def host_info():
    cores = os.cpu_count() or 1
    mem_total = mem_avail = 0.0
    with open("/proc/meminfo") as fh:
        info = {}
        for line in fh:
            k, v = line.split(":", 1)
            info[k] = float(v.split()[0]) / 1024.0   # kB -> MB
    mem_total = info.get("MemTotal", 0.0)
    mem_avail = info.get("MemAvailable", 0.0)
    return {"cores": cores, "mem_total_mb": round(mem_total, 1),
            "mem_available_mb": round(mem_avail, 1)}


def workload():
    """从 DB 取实测的单任务耗时与重试放大系数。"""
    db.init_db()
    lat = metrics.task_latency()
    task_ms = float(lat.get("avg_ms") or 0) or DEFAULT_TASK_MS
    conn = db.connect()
    try:
        row = conn.execute("SELECT AVG(retry_count) AS r, COUNT(*) AS c FROM submissions").fetchone()
        avg_retry = float(row["r"] or 0.0)
        total = int(row["c"] or 0)
    finally:
        conn.close()
    # 一个任务平均要被处理 1 + avg_retry 次（重试也算一次处理）
    amplify = 1.0 + avg_retry
    return {"task_ms": round(task_ms, 1), "avg_retry": round(avg_retry, 2),
            "amplify": round(amplify, 2), "samples": total}


def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)  # nosec
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def running_workers():
    """当前已存在（在跑或已启用）的 worker 实例编号。"""
    out = ""
    for cmd in ("systemctl list-units --no-legend 'screenshot-worker@*.service'",
                "systemctl list-unit-files --no-legend 'screenshot-worker@*.service'"):
        _, o, _ = sh(cmd)
        out += "\n" + o
    ids = set()
    for line in out.splitlines():
        m = re.search(r"screenshot-worker@(\d+)\.service", line)
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids)


def build_plan():
    host = host_info()
    wl = workload()

    # ---- A. 需求侧：Little's law + 排队论 ----
    lam = USERS / (WINDOW_MIN * 60.0)                 # 到达率 任务/秒
    lam_eff = lam * wl["amplify"]                     # 计入重试
    service_s = wl["task_ms"] / 1000.0                # 单任务服务时间
    # 服务台数 c 需满足 rho = lam_eff * service / c <= TARGET_UTIL
    c_need = lam_eff * service_s / TARGET_UTIL * SAFETY

    # ---- B. 资源侧 ----
    # 内存：能开多少 worker 实例
    worker_budget_mb = (host["mem_available_mb"] - RESERVED_MB
                        - RSS_API_MB * API_INSTANCES - RSS_HEALTHD_MB) * MEM_HEADROOM
    per_worker_mb = RSS_PROC_MB + RSS_THREAD_MB * THREADS_PER_WORKER
    max_instances = int(max(0.0, worker_budget_mb) // per_worker_mb)
    c_mem = max_instances * THREADS_PER_WORKER
    # CPU：每任务 CPU 时间 = 墙钟 * 比例
    cpu_per_task = service_s * CPU_PER_TASK_RATIO
    c_cpu = (host["cores"] * CPU_TARGET) / cpu_per_task if cpu_per_task > 0 else 999
    # DB：SQLite 写串行经验上限
    c_db = DB_MAX_CONCURRENCY

    c_cap = min(c_mem, c_cpu, c_db)
    c_final = int(min(math.ceil(c_need), math.floor(c_cap)))

    # ---- 拆成 实例 × 线程 ----
    if c_final <= 1:
        workers, threads = 1, 1
        switch = "关闭多线程（单实例单线程）：需求低于单线程处理能力"
    else:
        threads = min(THREADS_PER_WORKER, c_final)
        workers = max(MIN_WORKERS, min(MAX_WORKERS, math.ceil(c_final / threads)))
        switch = "开启多线程多任务：%d 个 worker 实例 × %d 线程" % (workers, threads)

    capacity = workers * threads
    est_mem = workers * (RSS_PROC_MB + RSS_THREAD_MB * threads) \
        + API_INSTANCES * RSS_API_MB + RSS_HEALTHD_MB
    est_cpu = min(100.0, capacity * cpu_per_task / host["cores"] * 100.0)

    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": host,
        "workload": wl,
        "params": {
            "USERS": USERS, "WINDOW_MIN": WINDOW_MIN, "SAFETY": SAFETY,
            "TARGET_UTIL": TARGET_UTIL, "THREADS_PER_WORKER": THREADS_PER_WORKER,
            "MAX_WORKERS": MAX_WORKERS, "MEM_HEADROOM": MEM_HEADROOM,
            "CPU_TARGET": CPU_TARGET, "CPU_PER_TASK_RATIO": CPU_PER_TASK_RATIO,
            "DB_MAX_CONCURRENCY": DB_MAX_CONCURRENCY, "RESERVED_MB": RESERVED_MB,
        },
        "demand": {
            "arrival_rps": round(lam, 3),
            "arrival_with_retry_rps": round(lam_eff, 3),
            "concurrency_needed": round(c_need, 2),
        },
        "ceiling": {
            "by_memory": c_mem, "by_cpu": round(c_cpu, 1), "by_db": c_db,
            "effective": round(c_cap, 1),
            "worker_budget_mb": round(max(0.0, worker_budget_mb), 1),
            "per_worker_mb": round(per_worker_mb, 1),
        },
        "plan": {
            "concurrency": c_final,
            "workers": workers,
            "threads_per_worker": threads,
            "real_concurrency": capacity,
            "switch": switch,
            "est_memory_mb": round(est_mem, 1),
            "est_cpu_percent": round(est_cpu, 1),
        },
        "scaling": {
            "scale_up_when": "queue_depth 持续 > 并发数（任务排队）且 CPU < %.0f%%、内存 < %.0f%%"
                             % (CPU_TARGET * 100, MEM_HEADROOM * 100),
            "scale_down_when": "queue_depth 长时间为 0 且 CPU < 20%",
            "never_exceed": "workers <= %d，总并发 <= %d（内存/CPU/DB 三者取小）"
                            % (MAX_WORKERS, int(c_cap)),
        },
    }


def apply_plan(plan, execute):
    """按计划调整 systemd 里的 worker 实例数与线程数。默认只打印，execute=True 才真做。"""
    workers = plan["plan"]["workers"]
    threads = plan["plan"]["threads_per_worker"]
    current = running_workers()
    actions = []

    dropin = "/etc/systemd/system/screenshot-worker@.service.d/threads.conf"
    actions.append("写 drop-in %s -> WORKER_THREADS=%d" % (dropin, threads))
    actions.append("systemctl daemon-reload")

    for i in range(1, max(workers, max(current) if current else 0) + 1):
        if i <= workers:
            actions.append("systemctl restart screenshot-worker@%d（应用新线程数并拉起）" % i)
        else:
            actions.append("systemctl stop screenshot-worker@%d（超出预算，回收）" % i)

    if not execute:
        return {"executed": False, "current_workers": current, "actions": actions}

    conf = "[Service]\nEnvironment=WORKER_THREADS=%d\n" % threads
    tmp = "/tmp/worker-threads.conf"
    with open(tmp, "w") as fh:
        fh.write(conf)
    sh("sudo install -d %s" % os.path.dirname(dropin))
    sh("sudo install -m 644 %s %s" % (tmp, dropin))
    sh("sudo systemctl daemon-reload")

    for i in range(1, workers + 1):
        sh("sudo systemctl enable screenshot-worker@%d" % i)
        sh("sudo systemctl restart screenshot-worker@%d" % i)
    top = max(current) if current else 0
    for i in range(workers + 1, top + 1):
        sh("sudo systemctl stop screenshot-worker@%d" % i)
        sh("sudo systemctl disable screenshot-worker@%d" % i)

    return {"executed": True, "current_workers": running_workers(), "actions": actions}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真去调整 systemd（默认只出计划）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    plan = build_plan()
    res = apply_plan(plan, args.apply)
    plan["apply"] = res

    with open(PLAN_FILE, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(plan, ensure_ascii=False))
        return 0

    h, w, d, c, p = plan["host"], plan["workload"], plan["demand"], plan["ceiling"], plan["plan"]
    print("=== 资源盘点 ===")
    print("  CPU %d 核 | 内存 总 %.0fMB / 可用 %.0fMB | 预留 %.0fMB"
          % (h["cores"], h["mem_total_mb"], h["mem_available_mb"], RESERVED_MB))
    print("=== 负载实测（来自 DB）===")
    print("  单任务平均耗时 %.1fms | 平均重试 %.2f 次 → 放大 %.2f 倍 | 样本 %d 条"
          % (w["task_ms"], w["avg_retry"], w["amplify"], w["samples"]))
    print("=== A. 需求侧（%d 人 / %d 分钟内提交完）===" % (USERS, int(WINDOW_MIN)))
    print("  到达率 %.3f 任务/秒，计入重试 %.3f 任务/秒" % (d["arrival_rps"], d["arrival_with_retry_rps"]))
    print("  按利用率 <= %.0f%% + 安全系数 %.1f → 需要并发 %.2f"
          % (TARGET_UTIL * 100, SAFETY, d["concurrency_needed"]))
    print("=== B. 资源侧上限 ===")
    print("  内存：worker 预算 %.0fMB / 每实例 %.1fMB → 上限 %d 并发"
          % (c["worker_budget_mb"], c["per_worker_mb"], c["by_memory"]))
    print("  CPU ：%d 核 × %.0f%% ÷ 每任务 CPU → 上限 %.0f 并发" % (h["cores"], CPU_TARGET * 100, c["by_cpu"]))
    print("  DB  ：SQLite 写串行经验上限 %d 并发" % c["by_db"])
    print("  取小 → 实际可用上限 %.1f 并发" % c["effective"])
    print("=== 结论 ===")
    print("  %s" % p["switch"])
    print("  目标并发 %d → 实际 %d（%d 实例 × %d 线程）"
          % (p["concurrency"], p["real_concurrency"], p["workers"], p["threads_per_worker"]))
    print("  预计占用：内存 %.1fMB（预算 %.0fMB）| CPU %.1f%%"
          % (p["est_memory_mb"], c["worker_budget_mb"], p["est_cpu_percent"]))
    print("=== 伸缩规则 ===")
    for k, v in plan["scaling"].items():
        print("  %-16s %s" % (k, v))
    print("=== 执行动作（%s）===" % ("已执行" if args.apply else "仅计划，加 --apply 生效"))
    for a in res["actions"]:
        print("  -", a)
    print("  当前在跑的 worker 实例:", res["current_workers"])
    print("  计划已写入:", PLAN_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
