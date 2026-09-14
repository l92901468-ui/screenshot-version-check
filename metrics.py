import os, time, threading, statistics
from datetime import datetime

import db, recognize

_WINDOW = 500                      # 请求延时滑动窗口
_req_ms = []
_lock = threading.Lock()


# ---------------- 请求延时（本实例，进程内采集） ----------------
def record_request(ms: float):
    with _lock:
        _req_ms.append(ms)
        if len(_req_ms) > _WINDOW:
            _req_ms.pop(0)


def _pct(vals, p):
    if not vals:
        return 0.0
    idx = max(0, min(len(vals) - 1, int(-(-p * len(vals) // 100)) - 1))
    return vals[idx]


def request_stats():
    with _lock:
        vals = sorted(_req_ms)
    if not vals:
        return {"avg_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "count": 0}
    return {"avg_ms": round(statistics.mean(vals), 1),
            "p95_ms": round(_pct(vals, 95), 1),
            "p99_ms": round(_pct(vals, 99), 1),
            "count": len(vals)}


# ---------------- 任务端到端延时（全局，来自 DB） ----------------
def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def task_latency():
    """任务从创建到终结（done / dlq）的耗时，单位毫秒"""
    con = db.connect()
    rows = con.execute("SELECT created_at, updated_at FROM submissions WHERE status IN ('done','dlq')").fetchall()
    con.close()
    vals = sorted((_dt(r["updated_at"]) - _dt(r["created_at"])).total_seconds() * 1000 for r in rows)
    if not vals:
        return {"avg_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "count": 0}
    return {"avg_ms": round(statistics.mean(vals), 1),
            "p95_ms": round(_pct(vals, 95), 1),
            "p99_ms": round(_pct(vals, 99), 1),
            "count": len(vals)}


# ---------------- 通过率 / 错误率 / 队列 / DLQ ----------------
def pass_rate():
    con = db.connect()
    rows = con.execute("""SELECT detected_version, required_version FROM submissions
                          WHERE status='done' AND detected_version IS NOT NULL""").fetchall()
    con.close()
    total = len(rows)
    passed = sum(1 for r in rows
                 if recognize.meets_version(r["detected_version"], r["required_version"] or recognize.REQUIRED_VERSION))
    return {"passed": passed, "total": total,
            "rate": round(passed / total * 100, 1) if total else 0.0}


def counts():
    con = db.connect()
    rows = con.execute("SELECT status, COUNT(*) c FROM submissions GROUP BY status").fetchall()
    con.close()
    d = {r["status"]: r["c"] for r in rows}
    d["total"] = sum(d.values())
    return d


def error_rate(c=None):
    c = c or counts()
    total = c.get("total", 0)
    if not total:
        return 0.0
    err = c.get("dlq", 0) + c.get("rejected", 0)
    return round(err / total * 100, 1)


# ---------------- CPU / 内存（读 /proc，无需第三方库） ----------------
def _cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:8]]
    return sum(v), v[3] + v[4]      # total, idle(+iowait)


def cpu_percent(interval=0.1):
    t1, i1 = _cpu_times()
    time.sleep(interval)
    t2, i2 = _cpu_times()
    dt, di = t2 - t1, i2 - i1
    return round(100.0 * (1 - di / dt), 1) if dt else 0.0


def memory_percent():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return round(100.0 * (total - avail) / total, 1)


# ---------------- 汇总 ----------------
def collect():
    c = counts()
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pass_rate_percent": pass_rate()["rate"],
        "task_latency": task_latency(),
        "request_latency": request_stats(),
        "error_rate_percent": error_rate(c),
        "cpu_percent": cpu_percent(),
        "memory_percent": memory_percent(),
        "queue_depth": c.get("pending", 0),
        "dlq_count": c.get("dlq", 0),
        "counts": c,
    }
