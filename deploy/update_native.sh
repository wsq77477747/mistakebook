#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/sql-wrongbook/app"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE_DIR="/opt/sql-wrongbook/releases/${RELEASE_ID}"

if [[ ! -f scripts/server.py || ! -f scripts/storage.py || ! -f scripts/mailer.py || ! -f assets/template.html ]]; then
  echo "请在更新包解压目录运行 deploy/update_native.sh" >&2
  exit 1
fi

/usr/bin/python3 -m compileall -q scripts
sudo systemctl start sql-wrongbook-backup.service || true

sudo install -d -m 0755 "$RELEASE_DIR"
sudo cp -a "$APP_DIR/scripts" "$APP_DIR/assets" "$APP_DIR/deploy" "$RELEASE_DIR/"

rollback() {
  echo "正在回滚旧版本……" >&2
  sudo cp -a "$RELEASE_DIR/scripts/." "$APP_DIR/scripts/"
  sudo cp -a "$RELEASE_DIR/assets/." "$APP_DIR/assets/"
  sudo cp -a "$RELEASE_DIR/deploy/." "$APP_DIR/deploy/"
  sudo chown -R ubuntu:ubuntu "$APP_DIR/scripts" "$APP_DIR/assets" "$APP_DIR/deploy"
  sudo systemctl restart sql-wrongbook.service
}

sudo systemctl stop sql-wrongbook.service
if ! sudo cp -a scripts/. "$APP_DIR/scripts/" \
  || ! sudo cp -a assets/. "$APP_DIR/assets/" \
  || ! sudo cp -a deploy/. "$APP_DIR/deploy/"; then
  rollback
  echo "更新文件失败，已回滚旧版本" >&2
  exit 1
fi
sudo chown -R ubuntu:ubuntu "$APP_DIR/scripts" "$APP_DIR/assets" "$APP_DIR/deploy"

if ! sudo systemctl start sql-wrongbook.service; then
  rollback
  echo "更新失败，已回滚旧版本" >&2
  exit 1
fi

for _ in {1..30}; do
  if curl -fsS http://127.0.0.1/api/auth/me >/dev/null 2>&1; then
    echo "UPDATE_OK"
    sudo systemctl --no-pager --full status sql-wrongbook.service | sed -n '1,12p'
    exit 0
  fi
  sleep 2
done

rollback
echo "更新后健康检查失败，已回滚旧版本" >&2
sudo journalctl -u sql-wrongbook.service -n 80 --no-pager >&2 || true
exit 1
