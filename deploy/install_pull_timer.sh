#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/sql-wrongbook/app"
STATE_DIR="/opt/sql-wrongbook/deploy-state"

if [[ ! -f deploy/pull_update.py ]]; then
  echo "请在项目解压目录运行 deploy/install_pull_timer.sh" >&2
  exit 1
fi

sudo install -d -m 0755 "$APP_DIR/deploy"
sudo install -d -m 0750 "$STATE_DIR"
sudo install -m 0755 deploy/pull_update.py "$APP_DIR/deploy/pull_update.py"

sudo tee /etc/systemd/system/sql-wrongbook-pull.service >/dev/null <<'EOF'
[Unit]
Description=Pull and deploy a tested SQL Wrongbook release from GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=/opt/sql-wrongbook/app
Environment=SQL_WRONGBOOK_GITHUB_REPOSITORY=wsq77477747/mistakebook
Environment=SQL_WRONGBOOK_GITHUB_BRANCH=main
Environment=SQL_WRONGBOOK_CI_WORKFLOW_PATH=.github/workflows/deploy.yml
Environment=SQL_WRONGBOOK_DEPLOY_STATE=/opt/sql-wrongbook/deploy-state/last_success_sha
ExecStart=/usr/bin/python3 /opt/sql-wrongbook/app/deploy/pull_update.py
TimeoutStartSec=10min
Nice=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sql-wrongbook-pull.timer >/dev/null <<'EOF'
[Unit]
Description=Check GitHub for tested SQL Wrongbook updates every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=true
Unit=sql-wrongbook-pull.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sql-wrongbook-pull.timer

echo "PULL_TIMER_OK"
systemctl list-timers --all sql-wrongbook-pull.timer --no-pager
echo "手动检查：sudo systemctl start sql-wrongbook-pull.service"
echo "查看日志：sudo journalctl -u sql-wrongbook-pull.service -n 100 --no-pager"
