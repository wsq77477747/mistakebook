# -*- coding: utf-8 -*-
"""
mailer.py —— 注册邮箱验证码发信（Python 标准库，零依赖）
=========================================================
配置文件：config/email_config.json（不存在或 enabled=false 视为未启用，
此时注册流程自动降级为不需要验证码，保持既有体验）。

配置示例（QQ 邮箱）：
{
  "enabled": true,
  "host": "smtp.qq.com",
  "port": 465,
  "use_ssl": true,
  "use_starttls": false,
  "username": "you@qq.com",
  "password": "SMTP 授权码（不是邮箱登录密码）",
  "sender": "you@qq.com",
  "sender_name": "错题本"
}
"""
import json
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
CONFIG_FILE = os.path.join(ROOT, "config", "email_config.json")

SENDER_NAME_FALLBACK = "错题本"


def load_email_config():
    """读取并校验邮件配置；未配置/未启用/字段不全时返回 None。"""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not cfg.get("enabled"):
        return None
    host = str(cfg.get("host") or "").strip()
    username = str(cfg.get("username") or "").strip()
    password = str(cfg.get("password") or "").strip()
    sender = str(cfg.get("sender") or "").strip() or username
    if not (host and username and password and sender):
        return None
    try:
        port = int(cfg.get("port") or 465)
    except (TypeError, ValueError):
        port = 465
    return {
        "host": host,
        "port": port,
        "use_ssl": bool(cfg.get("use_ssl", True)),
        "use_starttls": bool(cfg.get("use_starttls", False)),
        "username": username,
        "password": password,
        "sender": sender,
        "sender_name": str(cfg.get("sender_name") or SENDER_NAME_FALLBACK),
    }


def smtp_configured():
    return load_email_config() is not None


def register_code_required():
    """注册是否需要邮箱验证码：仅当 SMTP 已配置并启用时为真。"""
    return smtp_configured()


def _connect(cfg):
    if cfg["use_ssl"]:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
    try:
        if not cfg["use_ssl"] and cfg["use_starttls"]:
            server.starttls(context=ssl.create_default_context())
        server.login(cfg["username"], cfg["password"])
    except Exception:
        try:
            server.quit()
        except Exception:
            pass
        raise
    return server


def _send_mail(cfg, to, subject, html):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg["sender_name"], cfg["sender"]))
    msg["To"] = to
    with _connect(cfg) as server:
        server.sendmail(cfg["sender"], [to], msg.as_string())


_CODE_MAIL_TMPL = """<div style="max-width:480px;margin:0 auto;font-family:'Microsoft YaHei',Arial,sans-serif;
color:#222;border:1px solid #eee;border-radius:12px;padding:28px 32px;">
  <h2 style="margin:0 0 4px;font-size:20px;">📚 错题本</h2>
  <p style="margin:0 0 18px;color:#888;font-size:13px;">你正在注册错题本账号，验证码如下：</p>
  <div style="font-size:32px;font-weight:700;letter-spacing:8px;background:#f5f6fa;
border-radius:10px;padding:16px 0;text-align:center;color:#1d4ed8;">{code}</div>
  <p style="margin:18px 0 4px;font-size:13px;color:#666;">
    验证码 {ttl} 分钟内有效，请勿泄露给他人。若非本人操作，请忽略本邮件。</p>
</div>"""


_PURPOSE_CODE_MAIL_TMPL = """<div style="max-width:480px;margin:0 auto;font-family:'Microsoft YaHei',Arial,sans-serif;
color:#222;border:1px solid #eee;border-radius:12px;padding:28px 32px;">
  <h2 style="margin:0 0 4px;font-size:20px;">错题本 · {purpose_label}</h2>
  <p style="margin:0 0 18px;color:#888;font-size:13px;">你正在进行“{purpose_label}”操作，验证码如下：</p>
  <div style="font-size:32px;font-weight:700;letter-spacing:8px;background:#f5f6fa;
border-radius:10px;padding:16px 0;text-align:center;color:#1d4ed8;">{code}</div>
  <p style="margin:18px 0 4px;font-size:13px;color:#666;">
    验证码 {ttl} 分钟内有效，请勿泄露给他人。若非本人操作，请忽略本邮件。</p>
</div>"""


def send_verification_code(to, code, ttl_minutes=10, purpose="register"):
    """发送指定用途的验证码邮件。未配置时抛 RuntimeError，SMTP 失败时向上抛出。"""
    cfg = load_email_config()
    if not cfg:
        raise RuntimeError("邮件服务未配置")
    labels = {"register": "注册账号", "login": "登录账号", "reset": "找回密码"}
    purpose_label = labels.get(str(purpose), "身份验证")
    html = _PURPOSE_CODE_MAIL_TMPL.format(
        code=str(code), ttl=ttl_minutes, purpose_label=purpose_label
    )
    _send_mail(cfg, to, "错题本%s验证码：%s" % (purpose_label, code), html)


def send_test_email(to, cfg=None):
    """发送测试邮件；cfg 传入时用临时配置（供保存前连通性测试，不落盘）。"""
    cfg = cfg or load_email_config()
    if not cfg:
        raise RuntimeError("邮件服务未配置")
    html = ('<div style="font-family:Arial,sans-serif;color:#222;"><h2>📧 邮件服务配置成功</h2>'
            '<p>这是一封来自错题本的测试邮件，收到即说明 SMTP 配置可用。</p></div>')
    _send_mail(cfg, to, "错题本邮件服务测试", html)
