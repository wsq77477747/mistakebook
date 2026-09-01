# -*- coding: utf-8 -*-
"""SQLite persistence, accounts, review scheduling, and sync primitives."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.environ.get("SQL_WRONGBOOK_DB", os.path.join(DATA_DIR, "sql_review.db"))
QUIZ_DIR = os.path.join(ROOT, "错题库")
SESSION_DAYS = 30
SESSION_RENEW_THRESHOLD_DAYS = SESSION_DAYS // 2  # 剩余不足一半时滑动续期
FREE_AI_CALLS_PER_DAY = 3      # 使用站点默认模型时，每用户每日免费次数
INVITE_BONUS_CALLS = 10        # 有效邀请码注册成功后，邀请人与被邀请人每日额度各 +10
MAX_ACCOUNTS_PER_EMAIL = 3
AUTH_TICKET_TTL_MINUTES = 10
PBKDF2_ITERATIONS = 240_000
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

# ================= 等级 / 经验 / 每日任务 =================
# 30 级称号，每 5 级一个大段位（初识→渐悟→精进→通达→卓越→传说）
LEVEL_TITLES = [
    "数据萌新", "查询见习生", "SELECT 学徒", "条件过滤手", "初阶查询员",      # 1-5  初识
    "表连接行者", "聚合运算手", "分组分析师", "子查询能手", "熟练查询师",    # 6-10 渐悟
    "窗口函数客", "索引优化者", "查询规划师", "数据建模师", "逻辑架构师",    # 11-15 精进
    "性能调优师", "数据工程师", "洞察分析师", "商业智能师", "算法炼金师",    # 16-20 通达
    "数据科学家", "建模艺术家", "全域架构师", "首席数据官", "数据博学者",    # 21-25 卓越
    "数据宗师", "查询之灵", "逻辑圣手", "数据贤者", "数据之神",             # 26-30 传说
]
TIER_NAMES = ["初识", "渐悟", "精进", "通达", "卓越", "传说"]
# 各段位主题渐变色（前端徽章/进度条共用）
TIER_COLORS = [
    ("#b08d57", "#d4af7a"),  # 初识 青铜
    ("#94a3b8", "#cbd5e1"),  # 渐悟 白银
    ("#eab308", "#facc15"),  # 精进 黄金
    ("#06b6d4", "#3b82f6"),  # 通达 铂金
    ("#8b5cf6", "#ec4899"),  # 卓越 钻石
    ("#f97316", "#ec4899"),  # 传说 炫彩
]
MAX_LEVEL = len(LEVEL_TITLES)

def level_cum_exp(level):
    """达到指定等级所需的累计经验（Lv1 为 0）。每级增量 50+(L-1)*20。"""
    if level <= 1:
        return 0
    n = level - 1
    return n * (50 + 10 * (n - 1))

def level_from_exp(exp):
    """由总经验推算等级状态：等级、称号、段位、本级起点、下级所需总量、进度比例。"""
    exp = max(0, int(exp or 0))
    level = 1
    while level < MAX_LEVEL and exp >= level_cum_exp(level + 1):
        level += 1
    title = LEVEL_TITLES[level - 1]
    tier_idx = (level - 1) // 5
    tier = TIER_NAMES[tier_idx]
    c1, c2 = TIER_COLORS[tier_idx]
    cur_base = level_cum_exp(level)
    if level >= MAX_LEVEL:
        return {"level": level, "title": title, "tier": tier, "tier_index": tier_idx,
                "color1": c1, "color2": c2, "cur_base": cur_base, "next_need": cur_base,
                "into_level": exp - cur_base, "span": 1, "progress": 1.0, "maxed": True}
    next_base = level_cum_exp(level + 1)
    span = next_base - cur_base
    return {"level": level, "title": title, "tier": tier, "tier_index": tier_idx,
            "color1": c1, "color2": c2, "cur_base": cur_base, "next_need": next_base,
            "into_level": exp - cur_base, "span": span,
            "progress": (exp - cur_base) / span, "maxed": False}

# 每日任务清单：metric 对应 daily_progress 的计数字段，达成自动发奖励
DAILY_TASKS = [
    {"key": "login1",    "label": "每日登录",        "metric": "login_done",  "target": 1,  "reward": 10},
    {"key": "review3",   "label": "复习 3 道错题",   "metric": "reviews",    "target": 3,  "reward": 15},
    {"key": "review10",  "label": "复习 10 道错题",  "metric": "reviews",    "target": 10, "reward": 30},
    {"key": "add1",      "label": "新增 1 道错题",   "metric": "adds",       "target": 1,  "reward": 15},
    {"key": "interact1", "label": "讨论室互动 1 次", "metric": "interactions","target": 1, "reward": 10},
]
# 即时行为经验与每日上限（防刷）
ACTION_EXP = {"review": 5, "add": 8, "interact": 3}
ACTION_DAILY_CAP = {"review": 100, "add": 80, "interact": 30}


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def validate_password(password, label="密码"):
    """密码至少 6 位，且字母、数字、符号三类中至少包含两类。"""
    value = str(password or "")
    if not value:
        raise ValueError("请填写%s。" % label)
    if len(value) < 6:
        raise ValueError("%s至少需要 6 个字符。" % label)
    categories = sum((
        any(ch.isalpha() for ch in value),
        any(ch.isdigit() for ch in value),
        any(not ch.isalnum() and not ch.isspace() for ch in value),
    ))
    if categories < 2:
        raise ValueError("%s必须包含字母、数字或符号中的至少两种。" % label)
    return True


def _today():
    return dt.date.today()


def _date_text(value, fallback=None):
    try:
        return dt.date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return (fallback or _today()).isoformat()


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15, factory=_ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_db():
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL,
              username_norm TEXT NOT NULL UNIQUE,
              email TEXT NOT NULL DEFAULT '',
              email_norm TEXT NOT NULL DEFAULT '',
              password_salt TEXT NOT NULL,
              password_hash TEXT NOT NULL,
              is_admin INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              persistent INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS questions (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              source_file TEXT,
              no TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              cat TEXT NOT NULL DEFAULT '未分类',
              diff TEXT NOT NULL DEFAULT '简单',
              first_wrong_date TEXT NOT NULL,
              source TEXT NOT NULL DEFAULT '',
              error_type TEXT NOT NULL DEFAULT '其他',
              status TEXT NOT NULL DEFAULT '未掌握',
              wrong_times INTEGER NOT NULL DEFAULT 1,
              rewrong_dates TEXT NOT NULL DEFAULT '[]',
              summary TEXT NOT NULL DEFAULT '',
              body_md TEXT NOT NULL DEFAULT '',
              search_text TEXT NOT NULL DEFAULT '',
              ease REAL NOT NULL DEFAULT 2.5,
              interval_days INTEGER NOT NULL DEFAULT 0,
              repetitions INTEGER NOT NULL DEFAULT 0,
              lapses INTEGER NOT NULL DEFAULT 0,
              next_review_at TEXT NOT NULL,
              last_review_at TEXT,
              version INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              notebook_id TEXT,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS review_events (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
              rating INTEGER NOT NULL,
              previous_due TEXT,
              next_due TEXT NOT NULL,
              previous_interval INTEGER NOT NULL,
              next_interval INTEGER NOT NULL,
              device_id TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS changes (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              entity_type TEXT NOT NULL,
              entity_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              version INTEGER NOT NULL,
              changed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_events (
              id TEXT PRIMARY KEY,
              user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
              device_id TEXT NOT NULL DEFAULT '',
              event_type TEXT NOT NULL,
              page TEXT NOT NULL DEFAULT '',
              metadata TEXT NOT NULL DEFAULT '{}',
              ip TEXT NOT NULL DEFAULT '',
              user_agent TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_codes (
              id TEXT PRIMARY KEY,
              email_norm TEXT NOT NULL,
              code_hash TEXT NOT NULL,
              purpose TEXT NOT NULL DEFAULT 'register',
              attempts INTEGER NOT NULL DEFAULT 0,
              used_at TEXT,
              ip TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_tickets (
              token_hash TEXT PRIMARY KEY,
              email_norm TEXT NOT NULL,
              purpose TEXT NOT NULL,
              used_at TEXT,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS community_posts (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS community_comments (
              id TEXT PRIMARY KEY,
              post_id TEXT NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              content TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              deleted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS community_likes (
              post_id TEXT NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              PRIMARY KEY(post_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS community_shares (
              post_id TEXT NOT NULL REFERENCES community_posts(id) ON DELETE CASCADE,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              PRIMARY KEY(post_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_expiry
              ON sessions(user_id, expires_at);
            CREATE INDEX IF NOT EXISTS idx_questions_user_due
              ON questions(user_id, deleted_at, next_review_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_user_source_file
              ON questions(user_id, source_file) WHERE source_file IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_reviews_user_question_date
              ON review_events(user_id, question_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_changes_user_sequence
              ON changes(user_id, sequence);
            CREATE INDEX IF NOT EXISTS idx_events_user_created
              ON user_events(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_type_created
              ON user_events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_device_created
              ON user_events(device_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_email_codes_email
              ON email_codes(email_norm, purpose, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_tickets_email
              ON auth_tickets(email_norm, purpose, expires_at);
            CREATE INDEX IF NOT EXISTS idx_community_posts_created
              ON community_posts(deleted_at, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_community_posts_user
              ON community_posts(user_id, deleted_at, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_community_comments_post
              ON community_comments(post_id, deleted_at, created_at);
            CREATE INDEX IF NOT EXISTS idx_community_likes_post
              ON community_likes(post_id);
            CREATE INDEX IF NOT EXISTS idx_community_shares_post
              ON community_shares(post_id);

            CREATE TABLE IF NOT EXISTS notebooks (
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              color TEXT NOT NULL DEFAULT '#3b82f6',
              icon TEXT NOT NULL DEFAULT '📘',
              sort_order INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              archived_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_notebooks_user
              ON notebooks(user_id, archived_at, sort_order);
            CREATE INDEX IF NOT EXISTS idx_questions_user_nb
              ON questions(user_id, notebook_id, deleted_at);
            CREATE TABLE IF NOT EXISTS daily_progress (
              user_id TEXT NOT NULL,
              date TEXT NOT NULL,
              login_done INTEGER NOT NULL DEFAULT 0,
              reviews INTEGER NOT NULL DEFAULT 0,
              adds INTEGER NOT NULL DEFAULT 0,
              interactions INTEGER NOT NULL DEFAULT 0,
              exp_review INTEGER NOT NULL DEFAULT 0,
              exp_add INTEGER NOT NULL DEFAULT 0,
              exp_interact INTEGER NOT NULL DEFAULT 0,
              claimed TEXT NOT NULL DEFAULT '[]',
              PRIMARY KEY(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS friendships (
              id TEXT PRIMARY KEY,
              requester_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              addressee_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              status TEXT NOT NULL DEFAULT 'pending',
              pair_key TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_friend_pair ON friendships(pair_key);
            CREATE INDEX IF NOT EXISTS idx_friend_addressee ON friendships(addressee_id,status);
            CREATE INDEX IF NOT EXISTS idx_friend_requester ON friendships(requester_id,status);
            """
        )
        user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        for col, ddl in (
            ("invite_code", "TEXT NOT NULL DEFAULT ''"),
            ("invite_bonus", "INTEGER NOT NULL DEFAULT 0"),
            ("invited_by", "TEXT"),
            ("ai_config", "TEXT NOT NULL DEFAULT ''"),
            ("avatar", "TEXT NOT NULL DEFAULT ''"),
            ("display_name", "TEXT NOT NULL DEFAULT ''"),
            ("bio", "TEXT NOT NULL DEFAULT ''"),
            ("exp", "INTEGER NOT NULL DEFAULT 0"),
            ("last_login_date", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in user_columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        # 为存量用户补生成邀请码（去掉易混淆字符的 8 位大写码）
        for (uid,) in db.execute(
            "SELECT id FROM users WHERE invite_code='' OR invite_code IS NULL"
        ).fetchall():
            while True:
                code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
                if not db.execute("SELECT 1 FROM users WHERE invite_code=?", (code,)).fetchone():
                    break
            db.execute("UPDATE users SET invite_code=? WHERE id=?", (code, uid))
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if "persistent" not in session_columns:
            # 旧库默认视为持久会话，保持升级前的自动登录行为
            db.execute("ALTER TABLE sessions ADD COLUMN persistent INTEGER NOT NULL DEFAULT 1")
        if "is_admin" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            db.execute(
                "UPDATE users SET is_admin=1 WHERE id=(SELECT id FROM users ORDER BY created_at,id LIMIT 1)"
            )
        if "email" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        if "email_norm" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN email_norm TEXT NOT NULL DEFAULT ''")
        # —— 错题本（科目）系统迁移 ——
        question_columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
        if "notebook_id" not in question_columns:
            db.execute("ALTER TABLE questions ADD COLUMN notebook_id TEXT")
        for (uid,) in db.execute("SELECT id FROM users").fetchall():
            _nb_id, _ = _ensure_default_notebook(db, uid)
            db.execute(
                "UPDATE questions SET notebook_id=? WHERE user_id=? AND notebook_id IS NULL",
                (_nb_id, uid),
            )
        db.execute("DROP INDEX IF EXISTS idx_users_email_norm")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_email_norm_lookup "
            "ON users(email_norm) WHERE email_norm != ''"
        )
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))
        db.execute("PRAGMA optimize")


