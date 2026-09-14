#!/usr/bin/env bash
# 启用"容器接管 prod"（**当前未启用，需要时手工执行本脚本**）
#
# 顺序（重点：先把数据搬进容器卷，再切流量，避免切过去发现是空库）：
#   1. 备份数据库
#   2. 停 systemd 的 api/worker（让 DB 落定，此期间站点短暂不可用）
#   3. 把 app.db + uploads 复制进容器卷
#   4. 起容器 → 三个实例过健康门禁
#   5. 切 nginx upstream 到容器端口
#   6. 禁用 systemd 旧服务
# 任一步失败都会把 systemd 起回来，nginx 保持指向原处。
set -u
set -o pipefail

REPO=/home/ubuntu/screenshot-api
NGINX_SITE=/etc/nginx/sites-available/screenshot-https
BACKUP_DIR=/home/ubuntu/screenshot-api/cd/prod-container/backup
VOL=screenshot-api_prod-data
IMAGE_TAG=${IMAGE_TAG:-screenshot-api:latest}
DOCKER=docker
if ! docker info >/dev/null 2>&1; then DOCKER="sudo -n docker"; fi

say() { printf "%b\n" "$(date +%H:%M:%S) $1"; }
ok()  { say "\033[32m[PASS]\033[0m $1"; }
bad() { say "\033[31m[FAIL]\033[0m $1"; }
restore_systemd() {
  say "回退：重新拉起 systemd 服务"
  sudo systemctl start screenshot-api@8001 screenshot-api@8002 screenshot-api@8003 \
                       screenshot-worker@1 screenshot-worker@2 2>/dev/null
  sudo systemctl start screenshot-healthd 2>/dev/null
}

cd "$REPO" || exit 1
mkdir -p "$BACKUP_DIR"

say "1/6 备份数据库 -> $BACKUP_DIR"
cp -f app.db app.db-wal app.db-shm "$BACKUP_DIR"/ 2>/dev/null
cp -f app.db "$BACKUP_DIR/app.db.$(date +%Y%m%d%H%M%S)"
ok "已备份"

say "2/6 停 systemd 的 api/worker（DB 落定，站点短暂不可用）"
sudo systemctl stop screenshot-api@8001 screenshot-api@8002 screenshot-api@8003 \
                    screenshot-worker@1 screenshot-worker@2

say "3/6 把数据搬到共享目录 data/（容器和 systemd 共用同一份，避免数据分叉）"
mkdir -p data/uploads
cp -f app.db data/app.db
cp -f app.db-wal app.db-shm data/ 2>/dev/null || true
cp -rf uploads/. data/uploads/ 2>/dev/null || true
# 给 systemd 的服务也加上 DB_PATH/UPLOAD_DIR，指向同一份数据（回退时两边一致）
sudo install -d /etc/systemd/system/screenshot-api@.service.d /etc/systemd/system/screenshot-worker@.service.d
printf '[Service]\nEnvironment=DB_PATH=%s/data/app.db\nEnvironment=UPLOAD_DIR=%s/data/uploads\n' "$REPO" "$REPO" | sudo tee /etc/systemd/system/screenshot-api@.service.d/data.conf > /dev/null
printf '[Service]\nEnvironment=DB_PATH=%s/data/app.db\nEnvironment=UPLOAD_DIR=%s/data/uploads\n' "$REPO" "$REPO" | sudo tee /etc/systemd/system/screenshot-worker@.service.d/data.conf > /dev/null
printf '[Service]\nEnvironment=DB_PATH=%s/data/app.db\n' "$REPO" | sudo tee /etc/systemd/system/screenshot-healthd.service.d/data.conf > /dev/null 2>&1 || (sudo install -d /etc/systemd/system/screenshot-healthd.service.d && printf '[Service]\nEnvironment=DB_PATH=%s/data/app.db\n' "$REPO" | sudo tee /etc/systemd/system/screenshot-healthd.service.d/data.conf > /dev/null)
sudo systemctl daemon-reload
ok "数据已就位 $REPO/data"

say "4/6 起容器并过健康门禁（9111/9112/9113）"
if ! IMAGE_TAG="$IMAGE_TAG" $DOCKER compose -f docker-compose.prod.yml up -d; then
  bad "容器启动失败"; restore_systemd; exit 1
fi
for p in 9111 9112 9113; do
  if ! python3 cd/health_gate.py --url "http://127.0.0.1:$p/api/health" --wait 6 --times 2 --interval 3; then
    bad "$p 未通过门禁，撤掉容器"
    IMAGE_TAG="$IMAGE_TAG" $DOCKER compose -f docker-compose.prod.yml down
    restore_systemd
    exit 1
  fi
done
ok "三个实例都健康"

say "5/6 切 nginx upstream 8001-8003 -> 9111-9113"
sudo cp "$NGINX_SITE" "$NGINX_SITE.bak.$(date +%Y%m%d%H%M%S)"
sudo python3 - "$NGINX_SITE" << 'PY'
import sys
path = sys.argv[1]
s = open(path, encoding="utf-8").read()
for old, new in (("8001", "9111"), ("8002", "9112"), ("8003", "9113")):
    s = s.replace("127.0.0.1:%s" % old, "127.0.0.1:%s" % new)
open(path, "w", encoding="utf-8").write(s)
PY
if ! sudo nginx -t; then
  bad "nginx 配置校验失败，恢复备份"; sudo cp "$(ls -t $NGINX_SITE.bak.* | head -1)" "$NGINX_SITE"
  IMAGE_TAG="$IMAGE_TAG" $DOCKER compose -f docker-compose.prod.yml down
  restore_systemd
  exit 1
fi
sudo systemctl reload nginx
if curl -sk https://127.0.0.1:8443/api/health | grep -q '"ok": *true'; then
  ok "对外 HTTPS 正常"
else
  bad "对外验证失败，回退 nginx"
  sudo cp "$(ls -t $NGINX_SITE.bak.* | head -1)" "$NGINX_SITE"; sudo systemctl reload nginx
  IMAGE_TAG="$IMAGE_TAG" $DOCKER compose -f docker-compose.prod.yml down
  restore_systemd
  exit 1
fi

say "6/6 禁用 systemd 旧服务"
sudo systemctl disable screenshot-api@8001 screenshot-api@8002 screenshot-api@8003 \
                       screenshot-worker@1 screenshot-worker@2 screenshot-healthd
sudo systemctl stop screenshot-healthd
ok "容器已接管 prod。回退：bash cd/prod-container/disable.sh"
