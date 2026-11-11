"""传给 claude 的时区：默认跟出口、CCA_TZ=auto 跟本机、写死 IANA 名。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca import env
from cca.api import egress


class LaunchTzTest(unittest.TestCase):
    def tz(self, *, cca_tz: str | None, info: dict | None, shell_tz: str = ""):
        environ = {k: v for k, v in os.environ.items() if k != "CCA_TZ"}
        if cca_tz is not None:
            environ["CCA_TZ"] = cca_tz
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(env, "SHELL_TZ", shell_tz):
            return env.launch_tz(info)

    def test_default_follows_egress(self):
        self.assertEqual(self.tz(cca_tz=None, info={"timezone": "America/New_York"}), "America/New_York")

    def test_default_falls_back_when_probe_failed(self):
        self.assertEqual(self.tz(cca_tz=None, info=None), env.CLAUDE_TZ)

    def test_egress_keyword_is_the_default(self):
        self.assertEqual(self.tz(cca_tz="egress", info={"timezone": "Europe/Berlin"}), "Europe/Berlin")

    def test_auto_uses_shell_tz(self):
        self.assertEqual(self.tz(cca_tz="auto", info={"timezone": "Asia/Shanghai"},
                                 shell_tz="America/Los_Angeles"), "America/Los_Angeles")

    def test_auto_is_case_insensitive(self):
        self.assertEqual(self.tz(cca_tz="AUTO", info=None, shell_tz="Europe/Berlin"), "Europe/Berlin")

    def test_auto_skips_non_iana_shell_tz(self):
        """"PST8PDT" 这类缩写不是 IANA 名，不去用它。"""
        with mock.patch.object(env.Path, "read_text", return_value="America/Chicago\n"):
            self.assertEqual(self.tz(cca_tz="auto", info=None, shell_tz="PST8PDT"), "America/Chicago")

    def test_explicit_iana_wins_over_egress(self):
        self.assertEqual(self.tz(cca_tz="Asia/Tokyo", info={"timezone": "Asia/Shanghai"}), "Asia/Tokyo")


if __name__ == "__main__":
    unittest.main()


class ProbeTzTest(unittest.TestCase):
    """起会话的路径（handoff / takeover）用 probe_tz：跟出口，CCA_TZ 可覆盖。"""

    def probe(self, *, cca_tz: str | None, timezone: str, shell_tz: str = "America/Los_Angeles"):
        environ = {k: v for k, v in os.environ.items() if k != "CCA_TZ"}
        if cca_tz is not None:
            environ["CCA_TZ"] = cca_tz
        info = {"timezone": timezone}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(env, "SHELL_TZ", shell_tz), \
                mock.patch.object(env, "profile_kind", return_value="oauth"), \
                mock.patch.object(env, "proxy_settings", return_value=("http://p", False)), \
                mock.patch.object(env.egress, "probe_cached", return_value=info):
            return env.probe_tz("001")

    def test_follows_egress(self):
        self.assertEqual(self.probe(cca_tz=None, timezone="Asia/Tokyo"), "Asia/Tokyo")

    def test_cca_tz_auto_overrides_egress(self):
        self.assertEqual(self.probe(cca_tz="auto", timezone="Asia/Tokyo"), "America/Los_Angeles")

    def test_cca_tz_explicit_overrides_egress(self):
        self.assertEqual(self.probe(cca_tz="Europe/Berlin", timezone="Asia/Tokyo"), "Europe/Berlin")

    def test_probe_failure_falls_back(self):
        with mock.patch.object(env, "profile_kind", return_value="oauth"), \
                mock.patch.object(env, "proxy_settings", return_value=("http://p", False)), \
                mock.patch.object(env.egress, "probe_cached", return_value=None):
            self.assertEqual(env.probe_tz("001"), env.CLAUDE_TZ)

    def test_build_env_does_not_touch_tz(self):
        """辅助子进程（claude agents 等）保持继承来的 TZ，别为它们多探一次。"""
        environ = {k: v for k, v in os.environ.items() if k != "CCA_TZ"}
        environ["TZ"] = "Asia/Shanghai"
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(env, "tui_mode", return_value="default"), \
                mock.patch.object(env, "proxy_settings", return_value=(None, False)), \
                mock.patch.object(env, "profile_kind", return_value="oauth"):
            self.assertEqual(env.build_env("001").get("TZ"), "Asia/Shanghai")


class ProbeCacheTest(unittest.TestCase):
    def test_probe_once_per_proxy(self):
        with mock.patch.object(egress, "_PROBE_CACHE", {}), \
                mock.patch.object(egress, "probe", return_value={"timezone": "Asia/Tokyo"}) as probe:
            egress.probe_cached("http://p")
            egress.probe_cached("http://p")
            egress.probe_cached("http://q")
        self.assertEqual(probe.call_count, 2)


    def test_cached_result_expires_so_a_failed_probe_is_retried(self):
        results = iter([None, {"timezone": "Asia/Tokyo"}])
        clock = iter([0.0, 10.0, egress.PROBE_TTL + 1])
        with mock.patch.object(egress, "_PROBE_CACHE", {}), \
                mock.patch.object(egress.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(egress, "probe", side_effect=lambda _p: next(results)) as probe:
            self.assertIsNone(egress.probe_cached("http://p"))
            self.assertIsNone(egress.probe_cached("http://p"))          # TTL 内沿用
            self.assertEqual(egress.probe_cached("http://p"), {"timezone": "Asia/Tokyo"})
        self.assertEqual(probe.call_count, 2)


class ProbeSkipTest(unittest.TestCase):
    """CCA_TZ 写死时不要去探测出口：handoff/takeover 会快 3 秒，也不依赖网络。"""

    def test_fixed_tz_skips_probe(self):
        with mock.patch.dict(os.environ, {"CCA_TZ": "America/Los_Angeles"}, clear=False), \
                mock.patch.object(env, "profile_kind", return_value="oauth"), \
                mock.patch.object(env, "proxy_settings", return_value=("http://p", False)), \
                mock.patch.object(env.egress, "probe_cached") as probe:
            self.assertEqual(env.probe_tz("001"), "America/Los_Angeles")
        probe.assert_not_called()

    def test_egress_still_probes(self):
        with mock.patch.dict(os.environ, {"CCA_TZ": "egress"}, clear=False), \
                mock.patch.object(env, "profile_kind", return_value="oauth"), \
                mock.patch.object(env, "proxy_settings", return_value=("http://p", False)), \
                mock.patch.object(env.egress, "probe_cached", return_value={"timezone": "Asia/Tokyo"}) as probe:
            self.assertEqual(env.probe_tz("001"), "Asia/Tokyo")
        probe.assert_called_once()


class RunningSessionTzTest(unittest.TestCase):
    """只有 claude 进程才算会话：带 CLAUDE_CONFIG_DIR 的后台脚本不能误伤。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def add_proc(self, pid: int, argv: list, environ: list):
        folder = self.root / str(pid)
        folder.mkdir()
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (folder / "environ").write_bytes(b"\0".join(e.encode() for e in environ) + b"\0")

    def tz(self):
        with mock.patch.object(env, "profile_dir", return_value=Path("/accounts/b004")):
            return env.running_session_tz("b004", self.root)

    def test_ignores_scripts_that_only_carry_the_config_dir(self):
        self.add_proc(101, ["sleep", "600"],
                      ["CLAUDE_CONFIG_DIR=/accounts/b004", "TZ=Asia/Shanghai"])
        self.assertEqual(self.tz(), [])

    def test_keeps_real_claude_session(self):
        self.add_proc(102, ["claude", "bg-spare", "--bg-spare", "/run/x.sock"],
                      ["CLAUDE_CONFIG_DIR=/accounts/b004", "TZ=America/Los_Angeles"])
        self.assertEqual(self.tz(), [(102, "America/Los_Angeles")])

    def test_keeps_npm_installed_claude(self):
        self.add_proc(103, ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"],
                      ["CLAUDE_CONFIG_DIR=/accounts/b004", "TZ=America/Los_Angeles"])
        self.assertEqual(self.tz(), [(103, "America/Los_Angeles")])

    def test_missing_tz_is_reported(self):
        self.add_proc(104, ["claude", "--resume", "abc"], ["CLAUDE_CONFIG_DIR=/accounts/b004"])
        self.assertEqual(self.tz(), [(104, "(未设置)")])

    def test_other_account_is_ignored(self):
        self.add_proc(105, ["claude"], ["CLAUDE_CONFIG_DIR=/accounts/001", "TZ=Asia/Shanghai"])
        self.assertEqual(self.tz(), [])