def _password_digest(password, salt_hex):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    ).hex()


def register_user(username, password, email, invite_code=None):
    username = str(username or "").strip()
    username_norm = username.casefold()
    email = str(email or "").strip()
    email_norm = email.casefold()
    if len(username) < 3 or len(username) > 80:
        raise ValueError("账号长度需为 3–80 个字符。")
    validate_password(password)
    if not email:
        raise ValueError("请填写邮箱。")
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ValueError("邮箱格式不正确，请填写形如 name@example.com 的地址。")
    salt = secrets.token_hex(16)
    now = _now()
    user_id = str(uuid.uuid4())
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        first_user = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        account_count = db.execute(
            "SELECT COUNT(*) FROM users WHERE email_norm=?", (email_norm,)
        ).fetchone()[0]
        if account_count >= MAX_ACCOUNTS_PER_EMAIL:
            raise ValueError("该邮箱最多只能注册 %d 个账号。" % MAX_ACCOUNTS_PER_EMAIL)
        inviter_id = None
        code = str(invite_code or "").strip().upper()
        if code:
            inviter = db.execute(
                "SELECT id FROM users WHERE invite_code=?", (code,)
            ).fetchone()
            if inviter:
                inviter_id = inviter["id"]
        while True:
            my_code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
            if not db.execute("SELECT 1 FROM users WHERE invite_code=?", (my_code,)).fetchone():
                break
        try:
            db.execute(
                "INSERT INTO users(id,username,username_norm,email,email_norm,password_salt,password_hash,"
                "is_admin,created_at,updated_at,invite_code,invited_by) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, username, username_norm, email, email_norm, salt,
                 _password_digest(password, salt), 1 if first_user else 0, now, now,
                 my_code, inviter_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该账号已存在。") from exc
        if inviter_id:  # 邀请成功：邀请人 +1 奖励单位；被邀请人由 invited_by 自动获得 1 单位
            db.execute("UPDATE users SET invite_bonus=invite_bonus+1 WHERE id=?", (inviter_id,))
        _ensure_default_notebook(db, user_id, now)
    imported = import_legacy_questions(user_id) if first_user else 0
    return {"id": user_id, "username": username, "email": email,
            "is_admin": first_user, "imported": imported,
            "invite_code": my_code, "invite_applied": bool(inviter_id),
            "avatar": "", "display_name": "", "bio": ""}


def email_registered(email_norm):
    with connect() as db:
        row = db.execute("SELECT 1 FROM users WHERE email_norm=?", (str(email_norm or ""),)).fetchone()
    return bool(row)


def email_account_count(email):
    email_norm = str(email or "").strip().casefold()
    if not email_norm:
        return 0
    with connect() as db:
        return int(db.execute(
            "SELECT COUNT(*) FROM users WHERE email_norm=?", (email_norm,)
        ).fetchone()[0])


def users_by_email(email):
    """返回邮箱下的全部账号；仅供验证码已验证后的账号选择流程使用。"""
    email_norm = str(email or "").strip().casefold()
    if not email_norm:
        return []
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM users WHERE email_norm=? ORDER BY created_at,id", (email_norm,)
        ).fetchall()
    return [_user_public(row) for row in rows]


def user_by_email(email):
    """按邮箱查用户（大小写不敏感）；不存在返回 None。"""
    email_norm = str(email or "").strip().casefold()
    if not email_norm:
        return None
    users = users_by_email(email_norm)
    return users[0] if users else None


# ---- 用户资料 ----

def _user_public(row):
    """把 users 表行统一转成对外的用户信息字典。"""
    if row is None:
        return None
    keys = row.keys()
    return {
        "id": row["id"],
        "username": row["username"],
        "email": row["email"] if "email" in keys else "",
        "is_admin": bool(row["is_admin"]),
        "avatar": row["avatar"] if "avatar" in keys else "",
        "display_name": row["display_name"] if "display_name" in keys else "",
        "bio": row["bio"] if "bio" in keys else "",
        "exp": int(row["exp"]) if "exp" in keys and row["exp"] is not None else 0,
        "invite_code": row["invite_code"] if "invite_code" in keys else "",
    }


def get_profile(user_id):
    """获取用户完整资料。"""
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _user_public(row)


def update_profile(user_id, display_name=None, bio=None, avatar=None):
    """更新用户资料；只更新传入的字段。返回更新后的资料。"""
    sets, params = [], []
    if display_name is not None:
        name = str(display_name).strip()[:40]
        sets.append("display_name=?")
        params.append(name)
    if bio is not None:
        b = str(bio).strip()[:200]
        sets.append("bio=?")
        params.append(b)
    if avatar is not None:
        av = str(avatar or "")
        # 头像限制 300KB（data URL），防止数据库膨胀
        if len(av) > 300_000:
            raise ValueError("头像图片过大，请压缩到 200KB 以内。")
        if av and not (av.startswith("data:image/") or av.startswith("/assets/avatars/")):
            raise ValueError("头像格式不正确。")
        sets.append("avatar=?")
        params.append(av)
    if not sets:
        return get_profile(user_id)
    sets.append("updated_at=?")
    params.append(_now())
    params.append(user_id)
    with connect() as db:
        db.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", params)
    return get_profile(user_id)


# ---- 等级 / 经验系统 ----
def grant_exp(user_id, amount, db=None):
    """增加经验，返回 (新总经验, 旧等级, 新等级)；可复用外部事务 db。"""
    amount = int(amount or 0)

    def _do(conn):
        row = conn.execute("SELECT exp FROM users WHERE id=?", (user_id,)).fetchone()
        old_exp = int(row["exp"] or 0) if row else 0
        old_lv = level_from_exp(old_exp)["level"]
        new_exp = old_exp + amount
        conn.execute("UPDATE users SET exp=? WHERE id=?", (new_exp, user_id))
        return new_exp, old_lv, level_from_exp(new_exp)["level"]

    if db is not None:
        return _do(db)
    with connect() as conn:
        return _do(conn)


def _ensure_daily_row(conn, user_id, today):
    row = conn.execute(
        "SELECT * FROM daily_progress WHERE user_id=? AND date=?", (user_id, today)
    ).fetchone()
    if row:
        return row
    conn.execute("INSERT INTO daily_progress(user_id,date) VALUES(?,?)", (user_id, today))
    return conn.execute(
        "SELECT * FROM daily_progress WHERE user_id=? AND date=?", (user_id, today)
    ).fetchone()


