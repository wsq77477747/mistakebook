# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_QUIZ_DIR = os.path.join(ROOT, "tests", "fixtures", "legacy_questions")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = storage.DB_PATH
        self.original_quiz_dir = storage.QUIZ_DIR
        self.original_pbkdf2_iterations = storage.PBKDF2_ITERATIONS
        storage.DB_PATH = os.path.join(self.tmp.name, "test.db")
        storage.QUIZ_DIR = FIXTURE_QUIZ_DIR
        storage.PBKDF2_ITERATIONS = 1_000
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self.original_db_path
        storage.QUIZ_DIR = self.original_quiz_dir
        storage.PBKDF2_ITERATIONS = self.original_pbkdf2_iterations
        self.tmp.cleanup()

    def test_accounts_are_isolated_and_first_account_imports_legacy(self):
        first = storage.register_user("owner@example.com", "password123", "owner@example.com")
        second = storage.register_user("reader@example.com", "password456", "reader@example.com")
        self.assertGreaterEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertGreaterEqual(len(storage.list_questions(first["id"])), 1)
        self.assertEqual(storage.list_questions(second["id"]), [])
        self.assertIsNotNone(storage.authenticate("OWNER@example.com", "password123"))
        self.assertIsNone(storage.authenticate("owner@example.com", "wrong-password"))

    def test_register_requires_valid_email_and_limits_three_accounts(self):
        storage.register_user("owner", "password123", "owner@example.com")
        with self.assertRaisesRegex(ValueError, "至少需要 6"):
            storage.register_user("short-password", "a1!", "short-password@example.com")
        with self.assertRaisesRegex(ValueError, "至少两种"):
            storage.register_user("letters-only", "abcdef", "letters-only@example.com")
        with self.assertRaisesRegex(ValueError, "至少两种"):
            storage.register_user("digits-only", "123456", "digits-only@example.com")
        six_chars = storage.register_user("six-valid", "abc123", "six-valid@example.com")
        self.assertIsNotNone(storage.authenticate(six_chars["username"], "abc123"))
        with self.assertRaises(ValueError):
            storage.register_user("no-email", "password123", "")
        with self.assertRaises(ValueError):
            storage.register_user("bad-email", "password123", "not-an-email")
        storage.register_user("second-owner", "password456", "OWNER@Example.com")
        storage.register_user("third-owner", "password789", "owner@example.com")
        with self.assertRaisesRegex(ValueError, "最多只能注册"):
            storage.register_user("fourth-owner", "password000", "owner@example.com")
        self.assertEqual(storage.email_account_count("OWNER@example.com"), 3)
        authenticated = storage.authenticate("owner@example.com", "password123")
        self.assertIsNotNone(authenticated)
        self.assertEqual(authenticated["email"], "owner@example.com")

    def test_auth_ticket_password_reset_and_community(self):
        first = storage.register_user("community-one", "password123", "shared@example.com")
        second = storage.register_user("community-two", "password456", "shared@example.com")
        self.assertEqual(len(storage.users_by_email("SHARED@example.com")), 2)
        ticket = storage.create_auth_ticket("shared@example.com", "reset")
        selected = storage.consume_auth_ticket(ticket, "reset", second["id"])
        self.assertEqual(selected["username"], "community-two")
        with self.assertRaises(ValueError):
            storage.consume_auth_ticket(ticket, "reset", first["id"])
        storage.update_password(second["id"], "new-password-456")
        self.assertIsNotNone(storage.authenticate("community-two", "new-password-456"))
        self.assertIsNone(storage.authenticate("community-two", "password456"))
        post_id = storage.create_community_post(first["id"], "窗口函数如何复习？")
        storage.add_community_comment(second["id"], post_id, "先掌握分区和排序。")
        liked = storage.toggle_community_like(second["id"], post_id)
        self.assertTrue(liked["liked"])
        self.assertEqual(storage.share_community_post(second["id"], post_id)["share_count"], 1)
        posts = storage.list_community_posts(second["id"])
        self.assertEqual(posts[0]["comment_count"], 1)
        self.assertTrue(posts[0]["liked"])
        profile = storage.public_community_profile(first["id"], second["id"])
        self.assertEqual(profile["post_count"], 1)
        self.assertEqual(profile["received_likes"], 1)

    def test_existing_account_without_email_survives_schema_upgrade(self):
        legacy_db = os.path.join(self.tmp.name, "legacy.db")
        storage.DB_PATH = legacy_db
        salt = "00" * 16
        db = sqlite3.connect(legacy_db)
        try:
            db.execute(
                "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL, "
                "username_norm TEXT NOT NULL UNIQUE, password_salt TEXT NOT NULL, "
                "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",
                ("legacy-user", "legacy", "legacy", salt,
                 storage._password_digest("password123", salt), 1,
                 "2026-08-30T00:00:00Z", "2026-08-30T00:00:00Z"),
            )
            db.commit()
        finally:
            db.close()
        storage.init_db()
        authenticated = storage.authenticate("legacy", "password123")
        self.assertIsNotNone(authenticated)
        self.assertEqual(authenticated["email"], "")

    def test_email_code_create_verify_and_expiry(self):
        email = "coder@example.com"
        code, expires = storage.create_email_code(email)
        self.assertRegex(code, r"^\d{6}$")
        self.assertTrue(storage.verify_email_code(email, code))
        with self.assertRaises(ValueError):
            storage.verify_email_code(email, code)  # 已使用，不能重复消费
        storage.create_email_code(email)
        with self.assertRaises(ValueError):
            storage.verify_email_code(email, "000000" if code != "000000" else "111111")
        with self.assertRaises(ValueError):
            storage.verify_email_code("other@example.com", code)

    def test_email_code_resend_limit_and_hourly_cap(self):
        email = "flood@example.com"
        storage.create_email_code(email)
        with self.assertRaises(ValueError):
            storage.create_email_code(email)  # 60 秒重发间隔
        # 回拨 2 分钟：绕开重发间隔，但仍计入口上限
        with storage.connect() as db:
            db.execute("UPDATE email_codes SET created_at=?",
                       (self._shift_now(seconds=-120),))
        for _ in range(storage.EMAIL_CODE_MAX_PER_HOUR - 1):
            storage.create_email_code(email, purpose="register")
            with storage.connect() as db:
                db.execute("UPDATE email_codes SET created_at=?",
                           (self._shift_now(seconds=-120),))
        with self.assertRaises(ValueError):
            storage.create_email_code(email)  # 每小时上限

    @staticmethod
    def _shift_now(seconds):
        import datetime as _dt
        return (_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
                + _dt.timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")

    def test_email_code_expired_and_max_attempts(self):
        email = "expire@example.com"
        code, _ = storage.create_email_code(email)
        with storage.connect() as db:
            db.execute("UPDATE email_codes SET expires_at='2020-01-01T00:00:00Z', created_at=?",
                       (self._shift_now(seconds=-120),))
        with self.assertRaises(ValueError):
            storage.verify_email_code(email, code)  # 已过期
        storage.create_email_code(email)
        wrong = "000000"
        for _ in range(storage.EMAIL_CODE_MAX_ATTEMPTS):
            with self.assertRaises(ValueError):
                storage.verify_email_code(email, wrong)
        # 次数用尽后，即使再输入（无论对错）都必须重新获取
        with self.assertRaises(ValueError):
            storage.verify_email_code(email, "123456")

    def test_email_code_purpose_isolation(self):
        email = "purpose@example.com"
        code, _ = storage.create_email_code(email, purpose="register")
        with self.assertRaises(ValueError):
            storage.verify_email_code(email, code, purpose="login")  # 注册码不能用于登录
        login_code, _ = storage.create_email_code(email, purpose="login")
        self.assertTrue(storage.verify_email_code(email, login_code, purpose="login"))

    def test_user_by_email_case_insensitive(self):
        storage.register_user("byemail", "password123", "ByEmail@Example.com")
        user = storage.user_by_email("byemail@example.com")
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "byemail")
        self.assertIsNotNone(storage.user_by_email("BYEMAIL@EXAMPLE.COM"))
        self.assertIsNone(storage.user_by_email("nobody@example.com"))
        self.assertIsNone(storage.user_by_email(""))

    def test_invite_code_credit_and_quota(self):
        inviter = storage.register_user("inviter", "password123", "inviter@example.com")
        self.assertTrue(inviter["invite_code"])
        self.assertEqual(storage.daily_ai_quota(inviter["id"]), storage.FREE_AI_CALLS_PER_DAY)
        # 无效邀请码被忽略，不影响注册
        guest = storage.register_user("guest", "password123", "guest@example.com", invite_code="BADCODE1")
        self.assertFalse(guest["invite_applied"])
        # 有效邀请码：邀请人与被邀请人双方额度都 +10
        friend = storage.register_user("friend", "password123", "friend@example.com",
                                       invite_code=inviter["invite_code"])
        self.assertTrue(friend["invite_applied"])
        summary = storage.invite_summary(inviter["id"])
        self.assertEqual(summary["invite_bonus"], 1)
        self.assertEqual(storage.daily_ai_quota(inviter["id"]),
                         storage.FREE_AI_CALLS_PER_DAY + storage.INVITE_BONUS_CALLS)
        self.assertEqual(storage.daily_ai_quota(friend["id"]),
                         storage.FREE_AI_CALLS_PER_DAY + storage.INVITE_BONUS_CALLS)
        friend_summary = storage.invite_summary(friend["id"])
        self.assertTrue(friend_summary["received_invite_reward"])
        self.assertEqual(friend_summary["reward_units"], 1)

    def test_user_ai_config_and_call_counting(self):
        user = storage.register_user("owncfg", "password123", "owncfg@example.com")
        self.assertIsNone(storage.get_user_ai_config(user["id"]))
        # 站点默认模型的调用计入当日额度
        storage.record_event(user_id=user["id"], event_type="ai_chat", metadata={"own_config": False})
        storage.record_event(user_id=user["id"], event_type="ai_classify", metadata={})
        self.assertEqual(storage.count_ai_calls_today(user["id"]), 2)
        # 用户自有 Key 的调用不计入
        storage.record_event(user_id=user["id"], event_type="ai_chat", metadata={"own_config": True})
        self.assertEqual(storage.count_ai_calls_today(user["id"]), 2)
        # 配置保存/读取/清除
        storage.save_user_ai_config(user["id"], {
            "base_url": "https://example.com/v1", "api_key": "sk-abc123", "model": "glm-4"})
        cfg = storage.get_user_ai_config(user["id"])
        self.assertEqual(cfg["model"], "glm-4")
        self.assertEqual(cfg["api_key"], "sk-abc123")
        storage.save_user_ai_config(user["id"], None)
        self.assertIsNone(storage.get_user_ai_config(user["id"]))

    def test_import_markdown_content_dedupes_and_parses_frontmatter(self):
        user = storage.register_user("importer", "password123", "importer@example.com")
        md = (
            "---\n题号: SQL12\n标题: 批量导入的题目\n知识点: JOIN 连接\n难度: 中等\n"
            "日期: 2026-08-31\n来源: 牛客\n错误类型: 逻辑错误\n状态: 未掌握\n"
            "做错次数: 2\n重错日期: 2026-08-30, 2026-08-31\n一句话总结: 忘了去重\n---\n"
            "## 题目\nselect * from t;\n"
        )
        question_id, created = storage.import_markdown_content(user["id"], "a.md", md)
        self.assertTrue(created)
        question = storage.get_question(user["id"], question_id)
        self.assertEqual(question["title"], "批量导入的题目")
        self.assertEqual(question["cat"], "JOIN 连接")
        self.assertEqual(question["times"], 2)
        # 相同内容再次导入自动跳过
        _qid, created = storage.import_markdown_content(user["id"], "a.md", md)
        self.assertFalse(created)
        # 同名但内容不同 → 作为新错题导入
        _qid, created = storage.import_markdown_content(user["id"], "a.md", md.replace("SQL12", "SQL13"))
        self.assertTrue(created)
        # 无 frontmatter 的纯正文也能导入
        _qid, created = storage.import_markdown_content(user["id"], "b.md", "只有正文的一段错题笔记")
        self.assertTrue(created)
        # 正文为空 → 报错
        with self.assertRaises(ValueError):
            storage.import_markdown_content(user["id"], "c.md", "---\n题号: SQL14\n---\n   ")
        # 已有账号后不再满足「本地错题库待导入」条件
        self.assertFalse(storage.legacy_questions_available())

    def test_review_creates_history_and_moves_due_date(self):
        user = storage.register_user("reviewer", "password123", "reviewer@example.com")
        question = storage.list_questions(user["id"])[0]
        result = storage.record_review(user["id"], question["id"], 2, "test-device")
        self.assertGreaterEqual(result["interval_days"], 1)
        self.assertGreater(result["next_review_at"], storage._today().isoformat())
        history = storage.review_history(user["id"])
        self.assertEqual(history[0]["question_id"], question["id"])
        self.assertEqual(history[0]["rating"], 2)

    def test_sync_cursor_and_version_conflict(self):
        user = storage.register_user("sync-user", "password123", "sync-user@example.com")
        initial = storage.sync_pull(user["id"], 0)
        self.assertTrue(initial["changes"])
        question = storage.list_questions(user["id"])[0]
        pushed = storage.sync_push(user["id"], [{
            "id": question["id"],
            "base_version": question["version"],
            "record": dict(question, title="同步后的标题"),
        }])
        self.assertEqual(len(pushed["applied"]), 1)
        conflict = storage.sync_push(user["id"], [{
            "id": question["id"],
            "base_version": question["version"],
            "record": dict(question, title="过期客户端修改"),
        }])
        self.assertEqual(len(conflict["conflicts"]), 1)
        review_push = [{"entity_type": "review", "id": "offline-review-1", "record": {
            "question_id": question["id"], "rating": 2, "device_id": "mini-program"
        }}]
        self.assertEqual(len(storage.sync_push(user["id"], review_push)["applied"]), 1)
        self.assertEqual(len(storage.sync_push(user["id"], review_push)["applied"]), 1)
        matching = [r for r in storage.review_history(user["id"]) if r["id"] == "offline-review-1"]
        self.assertEqual(len(matching), 1)


if __name__ == "__main__":
    unittest.main()
