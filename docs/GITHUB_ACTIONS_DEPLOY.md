# GitHub Actions 自动部署

本项目使用 GitHub Actions 完成以下流程：

1. 每次向 `main` 分支推送代码后，自动运行后端测试、前端语法检查和部署脚本检查。
2. 测试全部通过后，将 `scripts/`、`assets/` 和 `deploy/` 打包并上传到腾讯云服务器。
3. 服务器先备份当前版本，再更新应用并进行健康检查；更新失败时自动回滚。

部署不会覆盖服务器上的数据库、AI/SMTP 密钥或错题源文件。`data/`、`config/` 和 `错题库/` 不在自动上传范围内。

## 一、服务器前提

- 公网地址：`49.232.12.206`
- SSH 用户：`ubuntu`
- SSH 端口：`2222`
- 应用目录：`/opt/sql-wrongbook/app`
- systemd 服务：`sql-wrongbook.service`
- GitHub 托管运行器的出口 IP 会变化，因此腾讯云防火墙的 TCP 2222 需要允许全部 IPv4 地址访问。服务器必须继续保持仅密钥登录，并禁用 SSH 密码登录。
- `ubuntu` 用户执行 `sudo -n true` 必须成功，否则无人值守部署会停在 sudo 密码提示处。

## 二、创建一把专用部署密钥

不要复用个人 SSH 密钥，也不要把私钥发送到聊天、邮件或提交进 Git。

在本机 PowerShell 中运行：

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\sql_wrongbook_github_actions_ed25519" -C "github-actions-mistakebook"
```

出现口令提示时连续按两次 Enter，创建一把无口令、仅供 GitHub Actions 使用的密钥。然后复制公钥：

```powershell
Get-Content "$env:USERPROFILE\.ssh\sql_wrongbook_github_actions_ed25519.pub" | Set-Clipboard
```

在腾讯云网页终端登录服务器，依次运行：

```bash
install -d -m 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
read -r CI_PUBLIC_KEY
```

执行最后一条命令后，终端会等待输入。粘贴刚才复制的一整行公钥并按 Enter，然后运行：

```bash
grep -qxF "$CI_PUBLIC_KEY" ~/.ssh/authorized_keys || printf '%s\n' "$CI_PUBLIC_KEY" >> ~/.ssh/authorized_keys
unset CI_PUBLIC_KEY
sudo -n true && echo SUDO_OK
```

看到 `SUDO_OK` 后，在本机 PowerShell 验证新密钥：

```powershell
ssh -i "$env:USERPROFILE\.ssh\sql_wrongbook_github_actions_ed25519" -p 2222 -o IdentitiesOnly=yes ubuntu@49.232.12.206 "echo SSH_OK; sudo -n true && echo SUDO_OK"
```

应同时看到 `SSH_OK` 和 `SUDO_OK`。

## 三、固定服务器身份

在腾讯云网页终端运行：

```bash
sudo awk '{print "[49.232.12.206]:2222 " $1 " " $2}' /etc/ssh/ssh_host_ed25519_key.pub
```

保存输出的完整一行，格式类似：

```text
[49.232.12.206]:2222 ssh-ed25519 AAAAC3...
```

这会让 GitHub Actions 校验服务器身份，避免把代码和凭据发送给被冒充的服务器。以后如果重装服务器或重新生成 SSH 主机密钥，需要重新取得并更新该值。

## 四、配置 GitHub 仓库 Secrets

打开仓库 `wsq77477747/mistakebook`，依次进入：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

添加以下 5 个 Repository secrets：

| Secret 名称 | 值 |
| --- | --- |
| `SSH_HOST` | `49.232.12.206` |
| `SSH_USER` | `ubuntu` |
| `SSH_PORT` | `2222` |
| `SSH_PRIVATE_KEY` | 专用部署私钥的完整内容 |
| `SSH_KNOWN_HOSTS` | 上一步得到的 `[49.232.12.206]:2222 ssh-ed25519 ...` 完整一行 |

在本机 PowerShell 中复制私钥：

```powershell
Get-Content -Raw "$env:USERPROFILE\.ssh\sql_wrongbook_github_actions_ed25519" | Set-Clipboard
```

把剪贴板内容直接粘贴到 `SSH_PRIVATE_KEY`，必须包括开头和结尾：

```text
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
```

不要创建同名的普通 Variables；工作流读取的是 Repository secrets。

## 五、提交并首次启用

自动部署工作流位于 `.github/workflows/deploy.yml`。它在代码进入 `main` 时启用，所以先检查本次准备提交的文件：

```powershell
cd "C:\Users\Administrator\Documents\AI+DA项目\SQL错题整理"
git status --short
git add -A
git status --short
git commit -m "新增邮箱验证码并启用自动部署"
git push origin main
```

提交前重点确认没有 `config/ai_config.json`、`config/email_config.json`、数据库文件或私钥。它们已经由 `.gitignore` 排除，但仍应人工检查一次。

推送后进入 GitHub 仓库的 `Actions` 页面，打开 `Test and Deploy`：

- `Test` 先运行；
- 测试通过后运行 `Deploy production`；
- 部署日志末尾出现 `UPDATE_OK` 即为成功。

也可以进入该工作流，点击 `Run workflow` 手动重新部署当前 `main`。

## 六、以后如何更新

以后不需要手动上传 ZIP。正常流程是：

```powershell
git add -A
git commit -m "说明本次更新"
git push origin main
```

GitHub Actions 会自动完成测试、上传和部署。Pull Request 只运行测试，不会部署生产服务器。

服务器旧版本保存在 `/opt/sql-wrongbook/releases/<UTC时间>`。若健康检查失败，脚本会自动恢复刚才备份的 `scripts/`、`assets/` 和 `deploy/`。

## 七、常见错误

- `Missing repository secret`：对应的 Secret 未创建、名称拼写错误或值为空。
- `Host key verification failed`：`SSH_KNOWN_HOSTS` 不是服务器输出的完整一行，或服务器主机密钥已变化。
- `Permission denied (publickey)`：GitHub 中的私钥与服务器 `authorized_keys` 中的公钥不配对。
- `sudo: a password is required`：先在服务器修复 `ubuntu` 用户的非交互 sudo 权限，再重新运行工作流。
- `Connection timed out`：检查腾讯云防火墙和 Ubuntu 防火墙是否允许 TCP 2222。
- 健康检查失败：工作流会自动回滚，并在日志中打印 `sql-wrongbook.service` 最近的服务日志。

首次自动部署成功后，如果腾讯云防火墙中仍保留名为“Codex临时部署”的临时规则，可以删除它；保留正式的 TCP 2222 规则。80/443 继续用于网站访问。
