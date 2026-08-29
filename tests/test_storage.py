# -*- coding: utf-8 -*-
import os
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
        first = storage.register_user("owner@example.com", "password123")
        second = storage.register_user("reader@example.com", "password456")
        self.assertGreaterEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertGreaterEqual(len(storage.list_questions(first["id"])), 1)
        self.assertEqual(storage.list_questions(second["id"]), [])
        self.assertIsNotNone(storage.authenticate("OWNER@example.com", "password123"))
        self.assertIsNone(storage.authenticate("owner@example.com", "wrong-password"))

    def test_review_creates_history_and_moves_due_date(self):
        user = storage.register_user("reviewer", "password123")
        question = storage.list_questions(user["id"])[0]
        result = storage.record_review(user["id"], question["id"], 2, "test-device")
        self.assertGreaterEqual(result["interval_days"], 1)
        self.assertGreater(result["next_review_at"], storage._today().isoformat())
        history = storage.review_history(user["id"])
        self.assertEqual(history[0]["question_id"], question["id"])
        self.assertEqual(history[0]["rating"], 2)

    def test_sync_cursor_and_version_conflict(self):
        user = storage.register_user("sync-user", "password123")
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
