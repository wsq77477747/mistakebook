# -*- coding: utf-8 -*-
"""
server.py —— SQL 错题本本地 AI 代理服务（Python 标准库，零依赖）
=================================================================
职责：
  1. 静态托管本目录（打开 http://127.0.0.1:8765/ 即错题本页面）
  2. GET/POST /api/config       读写 AI 配置（Base URL / API Key / 模型），
                                Key 保存在本机 ai_config.json，不放进网页
  3. POST /api/chat             把浏览器发来的对话转发给 OpenAI 兼容 LLM API
  4. POST /api/classify         用 LLM 把「原始错题内容」自动整理归类，返回结构化结果（不落盘，供预览）
  5. POST /api/save             把确认后的错题写入 错题库/<知识点>/ 并自动重建 index.html

启动：双击「打开错题本.bat」（自动启动本服务）
停止：双击「停止服务.bat」 或直接关闭本服务的控制台窗口
"""
import http.server
import json
import os
import re
import sys
import time
import base64
import atexit
import subprocess
import urllib.request
import urllib.error
from http import cookies
from urllib.parse import parse_qs, unquote, urlparse

import rebuild_index as indexer
import mailer
import storage

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)            # 项目根目录（本脚本位于根目录/scripts 下）
CONFIG_FILE = os.path.join(ROOT, "config", "ai_config.json")
QUIZ_DIR = os.path.join(ROOT, "错题库")
PID_FILE = os.path.join(ROOT, "server.pid")
REBUILD_SCRIPT = os.path.join(SCRIPT_DIR, "rebuild_index.py")
PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "127.0.0.1")
DEFAULT_BASE = "https://ark.cn-beijing.volces.com/api/v3"

CLS_SYSTEM_PROMPT = """你是 SQL 错题归档助手。用户会粘贴一道错题的原始信息（题目描述、他写的 SQL、报错信息、讲解等），也可能附带错题截图。
如果附带截图：请仔细识别图片中的题目描述、数据表、SQL 代码和报错内容，把它们原样转写到对应小节，不要遗漏表头和条件。
请把信息整理成结构化的归档结果。只输出一个 JSON 对象，不要输出任何其他文字或 markdown 代码块包裹。
字段要求：
- no: 题号，如 SQL9；无法确定就填 SQL???
- title: 一句话标题，简洁，如「查找除复旦大学的用户信息」
- cat: 知识点分类。必须【优先】从给定的候选分类里选最贴切的一个；只有所有候选都不合适时，才允许新起一个简短分类名（如 WHERE 条件过滤 / JOIN 连接 / 窗口函数）
- diff: 难度，只能是 简单 / 中等 / 困难 之一
- date: 做错日期，直接用给定的 today
- src: 题目来源，如 牛客 / LeetCode / 力扣 / 面试
- errtype: 错误类型，只能是 语法错误 / 逻辑错误 / 函数用法 / NULL陷阱 / 其他 之一
- status: 复习状态，只能是 未掌握 / 复习中 / 已掌握 之一，默认 未掌握
- times: 做错次数，默认 1
- redates: 历次做错日期数组，默认 [date]
- summary: 一句话错因，≤50 字，点出关键错误原因
- body_md: 用 Markdown 写正文，必须用 ## 分 5 个小节：
    ## 题目（题目描述，如有多行数据可写 Markdown 表格）
    ## 我的错误写法（```sql 代码块，附报错信息）
    ## 正确写法（```sql 代码块）
    ## 错因分析（要点式，讲清楚为什么错、怎么改）
    ## 知识点总结（可复用要点/易错点）

候选分类：{categories}
今天日期：{today}"""


# ---------- 通用工具 ----------
def _read_config_raw():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_config():
    """站点级 AI 配置：优先 config/ai_config.json，缺失字段回退环境变量
    （AI_BASE_URL / AI_API_KEY / AI_MODEL），便于服务器部署时通过
    systemd Environment 或 secrets 注入私密配置，而无需把 Key 提交进仓库。"""
    cfg = _read_config_raw()
    return {
        "base_url": str(cfg.get("base_url") or os.environ.get("AI_BASE_URL") or DEFAULT_BASE),
        "api_key": str(cfg.get("api_key") or os.environ.get("AI_API_KEY") or ""),
        "model": str(cfg.get("model") or os.environ.get("AI_MODEL") or ""),
    }


