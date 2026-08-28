# 项目环境约束（给所有协作 Agent）

## 运行环境
- Windows + PowerShell，`python` 可用（默认解释器）
- 项目根：`C:\Users\Administrator\Documents\AI+DA项目\SQL错题整理`

## 铁律：禁止 `python -c` 内联执行
PowerShell 对中文引号、反斜杠、三引号 `'''`、`\"` 的转义会反复破坏代码，导致 SyntaxError。
**任何非平凡 Python 代码（含中文 / 引号 / 反斜杠 / 三引号）一律先用 Write 工具写成 `.py` 文件，再 `python xxx.py` 执行。**
执行完及时删除临时脚本（命名 `_xxx.py`）。

## 其他 PowerShell 坑
- `curl` 是 `Invoke-WebRequest` 的别名，下载文件必须用 `curl.exe -L -o <file> <url>`。
- 路径含中文时用双引号包裹。
- 长命令超过前台等待会自动转后台，用 `TaskOutput` 取回结果。

## 项目启动
- 双击 `打开错题本.bat`：重建索引 → 启动本地服务（127.0.0.1:8765）→ 打开页面。
- 服务脚本：`scripts/server.py`（Python 零依赖，标准库 http.server）。
- 索引重建：`python scripts/rebuild_index.py`。
- 停止服务：`停止服务.bat` 或杀掉 8765 端口进程。

## 关键配置
- AI 配置：`config/ai_config.json`（base_url / api_key / model / presets）。
- 当前默认：阿里云百炼 `qwen-vl-plus`（视觉+文本全能，已验证可用）。
- 保存配置前必须通过 `/api/test` 连通性测试（前端已内置门禁）。