def record_action(user_id, action):
    """记录一次行为（login/review/add/interact），发即时经验并自动结算每日任务奖励。"""
    today = _today().isoformat()
    gained, finished_tasks, leveled = 0, [], None
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_daily_row(conn, user_id, today)
        if action == "login":
            if not row["login_done"]:
                conn.execute(
                    "UPDATE daily_progress SET login_done=1 WHERE user_id=? AND date=?",
                    (user_id, today))
        elif action in ("review", "add", "interact"):
            col = {"review": "reviews", "add": "adds", "interact": "interactions"}[action]
            expcol = {"review": "exp_review", "add": "exp_add",
                      "interact": "exp_interact"}[action]
            already = int(row[expcol] or 0)
            this_exp = min(ACTION_EXP[action], max(0, ACTION_DAILY_CAP[action] - already))
            conn.execute(
                f"UPDATE daily_progress SET {col}={col}+1, {expcol}={expcol}+? "
                "WHERE user_id=? AND date=?", (this_exp, user_id, today))
            if this_exp > 0:
                _, old_lv, new_lv = grant_exp(user_id, this_exp, db=conn)
                gained += this_exp
                if new_lv > old_lv:
                    leveled = [old_lv, new_lv]
        # 自动结算达成的每日任务
        row = conn.execute(
            "SELECT * FROM daily_progress WHERE user_id=? AND date=?", (user_id, today)
        ).fetchone()
        try:
            claimed = json.loads(row["claimed"] or "[]")
        except ValueError:
            claimed = []
        metrics = {"login_done": int(row["login_done"]), "reviews": int(row["reviews"]),
                   "adds": int(row["adds"]), "interactions": int(row["interactions"])}
        reward_total = 0
        for t in DAILY_TASKS:
            if t["key"] in claimed:
                continue
            if metrics.get(t["metric"], 0) >= t["target"]:
                claimed.append(t["key"])
                reward_total += t["reward"]
                finished_tasks.append({"key": t["key"], "label": t["label"],
                                       "reward": t["reward"]})
        if reward_total:
            _, old_lv, new_lv = grant_exp(user_id, reward_total, db=conn)
            gained += reward_total
            if new_lv > old_lv:
                leveled = [old_lv, new_lv]
        conn.execute("UPDATE daily_progress SET claimed=? WHERE user_id=? AND date=?",
                     (json.dumps(claimed, ensure_ascii=False), user_id, today))
        if action == "login":
            conn.execute("UPDATE users SET last_login_date=? WHERE id=?", (today, user_id))
        exp_row = conn.execute("SELECT exp FROM users WHERE id=?", (user_id,)).fetchone()
        total_exp = int(exp_row["exp"] or 0)
    return {"action": action, "gained": gained, "total_exp": total_exp,
            "finished_tasks": finished_tasks, "leveled": leveled}


def get_level_state(user_id):
    """返回等级/称号/经验进度 + 今日任务清单与完成情况。"""
    with connect() as conn:
        urow = conn.execute("SELECT exp FROM users WHERE id=?", (user_id,)).fetchone()
        total_exp = int(urow["exp"] or 0) if urow else 0
        today = _today().isoformat()
        drow = conn.execute(
            "SELECT * FROM daily_progress WHERE user_id=? AND date=?", (user_id, today)
        ).fetchone()
    lv = level_from_exp(total_exp)
    metrics = {"login_done": 0, "reviews": 0, "adds": 0, "interactions": 0}
    claimed = []
    instant = 0
    if drow:
        for k in metrics:
            metrics[k] = int(drow[k] or 0)
        try:
            claimed = json.loads(drow["claimed"] or "[]")
        except ValueError:
            claimed = []
        instant = int(drow["exp_review"] + drow["exp_add"] + drow["exp_interact"])
    tasks, done_count = [], 0
    for t in DAILY_TASKS:
        is_done = t["key"] in claimed
        done_count += 1 if is_done else 0
        tasks.append({"key": t["key"], "label": t["label"], "metric": t["metric"],
                      "target": t["target"], "reward": t["reward"],
                      "current": min(metrics.get(t["metric"], 0), t["target"]),
                      "done": is_done})
    task_reward_got = sum(t["reward"] for t in DAILY_TASKS if t["key"] in claimed)
    state = {"exp": total_exp}
    state.update(lv)
    state.update({"tasks": tasks, "done_count": done_count,
                  "task_total": len(DAILY_TASKS),
                  "gained_today": instant + task_reward_got, "metrics": metrics})
    return state


# ---- 用户级 AI 配置与每日免费额度 ----

def get_user_ai_config(user_id):
    """读取用户自己的 AI 配置；未配置或字段不全返回 None。"""
    with connect() as db:
        row = db.execute("SELECT ai_config FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["ai_config"]:
        return None
    try:
        cfg = json.loads(row["ai_config"])
    except ValueError:
        return None
    if not (str(cfg.get("api_key") or "").strip() and str(cfg.get("model") or "").strip()):
        return None
    return {
        "base_url": str(cfg.get("base_url") or "").strip(),
        "api_key": str(cfg.get("api_key") or "").strip(),
        "model": str(cfg.get("model") or "").strip(),
    }


def save_user_ai_config(user_id, cfg):
    """保存用户自己的 AI 配置；cfg 为 None 时清除（恢复使用站点默认）。"""
    payload = json.dumps(cfg, ensure_ascii=False) if cfg else ""
    with connect() as db:
        db.execute("UPDATE users SET ai_config=? WHERE id=?", (payload, user_id))


def daily_ai_quota(user_id):
    """邀请人每成功邀请 1 人 +10；被邀请人首次使用有效邀请码也 +10。"""
    with connect() as db:
        row = db.execute(
            "SELECT invite_bonus, invited_by FROM users WHERE id=?", (user_id,)
        ).fetchone()
    reward_units = (int(row["invite_bonus"] or 0) + (1 if row["invited_by"] else 0)) if row else 0
    return FREE_AI_CALLS_PER_DAY + INVITE_BONUS_CALLS * reward_units


def count_ai_calls_today(user_id):
    """统计今日通过站点默认模型发起的 AI 调用次数（用户自有 Key 的调用不计入）。

    created_at 存的是 UTC；以「本地日历日零点对应的 UTC 时刻」为起点，
    保证配额按用户本地日期重置。
    """
    local_midnight = (dt.datetime.now().astimezone()
                      .replace(hour=0, minute=0, second=0, microsecond=0)
                      .astimezone(dt.timezone.utc))
    cutoff = local_midnight.isoformat().replace("+00:00", "Z")
    with connect() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM user_events WHERE user_id=? AND event_type LIKE 'ai_%' "
            "AND created_at>=? AND COALESCE(json_extract(metadata,'$.own_config'),0)=0",
            (user_id, cutoff),
        ).fetchone()
    return int(row[0])


def invite_summary(user_id):
    with connect() as db:
        row = db.execute(
            "SELECT invite_code, invite_bonus, invited_by FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if not row:
        return {"invite_code": "", "invite_bonus": 0, "reward_units": 0,
                "received_invite_reward": False}
    invited_count = int(row["invite_bonus"] or 0)
    received = bool(row["invited_by"])
    return {"invite_code": row["invite_code"], "invite_bonus": invited_count,
            "reward_units": invited_count + (1 if received else 0),
            "received_invite_reward": received, "invited_by": row["invited_by"]}


def authenticate(username, password):
    identifier = str(username or "").strip().casefold()
    with connect() as db:
        username_row = db.execute(
            "SELECT * FROM users WHERE username_norm=?", (identifier,)
        ).fetchone()
        rows = ([username_row] if username_row else []) + list(db.execute(
            "SELECT * FROM users WHERE email_norm!='' AND email_norm=? ORDER BY created_at,id",
            (identifier,),
        ).fetchall())
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        actual = _password_digest(str(password or ""), row["password_salt"])
        if hmac.compare_digest(actual, row["password_hash"]):
            return _user_public(row)
    return None


def create_session(user_id, persistent=True):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    expires = now + dt.timedelta(days=SESSION_DAYS)
    with connect() as db:
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,expires_at,created_at,persistent) VALUES(?,?,?,?,?)",
            (token_hash, user_id, expires.isoformat().replace("+00:00", "Z"),
             now.isoformat().replace("+00:00", "Z"), 1 if persistent else 0),
        )
    return token


def refresh_session(token):
    """滑动续期：持久会话剩余有效期不足一半时延长至完整期限。

    返回 True 表示已续期（服务端应同步刷新客户端 Cookie）；
    非持久会话（未勾选自动登录）不续期，随浏览器关闭自然失效。
    """
    if not token:
        return False
    token_hash = hashlib.sha256(str(token).encode("ascii", "ignore")).hexdigest()
    now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    threshold = (now_dt + dt.timedelta(days=SESSION_RENEW_THRESHOLD_DAYS)).isoformat().replace("+00:00", "Z")
    with connect() as db:
        row = db.execute(
            "SELECT expires_at, persistent FROM sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()
        if not row or not row["persistent"] or row["expires_at"] > threshold:
            return False
        db.execute(
            "UPDATE sessions SET expires_at=? WHERE token_hash=?",
            ((now_dt + dt.timedelta(days=SESSION_DAYS)).isoformat().replace("+00:00", "Z"), token_hash),
        )
    return True


def user_for_session(token):
    if not token:
        return None
    token_hash = hashlib.sha256(str(token).encode("ascii", "ignore")).hexdigest()
    with connect() as db:
        row = db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (token_hash, _now()),
        ).fetchone()
    return _user_public(row) if row else None


def delete_session(token):
    if not token:
        return
    token_hash = hashlib.sha256(str(token).encode("ascii", "ignore")).hexdigest()
    with connect() as db:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))


def create_auth_ticket(email, purpose):
    """验证码校验后签发短期、一次性的账号选择票据。"""
    purpose = str(purpose or "").strip()
    if purpose not in {"login", "reset"}:
        raise ValueError("不支持的验证用途。")
    email_norm = str(email or "").strip().casefold()
    if not users_by_email(email_norm):
        raise ValueError("该邮箱尚未绑定账号。")
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    expires = now_dt + dt.timedelta(minutes=AUTH_TICKET_TTL_MINUTES)
    with connect() as db:
        db.execute("DELETE FROM auth_tickets WHERE expires_at<=? OR used_at IS NOT NULL", (_now(),))
        db.execute(
            "INSERT INTO auth_tickets(token_hash,email_norm,purpose,created_at,expires_at) "
            "VALUES(?,?,?,?,?)",
            (token_hash, email_norm, purpose,
             now_dt.isoformat().replace("+00:00", "Z"),
             expires.isoformat().replace("+00:00", "Z")),
        )
    return token


