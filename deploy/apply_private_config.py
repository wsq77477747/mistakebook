#!/usr/bin/env python3
"""把私密配置（AI Key、SMTP 授权码）落到服务器的持久配置目录。

背景：config/ai_config.json 含密钥，不进 Git；服务器通过 GitHub 拉取部署，
归档里没有这份文件，导致线上 AI 接口 NOT_CONFIGURED。本脚本在服务器上
运行一次，从环境变量生成真实配置到持久目录（默认 /opt/sql-wrongbook/config，
该目录不随版本更新覆盖）。

用法（在服务器上，任意目录）：
  sudo AI_API_KEY=sk-xxx \
       AI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
       AI_MODEL=qwen-vl-max \
       python3 apply_private_config.py

可选的 SMTP 邮件配置（注册验证码用）：
  EMAIL_ENABLED=1 EMAIL_HOST=smtp.qq.com EMAIL_PORT=465 \
  EMAIL_USERNAME=you@qq.com EMAIL_PASSWORD=授权码 EMAIL_SENDER=you@qq.com

行为：
  - 只写环境变量里提供的字段，已有配置文件中的其余字段原样保留（幂等，可重复运行）；
  - 未提供任何相关环境变量时打印现状并退出，不做修改；
  - 文件权限 0600，属主与运行用户一致。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_CONFIG_DIR = "/opt/sql-wrongbook/config"
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

AI_FIELDS = {
    # 环境变量 -> 配置文件字段
    "AI_BASE_URL": "base_url",
    "AI_API_KEY": "api_key",
    "AI_MODEL": "model",
}
EMAIL_FIELDS = {
    "EMAIL_ENABLED": "enabled",
    "EMAIL_HOST": "host",
    "EMAIL_PORT": "port",
    "EMAIL_USERNAME": "username",
    "EMAIL_PASSWORD": "password",
    "EMAIL_SENDER": "sender",
    "EMAIL_SENDER_NAME": "sender_name",
    "EMAIL_USE_SSL": "use_ssl",
}
EMAIL_DEFAULTS = {
    "enabled": True,
    "port": 465,
    "use_ssl": True,
    "use_starttls": False,
    "sender_name": "SQL 错题本",
}
SECRET_FIELDS = {"api_key", "password"}


def config_dir() -> Path:
    return Path(os.environ.get("SQL_WRONGBOOK_CONFIG_DIR", DEFAULT_CONFIG_DIR))


def _parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce(field: str, raw: str):
    if field == "enabled":
        return _parse_bool(raw)
    if field in {"port", "use_ssl", "use_starttls"}:
        try:
            return int(raw) if field == "port" else _parse_bool(raw)
        except ValueError:
            return EMAIL_DEFAULTS[field]
    return str(raw).strip()


def collect_updates(env: dict[str, str] | None = None) -> tuple[dict, dict]:
    """从环境变量收集要写入的字段。返回 (ai_updates, email_updates)。

    邮件字段仅在 EMAIL_ENABLED 为真值时收集——这是「本次要配置邮件」的显式开关，
    防止只想更新 AI Key 时误触邮件配置。
    """
    env = os.environ if env is None else env
    ai = {}
    for env_name, field in AI_FIELDS.items():
        raw = env.get(env_name, "").strip()
        if raw:
            ai[field] = raw
    if ai and "base_url" not in ai:
        ai.setdefault("base_url", DEFAULT_BASE_URL)
    email = {}
    if _parse_bool(env.get("EMAIL_ENABLED", "")):
        # 缺省字段（enabled/port/use_ssl 等）在合并阶段用 EMAIL_DEFAULTS 补齐
        for env_name, field in EMAIL_FIELDS.items():
            raw = env.get(env_name, "").strip()
            if raw:
                email[field] = _coerce(field, raw)
    return ai, email


def merge_into(path: Path, updates: dict, defaults: dict | None = None) -> dict:
    """把 updates 合并进 JSON 文件（不存在则创建），返回合并后的完整配置。

    defaults 仅用于补齐文件与 updates 都没有的字段，不会覆盖已有值。
    """
    current = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            current = {}
    for key, value in (defaults or {}).items():
        if key not in current:
            current[key] = value
    current.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return current


def masked(config: dict) -> dict:
    return {
        key: (f"***{str(value)[-4:]}" if key in SECRET_FIELDS and value else value)
        for key, value in sorted(config.items())
    }


def main() -> int:
    ai_updates, email_updates = collect_updates()
    if not ai_updates and not email_updates:
        print("未检测到 AI_* / EMAIL_* 环境变量，未做任何修改。")
        print("用法示例：sudo AI_API_KEY=sk-xxx AI_BASE_URL=... AI_MODEL=qwen-vl-max python3 apply_private_config.py")
        return 0
    directory = config_dir()
    if ai_updates:
        target = directory / "ai_config.json"
        merged = merge_into(target, ai_updates)
        print(f"AI 配置已写入 {target}: {json.dumps(masked(merged), ensure_ascii=False)}")
    if email_updates:
        target = directory / "email_config.json"
        merged = merge_into(target, email_updates, defaults=EMAIL_DEFAULTS)
        print(f"邮件配置已写入 {target}: {json.dumps(masked(merged), ensure_ascii=False)}")
    print("提示：配置目录为持久目录，后续版本更新不会覆盖；如站点服务正在运行，"
          "无需重启——AI 配置每次请求都会重新读取。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
