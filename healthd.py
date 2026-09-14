import os
import time
import json

import db
import logutil
import metrics
import rollback

INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "30"))   # 采集间隔（秒）
# 是否允许健康检查异常时自动回滚（1=自动，0=只告警）。可用 systemctl edit 覆盖。
os.environ.setdefault("AUTO_ROLLBACK", "1")
# 注：请求延时按进程采集，healthd 自身不处理请求，故周期日志只记全局指标（DB 派生）；
#     请求延时在 GET /api/health 的响应里按被查询的 API 实例返回。
log = logutil.setup("healthd")


def main():
    db.init_db()
    log.info("healthd 启动 | 采集间隔=%ss | 自动回滚=%s"
             % (INTERVAL, os.environ.get("AUTO_ROLLBACK")))
    while True:
        try:
            m = metrics.collect()
            log.info(
                "健康检查 | CPU=%s%% 内存=%s%% | 通过率=%s%% 错误率=%s%% | "
                "任务延时 avg=%sms p95=%sms p99=%sms | "
                "队列深度=%s DLQ=%s | 分布=%s"
                % (m["cpu_percent"], m["memory_percent"],
                   m["pass_rate_percent"], m["error_rate_percent"],
                   m["task_latency"]["avg_ms"], m["task_latency"]["p95_ms"], m["task_latency"]["p99_ms"],
                   m["queue_depth"], m["dlq_count"], json.dumps(m["counts"], ensure_ascii=False)))
        except Exception:
            log.exception("健康检查采集异常")

        # 异常则进入判定：看任务、看状态、必要时回滚（内部有自己的异常处理）
        try:
            result = rollback.on_health_sample(metrics.collect())
            if result != "healthy":
                log.warning("健康自检处置结果=%s" % result)
        except Exception:
            log.exception("回滚判定异常")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
