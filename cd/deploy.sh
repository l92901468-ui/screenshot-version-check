#!/usr/bin/env bash
# CD 流水线：image -> staging -> health check -> prod -> health check -> rollback
#
# 与 CI 的衔接：默认读 ci/reports/last_build.json（CI 写出的 {commit, image}），
# 也可以手工指定 --image / --commit。判据全部复用 rollback.evaluate（和 healthd 同一套）。
set -u
set -o pipefail

REPO=/home/ubuntu/screenshot-api
CD=$REPO/cd
REPORT=$CD/reports
mkdir -p "$REPORT"
TS=$(date +%Y%m%d%H%M%S)

TARGET_ENV=staging
IMAGE=""
COMMIT=""
FROM_CI=1
APPLY_CAP=0
DO_ROLLBACK=1
STAGING_URL=http://127.0.0.1:9001/api/health
PROD_URL=http://127.0.0.1:8001/api/health
GATE_TIMES=3
GATE_INTERVAL=5

while [ $# -gt 0 ]; do
  case "$1" in
    --env) TARGET_ENV=$2; shift 2;;
    --image) IMAGE=$2; FROM_CI=0; shift 2;;
    --commit) COMMIT=$2; shift 2;;
    --apply-capacity) APPLY_CAP=1; shift;;
    --no-rollback) DO_ROLLBACK=0; shift;;
    --times) GATE_TIMES=$2; shift 2;;
    --interval) GATE_INTERVAL=$2; shift 2;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "未知参数: $1"; exit 1;;
  esac
done

DOCKER=docker
if ! docker info >/dev/null 2>&1; then DOCKER="sudo -n docker"; fi

GREEN="\033[32m"; RED="\033[31m"; YEL="\033[33m"; NC="\033[0m"
say() { printf '%b\n' "$(date +%H:%M:%S) $1"; }
ok()   { say "${GREEN}[PASS]${NC} $1"; }
bad()  { say "${RED}[FAIL]${NC} $1"; }
warn() { say "${YEL}[SKIP]${NC} $1"; }

STAGE_STATUS="not_run"; PROD_STATUS="not_run"; RESULT="FAILED"

# ---------- 1. 输入解析（CI 产物 -> CD 消费）----------
say "========== CD 启动 | 环境=$TARGET_ENV =========="
cd "$REPO" || exit 1
if [ "$FROM_CI" = 1 ] && [ -f ci/reports/last_build.json ]; then
  IMAGE=$(python3 -c "import json;print(json.load(open('ci/reports/last_build.json'))['image'])")
  CCOMMIT=$(python3 -c "import json;print(json.load(open('ci/reports/last_build.json'))['commit'])")
  [ -n "$COMMIT" ] || COMMIT=$CCOMMIT
  say "取自 CI 产物 ci/reports/last_build.json -> image=$IMAGE commit=${COMMIT:-未知}"
fi
[ -n "$COMMIT" ] || COMMIT=$(git rev-parse --short HEAD)
[ -n "$IMAGE" ] || IMAGE="screenshot-api:latest"

if ! $DOCKER image inspect "$IMAGE" >/dev/null 2>&1; then
  bad "镜像 $IMAGE 不存在，先跑 CI（bash ci/ci.sh）"
  exit 1
fi
ok "镜像就绪 $IMAGE | 目标 commit=$COMMIT"

# ---------- 2. staging：容器部署 + 健康门禁 ----------
say "---------- 阶段 staging（容器）----------"
if IMAGE_TAG="$IMAGE" $DOCKER compose up -d --no-build >> "$REPORT/deploy-$TS.log" 2>&1; then
  ok "staging 容器已启动（9001，直接用 CI 构建的镜像）"
elif IMAGE_TAG="$IMAGE" $DOCKER compose up -d --build >> "$REPORT/deploy-$TS.log" 2>&1; then
  ok "staging 容器已启动（9001，镜像不存在已本地构建）"
else
  bad "staging 启动失败，详见 $REPORT/deploy-$TS.log"
  printf '{"ts":"%s","env":"%s","image":"%s","commit":"%s","staging":"FAILED","prod":"not_run","result":"FAILED"}\n' \
    "$(date +%Y-%m-%d\ %H:%M:%S)" "$TARGET_ENV" "$IMAGE" "$COMMIT" > "$REPORT/deploy-$TS.json"
  exit 1
