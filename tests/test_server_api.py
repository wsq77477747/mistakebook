# -*- coding: utf-8 -*-
import http.cookiejar
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import server


class ServerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        server.storage.DB_PATH = os.path.join(cls.tmp.name, "api.db")
        server.storage.PBKDF2_ITERATIONS = 1_000
        server.storage.init_db()
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        cls.tmp.cleanup()

    def setUp(self):
        jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def request(self, path, payload=None, method=None, client=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method or ("POST" if payload is not None else "GET"),
            headers={"Content-Type": "application/json"},
        )
        with (client or self.client).open(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_auth_questions_review_and_sync(self):
        status, registered = self.request(
            "/api/auth/register", {"username": "api-user", "password": "password123"}
        )
        self.assertEqual(status, 201)
        self.assertGreaterEqual(registered["imported"], 1)
        _, listing = self.request("/api/questions")
        self.assertGreaterEqual(len(listing["questions"]), 1)
        question = listing["questions"][0]
        self.assertIn("prompt_html", question)
        _, today = self.request("/api/review/today")
        self.assertGreaterEqual(today["stats"]["due"], 1)
        _, reviewed = self.request(
            "/api/review", {"question_id": question["id"], "rating": 2, "device_id": "test"}
        )
        self.assertTrue(reviewed["ok"])
        _, sync = self.request("/api/sync?since=0")
        self.assertTrue(sync["changes"])
        token_client = urllib.request.build_opener()
        _, login = self.request("/api/auth/login", {
            "username": "api-user", "password": "password123", "client_type": "mini_program"
        }, client=token_client)
        bearer_request = urllib.request.Request(
            self.base + "/api/questions", headers={"Authorization": "Bearer " + login["session_token"]}
        )
        with urllib.request.urlopen(bearer_request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_questions_require_login(self):
        anonymous = urllib.request.build_opener()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/questions", client=anonymous)
        self.assertEqual(raised.exception.code, 401)

    def test_private_files_are_not_served(self):
        for path in ("/config/ai_config.example.json", "/错题库/.gitkeep", "/scripts/server.py"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(self.base + urllib.parse.quote(path, safe="/"), timeout=5)
            self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
