# -*- coding: utf-8 -*-
"""
rebuild_index.py —— 错题本索引重建脚本
=============================================
功能：扫描「错题库」目录下所有 .md 错题文件，解析头部元信息，
      按「知识点」自动分门别类，读取 template.html 模板，
      生成带 分类导航/跳转入口/趋势分析/智能检索/AI助手 的 index.html，
      并把每道错题的结构化数据内嵌为 window.__QUIZ_DATA__。
用法：双击「打开错题本.bat」即可（自动执行本脚本并启动本地服务）。
      或在命令行运行：python rebuild_index.py
说明：以后新增错题，只需把写好的 .md 文件放进「错题库」任意子目录，
      再次运行本脚本，索引会自动更新（支持嵌套目录）。
"""
import os
import re
import html as html_mod
import json
import datetime

# ---------- 路径 ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)            # 项目根目录（本脚本位于根目录/scripts 下）
QUIZ_DIR = os.path.join(ROOT, "错题库")
TEMPLATE_FILE = os.path.join(ROOT, "assets", "template.html")
OUT_FILE = os.path.join(ROOT, "index.html")

# ---------- 元信息解析 ----------
META_KEYS = ["题号", "学科", "标题", "知识点", "难度", "日期", "来源", "错误类型",
             "状态", "做错次数", "重错日期", "一句话总结"]


def parse_md(path):
    """解析单个 .md 文件，返回 (meta_dict, body_str)"""
    with open(path, encoding="utf-8") as f:
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
    for k in META_KEYS:
        meta.setdefault(k, "")
    return meta, body


def split_sections(body):
    """按 '## 标题' 把正文拆成 [(标题, 内容), ...]"""
    parts = re.split(r"^##\s+(.+?)\s*$", body, flags=re.M)
    sections = []
    i = 1
    while i + 1 < len(parts):
        sections.append((parts[i].strip(), parts[i + 1].strip()))
        i += 2
    return sections


# ---------- 极简 Markdown -> HTML ----------
def inline(text):
    t = html_mod.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t


