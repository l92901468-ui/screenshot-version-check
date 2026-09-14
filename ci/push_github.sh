#!/usr/bin/env bash
# 把代码推到 GitHub 上的公开仓库 screenshot-version-check
#
# 用法：bash ci/push_github.sh <你的 GitHub 用户名>
#
# 前置条件：
#   1. 已在 GitHub → Settings → SSH and GPG keys 添加了本机公钥
#      （cat ~/.ssh/id_ed25519.pub）
#   2. 已在 GitHub 上建好空的公开仓库 screenshot-version-check（不要勾 README/LICENSE）
set -euo pipefail

USER_NAME="${1:-}"
REPO="screenshot-version-check"
REMOTE_URL="git@github.com:${USER_NAME}/${REPO}.git"

if [ -z "$USER_NAME" ]; then
    echo "用法: bash ci/push_github.sh <GitHub 用户名>"
    exit 1
fi

cd /home/ubuntu/screenshot-api

# 1. SSH 连通性自检，先失败在前面，别推到一半才报错
echo "==> 检查 GitHub SSH 连通性"
if ! ssh -o BatchMode=yes -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    echo "FAIL: GitHub SSH 未通过。请确认公钥已添加到 GitHub。"
    echo "      公钥内容："
    cat ~/.ssh/id_ed25519.pub
    exit 1
fi
echo "OK: SSH 鉴权通过"

# 2. 本地先跑一遍测试，别把红的代码推上去
echo "==> 本地单元测试"
python3 -m unittest discover -s tests -q 2>&1 | tail -3

# 3. 挂远程（已存在就换成正确的地址）
if git remote get-url github >/dev/null 2>&1; then
    git remote set-url github "$REMOTE_URL"
else
    git remote add github "$REMOTE_URL"
fi
echo "OK: remote github -> $REMOTE_URL"

# 4. 推
echo "==> 推送 main 分支"
git push -u github main

echo
echo "完成: https://github.com/${USER_NAME}/${REPO}"
