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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)            # 项目根目录（本脚本位于根目录/scripts 下）
CONFIG_FILE = os.path.join(ROOT, "config", "ai_config.json")
QUIZ_DIR = os.path.join(ROOT, "错题库")
PID_FILE = os.path.join(ROOT, "server.pid")
REBUILD_SCRIPT = os.path.join(SCRIPT_DIR, "rebuild_index.py")
PORT = int(os.environ.get("PORT", "8765"))
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
    cfg = _read_config_raw()
    return {
        "base_url": str(cfg.get("base_url") or DEFAULT_BASE),
        "api_key": str(cfg.get("api_key") or ""),
        "model": str(cfg.get("model") or ""),
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


def _call_llm(messages, temperature=0.3, retries=3, base_delay=2.0, max_tokens=None, json_mode=False):
    """转发对话给 OpenAI 兼容 LLM，返回 content 字符串。
    遇到 429（访问量过大/限流）或 5xx 时按退避自动重试，最多 retries 次。"""
    cfg = load_config()
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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # ---- 基础工具 ----
    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        sys.stdout.write("[server] " + fmt % args + "\n")

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
            return self._get_config()
        if path == "/api/get_quiz":
            return self._get_quiz()
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/config":
            return self._save_config()
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

    # ---- 配置 ----
    def _get_config(self):
        cfg = load_config()
        key = cfg.get("api_key", "")
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

    # ---- 删除错题 ----
    def _delete_question(self):
        body = self._read_body()
        rel = (body.get("file") or "").strip()
        if not rel:
            return self._send_json(400, {"error": "NO_FILE", "message": "缺少文件路径。"})
        # 安全校验：必须在错题库目录下，且是 .md 文件
        fp = os.path.normpath(os.path.join(ROOT, rel))
        if not fp.startswith(os.path.normpath(os.path.join(ROOT, "错题库"))):
            return self._send_json(400, {"error": "INVALID_PATH", "message": "文件路径不合法。"})
        if not fp.lower().endswith(".md"):
            return self._send_json(400, {"error": "NOT_MD", "message": "只能删除 .md 错题文件。"})
        if not os.path.isfile(fp):
            return self._send_json(404, {"error": "NOT_FOUND", "message": "文件不存在。"})
        # 提取题号，查找关联截图
        import re as _re
        no = ""
        try:
            with open(fp, encoding="utf-8") as f:
                head = f.read(2000)
            mm = _re.search(r"题号\s*[:：]\s*(.+)", head)
            if mm:
                no = mm.group(1).strip()
        except Exception:
            pass
        deleted = [os.path.basename(fp)]
        os.remove(fp)
        # 删除关联截图：同目录或上级目录中文件名含题号的图片
        if no:
            search_dirs = [os.path.dirname(fp)]
            parent = os.path.dirname(os.path.dirname(fp))
            if parent not in search_dirs:
                search_dirs.append(parent)
            for sd in search_dirs:
                if not os.path.isdir(sd):
                    continue
                for fn in os.listdir(sd):
                    if no in fn and fn.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        try:
                            os.remove(os.path.join(sd, fn))
                            deleted.append(fn)
                        except Exception:
                            pass
        # 重建索引
        try:
            _rebuild()
        except Exception as e:
            return self._send_json(500, {"error": "REBUILD_FAIL", "message": "删除成功但重建索引失败: %s" % e})
        return self._send_json(200, {"ok": True, "deleted": deleted})

    # ---- 更新复习状态 ----
    def _update_status(self):
        body = self._read_body()
        rel = (body.get("file") or "").strip()
        status = (body.get("status") or "").strip()
        valid = {"未掌握", "复习中", "已掌握"}
        if not rel:
            return self._send_json(400, {"error": "NO_FILE", "message": "缺少文件路径。"})
        if status not in valid:
            return self._send_json(400, {"error": "INVALID_STATUS", "message": "状态只能是 未掌握/复习中/已掌握。"})
        fp = os.path.normpath(os.path.join(ROOT, rel))
        if not fp.startswith(os.path.normpath(os.path.join(ROOT, "错题库"))):
            return self._send_json(400, {"error": "INVALID_PATH", "message": "文件路径不合法。"})
        if not os.path.isfile(fp):
            return self._send_json(404, {"error": "NOT_FOUND", "message": "文件不存在。"})
        import re as _re
        with open(fp, encoding="utf-8") as f:
            content = f.read()
        # 更新 frontmatter 中的状态字段
        if _re.search(r"^状态\s*[:：]", content, _re.M):
            content = _re.sub(r"^(状态\s*[:：])\s*(.*)$", lambda m: m.group(1) + " " + status, content, flags=_re.M)
        else:
            # 在 frontmatter 末尾插入状态字段
            content = _re.sub(r"^(---\s*$)", "状态: " + status + "\n\1", content, count=1, flags=_re.M)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            _rebuild()
        except Exception as e:
            return self._send_json(500, {"error": "REBUILD_FAIL", "message": "状态更新成功但重建索引失败: %s" % e})
        return self._send_json(200, {"ok": True, "status": status})

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
        cfg = load_config()
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
            reply = _call_llm(messages)
            self._send_json(200, {"reply": reply})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            self._send_json(e.code, {"error": "LLM_API_ERROR", "message": f"LLM 返回 {e.code}: {detail}"})
        except Exception as e:
            self._send_json(500, {"error": "PROXY_ERROR", "message": f"代理转发失败: {e}"})

    # ---- AI 自动整理归类（不落盘，返回预览） ----
    def _classify(self):
        body = self._read_body()
        cfg = load_config()
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
            content = _call_llm([
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
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            self._send_json(e.code, {"error": "LLM_API_ERROR", "message": f"LLM 返回 {e.code}: {detail}"})
        except Exception as e:
            self._send_json(500, {"error": "PARSE_ERROR", "message": f"AI 整理失败: {e}"})

    # ---- 获取错题详情（用于编辑） ----
    def _get_quiz(self):
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        rel = (qs.get("file", [""])[0] or "").strip()
        if not rel:
            return self._send_json(400, {"error": "NO_FILE", "message": "缺少文件路径。"})
        fp = os.path.normpath(os.path.join(ROOT, rel))
        if not fp.startswith(os.path.normpath(os.path.join(ROOT, "错题库"))):
            return self._send_json(400, {"error": "INVALID_PATH", "message": "文件路径不合法。"})
        if not os.path.isfile(fp):
            return self._send_json(404, {"error": "NOT_FOUND", "message": "文件不存在。"})
        with open(fp, encoding="utf-8") as f:
            content = f.read()
        meta = {}
        body = content
        m = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", content, re.S)
        if m:
            fm = m.group(1)
            body = content[m.end():]
            for line in fm.splitlines():
                mm = re.match(r"^\s*([^:：]+?)\s*[:：]\s*(.*)$", line)
                if mm:
                    meta[mm.group(1).strip()] = mm.group(2).strip()
        return self._send_json(200, {"ok": True, "meta": meta, "body": body})

    # ---- 编辑错题（更新元信息和正文） ----
    def _edit_quiz(self):
        body = self._read_body()
        rel = (body.get("file") or "").strip()
        meta = body.get("meta") or {}
        new_body = (body.get("body") or "").strip()
        if not rel:
            return self._send_json(400, {"error": "NO_FILE", "message": "缺少文件路径。"})
        if not new_body:
            return self._send_json(400, {"error": "EMPTY_BODY", "message": "正文不能为空。"})
        fp = os.path.normpath(os.path.join(ROOT, rel))
        if not fp.startswith(os.path.normpath(os.path.join(ROOT, "错题库"))):
            return self._send_json(400, {"error": "INVALID_PATH", "message": "文件路径不合法。"})
        if not os.path.isfile(fp):
            return self._send_json(404, {"error": "NOT_FOUND", "message": "文件不存在。"})
        # 构建 frontmatter
        keys_order = ["题号", "标题", "知识点", "难度", "日期", "来源", "错误类型",
                      "状态", "做错次数", "重错日期", "一句话总结"]
        lines = ["---"]
        for k in keys_order:
            v = str(meta.get(k, "") or "").strip()
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        front = "\n".join(lines) + "\n"
        with open(fp, "w", encoding="utf-8") as f:
            f.write(front + new_body + "\n")
        # 重建索引
        try:
            _rebuild()
        except Exception as e:
            return self._send_json(200, {"ok": True, "warning": f"已保存，但重建索引失败: {e}"})
        return self._send_json(200, {"ok": True})

    # ---- AI修正错题（多轮对话，不落盘） ----
    def _ai_revise(self):
        body = self._read_body()
        cfg = load_config()
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
            content = _call_llm(messages, temperature=0.3, max_tokens=6000)
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
            return self._send_json(200, {"ok": True, "reply": reply, "meta": new_meta, "body": new_body})
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return self._send_json(e.code, {"error": "LLM_API_ERROR", "message": f"LLM 返回 {e.code}: {detail}"})
        except Exception as e:
            return self._send_json(500, {"error": "REVISE_FAIL", "message": f"AI修正失败: {e}"})

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

        folder = os.path.join(QUIZ_DIR, cat)
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

        try:
            _rebuild()
        except Exception as e:
            self._send_json(200, {"ok": True, "path": os.path.relpath(path, ROOT),
                                  "image": image_rel,
                                  "warning": f"已写入，但重建索引失败: {e}"})
            return
        self._send_json(200, {"ok": True, "path": os.path.relpath(path, ROOT),
                              "image": image_rel,
                              "no": no, "title": title, "cat": cat})


def main():
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))
    except Exception:
        pass

    if not os.path.exists(CONFIG_FILE):
        save_config({"base_url": DEFAULT_BASE, "api_key": "", "model": ""})

    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"[错误] 端口 {PORT} 被占用，可能服务已在运行：{e}")
        sys.exit(1)

    print("=" * 46)
    print("  SQL 错题本本地服务已启动")
    print(f"  页面地址：http://127.0.0.1:{PORT}/")
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
