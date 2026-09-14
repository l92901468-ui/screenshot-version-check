#!/usr/bin/env bash
# 从容器回退到 systemd（enable.sh 的反操作）
set -u
set -o pipefail
REPO=/home/ubuntu/screenshot-api
NGINX_SITE=/etc/nginx/sites-available/screenshot-https
BACKUP=$(ls -t $NGINX_SITE.bak.* 2>/dev/null | head -1)

cd "$REPO" || exit 1
echo "1/3 起回 systemd 服务"
sudo systemctl enable --now screenshot-api@8001 screenshot-api@8002 screenshot-api@8003 \
                            screenshot-worker@1 screenshot-worker@2 screenshot-healthd

echo "2/3 把 upstream 切回 8001-8003"
if [ -n "$BACKUP" ]; then
  sudo cp "$BACKUP" "$NGINX_SITE"
else
  sudo python3 - "$NGINX_SITE" << 'PY'
import sys
path = sys.argv[1]
s = open(path, encoding="utf-8").read()
for old, new in (("9111", "8001"), ("9112", "8002"), ("9113", "8003")):
    s = s.replace("127.0.0.1:%s" % old, "127.0.0.1:%s" % new)
open(path, "w", encoding="utf-8").write(s)
PY
fi
sudo nginx -t && sudo systemctl reload nginx

echo "3/3 停掉容器"
IMAGE_TAG=${IMAGE_TAG:-screenshot-api:latest} docker compose -f docker-compose.prod.yml down
echo "已回到 systemd 模式"
