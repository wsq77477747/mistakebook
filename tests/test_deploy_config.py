# -*- coding: utf-8 -*-
"""私密配置注入：load_config 环境变量兜底 + deploy/apply_private_config.py 行为。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import server


def _load_deploy_script():
    path = os.path.join(ROOT, "deploy", "apply_private_config.py")
    spec = importlib.util.spec_from_file_location("apply_private_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoadConfigEnvFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig_file = server.CONFIG_FILE
        server.CONFIG_FILE = os.path.join(self.tmp.name, "ai_config.json")
        for name in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
            self.environ_backup(name)

    def environ_backup(self, name):
        if name in os.environ:
            self._had_env = getattr(self, "_had_env", {})
            self._had_env[name] = os.environ.pop(name)

    def tearDown(self):
        server.CONFIG_FILE = self.orig_file
        for name in ("AI_BASE_URL", "AI_API_KEY", "AI_MODEL"):
            os.environ.pop(name, None)
        for name, value in getattr(self, "_had_env", {}).items():
            os.environ[name] = value
        self.tmp.cleanup()

    def test_file_value_wins_over_env(self):
        with open(server.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"base_url": "https://file.example/v1", "api_key": "sk-file",
                       "model": "file-model"}, f)
        os.environ["AI_API_KEY"] = "sk-env"
        cfg = server.load_config()
        self.assertEqual(cfg["api_key"], "sk-file")
        self.assertEqual(cfg["model"], "file-model")

    def test_env_fills_missing_fields(self):
        # 配置文件不存在：api_key/model 来自环境变量
        os.environ["AI_API_KEY"] = "sk-env"
        os.environ["AI_MODEL"] = "env-model"
        os.environ["AI_BASE_URL"] = "https://env.example/v1"
        cfg = server.load_config()
        self.assertEqual(cfg["api_key"], "sk-env")
        self.assertEqual(cfg["model"], "env-model")
        self.assertEqual(cfg["base_url"], "https://env.example/v1")

    def test_empty_without_file_or_env(self):
        cfg = server.load_config()
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["base_url"], server.DEFAULT_BASE)


class ApplyPrivateConfigTests(unittest.TestCase):
    def setUp(self):
        self.script = _load_deploy_script()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.orig_dir = os.environ.get("SQL_WRONGBOOK_CONFIG_DIR")
        os.environ["SQL_WRONGBOOK_CONFIG_DIR"] = str(self.dir)

    def tearDown(self):
        if self.orig_dir is None:
            os.environ.pop("SQL_WRONGBOOK_CONFIG_DIR", None)
        else:
            os.environ["SQL_WRONGBOOK_CONFIG_DIR"] = self.orig_dir
        self.tmp.cleanup()

    def test_collect_ai_updates_and_default_base(self):
        ai, email = self.script.collect_updates({"AI_API_KEY": "sk-abc", "AI_MODEL": "qwen-vl-max"})
        self.assertEqual(ai, {"api_key": "sk-abc", "model": "qwen-vl-max",
                              "base_url": self.script.DEFAULT_BASE_URL})
        self.assertEqual(email, {})

    def test_merge_creates_and_preserves(self):
        target = self.dir / "ai_config.json"
        merged = self.script.merge_into(target, {"api_key": "sk-new"})
        self.assertEqual(merged["api_key"], "sk-new")
        # 二次合并保留已有字段
        merged = self.script.merge_into(target, {"model": "qwen-vl-max"})
        self.assertEqual(merged["api_key"], "sk-new")
        self.assertEqual(merged["model"], "qwen-vl-max")
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["api_key"], "sk-new")

    def test_defaults_do_not_override_existing(self):
        target = self.dir / "email_config.json"
        self.script.merge_into(target, {"host": "smtp.example.com", "port": 587})
        merged = self.script.merge_into(
            target, {"enabled": True},
            defaults={"enabled": True, "port": 465, "use_ssl": True, "sender_name": "SQL 错题本"})
        self.assertEqual(merged["port"], 587)  # 已有端口不被默认值覆盖
        self.assertEqual(merged["use_ssl"], True)  # 缺失字段由默认值补齐
        self.assertEqual(merged["sender_name"], "SQL 错题本")

    def test_email_updates_require_enabled_flag(self):
        ai, email = self.script.collect_updates({"EMAIL_HOST": "smtp.qq.com"})  # 未开 EMAIL_ENABLED
        self.assertEqual(email, {})  # 不收集，避免半配置状态
        ai, email = self.script.collect_updates({
            "EMAIL_ENABLED": "1", "EMAIL_HOST": "smtp.qq.com",
            "EMAIL_USERNAME": "a@qq.com", "EMAIL_PASSWORD": "secret"})
        self.assertTrue(email["enabled"])
        self.assertEqual(email["host"], "smtp.qq.com")
        self.assertEqual(email["password"], "secret")

    def test_masked_hides_secrets(self):
        shown = self.script.masked({"api_key": "sk-12345678", "model": "m"})
        self.assertEqual(shown["api_key"], "***5678")
        self.assertEqual(shown["model"], "m")


if __name__ == "__main__":
    unittest.main()