fi

if python3 "$CD/health_gate.py" --url "$STAGING_URL" --wait 8 --times "$GATE_TIMES" --interval "$GATE_INTERVAL"; then
  STAGE_STATUS=PASSED; ok "staging 健康门禁通过"
else
  STAGE_STATUS=FAILED; bad "staging 健康门禁未通过，停止发布"
  $DOCKER compose down >> "$REPORT/deploy-$TS.log" 2>&1
  printf '{"ts":"%s","env":"%s","image":"%s","commit":"%s","staging":"FAILED","prod":"not_run","result":"FAILED"}\n' \
    "$(date +%Y-%m-%d\ %H:%M:%S)" "$TARGET_ENV" "$IMAGE" "$COMMIT" > "$REPORT/deploy-$TS.json"
  exit 1
fi

# ---------- 3. prod ----------
if [ "$TARGET_ENV" != "prod" ]; then
  warn "未指定 --env prod，停在 staging（这是默认的安全行为）"
  printf '{"ts":"%s","env":"staging","image":"%s","commit":"%s","staging":"%s","prod":"not_run","result":"STAGING_OK"}\n' \
    "$(date +%Y-%m-%d\ %H:%M:%S)" "$IMAGE" "$COMMIT" "$STAGE_STATUS" > "$REPORT/deploy-$TS.json"
  say "报告: $REPORT/deploy-$TS.json"
  exit 0
fi

say "---------- 阶段 prod（systemd 现网）----------"
PREV=$(git rev-parse --short HEAD)
ok "记录回滚基线: $PREV"

# 容量预算：决定开几个 worker、每实例几线程
if [ "$APPLY_CAP" = 1 ]; then
  say "按资源预算调整并发（capacity.py --apply）"
  python3 "$CD/capacity.py" --apply | tail -12
else
  python3 "$CD/capacity.py" | tail -6
fi

git fetch -q origin main 2>> "$REPORT/deploy-$TS.log"
if git reset --hard "$COMMIT" >> "$REPORT/deploy-$TS.log" 2>&1; then
  ok "prod 代码已切到 $COMMIT"
else
  bad "切换版本失败"; exit 1
fi

sudo systemctl restart screenshot-api@8001 screenshot-api@8002 screenshot-api@8003 \
                       screenshot-worker@1 screenshot-worker@2 screenshot-worker@3
ok "prod 服务已重启"

if python3 "$CD/health_gate.py" --url "$PROD_URL" --wait 10 --times "$GATE_TIMES" --interval "$GATE_INTERVAL"; then
  PROD_STATUS=PASSED; RESULT=SUCCESS
  python3 "$REPO/rollback.py" --record good >/dev/null 2>&1
  ok "prod 健康门禁通过，版本 $COMMIT 标记为 good"
else
  PROD_STATUS=FAILED
  bad "prod 健康门禁未通过"
  if [ "$DO_ROLLBACK" = 1 ]; then
    say "触发回滚到基线 $PREV"
    if python3 "$REPO/rollback.py" --to "$PREV" 2>&1 | tail -3; then
      ok "回滚完成"
      if python3 "$CD/health_gate.py" --url "$PROD_URL" --wait 5 --times 2 --interval 5; then
        RESULT=ROLLED_BACK_AND_HEALTHY; ok "回滚后复检通过"
      else
        RESULT=ROLLED_BACK_BUT_UNHEALTHY; bad "回滚后仍不健康，需要人工介入"
      fi
    else
      RESULT=ROLLBACK_FAILED; bad "回滚失败，需要人工介入"
    fi
  else
    RESULT=FAILED_NO_ROLLBACK
  fi
fi

printf '{"ts":"%s","env":"prod","image":"%s","commit":"%s","prev_commit":"%s","staging":"%s","prod":"%s","result":"%s"}\n' \
  "$(date +%Y-%m-%d\ %H:%M:%S)" "$IMAGE" "$COMMIT" "$PREV" "$STAGE_STATUS" "$PROD_STATUS" "$RESULT" \
  > "$REPORT/deploy-$TS.json"
say "========== CD 结束 | 结果=$RESULT =========="
say "报告: $REPORT/deploy-$TS.json"
[ "$RESULT" = "SUCCESS" ] || [ "$RESULT" = "ROLLED_BACK_AND_HEALTHY" ]
