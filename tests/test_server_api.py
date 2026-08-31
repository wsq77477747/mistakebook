# -*- coding: utf-8 -*-
import datetime as dt
import hashlib
import http.cookiejar
import http.server
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_QUIZ_DIR = os.path.join(ROOT, "tests", "fixtures", "legacy_questions")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import server


class ServerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.original_db_path = server.storage.DB_PATH
        cls.original_quiz_dir = server.storage.QUIZ_DIR
        cls.original_pbkdf2_iterations = server.storage.PBKDF2_ITERATIONS
        server.storage.DB_PATH = os.path.join(cls.tmp.name, "api.db")
        server.storage.QUIZ_DIR = FIXTURE_QUIZ_DIR
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
        server.storage.DB_PATH = cls.original_db_path
        server.storage.QUIZ_DIR = cls.original_quiz_dir
        server.storage.PBKDF2_ITERATIONS = cls.original_pbkdf2_iterations
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
        try:
            with (client or self.client).open(req, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"raw": body}

    def request_raw(self, path, payload=None, method=None, client=None, token=None):
        """同 request，但返回 (status, headers)，用于检查 Set-Cookie。"""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method or ("POST" if payload is not None else "GET"),
            headers=headers,
        )
        try:
            with (client or self.client).open(req, timeout=5) as response:
                return response.status, response.headers
        except urllib.error.HTTPError as e:
            e.read()
            return e.code, e.headers

    def _session_expiry(self, token):
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        con = sqlite3.connect(server.storage.DB_PATH)
        try:
            row = con.execute(
                "SELECT expires_at FROM sessions WHERE token_hash=?", (token_hash,)
            ).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    def _backdate_session(self, token, days):
        target = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
                  ).isoformat().replace("+00:00", "Z")
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        con = sqlite3.connect(server.storage.DB_PATH)
        try:
            con.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (target, token_hash))
            con.commit()
        finally:
            con.close()
        return target

    def test_auth_questions_review_and_sync(self):
        status, registered = self.request(
            "/api/auth/register",
            {"username": "api-user", "password": "password123", "email": "api-user@example.com"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(registered["user"]["email"], "api-user@example.com")
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
            "username": "api-user@example.com", "password": "password123", "client_type": "mini_program"
        }, client=token_client)
        self.assertEqual(login["user"]["email"], "api-user@example.com")
        bearer_request = urllib.request.Request(
            self.base + "/api/questions", headers={"Authorization": "Bearer " + login["session_token"]}
        )
        with urllib.request.urlopen(bearer_request, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_questions_require_login(self):
        anonymous = urllib.request.build_opener()
        status, _ = self.request("/api/questions", client=anonymous)
        self.assertEqual(status, 401)

    def test_register_config_is_public(self):
        anonymous = urllib.request.build_opener()
        status, cfg = self.request("/api/auth/register_config", client=anonymous)
        self.assertEqual(status, 200)
        self.assertIn("email_code_required", cfg)

    def test_import_batch_endpoint(self):
        anonymous = urllib.request.build_opener()
        status, _ = self.request("/api/import_batch", {"files": []}, client=anonymous)
        self.assertEqual(status, 401)
        server.storage.register_user("import-user", "password123", "import-user@example.com")
        client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, _ = self.request("/api/auth/login",
                                 {"username": "import-user", "password": "password123"}, client=client)
        self.assertEqual(status, 200)
        files = [
            {"name": "q1.md", "content": "---\n题号: SQL21\n标题: 接口导入一\n知识点: 索引\n---\n正文一"},
            {"name": "q2.md", "content": "纯正文错题笔记"},
        ]
        status, result = self.request("/api/import_batch", {"files": files}, client=client)
        self.assertEqual(status, 200)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["failed"], [])
        status, result = self.request("/api/import_batch", {"files": files}, client=client)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["skipped"]), 2)  # 重复导入自动跳过
        status, result = self.request(
            "/api/import_batch",
            {"files": [{"name": "bad.md", "content": "---\n题号: X\n---\n  "}]},
            client=client)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(len(result["failed"]), 1)
        status, listing = self.request("/api/questions", client=client)
        titles = [q["title"] for q in listing["questions"]]
        self.assertIn("接口导入一", titles)
        self.assertIn("未命名", " ".join(titles))  # 纯正文文件用默认标题导入

    def test_register_config_reports_local_import_availability(self):
        status, cfg = self.request("/api/auth/register_config")
        self.assertEqual(status, 200)
        self.assertIn("will_import_local", cfg)
        self.assertFalse(cfg["will_import_local"])  # 测试库中已存在账号

    def test_email_code_login_flow(self):
        import mailer
        orig_required, orig_send = mailer.register_code_required, mailer.send_verification_code
        orig_configured = mailer.smtp_configured
        sent = {}
        mailer.register_code_required = lambda: True
        mailer.smtp_configured = lambda: True

        def fake_send(to, code, ttl_minutes=10):
            sent.setdefault("codes", {})[to] = code

        mailer.send_verification_code = fake_send
        try:
            status, cfg = self.request("/api/auth/register_config")
            self.assertTrue(cfg["email_code_login_available"])
            server.storage.register_user("codelogin", "password123", "codelogin@example.com")
            jar = http.cookiejar.CookieJar()
            client = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
            # 未注册邮箱请求登录验证码 → 404
            status, err = self.request(
                "/api/auth/send_code", {"email": "ghost@example.com", "purpose": "login"})
            self.assertEqual(status, 404)
            # 已注册邮箱：注册用途发码被拒，登录用途发码成功
            status, err = self.request(
                "/api/auth/send_code", {"email": "codelogin@example.com", "purpose": "register"})
            self.assertEqual(status, 400)
            status, resp = self.request(
                "/api/auth/send_code", {"email": "codelogin@example.com", "purpose": "login"})
            self.assertEqual(status, 200)
            code = sent["codes"]["codelogin@example.com"]
            # 错误验证码 → 401；正确验证码 → 登录成功并建立会话
            status, err = self.request(
                "/api/auth/login", {"email": "codelogin@example.com", "email_code": "000000",
                                    "remember_me": False})
            self.assertEqual(status, 401)
            status, login = self.request(
                "/api/auth/login", {"email": "codelogin@example.com", "email_code": code},
                client=client)
            self.assertEqual(status, 200, "login failed: %s" % json.dumps(err, ensure_ascii=False))
            self.assertEqual(login["user"]["username"], "codelogin")
            status, me = self.request("/api/auth/me", client=client)
            self.assertTrue(me["authenticated"])
            status, listing = self.request("/api/questions", client=client)
            self.assertEqual(status, 200)
        finally:
            mailer.register_code_required, mailer.send_verification_code = orig_required, orig_send
            mailer.smtp_configured = orig_configured

    def test_login_remember_me_controls_cookie_lifetime(self):
        server.storage.register_user("remember-user", "password123", "remember-user@example.com")
        client = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        login = {"username": "remember-user", "password": "password123"}
        status, headers = self.request_raw("/api/auth/login", dict(login, remember_me=False), client=client)
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "") or ""
        self.assertIn("sqlwb_session=", cookie)
        self.assertNotIn("Max-Age", cookie)  # 会话 Cookie：关闭浏览器即退出
        status, headers = self.request_raw("/api/auth/login", dict(login, remember_me=True), client=client)
        cookie = headers.get("Set-Cookie", "") or ""
        self.assertIn("Max-Age=%d" % (server.storage.SESSION_DAYS * 86400), cookie)
        status, headers = self.request_raw("/api/auth/login", dict(login), client=client)
        self.assertIn("Max-Age", headers.get("Set-Cookie", "") or "")  # 默认持久，兼容老客户端

    def test_session_sliding_renewal(self):
        registered = server.storage.register_user("renew-user", "password123", "renew-user@example.com")
        persistent_token = server.storage.create_session(registered["id"], persistent=True)
        volatile_token = server.storage.create_session(registered["id"], persistent=False)
        # 剩余 25 天（大于一半阈值）：不续期
        written = self._backdate_session(persistent_token, 25)
        status, _ = self.request_raw("/api/auth/me", token=persistent_token)
        self.assertEqual(status, 200)
        self.assertEqual(self._session_expiry(persistent_token), written)
        # 剩余 1 天（小于阈值）：滑动续期到完整期限并刷新 Cookie
        self._backdate_session(persistent_token, 1)
        status, headers = self.request_raw("/api/auth/me", token=persistent_token)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age", headers.get("Set-Cookie", "") or "")
        new_expiry = dt.datetime.fromisoformat(self._session_expiry(persistent_token).replace("Z", "+00:00"))
        self.assertGreater(new_expiry, dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=29))
        # 非持久会话（未勾选自动登录）即使临近过期也不续期、不刷新 Cookie
        written = self._backdate_session(volatile_token, 1)
        status, headers = self.request_raw("/api/auth/me", token=volatile_token)
        self.assertEqual(status, 200)
        self.assertEqual(self._session_expiry(volatile_token), written)
        self.assertNotIn("Max-Age", headers.get("Set-Cookie", "") or "")

    def test_email_code_flow_when_enabled(self):
        import mailer
        orig_required, orig_send = mailer.register_code_required, mailer.send_verification_code
        sent = {}
        mailer.register_code_required = lambda: True

        def fake_send(to, code, ttl_minutes=10):
            sent["to"], sent["code"] = to, code

        mailer.send_verification_code = fake_send
        try:
            status, cfg = self.request("/api/auth/register_config")
            self.assertEqual(status, 200)
            self.assertTrue(cfg["email_code_required"])
            register = {"username": "coded-user", "password": "password123",
                        "email": "coded@example.com"}
            status, err = self.request("/api/auth/register", dict(register))
            self.assertEqual(status, 400)  # 未带验证码
            status, resp = self.request("/api/auth/send_code", {"email": "coded@example.com"})
            self.assertEqual(status, 200)
            self.assertEqual(sent["to"], "coded@example.com")
            status, err = self.request(
                "/api/auth/register", dict(register, email_code="000000"))
            self.assertEqual(status, 400)  # 验证码错误
            status, reg = self.request(
                "/api/auth/register", dict(register, email_code=sent["code"]))
            self.assertEqual(status, 201)
            self.assertEqual(reg["user"]["email"], "coded@example.com")
            status, err = self.request("/api/auth/send_code", {"email": "coded@example.com"})
            self.assertEqual(status, 400)  # 已注册邮箱不再发码
        finally:
            mailer.register_code_required, mailer.send_verification_code = orig_required, orig_send

    def test_email_config_requires_admin(self):
        import mailer
        server.storage.register_user("mail-admin", "password123", "mail-admin@example.com")
        plain = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, _ = self.request("/api/auth/login",
                                 {"username": "mail-admin", "password": "password123"}, client=plain)
        self.assertEqual(status, 200)
        # 邮件配置写入必须落在临时目录，不能碰真实 config/email_config.json
        orig_file, mailer.CONFIG_FILE = mailer.CONFIG_FILE, os.path.join(self.tmp.name, "email_config.json")
        try:
            status, _ = self.request("/api/email/config", {"enabled": False}, client=plain)
            self.assertEqual(status, 403)
            admin = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            status, _ = self.request("/api/auth/login",
                                     {"username": "api-user", "password": "password123"}, client=admin)
            self.assertEqual(status, 200)
            status, cfg = self.request("/api/email/config",
                                       {"enabled": False, "host": "smtp.example.com",
                                        "username": "noreply@example.com", "password": "secret"},
                                       client=admin)
            self.assertEqual(status, 200)
            self.assertFalse(cfg["enabled"])
            self.assertTrue(cfg["configured"])  # 字段齐全即视为已配置
        finally:
            mailer.CONFIG_FILE = orig_file

    def test_private_files_are_not_served(self):
        for path in ("/config/ai_config.example.json", "/错题库/.gitkeep", "/scripts/server.py"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(self.base + urllib.parse.quote(path, safe="/"), timeout=5)
            self.assertEqual(raised.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