def render_table(rows):
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    data = []
    for r in rows[1:]:
        cells = [c.strip() for c in r.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        data.append(cells)
    out = ['<div class="table-scroll" tabindex="0" role="region" aria-label="题目表格，可横向滑动"><table><thead><tr>']
    out += [f"<th>{html_mod.escape(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for r in data:
        out.append("<tr>" + "".join(f"<td>{html_mod.escape(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table></div>")
    return "\n".join(out)


# ---------- 轻量 SQL 语法高亮 ----------
SQL_KEYWORDS = set("""
select from where and or not in null like insert into values update set delete
join left right inner outer full cross on group by order having limit offset as
distinct union all any some case when then else end is between exists primary
key foreign references create table drop alter index database use show desc asc
count sum avg min max if coalesce cast round date now current_date
current_timestamp interval extract partition over rows range unbounded
preceding following ifnull nullif
""".upper().split())

SQL_OPERATORS = {"!=", "<>", "=", ">", "<", ">=", "<=", "+", "-", "*", "/", "%",
                 "(", ")", ",", ";", "&&", "||", ".", "->", "->>"}

_HL_TOKEN = re.compile(
    r"('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|"
    r"\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b|[^\sA-Za-z0-9_'\"]+)"
)


def _hl_line(line):
    out = []
    pos = 0
    for m in _HL_TOKEN.finditer(line):
        gap = line[pos:m.start()]
        if gap:
            out.append(html_mod.escape(gap))
        tok = m.group(0)
        if tok[:1] in "'\"":
            out.append(f'<span class="s">{html_mod.escape(tok)}</span>')
        elif re.fullmatch(r"\d+(?:\.\d+)?", tok):
            out.append(f'<span class="n">{html_mod.escape(tok)}</span>')
        elif tok.upper() in SQL_KEYWORDS:
            out.append(f'<span class="k">{html_mod.escape(tok)}</span>')
        elif tok in SQL_OPERATORS:
            out.append(f'<span class="op">{html_mod.escape(tok)}</span>')
        else:
            out.append(html_mod.escape(tok))
        pos = m.end()
    if pos < len(line):
        out.append(html_mod.escape(line[pos:]))
    return "".join(out)


def _highlight_code(code, lang):
    if lang.lower() not in ("sql", "mysql"):
        return html_mod.escape(code)
    out = []
    for line in code.split("\n"):
        if line.lstrip().startswith("--"):
            out.append(f'<span class="c">{html_mod.escape(line)}</span>')
        else:
            out.append(_hl_line(line))
    return "\n".join(out)


def _render_text_block(text):
    lines = text.splitlines()
    result = []
    buf = []
    list_items = []
    table_rows = []

    def flush_para():
        if buf:
            result.append("<p>" + inline(" ".join(x.strip() for x in buf)) + "</p>")
            buf.clear()

    def flush_list():
        if list_items:
            result.append("<ul>" + "".join(f"<li>{inline(it)}</li>" for it in list_items) + "</ul>")
            list_items.clear()

    def flush_table():
        if table_rows:
            result.append(render_table(table_rows))
            table_rows.clear()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_table(); flush_list(); flush_para()
            continue
        if line.startswith("|"):
            flush_para(); flush_list()
            table_rows.append(line)
        elif re.match(r"^\s*[-*]\s+", line):
            flush_para(); flush_table()
            list_items.append(re.sub(r"^\s*[-*]\s+", "", line))
        else:
            flush_table(); flush_list()
            buf.append(line)
    flush_table(); flush_list(); flush_para()
    return "\n".join(result)


def render_block(text):
    parts = re.split(r"```(\w*)\r?\n(.*?)```", text, flags=re.S)
    out = []
    i = 0
    while i < len(parts):
        if i % 3 == 0:
            out.append(_render_text_block(parts[i]))
        else:
            lang = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            out.append(f'<pre class="code"><code>{_highlight_code(body, lang)}</code></pre>')
            i += 1
        i += 1
    return "\n".join(out)


def plain_text(body):
    """把正文转成纯文本（用于全文检索），去掉代码围栏与 Markdown 符号"""
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = re.sub(r"[#*>`|]", " ", body)
    body = re.sub(r"-{3,}", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def _safe_json(data):
    """序列化 JSON 并对 < 转义，防止 </script> 打断内嵌脚本"""
    s = json.dumps(data, ensure_ascii=False, indent=1)
    return s.replace("<", "\\u003c")


def build():
    files = []
    if os.path.isdir(QUIZ_DIR):
        for dirpath, _dirs, names in os.walk(QUIZ_DIR):
            for n in sorted(names):
                if n.lower().endswith(".md") and not n.startswith("_"):
                    files.append(os.path.join(dirpath, n))
    files.sort()

    questions = []
    for fp in files:
        meta, body = parse_md(fp)
        sections = split_sections(body)
        questions.append({
            "meta": meta,
            "sections": sections,
            "body": body,
            "file": os.path.relpath(fp, ROOT),
        })

    # 按知识点分组
    groups = {}
    for q in questions:
        key = q["meta"]["知识点"].strip() or "未分类"
        groups.setdefault(key, []).append(q)
    for g in groups.values():
        g.sort(key=lambda x: x["meta"]["日期"], reverse=True)

    total = len(questions)
    cat_n = len(groups)

    # ---- 侧栏导航 ----
    nav_html = []
    for i, key in enumerate(sorted(groups)):
        nav_html.append(f'<a href="#cat{i}">{html_mod.escape(key)}<span class="cnt">{len(groups[key])} 题</span></a>')
    nav_html = "\n".join(nav_html)

    navq = []
    for key in sorted(groups):
        navq.append(f'<div class="nav-sub"><b>{html_mod.escape(key)}</b>')
        for q in groups[key]:
            qid_v = qid(questions.index(q) + 1)
            label = f'{q["meta"]["题号"]} {q["meta"]["标题"]}'
            navq.append(f'<a href="#{qid_v}">· {html_mod.escape(label)}</a>')
        navq.append("</div>")
    navq = "\n".join(navq)

    # ---- 主体卡片 ----
    main_html = []
    qindex = 0
    data_list = []
    for ci, key in enumerate(sorted(groups)):
        main_html.append(f'<section class="cat" id="cat{ci}">')
        main_html.append(f'<h2>{html_mod.escape(key)} <span class="cnt-badge">{len(groups[key])} 题</span></h2>')
        for q in groups[key]:
            qindex += 1
            m = q["meta"]
            qid_v = qid(qindex)
            diff = m["难度"] or "未标注"
            main_html.append(f'<article class="qcard" id="{qid_v}" data-file="{html_mod.escape(q["file"])}">')
            main_html.append('<div class="qhead">')
            main_html.append(f'<span class="badge">{html_mod.escape(m["题号"])}</span>')
            main_html.append(f'<h3>{html_mod.escape(m["标题"])}</h3>')
            main_html.append('<span class="meta">')
            main_html.append(f'<span class="chip diff-{html_mod.escape(diff)}">{html_mod.escape(diff)}</span>')
            main_html.append(f'<span class="chip type">{html_mod.escape(m["错误类型"])}</span>')
            if m["状态"]:
                main_html.append(f'<span class="chip status status-clickable" data-file="{html_mod.escape(q["file"])}" data-status="{html_mod.escape(m["状态"])}" title="点击切换：未掌握→复习中→已掌握">{html_mod.escape(m["状态"])}</span>')
            main_html.append(f'<span class="chip">{html_mod.escape(m["日期"])}</span>')
            main_html.append(f'<span class="chip">{html_mod.escape(m["来源"])}</span>')
            main_html.append(f'<button class="qedit" data-file="{html_mod.escape(q["file"])}" title="编辑此题">✏️</button>')
            main_html.append(f'<button class="qdel" data-file="{html_mod.escape(q["file"])}" title="删除此题">🗑</button>')
            main_html.append('</span></div>')
            if m["一句话总结"]:
                main_html.append(f'<div class="summary">💡 一句话错因：{inline(m["一句话总结"])}</div>')
            main_html.append("<details><summary>查看题目 / 错因 / 解析详情</summary>")
            for h, c in q["sections"]:
                main_html.append(f"<h4>{html_mod.escape(h)}</h4>")
                main_html.append(render_block(c))
            main_html.append("</details>")
            main_html.append("</article>")

            # 结构化数据
            times = 1
            try:
                times = max(1, int(m["做错次数"] or 1))
            except ValueError:
                pass
            redates = [d.strip() for d in re.split(r"[,，;；\s]+", m["重错日期"]) if d.strip()]
            data_list.append({
                "no": m["题号"],
                "subject": m["学科"] or "SQL",
                "title": m["标题"],
                "cat": key,
                "diff": diff,
                "date": m["日期"],
                "src": m["来源"],
                "errtype": m["错误类型"],
                "status": m["状态"],
                "times": times,
                "redates": redates,
                "summary": m["一句话总结"],
                "text": " ".join([m["学科"], m["标题"], m["一句话总结"], plain_text(q["body"])]),
            })
        main_html.append("</section>")
    main_html = "\n".join(main_html)

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(TEMPLATE_FILE, encoding="utf-8") as f:
        template = f.read()

    html_out = (template
                .replace("{{STATS}}", f"共 {total} 题 · {cat_n} 个知识点 · 更新于 {now}")
                .replace("{{TOTAL}}", str(total))
                .replace("{{NAV}}", nav_html)
                .replace("{{NAVQ}}", navq)
                .replace("{{MAIN}}", main_html)
                .replace("{{DATA}}", _safe_json(data_list))
                .replace("{{TIME}}", now))

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"[OK] 共扫描 {total} 道错题，分 {cat_n} 类，已生成 index.html（含趋势/检索/AI组件）")
    print(f"     输出文件：{OUT_FILE}")


def qid(i):
    return f"q{i:04d}"


if __name__ == "__main__":
    build()
