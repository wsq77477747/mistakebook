# GitHub 测试与服务器主动拉取部署

本项目不再要求 GitHub Actions 通过 SSH 进入生产服务器。部署流程为：

1. 代码推送到 `main` 后，GitHub Actions 自动运行后端测试、前端语法检查及部署脚本检查。
2. 腾讯云服务器每 5 分钟查询一次 `main` 的最新提交 SHA。
3. 只有该 SHA 对应的 `.github/workflows/deploy.yml` 工作流成功后，服务器才下载代码包。
4. 服务器再次运行后端测试，然后备份、更新、健康检查；失败时恢复更新前版本。

服务器只需要访问 GitHub 的出站 HTTPS 443，不需要开放入站 SSH。部署不会覆盖 `/opt/sql-wrongbook/data`、`/opt/sql-wrongbook/config` 或 `/opt/sql-wrongbook/questions`。

## 一、推荐频率

定时器采用：

```ini
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=true
```

服务器启动 2 分钟后检查，之后约每 5 分钟检查一次；随机延迟最多 30 秒。没有新提交时只请求一次 GitHub API 并立即退出。

## 二、启用 GitHub Actions

打开仓库 `wsq77477747/mistakebook`：

`Settings` → `Actions` → `General` → `Actions permissions`

允许仓库运行工作流并保存。推送后在 `Actions` 页面应能看到 `Test before Server Pull`。服务器不会部署没有成功测试记录的提交。

新版工作流不再使用以下 SSH Secrets；确认主动拉取运行正常后可将它们删除：

- `SSH_HOST`
- `SSH_USER`
- `SSH_PORT`
- `SSH_PRIVATE_KEY`
- `SSH_KNOWN_HOSTS`

## 三、提交主动拉取代码

在本机 PowerShell 中执行：

```powershell
cd "C:\Users\Administrator\Documents\AI+DA项目\SQL错题整理"
git status --short
git add -A
git status --short
git commit -m "改为服务器每5分钟主动拉取部署"
git push origin main
```

进入 GitHub `Actions` 页面，等待这次提交的 `Test before Server Pull` 变为绿色成功状态。

## 四、首次安装定时器

由于入站 SSH 当前不可用，使用腾讯云网页终端执行一次引导安装：

```bash
bootstrap_dir="$(mktemp -d /tmp/sql-wrongbook-bootstrap.XXXXXX)"
curl -fL --retry 3 \
  -o "$bootstrap_dir/mistakebook.tar.gz" \
  https://codeload.github.com/wsq77477747/mistakebook/tar.gz/refs/heads/main
tar -xzf "$bootstrap_dir/mistakebook.tar.gz" -C "$bootstrap_dir"
project_dir="$(find "$bootstrap_dir" -mindepth 1 -maxdepth 1 -type d -print -quit)"
cd "$project_dir"
bash deploy/install_pull_timer.sh
```

成功时会输出：

```text
PULL_TIMER_OK
```

安装脚本会把拉取程序放在 `/opt/sql-wrongbook/app/deploy/pull_update.py`，状态保存在 `/opt/sql-wrongbook/deploy-state/last_success_sha`。

## 五、首次手动检查

无需等待 5 分钟，可以立即触发：

```bash
sudo systemctl start sql-wrongbook-pull.service
sudo journalctl -u sql-wrongbook-pull.service -n 100 --no-pager
```

成功日志应依次出现：

```text
Latest main commit: <SHA>
Downloading the tested release archive
UPDATE_OK
PULL_UPDATE_OK: deployed <SHA>
```

如果显示 `WAITING_FOR_CI`，进入 GitHub `Actions` 页面确认相同 SHA 的工作流是否已完成并成功。

## 六、查看运行状态

查看下一次检查时间：

```bash
systemctl list-timers --all sql-wrongbook-pull.timer --no-pager
```

查看最近部署日志：

```bash
sudo journalctl -u sql-wrongbook-pull.service -n 100 --no-pager
```

查看已成功部署的提交：

```bash
sudo cat /opt/sql-wrongbook/deploy-state/last_success_sha
```

立即检查更新：

```bash
sudo systemctl start sql-wrongbook-pull.service
```

暂停和恢复自动检查：

```bash
sudo systemctl disable --now sql-wrongbook-pull.timer
sudo systemctl enable --now sql-wrongbook-pull.timer
```

## 七、日常更新

以后只需要：

```powershell
git add -A
git commit -m "说明本次更新"
git push origin main
```

GitHub 测试成功后，服务器通常会在 5 分钟内自动部署。Pull Request 只运行测试；只有进入 `main` 的提交才会被服务器检查。

## 八、错误处理

- `No CI run exists for this commit`：GitHub Actions 未启用或尚未为该提交创建工作流。
- `CI is still running`：等待下一次定时检查。
- `CI completed without success`：修复测试后重新提交，失败提交不会部署。
- GitHub API 或 codeload 访问失败：服务记录失败，5 分钟后自动重试，不影响当前网站。
- 下载后的后端测试失败：不会执行更新。
- 更新或健康检查失败：`deploy/update_native.sh` 自动恢复更新前的 `scripts/`、`assets/` 和 `deploy/`。

主动拉取确认正常后，可以删除腾讯云中为排查创建的 TCP 22、2222 和 22022 公网规则；网站继续保留 80，配置 HTTPS 后保留 443。