def consume_auth_ticket(token, purpose, user_id):
    """消费账号选择票据，并校验所选账号确实属于已验证邮箱。"""
    token_hash = hashlib.sha256(str(token or "").encode("ascii", "ignore")).hexdigest()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        ticket = db.execute(
            "SELECT * FROM auth_tickets WHERE token_hash=? AND purpose=? "
            "AND used_at IS NULL AND expires_at>?",
            (token_hash, str(purpose or ""), _now()),
        ).fetchone()
        if not ticket:
            raise ValueError("验证已失效，请重新获取验证码。")
        row = db.execute(
            "SELECT * FROM users WHERE id=? AND email_norm=?",
            (str(user_id or ""), ticket["email_norm"]),
        ).fetchone()
        if not row:
            raise ValueError("所选账号与已验证邮箱不匹配。")
        db.execute("UPDATE auth_tickets SET used_at=? WHERE token_hash=?", (_now(), token_hash))
    return _user_public(row)


def update_password(user_id, new_password):
    password = str(new_password or "")
    validate_password(password, "新密码")
    salt = secrets.token_hex(16)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM users WHERE id=?", (str(user_id or ""),)).fetchone():
            raise ValueError("账号不存在。")
        db.execute(
            "UPDATE users SET password_salt=?,password_hash=?,updated_at=? WHERE id=?",
            (salt, _password_digest(password, salt), _now(), str(user_id)),
        )
        db.execute("DELETE FROM sessions WHERE user_id=?", (str(user_id),))
    return True


# ==================== 注册邮箱验证码 ====================

EMAIL_CODE_TTL_MINUTES = 10
EMAIL_CODE_RESEND_SECONDS = 60
EMAIL_CODE_MAX_PER_HOUR = 5
EMAIL_CODE_MAX_ATTEMPTS = 5


def _code_digest(code, email_norm):
    return hashlib.sha256(
        ("sqlwb-code:%s:%s" % (email_norm, str(code))).encode("utf-8")
    ).hexdigest()


