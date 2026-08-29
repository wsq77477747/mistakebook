#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/sql-wrongbook"
IMAGE="sql-wrongbook:local"
NETWORK="sql-wrongbook-net"

if [[ ! -f Dockerfile || ! -d scripts || ! -d assets ]]; then
  echo "请在解压后的项目目录运行 deploy/install.sh" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io curl
fi

sudo systemctl enable --now docker
sudo install -d -m 0750 \
  "$APP_DIR/data" \
  "$APP_DIR/config" \
  "$APP_DIR/questions" \
  "$APP_DIR/caddy_data" \
  "$APP_DIR/caddy_config"

if [[ -d "错题库" ]]; then
  sudo cp -a "错题库/." "$APP_DIR/questions/"
fi
sudo install -m 0644 deploy/Caddyfile "$APP_DIR/Caddyfile"

sudo docker build -t "$IMAGE" .
sudo docker network inspect "$NETWORK" >/dev/null 2>&1 || sudo docker network create "$NETWORK"
sudo docker rm -f sql-wrongbook-caddy sql-wrongbook-app >/dev/null 2>&1 || true

sudo docker run -d \
  --name sql-wrongbook-app \
  --restart unless-stopped \
  --network "$NETWORK" \
  -e HOST=0.0.0.0 \
  -e PORT=8765 \
  -e SQL_WRONGBOOK_DB=/app/data/sql_review.db \
  -v "$APP_DIR/data:/app/data" \
  -v "$APP_DIR/config:/app/config" \
  -v "$APP_DIR/questions:/app/错题库" \
  "$IMAGE"

sudo docker run -d \
  --name sql-wrongbook-caddy \
  --restart unless-stopped \
  --network "$NETWORK" \
  -p 80:80 \
  -p 443:443 \
  -v "$APP_DIR/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "$APP_DIR/caddy_data:/data" \
  -v "$APP_DIR/caddy_config:/config" \
  caddy:2-alpine

printf '%s\n' \
  '17 3 * * * root /usr/bin/docker exec sql-wrongbook-app python /app/deploy/backup_db.py >> /var/log/sql-wrongbook-backup.log 2>&1' \
  | sudo tee /etc/cron.d/sql-wrongbook-backup >/dev/null
sudo chmod 0644 /etc/cron.d/sql-wrongbook-backup

for _ in {1..30}; do
  if curl -fsS http://127.0.0.1/api/auth/me >/dev/null; then
    echo "DEPLOY_OK http://$(curl -fsS https://api.ipify.org || true)"
    sudo docker ps --filter name=sql-wrongbook --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    exit 0
  fi
  sleep 2
done

echo "部署后健康检查失败" >&2
sudo docker logs --tail 80 sql-wrongbook-app >&2 || true
sudo docker logs --tail 80 sql-wrongbook-caddy >&2 || true
exit 1
