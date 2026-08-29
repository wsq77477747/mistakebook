#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="/opt/sql-wrongbook"
APP_DIR="$BASE_DIR/app"
RUN_USER="ubuntu"

if [[ ! -f Dockerfile || ! -d scripts || ! -d assets ]]; then
  echo "请在解压后的项目目录运行 deploy/install_native.sh" >&2
  exit 1
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nginx curl python3

sudo install -d -m 0755 "$APP_DIR" "$BASE_DIR/data" "$BASE_DIR/config" "$BASE_DIR/questions"
sudo cp -a assets scripts deploy Dockerfile .dockerignore "$APP_DIR/"
if [[ -f config/ai_config.example.json ]]; then
  sudo install -m 0644 config/ai_config.example.json "$APP_DIR/ai_config.example.json"
fi
if [[ -d "错题库" ]]; then
  sudo cp -a "错题库/." "$BASE_DIR/questions/"
fi

for path in data config "错题库"; do
  if [[ -e "$APP_DIR/$path" || -L "$APP_DIR/$path" ]]; then
    sudo rm -rf -- "$APP_DIR/$path"
  fi
done
sudo ln -s "$BASE_DIR/data" "$APP_DIR/data"
sudo ln -s "$BASE_DIR/config" "$APP_DIR/config"
sudo ln -s "$BASE_DIR/questions" "$APP_DIR/错题库"
sudo chown -R "$RUN_USER:$RUN_USER" "$BASE_DIR"

sudo tee /etc/systemd/system/sql-wrongbook.service >/dev/null <<'EOF'
[Unit]
Description=SQL Wrongbook Web Application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/sql-wrongbook/app
Environment=HOST=127.0.0.1
Environment=PORT=8765
Environment=SQL_WRONGBOOK_DB=/opt/sql-wrongbook/data/sql_review.db
ExecStart=/usr/bin/python3 /opt/sql-wrongbook/app/scripts/server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/nginx/sites-available/sql-wrongbook >/dev/null <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    client_max_body_size 20m;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 190s;
    }
}
EOF

sudo ln -sfn /etc/nginx/sites-available/sql-wrongbook /etc/nginx/sites-enabled/sql-wrongbook
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t

sudo tee /etc/systemd/system/sql-wrongbook-backup.service >/dev/null <<'EOF'
[Unit]
Description=Backup SQL Wrongbook SQLite database

[Service]
Type=oneshot
Environment=SQL_WRONGBOOK_DB=/opt/sql-wrongbook/data/sql_review.db
ExecStart=/usr/bin/python3 /opt/sql-wrongbook/app/deploy/backup_db.py
EOF

sudo tee /etc/systemd/system/sql-wrongbook-backup.timer >/dev/null <<'EOF'
[Unit]
Description=Daily SQL Wrongbook database backup

[Timer]
OnCalendar=*-*-* 03:17:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sql-wrongbook.service
sudo systemctl enable --now nginx
sudo systemctl restart nginx
sudo systemctl enable --now sql-wrongbook-backup.timer

for _ in {1..30}; do
  if curl -fsS http://127.0.0.1/api/auth/me >/dev/null; then
    echo "DEPLOY_OK"
    systemctl --no-pager --full status sql-wrongbook.service | sed -n '1,12p'
    systemctl --no-pager --full status nginx | sed -n '1,8p'
    exit 0
  fi
  sleep 2
done

echo "部署后健康检查失败" >&2
sudo journalctl -u sql-wrongbook.service -n 80 --no-pager >&2 || true
exit 1