def load_presets():
    """读取已保存的配置选项（预设）"""
    cfg = _read_config_raw()
    return cfg.get("presets") or {}


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _safe_name(s, maxlen=60):
    """清理文件名/文件夹名中的非法字符"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "", str(s or "")).strip()
    return s[:maxlen] or "未命名"


def _extract_json(text):
    """从 LLM 输出中稳健提取 JSON 对象（容忍 ```json 围栏、多余文字、未转义换行、轻微截断）"""
    _ctrl_re = re.compile(r"[\x00-\x1f\x7f]")

    def _repair(span):
        # 仅把 JSON 字符串内部的未转义控制字符转义，不动结构
        out = []
        i, n = 0, len(span)
        in_str = False
        while i < n:
            ch = span[i]
            if in_str:
                if ch == "\\":
                    out.append(ch)
                    if i + 1 < n:
                        out.append(span[i + 1])
                        i += 2
                        continue
                elif ch == '"':
                    in_str = False
                    out.append(ch)
                    i += 1
                    continue
                elif _ctrl_re.match(ch):
                    out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t",
                                "\b": "\\b", "\f": "\\f"}.get(ch, "\\u%04x" % ord(ch)))
                    i += 1
                    continue
                else:
                    out.append(ch)
                    i += 1
                    continue
            else:
                if ch == '"':
                    in_str = True
                out.append(ch)
                i += 1
        return "".join(out)

    text = text.strip()
    # 仅当整体被围栏包裹时才去掉外层围栏，避免误匹配 JSON 字符串内部的 markdown 代码块
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    start = text.find("{")
    if start == -1:
        preview = text[:200].replace("\n", " ").replace("\r", " ")
        raise ValueError("LLM 输出中未找到 JSON 对象。原始输出前200字: " + preview)
    depth, in_str, esc = 0, False, False
    closed_at = -1
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    closed_at = i
                    break
    if closed_at >= 0:
        span = text[start:closed_at + 1]
    else:
        # 截断自修复：先闭合字符串，再补 } / ]
        span = text[start:]
        if in_str:
            span += '"'
        span += "}" * max(0, (span.count("{") - span.count("}")))
        span += "]" * max(0, (span.count("[") - span.count("]")))
    try:
        return json.loads(span)
    except Exception:
        fixed = _repair(span)
        try:
            return json.loads(fixed)
        except Exception as e:
            raise ValueError("LLM 返回的 JSON 无法解析（已尝试自动修复）: %s" % e)


def _unique_path(folder, filename):
    """若文件已存在，追加序号避免覆盖"""
    base, ext = os.path.splitext(filename)
    p = os.path.join(folder, filename)
    i = 2
    while os.path.exists(p):
        p = os.path.join(folder, f"{base}_{i}{ext}")
        i += 1
    return p


def _rebuild():
    """调用 rebuild_index.py 重建 index.html"""
    subprocess.run([sys.executable, REBUILD_SCRIPT], cwd=ROOT, timeout=120)


def _llm_endpoint(cfg):
    """兼容两种填法：只填 base（自动拼 /chat/completions）或填了完整端点"""
    base = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _decode_data_url(s):
    """解析 data URL，返回 (bytes, 扩展名)；失败返回 (None, None)"""
    m = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)", (s or "").strip())
    if not m:
        return None, None
    ext = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}.get(
        m.group(1).lower(), "png")
    try:
        return base64.b64decode(m.group(2)), ext
    except Exception:
        return None, None


def _call_llm(cfg, messages, temperature=0.3, retries=3, base_delay=2.0, max_tokens=None, json_mode=False):
    """转发对话给 OpenAI 兼容 LLM，返回 content 字符串。cfg 为实际使用的配置
    （站点默认或用户自有）。遇到 429（访问量过大/限流）或 5xx 时按退避自动重试，最多 retries 次。"""
    url = _llm_endpoint(cfg)
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"],
    }
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.loads(r.read().decode("utf-8"))
            return resp["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if json_mode and e.code == 400 and attempt == 0:
                # 服务端不支持 response_format 时去掉重试
                payload.pop("response_format", None)
                json_mode = False
                time.sleep(1)
                continue
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue
            raise
    raise last_err


class Handler(http.server.SimpleHTTPRequestHandler):
    PRIVATE_PREFIXES = ("/config", "/data", "/scripts", "/tests", "/错题库", "/.git")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def send_head(self):
        request_path = unquote(urlparse(self.path).path).replace("\\", "/")
        if any(request_path == prefix or request_path.startswith(prefix + "/")
               for prefix in self.PRIVATE_PREFIXES):
            self.send_error(404, "Not found")
            return None
        return super().send_head()

    # ---- 基础工具 ----
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _session_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return ""
        morsel = jar.get("sqlwb_session")
        return morsel.value if morsel else ""

    def _current_user(self):
        return storage.user_for_session(self._session_token())

    def _require_user(self):
        user = self._current_user()
        if not user:
            self._send_json(401, {"error": "AUTH_REQUIRED", "message": "请先登录。"})
            return None
        self.user = user
        return user

    def _set_session_cookie(self, token, remember=True):
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        if remember:
            # 持久 Cookie：30 天内再次访问自动登录
            value = "sqlwb_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (
                token, storage.SESSION_DAYS * 86400)
        else:
            # 会话 Cookie：不带 Max-Age，关闭浏览器即退出
            value = "sqlwb_session=%s; Path=/; HttpOnly; SameSite=Lax" % token
        if secure:
            value += "; Secure"
        self.send_header("Set-Cookie", value)

    def _clear_session_cookie(self):
        self.send_header("Set-Cookie", "sqlwb_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def _send_auth(self, code, obj, token=None, clear=False, remember=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token:
            self._set_session_cookie(token, remember=remember)
        if clear:
            self._clear_session_cookie()
        self.end_headers()
        self.wfile.write(body)

    def _question_payload(self, question):
        item = dict(question)
        sections = indexer.split_sections(item.get("body_md") or "")
        prompt_parts, answer_parts, all_parts = [], [], []
        for title, content in sections:
            block = "<h4>%s</h4>%s" % (indexer.html_mod.escape(title), indexer.render_block(content))
            all_parts.append(block)
            if title.strip() == "题目":
                prompt_parts.append(block)
            else:
                answer_parts.append(block)
        item["prompt_html"] = "".join(prompt_parts) or "<p>暂无题目描述。</p>"
        item["answer_html"] = "".join(answer_parts) or "<p>暂无解析。</p>"
        item["body_html"] = "".join(all_parts)
        return item

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _track_event(self, event_type, page="", metadata=None):
        try:
            user = getattr(self, "user", None) or self._current_user()
            user_id = user["id"] if user else None
            device_id = self.headers.get("X-Device-Id", "")[:80]
            ip = self.headers.get("X-Forwarded-For", self.client_address[0] if self.client_address else "")
            if "," in ip:
                ip = ip.split(",")[0].strip()
            ua = self.headers.get("User-Agent", "")[:300]
            storage.record_event(user_id=user_id, device_id=device_id, event_type=event_type,
                                 page=page, metadata=metadata, ip=ip, user_agent=ua)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        sys.stdout.write("[server] " + fmt % args + "\n")

    # ---- AI 配置与每日免费额度 ----
    def _effective_ai_config(self):
        """返回 (cfg, own)：用户自有配置优先；否则使用站点默认（管理员提供）。"""
        own = storage.get_user_ai_config(self.user["id"])
        if own:
            return own, True
        return load_config(), False

    def _reject_over_quota(self, own_cfg):
        """使用站点默认模型的普通用户受每日免费额度限制（管理员与自有 Key 不受限）。

        返回 True 表示已向客户端发送 429 响应，调用方应立即 return。
        """
        if own_cfg or self.user.get("is_admin"):
            return False
        used = storage.count_ai_calls_today(self.user["id"])
        total = storage.daily_ai_quota(self.user["id"])
        if used >= total:
            self._send_json(429, {
                "error": "AI_QUOTA_EXCEEDED",
                "message": "今日免费次数已用完（%d/%d 次）。可在「我的」页配置自己的 AI Key 解除限制，"
                           "或邀请新用户注册（每位 +10 次/日）。" % (used, total),
            })
            return True
        return False

    def _ai_quota(self):
        uid = self.user["id"]
        summary = storage.invite_summary(uid)
        total = storage.daily_ai_quota(uid)
        used = storage.count_ai_calls_today(uid)
        return self._send_json(200, {
            "base_free": storage.FREE_AI_CALLS_PER_DAY,
            "invite_bonus": summary["invite_bonus"],
            "daily_total": total,
            "used_today": used,
            "remaining": max(0, total - used),
            "invite_code": summary["invite_code"],
            "using_own_config": storage.get_user_ai_config(uid) is not None,
            "site_model": load_config().get("model") or "",
            "is_admin": bool(self.user.get("is_admin")),
        })

    def _my_ai_config_view(self):
        cfg = storage.get_user_ai_config(self.user["id"])
        if not cfg:
            return {"configured": False, "base_url": "", "model": "", "api_key_tail": ""}
        return {"configured": True, "base_url": cfg["base_url"], "model": cfg["model"],
                "api_key_tail": cfg["api_key"][-4:]}

    def _save_my_ai_config(self):
        body = self._read_body()
        if body.get("action") == "clear":
            storage.save_user_ai_config(self.user["id"], None)
            return self._send_json(200, {"ok": True, **self._my_ai_config_view()})
        cfg = {
            "base_url": (body.get("base_url") or DEFAULT_BASE).strip(),
            "api_key": (body.get("api_key") or "").strip(),
            "model": (body.get("model") or "").strip(),
        }
        if not cfg["api_key"]:  # Key 留空沿用已保存的
            existing = storage.get_user_ai_config(self.user["id"])
            if existing:
                cfg["api_key"] = existing["api_key"]
        if not cfg["api_key"] or not cfg["model"]:
            return self._send_json(400, {"error": "INCOMPLETE",
                                         "message": "请填写 API Key 和模型名（Key 留空则沿用已保存的）。"})
        storage.save_user_ai_config(self.user["id"], cfg)
        return self._send_json(200, {"ok": True, **self._my_ai_config_view()})

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._track_event("page_view", page="/")
        if path == "/api/auth/me":
            token = self._session_token()
            user = storage.user_for_session(token)
            if user and storage.refresh_session(token):
                # 活跃用户的持久会话已自动续期，同步刷新 Cookie
                return self._send_auth(200, {"authenticated": True, "user": user}, token=token)
            return self._send_json(200, {"authenticated": bool(user), "user": user})
        if path == "/api/auth/register_config":
            return self._send_json(200, {
                "email_code_required": mailer.register_code_required(),
                "email_code_login_available": mailer.smtp_configured(),
                "will_import_local": storage.legacy_questions_available(),
                "code_ttl_minutes": storage.EMAIL_CODE_TTL_MINUTES,
                "resend_interval_seconds": storage.EMAIL_CODE_RESEND_SECONDS,
            })
        if path.startswith("/api/") and not self._require_user():
            return
        if path == "/api/questions":
            return self._send_json(200, {"questions": [self._question_payload(q) for q in storage.list_questions(self.user["id"])]})
        if path == "/api/review/today":
            args = parse_qs(urlparse(self.path).query)
            due, stats = storage.due_questions(self.user["id"], args.get("limit", [20])[0])
            return self._send_json(200, {"questions": [self._question_payload(q) for q in due], "stats": stats})
        if path == "/api/review/history":
            args = parse_qs(urlparse(self.path).query)
            return self._send_json(200, {"reviews": storage.review_history(self.user["id"], args.get("limit", [100])[0])})
        if path == "/api/sync":
            args = parse_qs(urlparse(self.path).query)
            return self._send_json(200, storage.sync_pull(self.user["id"], args.get("since", [0])[0], args.get("limit", [500])[0]))
        if path == "/api/config":
            return self._get_config()
        if path == "/api/ai_quota":
            return self._ai_quota()
        if path == "/api/my_ai_config":
            return self._send_json(200, self._my_ai_config_view())
        if path == "/api/email/config":
            if not self.user.get("is_admin"):
                return self._send_json(403, {"error": "ADMIN_REQUIRED", "message": "只有站点管理员可以查看邮件配置。"})
            return self._send_json(200, self._email_config_view())
        if path == "/api/get_quiz":
            return self._get_quiz()
        if path == "/api/analytics/summary":
            return self._analytics_summary()
        if path == "/api/analytics/daily":
            return self._analytics_daily()
        if path == "/api/analytics/retention":
            return self._analytics_retention()
        if path == "/api/analytics/funnel":
            return self._analytics_funnel()
        if path == "/api/analytics/events":
            return self._analytics_events()
        if path == "/api/analytics/ai_usage":
            return self._analytics_ai_usage()
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        origin = self.headers.get("Origin", "")
        if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            return self._send_json(403, {"error": "ORIGIN_REJECTED", "message": "拒绝跨站写入请求。"})
        if path == "/api/auth/register":
            return self._register()
        if path == "/api/auth/send_code":
            return self._send_code()
        if path == "/api/auth/login":
            return self._login()
        if path == "/api/auth/logout":
            return self._logout()
        if path.startswith("/api/") and not self._require_user():
            return
        if path == "/api/review":
            return self._record_review()
        if path == "/api/sync/push":
            body = self._read_body()
            return self._send_json(200, storage.sync_push(self.user["id"], body.get("records") or []))
        if path == "/api/import_batch":
            return self._import_batch()
        if path == "/api/my_ai_config":
            return self._save_my_ai_config()
        if path == "/api/config" and not self.user.get("is_admin"):
            return self._send_json(403, {"error": "ADMIN_REQUIRED", "message": "只有站点管理员可以修改站点 AI 配置。"})
        if path in {"/api/email/config", "/api/email/test"} and not self.user.get("is_admin"):
            return self._send_json(403, {"error": "ADMIN_REQUIRED", "message": "只有站点管理员可以修改邮件配置。"})
        if path == "/api/config":
            return self._save_config()
        if path == "/api/email/config":
            return self._save_email_config()
        if path == "/api/email/test":
            return self._test_email_config()
        if path == "/api/chat":
            return self._chat()
        if path == "/api/classify":
            return self._classify()
        if path == "/api/test":
            return self._test_config()
        if path == "/api/delete":
            return self._delete_question()
        if path == "/api/update_status":
            return self._update_status()
        if path == "/api/save":
            return self._save_question()
        if path == "/api/edit_quiz":
            return self._edit_quiz()
        if path == "/api/ai_revise":
            return self._ai_revise()
        self._send_json(404, {"error": "NOT_FOUND", "message": "未知接口"})

    # ---- 账号与复习 ----
    def _register(self):
        body = self._read_body()
        email = str(body.get("email") or "").strip()
        try:
            if mailer.register_code_required():
                code = str(body.get("email_code") or "").strip()
                if not code:
                    return self._send_json(400, {"error": "EMAIL_CODE_REQUIRED",
                                                 "message": "请先获取邮箱验证码并填写后再注册。"})
                storage.verify_email_code(email, code, purpose="register")
            user = storage.register_user(body.get("username"), body.get("password"), email,
                                         invite_code=body.get("invite_code"))
            remember = bool(body.get("remember_me", True))
            token = storage.create_session(user["id"], persistent=remember)
            response = {"ok": True,
                        "user": {"id": user["id"], "username": user["username"], "email": user["email"],
                                 "is_admin": user["is_admin"]},
                        "imported": user["imported"],
                        "invite_applied": bool(user.get("invite_applied"))}
            if body.get("client_type") == "mini_program":
                response["session_token"] = token
            self._send_auth(201, response, token=token, remember=remember)
            self._track_event("register", metadata={"username": body.get("username", "")[:50], "imported": user.get("imported", 0)})
        except ValueError as exc:
            self._send_json(400, {"error": "REGISTER_FAILED", "message": str(exc)})

    def _send_code(self):
        body = self._read_body()
        purpose = "login" if body.get("purpose") == "login" else "register"
        email = str(body.get("email") or "").strip()
        if not storage.EMAIL_RE.match(email) or len(email) > 254:
            return self._send_json(400, {"error": "INVALID_EMAIL",
                                         "message": "邮箱格式不正确，请填写形如 name@example.com 的地址。"})
        if not mailer.register_code_required():
            return self._send_json(503, {"error": "EMAIL_NOT_CONFIGURED",
                                         "message": "邮件服务尚未配置，暂时无法发送验证码。"})
        email_norm = email.casefold()
        registered = storage.email_registered(email_norm)
        if purpose == "register" and registered:
            return self._send_json(400, {"error": "EMAIL_TAKEN", "message": "该邮箱已被注册，请直接登录。"})
        if purpose == "login" and not registered:
            return self._send_json(404, {"error": "EMAIL_NOT_FOUND",
                                         "message": "该邮箱尚未注册，请先创建账号。"})
        try:
            code, expires = storage.create_email_code(
                email, purpose=purpose,
                ip=self.headers.get("X-Forwarded-For", self.client_address[0] if self.client_address else ""))
        except ValueError as exc:
            return self._send_json(429, {"error": "CODE_RATE_LIMITED", "message": str(exc)})
        try:
            mailer.send_verification_code(email, code, storage.EMAIL_CODE_TTL_MINUTES)
        except Exception as e:
            return self._send_json(502, {"error": "SEND_FAIL", "message": "验证码邮件发送失败：%s" % e})
        self._track_event("send_code", page="/api/auth/send_code",
                          metadata={"purpose": purpose, "domain": email_norm.split("@")[-1][:50]})
        return self._send_json(200, {"ok": True, "expires_at": expires,
                                     "ttl_minutes": storage.EMAIL_CODE_TTL_MINUTES,
                                     "resend_interval_seconds": storage.EMAIL_CODE_RESEND_SECONDS})

    def _login(self):
        body = self._read_body()
        remember = bool(body.get("remember_me", True))
        email = str(body.get("email") or "").strip()
        code = str(body.get("email_code") or "").strip()
        if code:
            # 邮箱验证码登录：无需密码
            try:
                storage.verify_email_code(email, code, purpose="login")
            except ValueError as exc:
                return self._send_json(401, {"error": "LOGIN_FAILED", "message": str(exc)})
            user = storage.user_by_email(email)
            if not user:
                return self._send_json(401, {"error": "LOGIN_FAILED", "message": "该邮箱尚未注册。"})
        else:
            user = storage.authenticate(body.get("username"), body.get("password"))
            if not user:
                return self._send_json(401, {"error": "LOGIN_FAILED", "message": "账号或密码不正确。"})
        token = storage.create_session(user["id"], persistent=remember)
        response = {"ok": True, "user": user}
        if body.get("client_type") == "mini_program":
            response["session_token"] = token
        self._send_auth(200, response, token=token, remember=remember)
        self._track_event("login", metadata={"username": user["username"][:50], "method": "email_code" if code else "password"})

    def _logout(self):
        self._track_event("logout")
        storage.delete_session(self._session_token())
        self._send_auth(200, {"ok": True}, clear=True)

    def _import_batch(self):
        body = self._read_body()
        files = body.get("files")
        if not isinstance(files, list) or not files:
            return self._send_json(400, {"error": "NO_FILES", "message": "请先选择要导入的 Markdown 文件。"})
        if len(files) > 200:
            return self._send_json(400, {"error": "TOO_MANY_FILES", "message": "单次最多导入 200 个文件。"})
        imported_ids, skipped, failed = [], [], []
        for item in files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "未命名.md")[:120]
            content = str(item.get("content") or "")
            if len(content) > 1_000_000:
                failed.append({"name": name, "error": "文件过大（超过 1MB）"})
                continue
            try:
                question_id, created = storage.import_markdown_content(self.user["id"], name, content)
                if created:
                    imported_ids.append(question_id)
                else:
                    skipped.append({"name": name, "reason": "相同内容已导入过，自动跳过"})
            except ValueError as exc:
                failed.append({"name": name, "error": str(exc)})
            except Exception as exc:
                failed.append({"name": name, "error": str(exc)[:120]})
        if imported_ids:
            self._track_event("import_batch", metadata={"count": len(imported_ids)})
        return self._send_json(200, {"ok": True, "imported": len(imported_ids), "ids": imported_ids,
                                     "skipped": skipped, "failed": failed})

    def _record_review(self):
        body = self._read_body()
        try:
            result = storage.record_review(
                self.user["id"], str(body.get("question_id") or ""), body.get("rating"), body.get("device_id")
            )
            self._send_json(200, {"ok": True, "schedule": result})
            self._track_event("review", metadata={"question_id": str(body.get("question_id", ""))[:36], "rating": body.get("rating")})
        except (KeyError, ValueError) as exc:
            self._send_json(400, {"error": "REVIEW_FAILED", "message": str(exc)})

    # ---- 配置 ----
    def _get_config(self):
        cfg = load_config()
        key = cfg.get("api_key", "")
        if not self.user.get("is_admin"):
            return self._send_json(200, {
                "base_url": "", "model": cfg.get("model"), "configured": bool(key),
                "api_key_tail": "", "presets": [], "admin": False,
            })
        plist = []
        for name, p in load_presets().items():
            pkey = str(p.get("api_key") or "")
            plist.append({
                "name": name,
                "base_url": str(p.get("base_url") or ""),
                "model": str(p.get("model") or ""),
                "has_key": bool(pkey),
                "key_tail": pkey[-4:] if pkey else "",
            })
        self._send_json(200, {
            "base_url": cfg.get("base_url"),
            "model": cfg.get("model"),
            "configured": bool(key),
            "api_key_tail": key[-4:] if key else "",
            "presets": plist,
            "admin": True,
        })

    def _save_config(self):
        body = self._read_body()
        raw = _read_config_raw()
        action = body.get("action") or ""
        if action == "preset_save":
            name = _safe_name(body.get("name") or "", 20)
            if not name:
                return self._send_json(400, {"error": "EMPTY_NAME", "message": "请给选项起个名字。"})
            p_key = (body.get("api_key") or "").strip()
            if not p_key:  # 表单 Key 留空时，沿用当前已配置的 Key
                p_key = str(raw.get("api_key") or "")
            raw.setdefault("presets", {})
            raw["presets"][name] = {
                "base_url": (body.get("base_url") or DEFAULT_BASE).strip(),
                "api_key": p_key,
                "model": (body.get("model") or "").strip(),
            }
            save_config(raw)
            return self._send_json(200, {"ok": True, "preset": name})
        if action == "preset_apply":
            name = body.get("name") or ""
            presets = raw.get("presets") or {}
            if name not in presets:
                return self._send_json(404, {"error": "NO_PRESET", "message": f"选项「{name}」不存在。"})
            p = presets[name]
            raw["base_url"] = str(p.get("base_url") or DEFAULT_BASE)
            raw["api_key"] = str(p.get("api_key") or "")
            raw["model"] = str(p.get("model") or "")
            save_config(raw)
            return self._send_json(200, {"ok": True, "preset": name})
        if action == "preset_delete":
            name = body.get("name") or ""
            presets = raw.get("presets") or {}
            if name in presets:
                del presets[name]
                save_config(raw)
            return self._send_json(200, {"ok": True})
        # 默认：保存当前配置（保留 presets）
        cfg = load_config()
        if "base_url" in body:
            cfg["base_url"] = (body["base_url"] or DEFAULT_BASE).strip()
        if "api_key" in body:
            cfg["api_key"] = (body["api_key"] or "").strip()
        if "model" in body:
            cfg["model"] = (body["model"] or "").strip()
        raw.update(cfg)
        save_config(raw)
        self._send_json(200, {"ok": True, "configured": bool(cfg["api_key"])})

    # ---- 注册邮箱验证码（SMTP 配置仅管理员） ----
    @staticmethod
    def _email_config_view():
        try:
            with open(mailer.CONFIG_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        password = str(raw.get("password") or "")
        host = str(raw.get("host") or "")
        username = str(raw.get("username") or "")
        sender = str(raw.get("sender") or "") or username
        return {
            "enabled": bool(raw.get("enabled")),
            "host": host,
            "port": raw.get("port") or 465,
            "use_ssl": bool(raw.get("use_ssl", True)),
            "use_starttls": bool(raw.get("use_starttls", False)),
            "username": username,
            "password_tail": password[-2:] if password else "",
            "sender": sender,
            "sender_name": str(raw.get("sender_name") or mailer.SENDER_NAME_FALLBACK),
            "configured": bool(host and username and password and sender),
            "register_code_required": mailer.register_code_required(),
        }

    def _save_email_config(self):
        body = self._read_body()
        try:
            with open(mailer.CONFIG_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
        try:
            port = int(body.get("port") or 465)
        except (TypeError, ValueError):
            port = 465
        raw.update({
            "enabled": bool(body.get("enabled")),
            "host": (body.get("host") or "").strip(),
            "port": port,
            "use_ssl": bool(body.get("use_ssl", True)),
            "use_starttls": bool(body.get("use_starttls", False)),
            "username": (body.get("username") or "").strip(),
            "sender": (body.get("sender") or "").strip(),
            "sender_name": (body.get("sender_name") or mailer.SENDER_NAME_FALLBACK).strip(),
        })
        password = (body.get("password") or "").strip()
        if password:  # 留空沿用已保存的授权码
            raw["password"] = password
        raw.setdefault("password", "")
        try:
            with open(mailer.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return self._send_json(500, {"error": "SAVE_FAIL", "message": "保存邮件配置失败：%s" % e})
        return self._send_json(200, self._email_config_view())

    def _test_email_config(self):
        body = self._read_body()
        saved = self._email_config_view()
        password = (body.get("password") or "").strip()
        if not password:
            try:
                with open(mailer.CONFIG_FILE, encoding="utf-8") as f:
                    password = str(json.load(f).get("password") or "")
            except Exception:
                password = ""
        try:
            port = int(body.get("port") or saved.get("port") or 465)
        except (TypeError, ValueError):
            port = 465
        cfg = {
            "host": (body.get("host") or "").strip() or saved.get("host", ""),
            "port": port,
            "use_ssl": bool(body.get("use_ssl", saved.get("use_ssl", True))),
            "use_starttls": bool(body.get("use_starttls", saved.get("use_starttls", False))),
            "username": (body.get("username") or "").strip() or saved.get("username", ""),
            "password": password,
            "sender": (body.get("sender") or "").strip() or (body.get("username") or "").strip() or saved.get("sender", ""),
            "sender_name": (body.get("sender_name") or "").strip() or saved.get("sender_name", ""),
        }
        to = (body.get("to") or "").strip() or cfg["sender"] or cfg["username"]
        if not (cfg["host"] and cfg["username"] and cfg["password"] and to):
            return self._send_json(400, {"error": "EMAIL_NOT_CONFIGURED",
                                         "message": "请先填写 SMTP 服务器、发件账号和授权码。"})
        try:
            mailer.send_test_email(to, cfg=cfg)
        except Exception as e:
            return self._send_json(502, {"error": "TEST_FAIL", "message": "测试邮件发送失败：%s" % e})
        return self._send_json(200, {"ok": True, "to": to, "message": "测试邮件已发送到 %s，请查收。" % to})

    # ---- 删除错题 ----
    def _delete_question(self):
        body = self._read_body()
        question_id = str(body.get("file") or body.get("id") or "").strip()
        if not question_id:
            return self._send_json(400, {"error": "NO_ID", "message": "缺少错题 ID。"})
        try:
            version = storage.soft_delete_question(self.user["id"], question_id)
        except KeyError:
            return self._send_json(404, {"error": "NOT_FOUND", "message": "错题不存在。"})
        self._track_event("delete_question", metadata={"question_id": question_id[:36]})
        return self._send_json(200, {"ok": True, "deleted": [question_id], "version": version})

    # ---- 更新复习状态 ----
    def _update_status(self):
        body = self._read_body()
        question_id = str(body.get("file") or body.get("id") or "").strip()
        status = (body.get("status") or "").strip()
        valid = {"未掌握", "复习中", "已掌握"}
        if not question_id:
            return self._send_json(400, {"error": "NO_ID", "message": "缺少错题 ID。"})
        if status not in valid:
            return self._send_json(400, {"error": "INVALID_STATUS", "message": "状态只能是 未掌握/复习中/已掌握。"})
        try:
            version = storage.set_question_status(self.user["id"], question_id, status)
        except KeyError:
            return self._send_json(404, {"error": "NOT_FOUND", "message": "错题不存在。"})
        return self._send_json(200, {"ok": True, "status": status, "version": version})

    # ---- 配置测试（不落盘） ----
    def _test_config(self):
        body = self._read_body()
        saved = load_config()
        base_url = (body.get("base_url") or "").strip() or saved.get("base_url") or DEFAULT_BASE
        api_key = (body.get("api_key") or "").strip() or str(saved.get("api_key") or "")
        model = (body.get("model") or "").strip() or str(saved.get("model") or "")
        if not api_key:
            return self._send_json(400, {"error": "NO_KEY", "message": "未填写 API Key。"})
        if not model:
            return self._send_json(400, {"error": "NO_MODEL", "message": "未填写模型名。"})
        base = base_url.rstrip("/")
        url = base + "/chat/completions" if not base.endswith("/chat/completions") else base
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "请只回复OK"}],
            "max_tokens": 8,
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode("utf-8"))
            reply = resp["choices"][0]["message"]["content"] or ""
            return self._send_json(200, {"ok": True, "model": model, "reply": reply[:50]})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            return self._send_json(e.code, {"ok": False, "error": "TEST_FAIL",
                                            "message": "测试失败 HTTP %s: %s" % (e.code, detail)})
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": "TEST_FAIL", "message": "测试失败: %s" % e})

    # ---- AI 对话 ----
    def _chat(self):
        body = self._read_body()
        cfg, own = self._effective_ai_config()
        if self._reject_over_quota(own):
            return
        if not cfg.get("api_key"):
            return self._send_json(400, {"error": "NOT_CONFIGURED",
                                         "message": "尚未配置 API Key，请在 AI 助手设置中填写。"})
        if not cfg.get("model"):
            return self._send_json(400, {"error": "NO_MODEL",
                                         "message": "尚未配置模型名，请在 AI 助手设置中填写。"})
        messages = body.get("messages") or []
        if not messages:
            return self._send_json(400, {"error": "EMPTY_MESSAGES", "message": "对话内容为空。"})
        # 注入简洁系统提示（如果用户没传 system）
        has_system = any(m.get("role") == "system" for m in messages if isinstance(m, dict))
        if not has_system:
            messages.insert(0, {"role": "system", "content": "你是SQL错题本AI助手。回答务必简洁直接、要点式，不超过3句话，不要寒暄、不要重复用户问题、不要多余解释。能用一句话说清就不用两句。"})
        try:
            reply = _call_llm(cfg, messages)
            self._send_json(200, {"reply": reply})
            self._track_event("ai_chat", metadata={"msg_count": len(messages), "own_config": own})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            self._send_json(e.code, {"error": "LLM_API_ERROR", "message": f"LLM 返回 {e.code}: {detail}"})
        except Exception as e:
            self._send_json(500, {"error": "PROXY_ERROR", "message": f"代理转发失败: {e}"})

    # ---- AI 自动整理归类（不落盘，返回预览） ----
    def _classify(self):
        body = self._read_body()
        cfg, own = self._effective_ai_config()
        if self._reject_over_quota(own):
            return
        if not cfg.get("api_key"):
            return self._send_json(400, {"error": "NOT_CONFIGURED",
                                         "message": "尚未配置 API Key，请在 AI 助手设置中填写。"})
        if not cfg.get("model"):
            return self._send_json(400, {"error": "NO_MODEL",
                                         "message": "尚未配置模型名，请在 AI 助手设置中填写。"})
        raw = (body.get("raw") or "").strip()
        image = (body.get("image") or "").strip()   # 可选：错题截图 data URL
        if not raw and not image:
            return self._send_json(400, {"error": "EMPTY_RAW", "message": "请先粘贴错题内容或上传截图。"})
        categories = body.get("categories") or []
        today = (body.get("today") or "").strip()
        image = (body.get("image") or "").strip()   # 可选：错题截图 data URL
        prompt = CLS_SYSTEM_PROMPT.format(
            categories=json.dumps(categories, ensure_ascii=False),
            today=today or "未知")
        try:
            if image:
                user_content = [
                    {"type": "image_url", "image_url": {"url": image}},
                    {"type": "text", "text": raw or "请识别这张错题截图并整理成归档格式。"},
                ]
            else:
                user_content = raw
            content = _call_llm(cfg, [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ], temperature=0.2, max_tokens=6000)
            result = _extract_json(content)
            # 字段兜底
            result.setdefault("no", "SQL???")
            result.setdefault("title", "未命名错题")
            result.setdefault("cat", "未分类")
            result.setdefault("diff", "简单")
            result.setdefault("date", today or "")
            result.setdefault("src", "")
            result.setdefault("errtype", "其他")
            result.setdefault("status", "未掌握")
            result.setdefault("times", 1)
            result.setdefault("redates", [result.get("date")] if result.get("date") else [])
            result.setdefault("summary", "")
            result.setdefault("body_md", "")
            self._send_json(200, {"result": result})
            self._track_event("ai_classify", metadata={"has_image": bool(image), "cat": result.get("cat", "")[:30], "own_config": own})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            self._send_json(e.code, {"error": "LLM_API_ERROR", "message": f"LLM 返回 {e.code}: {detail}"})
        except Exception as e:
            self._send_json(500, {"error": "PARSE_ERROR", "message": f"AI 整理失败: {e}"})

    # ---- 获取错题详情（用于编辑） ----
    def _get_quiz(self):
        qs = parse_qs(urlparse(self.path).query)
        question_id = (qs.get("file", [""])[0] or qs.get("id", [""])[0] or "").strip()
        if not question_id:
            return self._send_json(400, {"error": "NO_ID", "message": "缺少错题 ID。"})
        question = storage.get_question(self.user["id"], question_id)
        if not question:
            return self._send_json(404, {"error": "NOT_FOUND", "message": "文件不存在。"})
        meta = {
            "题号": question["no"], "标题": question["title"], "知识点": question["cat"],
            "难度": question["diff"], "日期": question["date"], "来源": question["src"],
            "错误类型": question["errtype"], "状态": question["status"],
            "做错次数": str(question["times"]), "重错日期": ", ".join(question["redates"]),
            "一句话总结": question["summary"],
        }
        return self._send_json(200, {"ok": True, "meta": meta, "body": question["body_md"],
                                     "version": question["version"]})

    # ---- 编辑错题（更新元信息和正文） ----
    def _edit_quiz(self):
        body = self._read_body()
        question_id = str(body.get("file") or body.get("id") or "").strip()
        meta = body.get("meta") or {}
        new_body = (body.get("body") or "").strip()
        if not question_id:
            return self._send_json(400, {"error": "NO_ID", "message": "缺少错题 ID。"})
        if not new_body:
            return self._send_json(400, {"error": "EMPTY_BODY", "message": "正文不能为空。"})
        current = storage.get_question(self.user["id"], question_id)
        if not current:
            return self._send_json(404, {"error": "NOT_FOUND", "message": "文件不存在。"})
        data = {
            "no": meta.get("题号"), "title": meta.get("标题"), "cat": meta.get("知识点"),
            "diff": meta.get("难度"), "date": meta.get("日期"), "src": meta.get("来源"),
            "errtype": meta.get("错误类型"), "status": meta.get("状态"),
            "times": meta.get("做错次数"), "redates": meta.get("重错日期"),
            "summary": meta.get("一句话总结"), "body_md": new_body,
            "next_review_at": current["next_review_at"],
        }
        try:
            version = storage.update_question(self.user["id"], question_id, data, body.get("version"))
        except RuntimeError:
            return self._send_json(409, {"error": "VERSION_CONFLICT", "message": "该错题已在其他设备更新，请刷新后重试。"})
        self._track_event("edit_question", metadata={"question_id": question_id[:36]})
        return self._send_json(200, {"ok": True, "version": version})

    # ---- AI修正错题（多轮对话，不落盘） ----
    def _ai_revise(self):
        body = self._read_body()
        cfg, own = self._effective_ai_config()
        if self._reject_over_quota(own):
            return
        if not cfg.get("api_key"):
            return self._send_json(400, {"error": "NOT_CONFIGURED", "message": "尚未配置 API Key。"})
        if not cfg.get("model"):
            return self._send_json(400, {"error": "NO_MODEL", "message": "尚未配置模型名。"})
        meta = body.get("meta") or {}
        cur_body = (body.get("body") or "").strip()
        instruction = (body.get("instruction") or "").strip()
        history = body.get("messages") or []  # 历史对话：[{role, content}]
        if not instruction:
            return self._send_json(400, {"error": "EMPTY_INSTRUCTION", "message": "请输入修正指令。"})
        if not cur_body:
            return self._send_json(400, {"error": "EMPTY_BODY", "message": "当前错题正文为空。"})
        # 构造当前错题的文本表示
        meta_lines = []
        for k in ["题号","标题","知识点","难度","日期","来源","错误类型","状态","做错次数","一句话总结"]:
            v = str(meta.get(k, "") or "").strip()
            if v:
                meta_lines.append(f"{k}: {v}")
        meta_text = "\n".join(meta_lines)
        current_user_msg = f"""当前错题元信息：
{meta_text}

当前错题正文：
{cur_body}

修正指令：{instruction}"""

        system_msg = """你是SQL错题修正助手。用户会给你一道已整理的错题（含元信息和Markdown正文），以及一条修正指令。
请严格按照指令修正错题内容，保持原有格式不变。
只输出JSON，不要任何解释文字。

输出格式：
{
  "reply": "用一句话简要说明你做了哪些修改",
  "meta": {
    "题号": "", "标题": "", "知识点": "", "难度": "", "日期": "",
    "来源": "", "错误类型": "", "状态": "", "做错次数": "", "重错日期": "", "一句话总结": ""
  },
  "body_md": "修正后的Markdown正文，必须包含 ## 题目 / ## 我的错误写法 / ## 正确写法 / ## 错因分析 / ## 知识点总结 五个小节"
}"""

        # 构造多轮消息
        messages = [{"role": "system", "content": system_msg}]
        # 历史对话（只传 instruction 和 reply，不传完整错题内容，节省token）
        for h in history:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                messages.append({"role": h["role"], "content": str(h.get("content") or "")})
        # 当前轮：完整错题内容 + 指令
        messages.append({"role": "user", "content": current_user_msg})

        try:
            content = _call_llm(cfg, messages, temperature=0.3, max_tokens=6000)
            result = _extract_json(content)
            new_meta = result.get("meta") or {}
            new_body = (result.get("body_md") or "").strip()
            reply = str(result.get("reply") or "已完成修改").strip()
            if not new_body:
                return self._send_json(500, {"error": "EMPTY_RESULT", "message": "AI返回的正文为空。"})
            # 保留未修改的字段
            for k in ["题号","标题","知识点","难度","日期","来源","错误类型","状态","做错次数","重错日期","一句话总结"]:
                if not str(new_meta.get(k, "") or "").strip():
                    new_meta[k] = str(meta.get(k, "") or "")
            self._track_event("ai_revise", metadata={"question_id": str(body.get("file") or body.get("id") or "")[:36], "own_config": own})
            return self._send_json(200, {"ok": True, "reply": reply, "meta": new_meta, "body": new_body})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return self._send_json(e.code, {"error": "LLM_API_ERROR", "message": f"LLM 返回 {e.code}: {detail}"})
        except Exception as e:
            return self._send_json(500, {"error": "REVISE_FAIL", "message": f"AI修正失败: {e}"})

    # ---- 数据分析 API（仅管理员） ----
    def _require_admin(self):
        if not self.user.get("is_admin"):
            self._send_json(403, {"error": "ADMIN_REQUIRED", "message": "只有管理员可以查看数据分析。"})
            return False
        return True

    def _analytics_summary(self):
        if not self._require_admin():
            return
        args = parse_qs(urlparse(self.path).query)
        days = int(args.get("days", [30])[0])
        self._send_json(200, storage.get_analytics_summary(days))

    def _analytics_daily(self):
        if not self._require_admin():
            return
        args = parse_qs(urlparse(self.path).query)
        days = int(args.get("days", [30])[0])
        self._send_json(200, {"daily": storage.get_daily_stats(days)})

    def _analytics_retention(self):
        if not self._require_admin():
            return
        args = parse_qs(urlparse(self.path).query)
        days = int(args.get("days", [14])[0])
        self._send_json(200, {"retention": storage.get_retention(days)})

    def _analytics_funnel(self):
        if not self._require_admin():
            return
        self._send_json(200, {"funnel": storage.get_funnel()})

    def _analytics_events(self):
        if not self._require_admin():
            return
        args = parse_qs(urlparse(self.path).query)
        days = int(args.get("days", [30])[0])
        self._send_json(200, {"events": storage.get_event_breakdown(days)})

    def _analytics_ai_usage(self):
        if not self._require_admin():
            return
        args = parse_qs(urlparse(self.path).query)
        days = int(args.get("days", [30])[0])
        self._send_json(200, {"ai_usage": storage.get_ai_usage(days)})

    # ---- 确认入库（写入 md + 重建） ----
    def _save_question(self):
        b = self._read_body()
        no = _safe_name(b.get("no") or "SQL???", 20)
        title = _safe_name(b.get("title") or "未命名错题", 50)
        cat = _safe_name(b.get("cat") or "未分类", 30)
        date = _safe_name(b.get("date") or "", 10)
        try:
            times = max(1, int(b.get("times") or 1))
        except (TypeError, ValueError):
            times = 1
        redates = b.get("redates") or []
        if isinstance(redates, str):
            redates = [d.strip() for d in re.split(r"[,，;；\s]+", redates) if d.strip()]
        if not redates and date:
            redates = [date]
        body_md = str(b.get("body_md") or "").strip()
        if not body_md:
            return self._send_json(400, {"error": "EMPTY_BODY", "message": "正文不能为空。"})

        if self.user.get("is_admin"):
            folder = os.path.join(QUIZ_DIR, cat)
        else:
            folder = os.path.join(ROOT, "data", "user_content", self.user["id"], cat)
        os.makedirs(folder, exist_ok=True)
        filename = f"{date}_{no}_{title}.md" if date else f"{no}_{title}.md"
        path = _unique_path(folder, filename)

        front = (
            f"---\n"
            f"题号: {no}\n"
            f"标题: {title}\n"
            f"知识点: {cat}\n"
            f"难度: {_safe_name(b.get('diff') or '简单', 10)}\n"
            f"日期: {date}\n"
            f"来源: {_safe_name(b.get('src') or '', 30)}\n"
            f"错误类型: {_safe_name(b.get('errtype') or '其他', 20)}\n"
            f"状态: {_safe_name(b.get('status') or '未掌握', 10)}\n"
            f"做错次数: {times}\n"
            f"重错日期: {', '.join(redates)}\n"
            f"一句话总结: {_safe_name(b.get('summary') or '', 100)}\n"
            f"---\n\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(front + body_md + "\n")

        # 附带保存原始截图到同一分类目录
        image_rel = None
        img_data, img_ext = _decode_data_url(b.get("image") or "")
        if img_data:
            img_name = f"{date}_{no}_原始截图.{img_ext}" if date else f"{no}_原始截图.{img_ext}"
            img_path = _unique_path(folder, img_name)
            with open(img_path, "wb") as f:
                f.write(img_data)
            image_rel = os.path.relpath(img_path, ROOT)

        record = dict(b)
        record.update({"no": no, "title": title, "cat": cat, "date": date,
                       "times": times, "redates": redates, "body_md": body_md})
        question_id = storage.create_question(
            self.user["id"], record, source_file=os.path.relpath(path, ROOT)
        )
        self._track_event("add_question", metadata={"question_id": question_id[:36], "cat": cat[:30], "has_image": bool(image_rel)})

        try:
            _rebuild()
        except Exception as e:
            self._send_json(200, {"ok": True, "path": os.path.relpath(path, ROOT),
                                  "image": image_rel,
                                  "id": question_id,
                                  "warning": f"已写入，但重建索引失败: {e}"})
            return
        self._send_json(200, {"ok": True, "path": os.path.relpath(path, ROOT),
                              "image": image_rel,
                              "id": question_id,
                              "no": no, "title": title, "cat": cat})


def main():
    storage.init_db()
    _rebuild()
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))
    except Exception:
        pass

    if not os.path.exists(CONFIG_FILE):
        save_config({"base_url": DEFAULT_BASE, "api_key": "", "model": ""})

    try:
        httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print(f"[错误] 端口 {PORT} 被占用，可能服务已在运行：{e}")
        sys.exit(1)

    print("=" * 46)
    print("  SQL 错题本本地服务已启动")
    print(f"  页面地址：http://{HOST}:{PORT}/")
    print(f"  AI 配置：{CONFIG_FILE}")
    print("  关闭本窗口或双击「停止服务.bat」即可停止")
    print("=" * 46)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
