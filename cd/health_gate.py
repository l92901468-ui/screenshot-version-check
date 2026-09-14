#!/usr/bin/env python3
"""发布门禁用的健康检查。

关键点：**复用 rollback.evaluate 的同一套阈值与判据**，
这样"部署门禁判定不健康"和"运行期判定不健康要回滚"是同一个标准，
不会出现"门禁说健康、上线后 healthd 立刻判定异常要回滚"的割裂。
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import rollback  # noqa: E402


def fetch(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:   # nosec
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8001/api/health")
    ap.add_argument("--times", type=int, default=3, help="连续探测次数")
    ap.add_argument("--interval", type=int, default=5, help="每次间隔秒数")
    ap.add_argument("--allow", type=int, default=0, help="允许几次不健康仍算通过")
    ap.add_argument("--wait", type=int, default=0, help="探测前先等几秒（等服务起来）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.wait:
        time.sleep(args.wait)

    samples, bad = [], 0
    for i in range(args.times):
        item = {"seq": i + 1, "url": args.url}
        try:
            data = fetch(args.url)
            m = data.get("health") or data
            unhealthy, reasons = rollback.evaluate(m)
            item.update({
                "reachable": True,
                "ok": bool(data.get("ok", True)) and not unhealthy,
                "reasons": [r for _, r in reasons],
                "snapshot": {k: m.get(k) for k in
                             ("pass_rate_percent", "error_rate_percent", "queue_depth",
                              "dlq_count", "cpu_percent", "memory_percent")},
            })
        except Exception as exc:
            item.update({"reachable": False, "ok": False, "reasons": [str(exc)], "snapshot": {}})
        if not item["ok"]:
            bad += 1
        samples.append(item)
        if i < args.times - 1:
            time.sleep(args.interval)

    passed = bad <= args.allow
    out = {"url": args.url, "times": args.times, "bad": bad, "allow": args.allow,
           "passed": passed, "samples": samples,
           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if passed else 1

    print("=== 健康检查门禁 | %s ===" % args.url)
    for s in samples:
        flag = "OK  " if s["ok"] else "BAD "
        snap = s["snapshot"]
        print("  第%s次 [%s] 可达=%s 通过率=%s 错误率=%s 队列=%s DLQ=%s CPU=%s 内存=%s %s"
              % (s["seq"], flag, s["reachable"],
                 snap.get("pass_rate_percent"), snap.get("error_rate_percent"),
                 snap.get("queue_depth"), snap.get("dlq_count"),
                 snap.get("cpu_percent"), snap.get("memory_percent"),
                 ("<- " + "; ".join(s["reasons"])) if s["reasons"] else ""))
    print("  结果: %s（%d/%d 次不健康，允许 %d 次）"
          % ("PASSED" if passed else "FAILED", bad, args.times, args.allow))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
