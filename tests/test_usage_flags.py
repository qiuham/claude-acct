"""取额度那次 claude 调用：带上省启动的开关和环境变量（--bare 不行，它不读订阅登录）。"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.api import usage


class UsageFlagsTest(unittest.TestCase):
    def test_usage_call_is_slim(self):
        done = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch.object(usage.os.environ, "get", return_value=None), \
                mock.patch.object(usage, "profile_kind", return_value="oauth"), \
                mock.patch.object(usage.shutil, "which", return_value="/bin/claude"), \
                mock.patch("cca.env.build_env", return_value={"CLAUDE_CONFIG_DIR": "/x"}), \
                mock.patch.object(usage.subprocess, "run", return_value=done) as run, \
                mock.patch.object(usage, "usage_snapshot",
                                  return_value={"fetched_at": usage.time.time() + 60, "windows": {"five_hour": {}}}):
            windows, why = usage.fetch_usage_live("003")
        self.assertEqual((bool(windows), why), (True, ""))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["/bin/claude", "-p", "/usage"])
        self.assertIn("--safe-mode", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("--bare", argv)
        env = run.call_args.kwargs["env"]
        self.assertEqual((env["ENABLE_CLAUDEAI_MCP_SERVERS"], env["DISABLE_AUTOUPDATER"],
                          env["CLAUDE_CONFIG_DIR"]), ("false", "1", "/x"))
        self.assertNotIn("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", env)


class UsageReportTest(unittest.TestCase):
    """`--refresh` 要真的变新：读 CLI 输出正文，而不是只认 `.claude.json` 的快照。"""

    REPORT = {  # noqa: RUF012 - 测试常量，不会被改
        "result": ("You are currently using your subscription\n\n"
                   "Current session: 100% used · resets Nov 5, 12am (America/Los_Angeles)\n"
                   "Current week (all models): 88% used · resets Nov 9, 4pm (America/Los_Angeles)\n"),
    }

    def run_live(self, report: dict, *, snapshot_at: float | None = None):
        done = mock.Mock(returncode=0, stdout=json.dumps(report), stderr="")
        snapshot = {"fetched_at": snapshot_at if snapshot_at is not None else usage.time.time() + 60, "windows": {
            "five_hour": {"label": "5 小时", "percent": 91, "resets_at": 1793865600.0, "reset_passed": False},
            "seven_day": {"label": "7 天", "percent": 88, "resets_at": 1794268800.0, "reset_passed": False},
        }}
        with mock.patch.object(usage.os.environ, "get", return_value=None), \
                mock.patch.object(usage, "profile_kind", return_value="oauth"), \
                mock.patch.object(usage.shutil, "which", return_value="/bin/claude"), \
                mock.patch("cca.env.build_env", return_value={"CLAUDE_CONFIG_DIR": "/x"}), \
                mock.patch.object(usage.subprocess, "run", return_value=done), \
                mock.patch.object(usage, "usage_snapshot", return_value=snapshot):
            return usage.fetch_usage_live("002")

    def test_percent_comes_from_report_text(self):
        windows, why = self.run_live(self.REPORT)
        self.assertEqual(why, "")
        self.assertEqual(windows["five_hour"]["percent"], 100)   # 快照里还是 91
        self.assertEqual(windows["seven_day"]["percent"], 88)
        self.assertEqual(windows["five_hour"]["label"], "5 小时")  # 标签和重置时刻沿用快照

    def test_falls_back_to_snapshot_written_by_this_call(self):
        windows, why = self.run_live({})
        self.assertEqual(why, "")
        self.assertEqual(windows["five_hour"]["percent"], 91)

    def test_old_snapshot_is_not_reported_as_live(self):
        windows, why = self.run_live({}, snapshot_at=usage.time.time() - 3600)
        self.assertEqual((windows, why), ({}, "CLI 没有返回额度"))


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
