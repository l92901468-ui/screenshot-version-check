#!/usr/bin/env bash
# CI 流水线：push -> build -> test -> scan
# 真实执行：git push 到本地裸仓库（模拟远端）、docker build、容器内跑 unittest、扫描门禁
# 模拟部分：远端代码托管平台/Runner/镜像仓库/漏洞库 都不存在，用本地等价物代替
set -u
set -o pipefail

REPO=/home/ubuntu/screenshot-api
REMOTE=/home/ubuntu/screenshot-api-remote.git
IMAGE=screenshot-api
TAG=$(date +%Y%m%d%H%M%S)
LOGDIR=$REPO/ci/reports
mkdir -p "$LOGDIR"
RUNLOG=$LOGDIR/ci-$TAG.log

# docker 权限兜底：普通用户不在 docker 组时退回 sudo
DOCKER=docker
if ! docker info >/dev/null 2>&1; then DOCKER="sudo -n docker"; fi

GREEN="\033[32m"; RED="\033[31m"; YEL="\033[33m"; NC="\033[0m"
say() {
  local plain
  plain=$(printf '%b' "$1" | sed -E 's/\x1b\[[0-9;]*m//g')
  printf '%b\n' "$(date +%H:%M:%S) $1"
  echo "[$(date +%H:%M:%S)] $plain" >> "$RUNLOG"
}
ok()   { say "${GREEN}[PASS]${NC} $1"; }
bad()  { say "${RED}[FAIL]${NC} $1"; }
warn() { say "${YEL}[SIM ]${NC} $1"; }

stage_begin() { STAGE=$1; T0=$(date +%s%3N); say "========== 阶段 $1 开始 =========="; }
stage_cost()  { say "---------- 阶段 $STAGE 耗时 $(( $(date +%s%3N) - T0 ))ms ----------"; }

say "CI 流水线启动 | BUILD=$TAG | 阶段: push -> build -> test -> scan"

# ---------------- 阶段 1: push ----------------
stage_begin "push (代码推送)"
cd "$REPO" || exit 1
if [ ! -d .git ]; then
  git init -q -b main
  warn "仓库未初始化，已本地初始化（真实环境由 GitLab/GitHub 托管）"
fi
git config user.email "ci@screenshot.local" 2>/dev/null
git config user.name  "CI Bot" 2>/dev/null
git add -A
if git diff --cached --quiet; then
  warn "无变更，跳过 commit（真实环境由 webhook 触发，此处手动跑）"
else
  git commit -q -m "ci: 自动提交 $TAG" && ok "已提交变更"
fi
if [ ! -d "$REMOTE" ]; then
  git init -q --bare "$REMOTE"
  git remote remove origin 2>/dev/null
  git remote add origin "$REMOTE"
  warn "无远端仓库，已用本地裸仓库 $REMOTE 代替（模拟 GitLab/GitHub）"
fi
if git push -q origin HEAD:main 2>>"$RUNLOG"; then
  ok "push 成功 -> $(git rev-parse --short HEAD)"
else
  bad "push 失败"; stage_cost; exit 1
fi
stage_cost

# ---------------- 阶段 2: build ----------------
stage_begin "build (Docker 镜像构建)"
cd "$REPO" || exit 1
if $DOCKER build -t "$IMAGE:$TAG" -t "$IMAGE:latest" . >> "$RUNLOG" 2>&1; then
  ok "镜像构建成功 $IMAGE:$TAG"
else
  bad "镜像构建失败，详见 $RUNLOG"; stage_cost; exit 1
fi
# 产物校验：确认代码真的进了镜像（防止上下文打包不全 / 缓存串层）
if $DOCKER run --rm "$IMAGE:$TAG" test -f /app/app.py && $DOCKER run --rm "$IMAGE:$TAG" test -d /app/tests; then
  ok "构建产物校验通过（/app/app.py 与 /app/tests 存在）"
else
  bad "构建产物校验失败：镜像内容不完整，请检查 .dockerignore 与构建上下文"; stage_cost; exit 1
fi
stage_cost

# ---------------- 阶段 3: test ----------------
stage_begin "test (容器内单元测试)"
if $DOCKER run --rm "$IMAGE:$TAG" python3 -m unittest discover -s tests -p "test_*.py" -v >> "$RUNLOG" 2>&1; then
  ok "单元测试全部通过"
else
  bad "单元测试失败，详见 $RUNLOG"; stage_cost; exit 1
fi
stage_cost

# ---------------- 阶段 4: scan ----------------
stage_begin "scan (安全与质量扫描)"
if python3 "$REPO/ci/scan.py" --image "$IMAGE:$TAG" 2>&1 | tee -a "$RUNLOG"; then
  ok "扫描门禁通过"
else
  bad "扫描门禁拦截，流水线终止"; stage_cost; exit 1
fi
stage_cost

say "${GREEN}========== CI 流水线全部通过 ==========${NC}"
say "产物: $IMAGE:$TAG | 日志: $RUNLOG"
printf '{"commit":"%s","image":"%s","time":"%s"}\n' "$(git rev-parse --short HEAD)" "$IMAGE:$TAG" "$(date +%Y-%m-%d\ %H:%M:%S)" > "$LOGDIR/last_build.json"
exit 0