def create_email_code(email, purpose="register", ip=""):
    """生成并发号：返回 (code, expires_at)。违反重发间隔/每小时上限时抛 ValueError。"""
    email_norm = str(email or "").strip().casefold()
    if not email_norm:
        raise ValueError("请先填写邮箱。")
    code = "%06d" % secrets.randbelow(1_000_000)
    now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now = now_dt.isoformat().replace("+00:00", "Z")
    expires = (now_dt + dt.timedelta(minutes=EMAIL_CODE_TTL_MINUTES)).isoformat().replace("+00:00", "Z")
    with connect() as db:
        db.execute(
            "DELETE FROM email_codes WHERE expires_at<=? OR used_at IS NOT NULL AND created_at<=?",
            (now, (now_dt - dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")),
        )
        recent = db.execute(
            "SELECT created_at FROM email_codes WHERE email_norm=? AND purpose=? AND used_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (email_norm, purpose),
        ).fetchone()
        if recent and recent["created_at"] > (
            now_dt - dt.timedelta(seconds=EMAIL_CODE_RESEND_SECONDS)).isoformat().replace("+00:00", "Z"):
            raise ValueError("验证码发送太频繁，请 %d 秒后再试。" % EMAIL_CODE_RESEND_SECONDS)
        hourly = db.execute(
            "SELECT COUNT(*) FROM email_codes WHERE email_norm=? AND purpose=? AND created_at>?",
            (email_norm, purpose,
             (now_dt - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")),
        ).fetchone()[0]
        if hourly >= EMAIL_CODE_MAX_PER_HOUR:
            raise ValueError("该邮箱验证码发送次数已达上限，请 1 小时后再试。")
        db.execute(
            "INSERT INTO email_codes(id,email_norm,code_hash,purpose,attempts,ip,created_at,expires_at) "
            "VALUES(?,?,?,?,0,?,?,?)",
            (str(uuid.uuid4()), email_norm, _code_digest(code, email_norm), purpose,
             str(ip or "")[:45], now, expires),
        )
    return code, expires


def verify_email_code(email, code, purpose="register"):
    """校验验证码：成功标记已用；失败/过期/超次抛 ValueError。"""
    email_norm = str(email or "").strip().casefold()
    code = str(code or "").strip()
    now = _now()
    with connect() as db:
        row = db.execute(
            "SELECT * FROM email_codes WHERE email_norm=? AND purpose=? AND used_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (email_norm, purpose),
        ).fetchone()
        if not row or row["expires_at"] <= now:
            raise ValueError("验证码无效或已过期，请重新获取。")
        if row["attempts"] >= EMAIL_CODE_MAX_ATTEMPTS:
            raise ValueError("验证码错误次数过多，请重新获取。")
        if not hmac.compare_digest(row["code_hash"], _code_digest(code, email_norm)):
            db.execute("UPDATE email_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
            raise ValueError("验证码不正确，请核对后重新输入。")
        db.execute("UPDATE email_codes SET used_at=? WHERE id=?", (now, row["id"]))
    return True


def _parse_frontmatter_text(content):
    """解析 Markdown 文本中的 frontmatter，返回 (meta, body)。"""
    meta, body = {}, content
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", content, re.S)
    if match:
        body = content[match.end():]
        for line in match.group(1).splitlines():
            item = re.match(r"^\s*([^:：]+?)\s*[:：]\s*(.*)$", line)
            if item:
                meta[item.group(1).strip()] = item.group(2).strip()
    return meta, body.strip()


def _parse_frontmatter(path):
    with open(path, encoding="utf-8") as f:
        return _parse_frontmatter_text(f.read())


def _parse_dates(value):
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[,，;；\s]+", str(value or ""))
    return [x.strip() for x in values if str(x).strip()]


def _question_values(data):
    first_date = _date_text(data.get("date") or data.get("first_wrong_date"))
    status = str(data.get("status") or "未掌握")
    next_due = data.get("next_review_at") or (
        (dt.date.fromisoformat(first_date) + dt.timedelta(days=7)).isoformat()
        if status == "已掌握" else _today().isoformat()
    )
    body_md = str(data.get("body_md") or data.get("body") or "").strip()
    summary = str(data.get("summary") or "").strip()
    title = str(data.get("title") or "未命名错题").strip()
    cat = str(data.get("cat") or "未分类").strip()
    no = str(data.get("no") or "SQL???").strip()
    error_type = str(data.get("errtype") or data.get("error_type") or "其他").strip()
    search_text = " ".join((no, title, cat, error_type, summary, re.sub(r"\s+", " ", body_md)))
    try:
        wrong_times = max(1, int(data.get("times") or data.get("wrong_times") or 1))
    except (TypeError, ValueError):
        wrong_times = 1
    return {
        "no": no,
        "title": title,
        "cat": cat,
        "diff": str(data.get("diff") or "简单").strip(),
        "first_wrong_date": first_date,
        "source": str(data.get("src") or data.get("source") or "").strip(),
        "error_type": error_type,
        "status": status if status in {"未掌握", "复习中", "已掌握"} else "未掌握",
        "wrong_times": wrong_times,
        "rewrong_dates": json.dumps(_parse_dates(data.get("redates") or data.get("rewrong_dates")), ensure_ascii=False),
        "summary": summary,
        "body_md": body_md,
        "search_text": search_text,
        "next_review_at": _date_text(next_due),
    }


def _log_change(db, user_id, entity_id, operation, version, entity_type="question"):
    db.execute(
        "INSERT INTO changes(user_id,entity_type,entity_id,operation,version,changed_at) VALUES(?,?,?,?,?,?)",
        (user_id, entity_type, entity_id, operation, version, _now()),
    )


def create_question(user_id, data, source_file=None, question_id=None, notebook_id=None):
    values = _question_values(data)
    question_id = question_id or str(uuid.uuid4())
    now = _now()
    with connect() as db:
        nb_id = resolve_notebook_id(db, user_id, notebook_id or data.get("notebook_id"))
        db.execute(
            """INSERT INTO questions(
              id,user_id,source_file,no,title,cat,diff,first_wrong_date,source,error_type,status,
              wrong_times,rewrong_dates,summary,body_md,search_text,next_review_at,notebook_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                question_id, user_id, source_file, values["no"], values["title"], values["cat"],
                values["diff"], values["first_wrong_date"], values["source"], values["error_type"],
                values["status"], values["wrong_times"], values["rewrong_dates"], values["summary"],
                values["body_md"], values["search_text"], values["next_review_at"], nb_id, now, now,
            ),
        )
        _log_change(db, user_id, question_id, "upsert", 1)
    return question_id


def update_question(user_id, question_id, data, expected_version=None):
    values = _question_values(data)
    now = _now()
    with connect() as db:
        current = db.execute(
            "SELECT version FROM questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user_id),
        ).fetchone()
        if not current:
            raise KeyError("错题不存在。")
        if expected_version is not None and int(expected_version) != current["version"]:
            raise RuntimeError("VERSION_CONFLICT")
        version = current["version"] + 1
        db.execute(
            """UPDATE questions SET no=?,title=?,cat=?,diff=?,first_wrong_date=?,source=?,error_type=?,status=?,
              wrong_times=?,rewrong_dates=?,summary=?,body_md=?,search_text=?,next_review_at=?,version=?,updated_at=?
              WHERE id=? AND user_id=?""",
            (
                values["no"], values["title"], values["cat"], values["diff"], values["first_wrong_date"],
                values["source"], values["error_type"], values["status"], values["wrong_times"],
                values["rewrong_dates"], values["summary"], values["body_md"], values["search_text"],
                values["next_review_at"], version, now, question_id, user_id,
            ),
        )
        if data.get("notebook_id"):
            db.execute(
                "UPDATE questions SET notebook_id=? WHERE id=? AND user_id=?",
                (resolve_notebook_id(db, user_id, data.get("notebook_id")), question_id, user_id),
            )
        _log_change(db, user_id, question_id, "upsert", version)
    return version


def set_question_status(user_id, question_id, status):
    if status not in {"未掌握", "复习中", "已掌握"}:
        raise ValueError("状态不合法。")
    with connect() as db:
        row = db.execute(
            "SELECT version FROM questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user_id),
        ).fetchone()
        if not row:
            raise KeyError("错题不存在。")
        version = row["version"] + 1
        db.execute(
            "UPDATE questions SET status=?,version=?,updated_at=? WHERE id=? AND user_id=?",
            (status, version, _now(), question_id, user_id),
        )
        _log_change(db, user_id, question_id, "upsert", version)
    return version


def soft_delete_question(user_id, question_id):
    with connect() as db:
        row = db.execute(
            "SELECT version FROM questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user_id),
        ).fetchone()
        if not row:
            raise KeyError("错题不存在。")
        version = row["version"] + 1
        now = _now()
        db.execute(
            "UPDATE questions SET deleted_at=?,version=?,updated_at=? WHERE id=? AND user_id=?",
            (now, version, now, question_id, user_id),
        )
        _log_change(db, user_id, question_id, "delete", version)
    return version


def _row_to_question(row, include_body=True):
    item = {
        "id": row["id"],
        "file": row["id"],
        "no": row["no"],
        "title": row["title"],
        "cat": row["cat"],
        "diff": row["diff"],
        "date": row["first_wrong_date"],
        "src": row["source"],
        "errtype": row["error_type"],
        "status": row["status"],
        "times": row["wrong_times"],
        "redates": json.loads(row["rewrong_dates"] or "[]"),
        "summary": row["summary"],
        "text": row["search_text"],
        "next_review_at": row["next_review_at"],
        "last_review_at": row["last_review_at"],
        "interval_days": row["interval_days"],
        "repetitions": row["repetitions"],
        "lapses": row["lapses"],
        "version": row["version"],
        "updated_at": row["updated_at"],
        "notebook_id": row["notebook_id"] if "notebook_id" in row.keys() else None,
        "deleted": bool(row["deleted_at"]),
    }
    if include_body:
        item["body_md"] = row["body_md"]
    return item


DEFAULT_NOTEBOOK_NAME = "默认错题本"
NOTEBOOK_COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444",
                   "#8b5cf6", "#ec4899", "#14b8a6", "#f97316"]


def _ensure_default_notebook(db, user_id, now=None):
    """在给定连接/事务内确保用户至少有一个错题本，返回 (本id, 是否新建)。"""
    row = db.execute(
        "SELECT id FROM notebooks WHERE user_id=? AND archived_at IS NULL "
        "ORDER BY sort_order,created_at,id LIMIT 1",
        (str(user_id),),
    ).fetchone()
    if row:
        return row["id"], False
    nb_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO notebooks(id,user_id,name,color,icon,sort_order,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (nb_id, str(user_id), DEFAULT_NOTEBOOK_NAME, "#3b82f6", "📘", 0, now or _now()),
    )
    return nb_id, True


def _get_owned_notebook(db, user_id, nb_id):
    return db.execute(
        "SELECT * FROM notebooks WHERE id=? AND user_id=? AND archived_at IS NULL",
        (str(nb_id), str(user_id)),
    ).fetchone()


def resolve_notebook_id(db, user_id, nb_id):
    """返回归属该用户的有效错题本 id；传入为空或非法时回退到默认本。"""
    if nb_id:
        row = _get_owned_notebook(db, user_id, nb_id)
        if row:
            return row["id"]
    default_id, _ = _ensure_default_notebook(db, user_id)
    return default_id


def list_notebooks(user_id):
    with connect() as db:
        rows = db.execute(
            "SELECT n.*, (SELECT COUNT(*) FROM questions q WHERE q.notebook_id=n.id "
            "AND q.deleted_at IS NULL) AS cnt FROM notebooks n "
            "WHERE n.user_id=? AND n.archived_at IS NULL ORDER BY n.sort_order,n.created_at,n.id",
            (str(user_id),),
        ).fetchall()
        return [{
            "id": r["id"], "name": r["name"], "color": r["color"], "icon": r["icon"],
            "sort_order": r["sort_order"], "count": int(r["cnt"]),
        } for r in rows]


def create_notebook(user_id, name, color=None, icon=None):
    name = str(name or "").strip()
    if not name:
        raise ValueError("错题本名称不能为空。")
    if len(name) > 30:
        raise ValueError("名称最多 30 个字符。")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        cnt = db.execute(
            "SELECT COUNT(*) FROM notebooks WHERE user_id=? AND archived_at IS NULL",
            (str(user_id),),
        ).fetchone()[0]
        if cnt >= 30:
            raise ValueError("最多创建 30 个错题本。")
        dup = db.execute(
            "SELECT 1 FROM notebooks WHERE user_id=? AND name=? AND archived_at IS NULL",
            (str(user_id), name),
        ).fetchone()
        if dup:
            raise ValueError("已存在同名错题本。")
        nb_id = str(uuid.uuid4())
        picked = (str(color).strip() if color else "") or NOTEBOOK_COLORS[cnt % len(NOTEBOOK_COLORS)]
        picked_icon = (str(icon).strip()[:4] if icon else "") or "📘"
        db.execute(
            "INSERT INTO notebooks(id,user_id,name,color,icon,sort_order,created_at) VALUES(?,?,?,?,?,?,?)",
            (nb_id, str(user_id), name, picked[:20], picked_icon, cnt, _now()),
        )
    return nb_id


def update_notebook(user_id, nb_id, name=None, color=None, icon=None):
    sets, params = [], []
    if name is not None:
        name = str(name or "").strip()
        if not name:
            raise ValueError("名称不能为空。")
        if len(name) > 30:
            raise ValueError("名称最多 30 个字符。")
        sets.append("name=?"); params.append(name)
    if color is not None and str(color).strip():
        sets.append("color=?"); params.append(str(color).strip()[:20])
    if icon is not None and str(icon).strip():
        sets.append("icon=?"); params.append(str(icon).strip()[:4])
    if not sets:
        return
    params.extend([str(nb_id), str(user_id)])
    with connect() as db:
        row = _get_owned_notebook(db, user_id, nb_id)
        if not row:
            raise KeyError("错题本不存在。")
        if name is not None:
            dup = db.execute(
                "SELECT 1 FROM notebooks WHERE user_id=? AND name=? AND id<>? AND archived_at IS NULL",
                (str(user_id), name, str(nb_id)),
            ).fetchone()
            if dup:
                raise ValueError("已存在同名错题本。")
        db.execute("UPDATE notebooks SET " + ",".join(sets) + " WHERE id=? AND user_id=?", params)


def delete_notebook(user_id, nb_id):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = _get_owned_notebook(db, user_id, nb_id)
        if not row:
            raise KeyError("错题本不存在。")
        total = db.execute(
            "SELECT COUNT(*) FROM notebooks WHERE user_id=? AND archived_at IS NULL",
            (str(user_id),),
        ).fetchone()[0]
        if total <= 1:
            raise ValueError("至少保留一个错题本，不能删除。")
        cnt = db.execute(
            "SELECT COUNT(*) FROM questions WHERE notebook_id=? AND user_id=? AND deleted_at IS NULL",
            (str(nb_id), str(user_id)),
        ).fetchone()[0]
        if cnt:
            raise ValueError("该错题本里还有 %d 道题，请先把题目移到其它错题本后再删除。" % cnt)
        db.execute("DELETE FROM notebooks WHERE id=? AND user_id=?", (str(nb_id), str(user_id)))


def move_questions(user_id, question_ids, target_notebook_id):
    """把若干道题移动到目标错题本，返回实际移动条数。"""
    ids = [str(x) for x in (question_ids or []) if x]
    if not ids:
        return 0
    with connect() as db:
        target = resolve_notebook_id(db, user_id, target_notebook_id)
        placeholders = ",".join("?" * len(ids))
        cur = db.execute(
            "UPDATE questions SET notebook_id=?,updated_at=? WHERE user_id=? AND deleted_at IS NULL "
            "AND id IN (%s)" % placeholders,
            [target, _now(), str(user_id)] + ids,
        )
        return cur.rowcount


def list_questions(user_id, notebook_id=None):
    sql = "SELECT * FROM questions WHERE user_id=? AND deleted_at IS NULL"
    params = [user_id]
    if notebook_id and notebook_id != "all":
        sql += " AND notebook_id=?"
        params.append(notebook_id)
    sql += " ORDER BY first_wrong_date DESC,title"
    with connect() as db:
        rows = db.execute(sql, params).fetchall()
    return [_row_to_question(row) for row in rows]


def get_question(user_id, question_id, include_deleted=False):
    sql = "SELECT * FROM questions WHERE id=? AND user_id=?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with connect() as db:
        row = db.execute(sql, (question_id, user_id)).fetchone()
    return _row_to_question(row) if row else None


def due_questions(user_id, limit=20, notebook_id=None):
    limit = min(100, max(1, int(limit or 20)))
    nb_clause, nb_params = "", []
    if notebook_id and notebook_id != "all":
        nb_clause = " AND notebook_id=?"
        nb_params = [notebook_id]
    today = _today().isoformat()
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM questions WHERE user_id=? AND deleted_at IS NULL AND next_review_at<=? "
            + nb_clause +
            " ORDER BY next_review_at, lapses DESC, first_wrong_date LIMIT ?",
            [user_id, today] + nb_params + [limit],
        ).fetchall()
        reviewed_today = db.execute(
            "SELECT COUNT(*) FROM review_events r JOIN questions q ON q.id=r.question_id "
            "WHERE r.user_id=? AND substr(r.created_at,1,10)=?" +
            (" AND q.notebook_id=?" if nb_params else ""),
            [user_id, today] + nb_params,
        ).fetchone()[0]
        due_total = db.execute(
            "SELECT COUNT(*) FROM questions WHERE user_id=? AND deleted_at IS NULL AND next_review_at<=?" + nb_clause,
            [user_id, today] + nb_params,
        ).fetchone()[0]
    return [_row_to_question(row) for row in rows], {"due": due_total, "reviewed_today": reviewed_today}


def _schedule(row, rating):
    rating = int(rating)
    if rating not in (0, 1, 2, 3):
        raise ValueError("评分只能是 0–3。")
    interval = int(row["interval_days"] or 0)
    repetitions = int(row["repetitions"] or 0)
    lapses = int(row["lapses"] or 0)
    ease = float(row["ease"] or 2.5)
    status = "复习中"
    if rating == 0:
        interval, repetitions, lapses = 1, 0, lapses + 1
        ease = max(1.3, ease - 0.2)
        status = "未掌握"
    elif rating == 1:
        interval = max(1, round(max(1, interval) * 1.2))
        ease = max(1.3, ease - 0.15)
    elif rating == 2:
        interval = 1 if repetitions == 0 else 3 if repetitions == 1 else max(4, round(interval * ease))
        repetitions += 1
        status = "已掌握" if repetitions >= 4 else "复习中"
    else:
        interval = 4 if repetitions == 0 else max(7, round(max(1, interval) * (ease + 0.3)))
        repetitions += 1
        ease = min(3.2, ease + 0.1)
        status = "已掌握" if repetitions >= 3 else "复习中"
    due = (_today() + dt.timedelta(days=interval)).isoformat()
    return interval, repetitions, lapses, ease, status, due


def record_review(user_id, question_id, rating, device_id="", event_id=None):
    with connect() as db:
        if event_id:
            existing = db.execute(
                "SELECT id,next_due,next_interval FROM review_events WHERE id=? AND user_id=?",
                (event_id, user_id),
            ).fetchone()
            if existing:
                return {"event_id": existing["id"], "question_id": question_id,
                        "next_review_at": existing["next_due"], "interval_days": existing["next_interval"],
                        "idempotent": True}
        row = db.execute(
            "SELECT * FROM questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user_id),
        ).fetchone()
        if not row:
            raise KeyError("错题不存在。")
        interval, repetitions, lapses, ease, status, next_due = _schedule(row, rating)
        now = _now()
        version = row["version"] + 1
        db.execute(
            """UPDATE questions SET interval_days=?,repetitions=?,lapses=?,ease=?,status=?,next_review_at=?,
              last_review_at=?,version=?,updated_at=? WHERE id=? AND user_id=?""",
            (interval, repetitions, lapses, ease, status, next_due, now, version, now, question_id, user_id),
        )
        event_id = event_id or str(uuid.uuid4())
        db.execute(
            """INSERT INTO review_events(id,user_id,question_id,rating,previous_due,next_due,previous_interval,
              next_interval,device_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (event_id, user_id, question_id, int(rating), row["next_review_at"], next_due,
             row["interval_days"], interval, str(device_id or "")[:80], now),
        )
        _log_change(db, user_id, question_id, "upsert", version)
        _log_change(db, user_id, event_id, "create", 1, "review")
    return {
        "event_id": event_id,
        "question_id": question_id,
        "status": status,
        "next_review_at": next_due,
        "interval_days": interval,
        "version": version,
    }


def review_history(user_id, limit=100):
    limit = min(500, max(1, int(limit or 100)))
    with connect() as db:
        rows = db.execute(
            """SELECT r.*,q.no,q.title,q.cat FROM review_events r
               JOIN questions q ON q.id=r.question_id
               WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def sync_pull(user_id, since=0, limit=500):
    since = max(0, int(since or 0))
    limit = min(1000, max(1, int(limit or 500)))
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM changes WHERE user_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (user_id, since, limit),
        ).fetchall()
        payload = []
        cursor = since
        for row in rows:
            cursor = row["sequence"]
            item = dict(row)
            if row["entity_type"] == "question":
                question = db.execute(
                    "SELECT * FROM questions WHERE id=? AND user_id=?",
                    (row["entity_id"], user_id),
                ).fetchone()
                item["record"] = _row_to_question(question) if question else None
            else:
                review = db.execute(
                    "SELECT * FROM review_events WHERE id=? AND user_id=?",
                    (row["entity_id"], user_id),
                ).fetchone()
                item["record"] = dict(review) if review else None
            payload.append(item)
    return {"cursor": cursor, "changes": payload, "server_time": _now(), "has_more": len(rows) == limit}


def sync_push(user_id, records):
    applied, conflicts = [], []
    for record in records or []:
        if record.get("entity_type") == "review":
            data = record.get("record") or record
            try:
                result = record_review(
                    user_id, str(data.get("question_id") or ""), data.get("rating"),
                    data.get("device_id") or "", event_id=str(record.get("id") or data.get("id") or uuid.uuid4())
                )
                applied.append({"id": result["event_id"], "version": 1, "operation": "create", "entity_type": "review"})
            except (KeyError, ValueError) as exc:
                conflicts.append({"id": record.get("id"), "error": str(exc), "entity_type": "review"})
            continue
        question_id = str(record.get("id") or uuid.uuid4())
        operation = record.get("operation") or "upsert"
        current = get_question(user_id, question_id, include_deleted=True)
        expected = record.get("base_version")
        if current and expected is not None and int(expected) != current["version"]:
            conflicts.append({"id": question_id, "server": current})
            continue
        if operation == "delete":
            if current and not current["deleted"]:
                version = soft_delete_question(user_id, question_id)
                applied.append({"id": question_id, "version": version, "operation": "delete"})
            continue
        data = record.get("record") or record
        try:
            if current:
                version = update_question(user_id, question_id, data, expected_version=expected)
            else:
                create_question(user_id, data, question_id=question_id)
                version = 1
            applied.append({"id": question_id, "version": version, "operation": "upsert"})
        except RuntimeError:
            conflicts.append({"id": question_id, "server": get_question(user_id, question_id, True)})
    return {"applied": applied, "conflicts": conflicts}


def _meta_to_data(meta, body):
    """frontmatter 元数据 + 正文 → create_question 所需的 data 字典。"""
    return {
        "no": meta.get("题号"), "title": meta.get("标题"), "cat": meta.get("知识点"),
        "diff": meta.get("难度"), "date": meta.get("日期"), "src": meta.get("来源"),
        "errtype": meta.get("错误类型"), "status": meta.get("状态"),
        "times": meta.get("做错次数"), "redates": meta.get("重错日期"),
        "summary": meta.get("一句话总结"), "body_md": body,
    }


def import_markdown_content(user_id, name, content, notebook_id=None):
    """导入一段 Markdown 错题文本（含或不含 frontmatter）。

    以「用户 + 文件名 + 内容哈希」生成固定 ID：重复导入相同文件自动跳过；
    返回 (question_id, created)，created=False 表示该内容此前已导入过。
    正文为空时抛 ValueError。
    """
    meta, body = _parse_frontmatter_text(content)
    if not body.strip():
        raise ValueError("正文为空，无法导入。")
    question_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, "sql-wrongbook-import:%s:%s:%s" % (
            user_id, name, hashlib.sha256(content.encode("utf-8")).hexdigest()[:16])))
    try:
        create_question(user_id, _meta_to_data(meta, body), question_id=question_id, notebook_id=notebook_id)
    except sqlite3.IntegrityError:
        return question_id, False
    return question_id, True


def legacy_questions_available():
    """是否满足「首个账号自动导入本地错题库」的条件：尚无任何账号，且 错题库/ 下有可导入的 Markdown。"""
    with connect() as db:
        if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return False
    if not os.path.isdir(QUIZ_DIR):
        return False
    for _dirpath, _dirs, names in os.walk(QUIZ_DIR):
        for name in names:
            if name.lower().endswith(".md") and not name.startswith("_"):
                return True
    return False


def import_legacy_questions(user_id):
    if not os.path.isdir(QUIZ_DIR):
        return 0
    imported = 0
    for dirpath, _dirs, names in os.walk(QUIZ_DIR):
        for name in sorted(names):
            if not name.lower().endswith(".md") or name.startswith("_"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT)
            meta, body = _parse_frontmatter(path)
            try:
                create_question(user_id, _meta_to_data(meta, body), source_file=rel,
                                question_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "sql-wrongbook:" + rel)))
                imported += 1
            except sqlite3.IntegrityError:
                continue
    return imported


# ==================== 用户行为分析 ====================

def _community_user(row):
    keys = row.keys()
    exp = int(row["exp"]) if "exp" in keys and row["exp"] is not None else 0
    lv = level_from_exp(exp)
    return {
        "id": row["author_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "avatar": row["avatar"],
        "bio": row["bio"],
        "level": lv["level"], "title": lv["title"], "tier": lv["tier"],
        "color1": lv["color1"], "color2": lv["color2"],
    }


def list_community_posts(viewer_id="", limit=30, author_id=""):
    try:
        limit = min(50, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = 30
    params = [str(viewer_id or "")]
    where = "p.deleted_at IS NULL"
    if author_id:
        where += " AND p.user_id=?"
        params.append(str(author_id))
    params.append(limit)
    with connect() as db:
        rows = db.execute(
            """SELECT p.id,p.user_id AS author_id,p.content,p.created_at,p.updated_at,
                      u.username,u.display_name,u.avatar,u.bio,u.exp,
                      (SELECT COUNT(*) FROM community_likes l WHERE l.post_id=p.id) AS like_count,
                      (SELECT COUNT(*) FROM community_comments c WHERE c.post_id=p.id AND c.deleted_at IS NULL) AS comment_count,
                      (SELECT COUNT(*) FROM community_shares s WHERE s.post_id=p.id) AS share_count,
                      EXISTS(SELECT 1 FROM community_likes ml WHERE ml.post_id=p.id AND ml.user_id=?) AS liked
               FROM community_posts p JOIN users u ON u.id=p.user_id
               WHERE """ + where + " ORDER BY p.created_at DESC LIMIT ?",
            params,
        ).fetchall()
        posts = []
        for row in rows:
            comments = db.execute(
                """SELECT c.id,c.content,c.created_at,u.id AS author_id,
                          u.username,u.display_name,u.avatar,u.bio,u.exp
                   FROM community_comments c JOIN users u ON u.id=c.user_id
                   WHERE c.post_id=? AND c.deleted_at IS NULL
                   ORDER BY c.created_at ASC LIMIT 50""",
                (row["id"],),
            ).fetchall()
            posts.append({
                "id": row["id"], "content": row["content"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "author": _community_user(row),
                "like_count": int(row["like_count"]),
                "comment_count": int(row["comment_count"]),
                "share_count": int(row["share_count"]),
                "liked": bool(row["liked"]),
                "comments": [{
                    "id": c["id"], "content": c["content"], "created_at": c["created_at"],
                    "author": _community_user(c),
                } for c in comments],
            })
    return posts


def create_community_post(user_id, content):
    content = str(content or "").strip()
    if not content:
        raise ValueError("帖子内容不能为空。")
    if len(content) > 1000:
        raise ValueError("帖子内容不能超过 1000 个字符。")
    post_id, now = str(uuid.uuid4()), _now()
    with connect() as db:
        db.execute(
            "INSERT INTO community_posts(id,user_id,content,created_at,updated_at) VALUES(?,?,?,?,?)",
            (post_id, str(user_id), content, now, now),
        )
    return post_id


def add_community_comment(user_id, post_id, content):
    content = str(content or "").strip()
    if not content:
        raise ValueError("评论内容不能为空。")
    if len(content) > 500:
        raise ValueError("评论不能超过 500 个字符。")
    comment_id, now = str(uuid.uuid4()), _now()
    with connect() as db:
        if not db.execute(
            "SELECT 1 FROM community_posts WHERE id=? AND deleted_at IS NULL", (str(post_id),)
        ).fetchone():
            raise ValueError("帖子不存在或已删除。")
        db.execute(
            "INSERT INTO community_comments(id,post_id,user_id,content,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (comment_id, str(post_id), str(user_id), content, now, now),
        )
    return comment_id


def toggle_community_like(user_id, post_id):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "SELECT 1 FROM community_posts WHERE id=? AND deleted_at IS NULL", (str(post_id),)
        ).fetchone():
            raise ValueError("帖子不存在或已删除。")
        existing = db.execute(
            "SELECT 1 FROM community_likes WHERE post_id=? AND user_id=?",
            (str(post_id), str(user_id)),
        ).fetchone()
        if existing:
            db.execute("DELETE FROM community_likes WHERE post_id=? AND user_id=?",
                       (str(post_id), str(user_id)))
            liked = False
        else:
            db.execute("INSERT INTO community_likes(post_id,user_id,created_at) VALUES(?,?,?)",
                       (str(post_id), str(user_id), _now()))
            liked = True
        count = db.execute(
            "SELECT COUNT(*) FROM community_likes WHERE post_id=?", (str(post_id),)
        ).fetchone()[0]
    return {"liked": liked, "like_count": int(count)}


def share_community_post(user_id, post_id):
    with connect() as db:
        if not db.execute(
            "SELECT 1 FROM community_posts WHERE id=? AND deleted_at IS NULL", (str(post_id),)
        ).fetchone():
            raise ValueError("帖子不存在或已删除。")
        db.execute(
            "INSERT OR IGNORE INTO community_shares(post_id,user_id,created_at) VALUES(?,?,?)",
            (str(post_id), str(user_id), _now()),
        )
        count = db.execute(
            "SELECT COUNT(*) FROM community_shares WHERE post_id=?", (str(post_id),)
        ).fetchone()[0]
    return {"share_count": int(count)}


def public_community_profile(user_id, viewer_id=""):
    with connect() as db:
        row = db.execute(
            "SELECT id AS author_id,username,display_name,avatar,bio,exp,created_at FROM users WHERE id=?",
            (str(user_id),),
        ).fetchone()
        if not row:
            return None
        post_count = db.execute(
            "SELECT COUNT(*) FROM community_posts WHERE user_id=? AND deleted_at IS NULL",
            (str(user_id),),
        ).fetchone()[0]
        comment_count = db.execute(
            "SELECT COUNT(*) FROM community_comments WHERE user_id=? AND deleted_at IS NULL",
            (str(user_id),),
        ).fetchone()[0]
        received_likes = db.execute(
            "SELECT COUNT(*) FROM community_likes l JOIN community_posts p ON p.id=l.post_id "
            "WHERE p.user_id=? AND p.deleted_at IS NULL", (str(user_id),)
        ).fetchone()[0]
    relation = friendship_status(viewer_id, user_id) if viewer_id else "none"
    return {
        "user": _community_user(row), "joined_at": row["created_at"],
        "relation": relation,
        "post_count": int(post_count), "comment_count": int(comment_count),
        "received_likes": int(received_likes),
        "posts": list_community_posts(viewer_id, limit=10, author_id=user_id),
    }


# ---- 好友系统 ----

def _friend_pair_key(a, b):
    a, b = str(a), str(b)
    return "|".join(sorted((a, b)))


def _friend_card_from_user(row, me_id, relation="none"):
    """users 行 -> 好友/用户卡片（带等级头衔与我对其关系）。"""
    if row is None:
        return None
    keys = row.keys()
    exp = int(row["exp"]) if "exp" in keys and row["exp"] is not None else 0
    lv = level_from_exp(exp)
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] if "display_name" in keys else "",
        "avatar": row["avatar"] if "avatar" in keys else "",
        "bio": row["bio"] if "bio" in keys else "",
        "invite_code": row["invite_code"] if "invite_code" in keys else "",
        "level": lv["level"], "title": lv["title"], "tier": lv["tier"],
        "color1": lv["color1"], "color2": lv["color2"],
        "is_self": row["id"] == str(me_id),
        "relation": relation,
    }


def _friend_card_join(row, me_id, relation, request_id=None):
    """从 friendships JOIN users 的显式别名行构造好友卡片（避免 f.* / u.* 的 id 列冲突）。"""
    exp = int(row["exp"] or 0)
    lv = level_from_exp(exp)
    rid = request_id if request_id is not None else (
        row["request_id"] if "request_id" in row.keys() else None)
    return {
        "id": row["user_id"], "username": row["username"],
        "display_name": row["display_name"] or "", "avatar": row["avatar"] or "",
        "bio": row["bio"] or "", "invite_code": row["invite_code"] or "",
        "level": lv["level"], "title": lv["title"], "tier": lv["tier"],
        "color1": lv["color1"], "color2": lv["color2"],
        "is_self": row["user_id"] == str(me_id), "relation": relation,
        "request_id": rid,
    }


def _friendship_row(db, a, b):
    return db.execute(
        "SELECT * FROM friendships WHERE pair_key=?", (_friend_pair_key(a, b),)
    ).fetchone()


def friendship_status(me_id, other_id):
    """返回 none / pending_out（我发出待对方同意）/ pending_in（对方发给我）/ accepted。"""
    me_id, other_id = str(me_id), str(other_id)
    if me_id == other_id:
        return "self"
    with connect() as db:
        row = _friendship_row(db, me_id, other_id)
    if not row:
        return "none"
    if row["status"] == "accepted":
        return "accepted"
    return "pending_out" if row["requester_id"] == me_id else "pending_in"


def friend_id_set(me_id):
    """已成为好友的对方 id 集合（共享权限校验用）。"""
    with connect() as db:
        rows = db.execute(
            "SELECT requester_id,addressee_id FROM friendships WHERE status='accepted' "
            "AND (requester_id=? OR addressee_id=?)",
            (str(me_id), str(me_id)),
        ).fetchall()
    out = set()
    for r in rows:
        out.add(r["addressee_id"] if r["requester_id"] == str(me_id) else r["requester_id"])
    return out


def _resolve_friend_target(db, me_id, target):
    target = str(target or "").strip()
    if not target:
        raise ValueError("请输入对方的用户名或邀请码。")
    row = db.execute(
        "SELECT * FROM users WHERE invite_code=? OR username_norm=? OR id=?",
        (target.upper(), target.casefold(), target),
    ).fetchone()
    if not row:
        raise ValueError("没有找到该用户，请核对用户名或邀请码。")
    if row["id"] == str(me_id):
        raise ValueError("不能添加自己为好友。")
    return row


def send_friend_request(me_id, target):
    now = _now()
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        other = _resolve_friend_target(db, me_id, target)
        existing = _friendship_row(db, me_id, other["id"])
        if existing:
            if existing["status"] == "accepted":
                raise ValueError("你们已经是好友了。")
            if existing["requester_id"] == str(me_id):
                raise ValueError("已经发送过申请，等待对方同意。")
            # 对方此前已向我发起申请：直接建立好友关系
            db.execute(
                "UPDATE friendships SET status='accepted',updated_at=? WHERE id=?",
                (now, existing["id"]),
            )
            return {"ok": True, "accepted": True}
        fid = str(uuid.uuid4())
        db.execute(
            "INSERT INTO friendships(id,requester_id,addressee_id,status,pair_key,created_at,updated_at) "
            "VALUES(?,?,?,'pending',?,?,?)",
            (fid, str(me_id), other["id"], _friend_pair_key(me_id, other["id"]), now, now),
        )
    return {"ok": True, "accepted": False}


def respond_friend_request(me_id, request_id, action):
    action = str(action or "").lower()
    if action not in ("accept", "reject"):
        raise ValueError("操作类型不合法。")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM friendships WHERE id=? AND addressee_id=? AND status='pending'",
            (str(request_id), str(me_id)),
        ).fetchone()
        if not row:
            raise KeyError("好友申请不存在或已处理。")
        if action == "accept":
            db.execute(
                "UPDATE friendships SET status='accepted',updated_at=? WHERE id=?",
                (_now(), request_id),
            )
        else:  # 拒绝：直接删除，便于以后重新发起
            db.execute("DELETE FROM friendships WHERE id=?", (request_id,))
    return {"ok": True}


def cancel_friend_request(me_id, request_id):
    with connect() as db:
        cur = db.execute(
            "DELETE FROM friendships WHERE id=? AND requester_id=? AND status='pending'",
            (str(request_id), str(me_id)),
        )
        if cur.rowcount == 0:
            raise KeyError("好友申请不存在。")
    return {"ok": True}


def remove_friend(me_id, friend_id):
    with connect() as db:
        cur = db.execute(
            "DELETE FROM friendships WHERE pair_key=? AND status='accepted'",
            (_friend_pair_key(me_id, friend_id),),
        )
        if cur.rowcount == 0:
            raise KeyError("你们还不是好友。")
    return {"ok": True}


_FRIEND_JOIN_COLS = ("f.id AS request_id, u.id AS user_id, u.username, u.display_name, "
                      "u.avatar, u.bio, u.invite_code, u.exp")


def list_friends(me_id):
    with connect() as db:
        rows = db.execute(
            "SELECT " + _FRIEND_JOIN_COLS + " FROM friendships f JOIN users u ON "
            "(u.id=f.requester_id OR u.id=f.addressee_id) "
            "WHERE (f.requester_id=? OR f.addressee_id=?) AND u.id<>? AND f.status='accepted' "
            "ORDER BY u.display_name,u.username",
            (str(me_id), str(me_id), str(me_id)),
        ).fetchall()
    return [_friend_card_join(r, me_id, "accepted") for r in rows]


def list_friend_requests(me_id):
    with connect() as db:
        incoming = db.execute(
            "SELECT " + _FRIEND_JOIN_COLS + " FROM friendships f JOIN users u ON u.id=f.requester_id "
            "WHERE f.addressee_id=? AND f.status='pending' ORDER BY f.created_at DESC",
            (str(me_id),),
        ).fetchall()
        outgoing = db.execute(
            "SELECT " + _FRIEND_JOIN_COLS + " FROM friendships f JOIN users u ON u.id=f.addressee_id "
            "WHERE f.requester_id=? AND f.status='pending' ORDER BY f.created_at DESC",
            (str(me_id),),
        ).fetchall()
    return {
        "incoming": [_friend_card_join(r, me_id, "pending_in") for r in incoming],
        "outgoing": [_friend_card_join(r, me_id, "pending_out") for r in outgoing],
    }


def search_people(me_id, keyword, limit=10):
    kw = ("%" + str(keyword or "").strip() + "%")
    if kw == "%%":
        return []
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM users WHERE id<>? AND "
            "(username LIKE ? OR display_name LIKE ? OR invite_code=?) "
            "ORDER BY display_name,username LIMIT ?",
            (str(me_id), kw, kw, str(keyword or "").strip().upper(), limit),
        ).fetchall()
        result = []
        for r in rows:
            rel = "none"
            fr = _friendship_row(db, me_id, r["id"])
            if fr:
                if fr["status"] == "accepted":
                    rel = "accepted"
                elif fr["requester_id"] == str(me_id):
                    rel = "pending_out"
                else:
                    rel = "pending_in"
            result.append(_friend_card_from_user(r, me_id, rel))
    return result


def record_event(user_id=None, device_id="", event_type="", page="", metadata=None, ip="", user_agent=""):
    """记录用户行为事件。user_id 为 None 表示未登录用户。"""
    event_id = str(uuid.uuid4())
    now = _now()
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    with connect() as db:
        db.execute(
            "INSERT INTO user_events(id,user_id,device_id,event_type,page,metadata,ip,user_agent,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, user_id, str(device_id or "")[:80], str(event_type or "")[:50],
             str(page or "")[:200], meta_json, str(ip or "")[:45], str(user_agent or "")[:300], now),
        )
    return event_id


def _date_range(days):
    end = _today()
    start = end - dt.timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def get_daily_stats(days=30):
    """每日统计：页面浏览量、独立访客、注册数、活跃用户数、复习次数。"""
    start, end = _date_range(days)
    with connect() as db:
        rows = db.execute(
            """SELECT substr(created_at,1,10) as day,
                      COUNT(*) as total_events,
                      COUNT(DISTINCT COALESCE(user_id, 'anon_'||device_id)) as unique_visitors,
                      COUNT(DISTINCT user_id) as active_users,
                      SUM(CASE WHEN event_type='page_view' THEN 1 ELSE 0 END) as page_views,
                      SUM(CASE WHEN event_type='register' THEN 1 ELSE 0 END) as registrations,
                      SUM(CASE WHEN event_type='login' THEN 1 ELSE 0 END) as logins,
                      SUM(CASE WHEN event_type='review' THEN 1 ELSE 0 END) as reviews,
                      SUM(CASE WHEN event_type LIKE 'ai_%' THEN 1 ELSE 0 END) as ai_calls
               FROM user_events
               WHERE substr(created_at,1,10) BETWEEN ? AND ?
               GROUP BY day ORDER BY day""",
            (start, end),
        ).fetchall()
    # 填充没有数据的日期
    date_map = {r["day"]: dict(r) for r in rows}
    result = []
    current = dt.date.fromisoformat(start)
    end_date = dt.date.fromisoformat(end)
    while current <= end_date:
        day = current.isoformat()
        if day in date_map:
            result.append(date_map[day])
        else:
            result.append({"day": day, "total_events": 0, "unique_visitors": 0, "active_users": 0,
                           "page_views": 0, "registrations": 0, "logins": 0, "reviews": 0, "ai_calls": 0})
        current += dt.timedelta(days=1)
    return result


def get_retention(cohorts_days=14):
    """留存分析：按注册日分组，计算次日/3日/7日/14日留存率。"""
    with connect() as db:
        # 获取每个用户的注册日期
        users = db.execute(
            "SELECT id, substr(created_at,1,10) as reg_date FROM users ORDER BY created_at"
        ).fetchall()
        if not users:
            return []
        # 限制最近 N 天的 cohort
        cutoff = (_today() - dt.timedelta(days=cohorts_days - 1)).isoformat()
        cohorts = {}
        for u in users:
            reg = u["reg_date"]
            if reg < cutoff:
                continue
            if reg not in cohorts:
                cohorts[reg] = {"users": [], "size": 0}
            cohorts[reg]["users"].append(u["id"])
            cohorts[reg]["size"] += 1

        result = []
        for reg_date, info in sorted(cohorts.items()):
            user_ids = info["users"]
            size = info["size"]
            if size == 0:
                continue
            placeholders = ",".join("?" * len(user_ids))
            # 计算各天留存
            retention = {"cohort_date": reg_date, "cohort_size": size}
            for day_offset, label in [(1, "d1"), (3, "d3"), (7, "d7"), (14, "d14")]:
                target_date = (dt.date.fromisoformat(reg_date) + dt.timedelta(days=day_offset)).isoformat()
                if target_date > _today().isoformat():
                    retention[label] = None  # 还没到那天
                    retention[label + "_rate"] = None
                    continue
                row = db.execute(
                    f"SELECT COUNT(DISTINCT user_id) as cnt FROM user_events "
                    f"WHERE user_id IN ({placeholders}) AND substr(created_at,1,10)=? AND event_type!='register'",
                    (*user_ids, target_date),
                ).fetchone()
                retained = row["cnt"] if row else 0
                retention[label] = retained
                retention[label + "_rate"] = round(retained / size, 4) if size else 0
            result.append(retention)
    return result


def get_funnel():
    """行为漏斗：页面浏览 → 注册 → 添加错题 → 复习 → AI功能使用。"""
    with connect() as db:
        total_visitors = db.execute(
            "SELECT COUNT(DISTINCT COALESCE(user_id, 'anon_'||device_id)) as cnt FROM user_events WHERE event_type='page_view'"
        ).fetchone()["cnt"]
        registered = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
        added_question = db.execute(
            "SELECT COUNT(DISTINCT user_id) as cnt FROM user_events WHERE event_type='add_question'"
        ).fetchone()["cnt"]
        reviewed = db.execute(
            "SELECT COUNT(DISTINCT user_id) as cnt FROM user_events WHERE event_type='review'"
        ).fetchone()["cnt"]
        used_ai = db.execute(
            "SELECT COUNT(DISTINCT user_id) as cnt FROM user_events WHERE event_type LIKE 'ai_%'"
        ).fetchone()["cnt"]
    steps = [
        {"step": "页面浏览", "count": total_visitors, "rate_from_prev": 1.0},
        {"step": "注册账号", "count": registered, "rate_from_prev": round(registered / total_visitors, 4) if total_visitors else 0},
        {"step": "添加错题", "count": added_question, "rate_from_prev": round(added_question / registered, 4) if registered else 0},
        {"step": "进行复习", "count": reviewed, "rate_from_prev": round(reviewed / added_question, 4) if added_question else 0},
        {"step": "使用AI", "count": used_ai, "rate_from_prev": round(used_ai / reviewed, 4) if reviewed else 0},
    ]
    return steps


def get_event_breakdown(days=30):
    """事件类型分布统计。"""
    start, end = _date_range(days)
    with connect() as db:
        rows = db.execute(
            """SELECT event_type, COUNT(*) as cnt, COUNT(DISTINCT COALESCE(user_id, 'anon_'||device_id)) as unique_users
               FROM user_events
               WHERE substr(created_at,1,10) BETWEEN ? AND ?
               GROUP BY event_type ORDER BY cnt DESC""",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ai_usage(days=30):
    """AI功能使用情况：各功能调用次数、使用用户数、成功率。"""
    start, end = _date_range(days)
    with connect() as db:
        rows = db.execute(
            """SELECT event_type, COUNT(*) as cnt, COUNT(DISTINCT user_id) as users,
                      AVG(CAST(json_extract(metadata, '$.duration_ms') AS REAL)) as avg_duration
               FROM user_events
               WHERE event_type LIKE 'ai_%' AND substr(created_at,1,10) BETWEEN ? AND ?
               GROUP BY event_type ORDER BY cnt DESC""",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def get_analytics_summary(days=30):
    """汇总统计：总用户数、总错题数、总复习数、关键指标。"""
    start, end = _date_range(days)
    with connect() as db:
        total_users = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
        total_questions = db.execute(
            "SELECT COUNT(*) as cnt FROM questions WHERE deleted_at IS NULL"
        ).fetchone()["cnt"]
        total_reviews = db.execute("SELECT COUNT(*) as cnt FROM review_events").fetchone()["cnt"]
        period_visitors = db.execute(
            "SELECT COUNT(DISTINCT COALESCE(user_id, 'anon_'||device_id)) as cnt FROM user_events "
            "WHERE substr(created_at,1,10) BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"]
        period_registrations = db.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE substr(created_at,1,10) BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"]
        period_reviews = db.execute(
            "SELECT COUNT(*) as cnt FROM review_events WHERE substr(created_at,1,10) BETWEEN ? AND ?",
            (start, end),
        ).fetchone()["cnt"]
        mastered = db.execute(
            "SELECT COUNT(*) as cnt FROM questions WHERE status='已掌握' AND deleted_at IS NULL"
        ).fetchone()["cnt"]
    return {
        "total_users": total_users,
        "total_questions": total_questions,
        "total_reviews": total_reviews,
        "mastered_questions": mastered,
        "period_visitors": period_visitors,
        "period_registrations": period_registrations,
        "period_reviews": period_reviews,
        "period_days": days,
    }
