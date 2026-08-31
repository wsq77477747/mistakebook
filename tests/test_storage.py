# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        storage.DB_PATH = os.path.join(self.tmp.name, "test.db")
        storage.PBKDF2_ITERATIONS = 1_000
        storage.init_db()

    def tearDown(self):
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

    def test_register_requires_valid_and_unused_email(self):
        storage.register_user("owner", "password123", "owner@example.com")
        with self.assertRaises(ValueError):
            storage.register_user("no-email", "password123", "")
        with self.assertRaises(ValueError):
            storage.register_user("bad-email", "password123", "not-an-email")
        with self.assertRaises(ValueError):
            storage.register_user("dup-email", "password123", "OWNER@Example.com")
        authenticated = storage.authenticate("owner@example.com", "password123")
        self.assertIsNotNone(authenticated)
        self.assertEqual(authenticated["email"], "owner@example.com")

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
