# 账号、数据库与同步说明

## 当前实现

- SQLite 是错题、复习计划、复习历史和同步版本的权威数据源。
- 密码使用 PBKDF2-HMAC-SHA256 加盐保存，不保存明文密码。
- 注册必须填写格式有效且未被使用的邮箱；登录标识可使用账号名或邮箱。已有账号升级后邮箱默认为空，不影响继续使用账号名登录。
- 管理员在设置中启用 SMTP 邮件服务后，注册需要邮箱验证码（详见下文「注册邮箱验证码」）；未启用时注册不需要验证码，各客户端行为不变。
- 登录会话使用随机令牌和 HttpOnly、SameSite=Lax Cookie，有效期 30 天。
- 登录和注册请求可传 `"remember_me": false` 关闭自动登录：此时下发不带 `Max-Age` 的会话 Cookie，关闭浏览器即退出；默认或不传该字段视为开启自动登录（持久 Cookie）。
- 滑动续期：每次打开页面调用 `GET /api/auth/me` 时，若持久会话剩余有效期不足一半（少于 15 天），自动延长至完整 30 天并刷新 Cookie，活跃用户不会被登出；非持久会话不续期。
- 小程序不受影响：`client_type: "mini_program"` 返回的 `session_token` 仍为 30 天有效期，也可通过 `Authorization: Bearer` 调用 `/api/auth/me` 获得同样的滑动续期。
- 每条错题都属于一个用户；所有读写接口都进行用户归属校验。
- 第一个注册账号会自动导入 `错题库/` 中现有 Markdown；之后注册的账号从空错题库开始。
- Markdown 仍会作为本地可迁移备份保留，但网页不再把本地文件内容直接嵌入未登录页面。

默认数据库位于 `data/sql_review.db`，可通过环境变量 `SQL_WRONGBOOK_DB` 指定持久磁盘路径。

## 复习调度

评分分为四档：

- `0` 忘记：次日再次复习，重置连续掌握次数并增加遗忘次数。
- `1` 模糊：保持短间隔，降低易度。
- `2` 掌握：按 1 天、3 天和当前易度逐步延长。
- `3` 轻松：从 4 天起采用更长间隔，并提高易度。

每次评分都会同时写入 `review_events` 历史表，并更新错题的 `next_review_at`、`interval_days`、`ease`、`repetitions`、`lapses` 和 `status`。

## 同步 API

所有同步接口都需要登录会话。

网页使用 HttpOnly Cookie。注册请求需要同时提交 `username`、`password` 和 `email`；服务端启用邮件验证后还要提交 `email_code`。小程序登录时可在 `username` 字段提交账号名或邮箱，并增加 `"client_type": "mini_program"`；响应会返回 `session_token`，之后通过 `Authorization: Bearer <session_token>` 调用同步接口。

## 注册邮箱验证码

验证码流程只服务于注册，登录和同步协议不受影响。是否需要验证码由服务端配置决定：

- `GET /api/auth/register_config`（公开）：返回 `{"email_code_required": bool, "code_ttl_minutes": 10, "resend_interval_seconds": 60}`。客户端应在展示注册表单时读取一次，`email_code_required=false` 时不显示验证码输入框。
- `POST /api/auth/send_code`（公开）：`{"email": "name@example.com"}`，向该邮箱发送 6 位验证码。验证码 10 分钟内有效，同一邮箱 60 秒内只能重发一次，每小时最多发送 5 次；验证码校验连续错误 5 次后作废，必须重新获取。服务端只保存验证码哈希。已注册的邮箱会返回 400 `EMAIL_TAKEN`；发送成功返回 `{"ok": true, "expires_at": ...}`。
- `POST /api/auth/register`：启用验证码时 `email_code` 必填，校验通过才能创建账号。

邮件服务通过 `config/email_config.json` 配置（参考 `config/email_config.example.json`），管理员也可在网页设置弹窗中配置并「发送测试邮件」。文件未配置或 `enabled=false` 时自动降级为不需要验证码。

### 拉取增量

`GET /api/sync?since=<cursor>&limit=500`

返回：

- `cursor`：客户端下次拉取时保存的游标。
- `changes`：错题和复习事件的增量变化。
- `has_more`：是否需要继续分页拉取。
- `server_time`：服务端时间。

### 推送离线变化

`POST /api/sync/push`

```json
{
  "records": [
    {
      "id": "question-uuid",
      "base_version": 3,
      "operation": "upsert",
      "record": {"title": "...", "body_md": "..."}
    },
    {
      "entity_type": "review",
      "id": "device-generated-event-id",
      "record": {"question_id": "question-uuid", "rating": 2, "device_id": "mini-program"}
    }
  ]
}
```

错题更新采用版本号冲突检测：`base_version` 落后时返回服务器版本，客户端不得静默覆盖。复习事件以客户端事件 ID 幂等写入，重复推送不会重复安排复习。

### 批量导入 Markdown

`POST /api/import_batch`（需登录）：`{"files": [{"name": "q1.md", "content": "..."}]}`，单次最多 200 个文件、单个不超过 1MB。每个文件可含 frontmatter（题号/标题/知识点等，同 `模板.md`），也可只有纯正文。以「用户 + 文件名 + 内容哈希」生成固定 ID，重复导入相同文件返回 `skipped`，不会产生重复错题；响应包含 `imported` / `skipped` / `failed` 明细。错题只写入当前账号的数据库，不改写服务器上的 Markdown 文件。

注册引导：`GET /api/auth/register_config` 同时返回 `will_import_local`，仅在「站点还没有任何账号且 `错题库/` 下有 Markdown」时为 true——只有这种情形注册按钮才承诺自动导入；其余新用户注册后在「我的」页面手动批量导入或用 AI 识别图片导入。

## 部署边界

本地默认只监听 `127.0.0.1`。部署到云服务器时可设置：

- `HOST=0.0.0.0`
- `PORT=<平台分配端口>`
- `SQL_WRONGBOOK_DB=<持久磁盘上的数据库路径>`

公网部署必须放在 HTTPS 反向代理之后，并为数据库目录配置定期备份。AI Key 由首个管理员账号统一配置，普通账号不能修改站点级模型配置。

当前同步覆盖错题正文、结构化字段、调度状态和复习历史。原始截图仍保存在服务器文件系统；正式多实例部署时应迁移到对象存储，再给小程序提供鉴权下载地址。
