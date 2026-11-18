"""取额度只走 /api/oauth/usage：任何失败都不退回 `claude -p /usage`，原因照实写。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.api import oauth_usage, usage
from cca.paths import usage_cache_file

TOKEN = "sk-ant-oat01-FAKE"  # noqa: S105 - 假令牌


class NoCliFallbackTest(unittest.TestCase):
    """401、403、429、网络错、解析错、没令牌、令牌过期、没代理：都不起 claude，失败原因写对。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "CCA_PROXY": "http://localhost:7897"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CCA_OAUTH_USAGE_FILE", None)
        oauth_usage.read_claude_version.cache_clear()
        self.addCleanup(oauth_usage.read_claude_version.cache_clear)

    def seed(self, *, token=TOKEN, expires_in=3600):
        d = self.home / ".claude-accounts" / "b001"
        d.mkdir(parents=True, exist_ok=True)
        oauth = {"accessToken": token, "scopes": ["user:inference"],
                 "expiresAt": int((time.time() + expires_in) * 1000)}
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")

    def fetch(self, *, opener=None, env=None):
        """取一次额度；断言没有起任何子进程（claude -p 退回那条路已删掉）。"""
        def _open_ok(_request, _proxy):
            return json.dumps({"five_hour": {"utilization": 1}}).encode()
        with mock.patch.object(oauth_usage, "_open", side_effect=opener or _open_ok), \
                mock.patch.object(oauth_usage, "cli_version", return_value="2.1.327"), \
                mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(usage.subprocess, "run") as run, \
                mock.patch.object(usage, "_run_claude") as run_claude:
            windows, why = usage.fetch_usage_live("b001")
            result, failures = usage.live_usage(["b001"], force=True)
        run.assert_not_called()
        run_claude.assert_not_called()
        self.assertEqual(failures.get("b001", ""), why)
        self.assertEqual("b001" in result, bool(windows))
        return windows, why

    def http_error(self, code):
        def _open(request, _proxy):
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        return _open

    def test_ok(self):
        self.seed()
        windows, why = self.fetch()
        self.assertEqual((windows["five_hour"]["percent"], why), (1, ""))

    def test_each_failure_reason(self):
        def net(_request, _proxy):
            msg = "boom"
            raise OSError(msg)

        def bad(_request, _proxy):
            return b"<html>"
        cases = {
            "401": ({}, {"opener": self.http_error(401)}, "令牌过期或失效（HTTP 401）"),
            "403": ({}, {"opener": self.http_error(403)}, "令牌过期或失效（HTTP 403）"),
            "429": ({}, {"opener": self.http_error(429)}, "HTTP 429 限流"),
            "500": ({}, {"opener": self.http_error(500)}, "HTTP 500"),
            "网络错": ({}, {"opener": net}, "网络失败（OSError）"),
            "解析错": ({}, {"opener": bad}, "返回体解析不了"),
            "没令牌": ({"token": ""}, {}, "凭据里没有令牌"),
            "令牌过期": ({"expires_in": -60}, {}, "令牌过期"),
            "没代理": ({}, {"env": {"CCA_PROXY": ""}}, "没有配置代理"),
        }
        for label, (seed, kw, want) in cases.items():
            with self.subTest(label):
                usage_cache_file().unlink(missing_ok=True)
                self.seed(**seed)
                windows, why = self.fetch(**kw)
                self.assertEqual((windows, why), ({}, want))
                self.assertNotIn("claude -p", why)
                self.assertNotIn(TOKEN, why)

    def test_fallback_code_is_gone(self):
        for attr in ("_usage_via_cli", "_usage_from_report", "_usage_from_fixture"):
            self.assertFalse(hasattr(usage, attr), attr)
        src = Path(usage.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"/usage"', src)
        self.assertNotIn("allow_cli", src)


if __name__ == "__main__":
    unittest.main()


class AuthStatusLoggedOutTest(unittest.TestCase):
    """claude auth status 没登录时退出码 1 但照样输出 JSON：要记成未登录，不能当「取不到」沿用旧缓存。"""

    def test_exit_1_with_logged_out_json_is_logged_out(self):
        out = mock.Mock(returncode=1, stdout='{"loggedIn": false, "authMethod": "none"}')
        with mock.patch.dict(os.environ, {"CCA_SUBSCRIPTION_LIVE_FILE": ""}), \
             mock.patch.object(usage, "_run_claude", return_value=(out, "")):
            sub, why = usage.fetch_subscription_live("002")
        self.assertEqual((sub["status"], why), ("logged_out", ""))

    def test_non_json_failure_is_still_a_failure(self):
        out = mock.Mock(returncode=1, stdout="boom")
        with mock.patch.dict(os.environ, {"CCA_SUBSCRIPTION_LIVE_FILE": ""}), \
             mock.patch.object(usage, "_run_claude", return_value=(out, "")):
            self.assertEqual(usage.fetch_subscription_live("002"), ({}, "退出码 1"))


class CacheMergeTest(unittest.TestCase):
    """几个 `cca ls` 和 watchdog 同时刷缓存：各写各的号，旧条目不许盖掉新条目。"""

    def setUp(self):
        import tempfile  # noqa: PLC0415
        self.f = Path(tempfile.mkdtemp()) / "usage-cache.json"

    def test_merge_keeps_other_profiles(self):
        usage.merge_cache(self.f, {"b001": {"windows": {}, "fetched_at": 10}})
        usage.merge_cache(self.f, {"b002": {"windows": {}, "fetched_at": 11}})
        self.assertEqual(set(usage.read_cache(self.f)), {"b001", "b002"})

    def test_older_entry_does_not_overwrite_newer(self):
        usage.merge_cache(self.f, {"b001": {"windows": {"x": 1}, "fetched_at": 20}})
        usage.merge_cache(self.f, {"b001": {"windows": {"x": 0}, "fetched_at": 10}})
        self.assertEqual(usage.read_cache(self.f)["b001"]["fetched_at"], 20)

    def test_concurrent_writers_lose_nothing(self):
        import threading  # noqa: PLC0415
        names = [f"p{i}" for i in range(20)]
        threads = [threading.Thread(target=usage.merge_cache,
                                    args=(self.f, {n: {"windows": {}, "fetched_at": 1}}))
                   for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(usage.read_cache(self.f)), set(names))
        self.assertEqual(oct(self.f.stat().st_mode & 0o777), "0o600")
        self.assertEqual([p.name for p in self.f.parent.glob("*.tmp")], [])


class LockDirNotProfileTest(unittest.TestCase):
    """Claude Code 刷新凭据时在配置目录旁建 `<号>.lock` 锁目录：不能当成号。"""

    def test_lock_dirs_are_not_profiles(self):
        from cca.account.model import valid_name  # noqa: PLC0415
        self.assertFalse(valid_name("006.lock"))
        self.assertTrue(valid_name("b006"))
        self.assertTrue(valid_name("gpt-ds"))
