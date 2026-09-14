#!/usr/bin/env bash
# 总入口：把 CI 和 CD 串成一条流水线
#
#   bash pipeline.sh                 # CI(push→build→test→scan) → CD 到 staging
#   bash pipeline.sh --prod          # CI → staging → prod（prod 前会再过一次健康门禁）
#   bash pipeline.sh --skip-ci       # 跳过 CI，用最近一次 CI 产物直接部署
#   bash pipeline.sh --prod --capacity   # 部署 prod 时按资源预算调整 worker 并发
#
# 环节之间的关联（不是各写各的）：
#   ci/reports/last_build.json   CI 写 → CD 读（image + commit）
#   cd/health_gate.py            复用 rollback.evaluate，和 healthd/rollback 同一套判据
#   cd/capacity.py               按资源预算决定 worker 实例数 × 线程数，部署时应用
#   ci/versions.json             rollback 维护的健康版本基线，CD 成功后追加 good
set -u
set -o pipefail

REPO=/home/ubuntu/screenshot-api
RUN_CI=1
TARGET_ENV=staging
CAP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prod) TARGET_ENV=prod; shift;;
    --skip-ci) RUN_CI=0; shift;;
    --capacity) CAP=1; shift;;
    *) echo "未知参数: $1"; exit 1;;
  esac
done

cd "$REPO" || exit 1

if [ "$RUN_CI" = 1 ]; then
  echo "########## 阶段一：CI ##########"
  bash ci/ci.sh || { echo "CI 未通过，终止"; exit 1; }
else
  echo "########## 跳过 CI，使用上次产物 ##########"
fi

echo "########## 阶段二：CD ##########"
if [ "$CAP" = 1 ]; then
  bash cd/deploy.sh --env "$TARGET_ENV" --apply-capacity
else
  bash cd/deploy.sh --env "$TARGET_ENV"
fi
