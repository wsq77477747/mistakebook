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
        status, err = self.request(
            "/api/auth/register",
            {"username": "", "password": "abc123", "email": "missing-name@example.com"},
        )
        self.assertEqual(status, 400)
        self.assertIn("用户名", err["message"])
        status, err = self.request(
            "/api/auth/register",
            {"username": "bad-password", "password": "abcdef", "email": "bad-password@example.com"},
        )
        self.assertEqual(status, 400)
        self.assertIn("至少两种", err["message"])
        status, err = self.request(
            "/api/auth/register",
            {"username": "bad-email", "password": "abc123", "email": "not-an-email"},
        )
        self.assertEqual(status, 400)
        self.assertIn("邮箱格式", err["message"])
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

        def fake_send(to, code, ttl_minutes=10, purpose="register"):
            sent.setdefault("codes", {})[to] = code
            sent.setdefault("purposes", {})[to] = purpose

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
            status, err = self.request(
                "/api/auth/login", {"email": "codelogin@example.com"})
            self.assertEqual(status, 400)
            self.assertIn("登录验证码", err["message"])
            # 同一邮箱未满 3 个账号时，仍允许发送注册用途验证码
            status, err = self.request(
                "/api/auth/send_code", {"email": "codelogin@example.com", "purpose": "register"})
            self.assertEqual(status, 200)
            status, resp = self.request(
                "/api/auth/send_code", {"email": "codelogin@example.com", "purpose": "login"})
            self.assertEqual(status, 200)
            self.assertEqual(sent["purposes"]["codelogin@example.com"], "login")
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

    def test_multi_account_code_login_reset_and_community(self):
        import mailer
        orig_send, orig_configured = mailer.send_verification_code, mailer.smtp_configured
        sent = {}
        mailer.smtp_configured = lambda: True

        def fake_send(to, code, ttl_minutes=10, purpose="register"):
            sent[(to.casefold(), purpose)] = code

        mailer.send_verification_code = fake_send
        try:
            first = server.storage.register_user("shared-one", "password123", "shared-api@example.com")
            second = server.storage.register_user("shared-two", "password456", "shared-api@example.com")
            client = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            status, _ = self.request("/api/auth/send_code", {
                "email": "shared-api@example.com", "purpose": "login"})
            self.assertEqual(status, 200)
            status, selection = self.request("/api/auth/login", {
                "email": "shared-api@example.com",
                "email_code": sent[("shared-api@example.com", "login")],
            }, client=client)
            self.assertEqual(status, 200)
            self.assertTrue(selection["account_selection_required"])
            self.assertEqual(len(selection["accounts"]), 2)
            status, login = self.request("/api/auth/login", {
                "selection_token": selection["selection_token"],
                "account_id": second["id"],
            }, client=client)
            self.assertEqual(login["user"]["username"], "shared-two")

            status, created = self.request("/api/community/posts", {
                "content": "ROW_NUMBER 和 RANK 的区别是什么？"}, client=client)
            self.assertEqual(status, 201)
            post_id = created["post_id"]
            status, liked = self.request("/api/community/like", {"post_id": post_id}, client=client)
            self.assertTrue(liked["liked"])
            status, commented = self.request("/api/community/comments", {
                "post_id": post_id, "content": "并列排名的处理不同。"}, client=client)
            self.assertEqual(status, 201)
            status, shared = self.request("/api/community/share", {"post_id": post_id}, client=client)
            self.assertEqual(shared["share_count"], 1)
            anonymous = urllib.request.build_opener()
            status, posts = self.request("/api/community/posts", client=anonymous)
            self.assertEqual(status, 200)
            self.assertTrue(any(p["id"] == post_id for p in posts["posts"]))
            status, profile = self.request(
                "/api/community/profile?id=" + urllib.parse.quote(second["id"]), client=anonymous)
            self.assertEqual(profile["profile"]["post_count"], 1)

            status, _ = self.request("/api/auth/send_code", {
                "email": "shared-api@example.com", "purpose": "reset"})
            self.assertEqual(status, 200)
            reset_code = sent[("shared-api@example.com", "reset")]
            wrong_reset_code = "000000" if reset_code != "000000" else "111111"
            status, reset_error = self.request("/api/auth/reset_password", {
                "email": "shared-api@example.com", "email_code": wrong_reset_code,
            })
            self.assertEqual(status, 400)
            self.assertIn("验证码", reset_error["message"])
            status, reset = self.request("/api/auth/reset_password", {
                "email": "shared-api@example.com",
                "email_code": reset_code,
            })
            self.assertTrue(reset["account_selection_required"])
            status, invalid = self.request("/api/auth/reset_password", {
                "selection_token": reset["selection_token"], "account_id": first["id"],
                "new_password": "short",
            })
            self.assertEqual(status, 400)  # 不应消费一次性票据，修正密码后仍可继续
            status, done = self.request("/api/auth/reset_password", {
                "selection_token": reset["selection_token"], "account_id": first["id"],
                "new_password": "new-password-123",
            })
            self.assertEqual(status, 200)
            self.assertIsNotNone(server.storage.authenticate("shared-one", "new-password-123"))
            self.assertIsNone(server.storage.authenticate("shared-one", "password123"))
        finally:
            mailer.send_verification_code, mailer.smtp_configured = orig_send, orig_configured

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

    # 命名以 user_ 开头：字母序排在 test_auth 之后执行，避免抢占「首个注册用户=管理员」的测试前提
    def test_user_quota_invite_and_own_config(self):
        orig_call = server._call_llm
        orig_load_config = server.load_config
        server._call_llm = lambda cfg, messages, **kw: "ok"
        server.load_config = lambda: {
            "base_url": "https://ci.example/v1",
            "api_key": "sk-ci-placeholder",
            "model": "ci-test-model",
        }
        try:
            inviter = server.storage.register_user("quota-user", "password123", "quota-user@example.com")
            client = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            status, _ = self.request("/api/auth/login",
                                     {"username": "quota-user", "password": "password123"}, client=client)
            self.assertEqual(status, 200)
            status, q = self.request("/api/ai_quota", client=client)
            self.assertEqual(status, 200)
            self.assertEqual(q["daily_total"], q["base_free"])  # 无邀请：每日 3 次
            self.assertTrue(q["invite_code"])
            # 用满免费额度
            chat = {"messages": [{"role": "user", "content": "hi"}]}
            for _ in range(q["base_free"]):
                status, resp = self.request("/api/chat", chat, client=client)
                self.assertEqual(status, 200)
            status, err = self.request("/api/chat", chat, client=client)
            self.assertEqual(status, 429)
            self.assertEqual(err["error"], "AI_QUOTA_EXCEEDED")
            # 新用户使用邀请码注册 → 邀请人每日额度 +10，可继续调用
            friend = server.storage.register_user("quota-friend", "password123",
                                                  "quota-friend@example.com",
                                                  invite_code=inviter["invite_code"])
            self.assertTrue(friend["invite_applied"])
            status, q2 = self.request("/api/ai_quota", client=client)
            self.assertEqual(q2["daily_total"], q2["base_free"] + 10 * q2["invite_bonus"])
            self.assertEqual(q2["invite_bonus"], 1)
            status, resp = self.request("/api/chat", chat, client=client)
            self.assertEqual(status, 200)
            # 配置自己的 Key 后不再受额度限制，且不计入用量
            status, saved = self.request("/api/my_ai_config", {
                "base_url": "https://example.com/v1", "api_key": "sk-mykey", "model": "glm-4"},
                client=client)
            self.assertEqual(status, 200)
            self.assertTrue(saved["configured"])
            status, q3 = self.request("/api/ai_quota", client=client)
            self.assertTrue(q3["using_own_config"])
            status, resp = self.request("/api/chat", chat, client=client)
            self.assertEqual(status, 200)
            status, q4 = self.request("/api/ai_quota", client=client)
            # q2→q4 之间：配额内的邀请后调用 +1，自有 Key 调用不计入
            self.assertEqual(q4["used_today"], q2["used_today"] + 1)
            # 清除配置后恢复受限
            status, _ = self.request("/api/my_ai_config", {"action": "clear"}, client=client)
            status, q5 = self.request("/api/ai_quota", client=client)
            self.assertFalse(q5["using_own_config"])
            status, resp = self.request("/api/chat", chat, client=client)
            self.assertEqual(status, 200)  # 额度内还有剩余（13-4=9）
        finally:
            server._call_llm = orig_call
            server.load_config = orig_load_config

    def test_email_code_flow_when_enabled(self):
        import mailer
        orig_required, orig_send = mailer.register_code_required, mailer.send_verification_code
        orig_configured = mailer.smtp_configured
        sent = {}
        mailer.register_code_required = lambda: True
        mailer.smtp_configured = lambda: True

        def fake_send(to, code, ttl_minutes=10, purpose="register"):
            sent["to"], sent["code"], sent["purpose"] = to, code, purpose

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
            self.assertEqual(sent["purpose"], "register")
            status, err = self.request(
                "/api/auth/register", dict(register, email_code="000000"))
            self.assertEqual(status, 400)  # 验证码错误
            status, reg = self.request(
                "/api/auth/register", dict(register, email_code=sent["code"]))
            self.assertEqual(status, 201)
            self.assertEqual(reg["user"]["email"], "coded@example.com")
            self.assertEqual(cfg["max_accounts_per_email"], 3)
        finally:
            mailer.register_code_required, mailer.send_verification_code = orig_required, orig_send
            mailer.smtp_configured = orig_configured

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
