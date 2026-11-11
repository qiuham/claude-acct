"""sideagent 卡住判断与 api_retry：喂假 stream-json 行，不起 claude。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from sidehelp import use_temp_root  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca import env  # noqa: E402
from cca.account import store  # noqa: E402
from cca.session import sideagent  # noqa: E402
from cca.session import sideagent_server as server_mod  # noqa: E402

os.environ.setdefault("CCA_WATCHDOG_AUTOSTART", "0")   # 子进程里跑 cca 时别真的拉起换号常驻进程


def _line(**event) -> str:
    return json.dumps(event, ensure_ascii=False) + "\n"


def _retry(attempt=2, status=504, **extra) -> str:
    return _line(type="system", subtype="api_retry", attempt=attempt, max_retries=10,
                 retry_delay_ms=1000, error_status=status, error="x", **extra)


class StallDefaultTest(unittest.TestCase):
    def test_default_is_900(self):
        code = "from cca.session import sideagent; print(sideagent.STALL_SECONDS)"
        environ = {k: v for k, v in os.environ.items() if k != "CCA_SIDEAGENT_STALL"}
        environ["PYTHONPATH"] = str(REPO / "src")
        out = subprocess.run([sys.executable, "-c", code], env=environ, capture_output=True,
                             text=True, check=True)
        self.assertEqual(out.stdout.strip(), "900")

    def test_env_overrides(self):
        code = "from cca.session import sideagent; print(sideagent.STALL_SECONDS)"
        environ = {**os.environ, "CCA_SIDEAGENT_STALL": "42", "PYTHONPATH": str(REPO / "src")}
        out = subprocess.run([sys.executable, "-c", code], env=environ, capture_output=True,
                             text=True, check=True)
        self.assertEqual(out.stdout.strip(), "42")


class ApiRetryTest(unittest.TestCase):
    def setUp(self):
        use_temp_root(self)
        self.server = server_mod._SideagentServer("gem#t1", "/w", 60, [])  # noqa: SLF001
        sideagent._write_state("gem#t1", {"pid": 1})  # noqa: SLF001
        self.server.busy = True

    def state(self):
        return sideagent._read_state("gem#t1")  # noqa: SLF001

    def test_status_code_is_recorded_and_shown(self):
        self.server._note_progress(_retry(attempt=2, status=504))  # noqa: SLF001
        text = "API 没回应：504 网关超时，第 2/10 次重试"
        self.assertEqual(self.state()["api_retry"], text)
        self.assertIn("RETRY " + text, self.server.progress.read_text(encoding="utf-8"))
        sideagent._write_state("gem#t1", {**self.state(), "busy": True})  # noqa: SLF001
        self.assertEqual(sideagent.doing("gem#t1"), text)

    def test_status_reports_it_when_busy(self):
        self.server._note_progress(_retry())  # noqa: SLF001
        sideagent._write_state("gem#t1", {**self.state(), "busy": True})  # noqa: SLF001
        with mock.patch.object(sideagent, "running", return_value=True):
            info = sideagent.status("gem#t1")
        self.assertIn("504 网关超时", info["doing"])
        self.assertIn("第 2/10 次重试", info["api_retry"])

    def test_first_byte_timeout_summary(self):
        line = _retry(attempt=1, status=None, no_response={"waited_ms": 184000, "retry_wait_ms": 599000})
        self.server._note_progress(line)  # noqa: SLF001
        self.assertIn("首字节超时（等了 184 秒没有响应）", self.state()["api_retry"])

    def test_cleared_by_normal_output(self):
        for line in (_line(type="assistant", message={"content": [{"type": "text", "text": "hi"}]}),
                     _line(type="stream_event", event={"type": "message_start", "message": {}})):
            self.server._note_progress(_retry())  # noqa: SLF001
            self.assertTrue(self.state()["api_retry"])
            self.server._note_progress(line)  # noqa: SLF001
            self.assertEqual(self.state()["api_retry"], "")
            sideagent._write_state("gem#t1", {**self.state(), "busy": True})  # noqa: SLF001
            self.assertNotIn("API 没回应", sideagent.doing("gem#t1"))

    def test_subagent_stream_does_not_clear(self):
        self.server._note_progress(_retry())  # noqa: SLF001
        self.server._note_progress(_line(type="stream_event", parent_tool_use_id="t1",  # noqa: SLF001
                                         event={"type": "message_start", "message": {}}))
        self.assertIn("API 没回应", self.state()["api_retry"])

    def test_malformed_numbers_do_not_kill_reader(self):
        for attempt, limit, want in (("3/10", None, "第 0 次重试"), (float("inf"), "x", "第 0 次重试"),
                                     (2, None, "第 2 次重试"), (2, 10, "第 2/10 次重试")):
            line = _line(type="system", subtype="api_retry", attempt=attempt, max_retries=limit,
                         error_status=504, error="x")
            self.server._note_progress(line)  # noqa: SLF001
            self.assertIn(want, self.state()["api_retry"])

    def test_late_retry_after_turn_ended_is_ignored(self):
        self.server.busy = False
        self.server._note_progress(_retry())  # noqa: SLF001
        self.assertFalse(self.state().get("api_retry"))

    def test_stale_retry_is_hidden_when_not_busy(self):
        self.server._note_progress(_retry())  # noqa: SLF001
        sideagent._write_state("gem#t1", {**self.state(), "busy": False})  # noqa: SLF001
        self.assertNotIn("API 没回应", sideagent.doing("gem#t1"))
        with mock.patch.object(sideagent, "running", return_value=True):
            self.assertNotIn("api_retry", sideagent.status("gem#t1"))

    def test_new_turn_restarts_stall_clock(self):
        self.server.last_output = time.time() - 5000   # 空闲了很久
        self.server._start_turn_books()  # noqa: SLF001
        self.assertFalse(self.server._stalled())  # noqa: SLF001


class StallTextTest(unittest.TestCase):
    def setUp(self):
        use_temp_root(self)
        self.server = server_mod._SideagentServer("gem#t2", "/w", 60, [])  # noqa: SLF001
        sideagent._write_state("gem#t2", {"pid": 1})  # noqa: SLF001
        self.server.busy = True

    def test_without_retry_states_the_fact_only(self):
        text = self.server._stall_text(950)  # noqa: SLF001
        self.assertEqual(text, "卡住了：950 秒没有任何输出，claude 进程没有动静")
        self.assertNotIn("多半", text)

    def test_with_retry_reports_last_error(self):
        self.server._note_progress(_retry(attempt=3, status=504))  # noqa: SLF001
        text = self.server._stall_text(950)  # noqa: SLF001
        self.assertIn("950 秒没有任何输出", text)
        self.assertIn("504 网关超时", text)
        self.assertIn("第 3/10 次重试", text)

    def test_give_up_writes_it_and_clears_retry_state(self):
        self.server._note_progress(_retry())  # noqa: SLF001
        self.server.last_output = time.time() - 1000
        self.assertTrue(self.server._stalled())  # noqa: SLF001
        self.server._give_up()  # noqa: SLF001
        state = sideagent._read_state("gem#t2")  # noqa: SLF001
        self.assertIn("504 网关超时", state["stalled"])
        self.assertEqual(state["api_retry"], "")
        self.assertIn("STUCK 卡住了", self.server.progress.read_text(encoding="utf-8"))

    def test_not_stalled_under_threshold(self):
        self.server.last_output = time.time() - 400
        self.assertFalse(self.server._stalled())  # noqa: SLF001

    def test_cc_giving_up_reports_failed_with_real_error(self):
        self.server._note_progress(_retry(attempt=10, status=504))  # noqa: SLF001
        self.server.notify_turn = True
        self.server.turn_waited = True   # 等结果的调用方断了：由 sideagent 自己回报
        event = {"type": "result", "is_error": True, "subtype": "success",
                 "result": "API Error: 504 status code (no body)"}
        with mock.patch.object(self.server, "_notify_main") as notify:
            self.server._end_turn(None, event)  # noqa: SLF001
        notify.assert_called_once()
        kwargs = notify.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("API Error: 504", kwargs["error"])
        self.assertIn("第 10/10 次重试", kwargs["error"])


class InterruptedTurnTest(unittest.TestCase):
    def test_interrupted_error_result_is_not_failed(self):
        use_temp_root(self)
        server = server_mod._SideagentServer("gem#t3", "/w", 60, [])  # noqa: SLF001
        sideagent._write_state("gem#t3", {"pid": 1})  # noqa: SLF001
        server.busy = True
        server.notify_turn = True
        server.turn_waited = True
        server.turn_interrupted = True
        event = {"type": "result", "is_error": True, "subtype": "error_during_execution", "result": "partial"}
        with mock.patch.object(server, "_notify_main") as notify:
            server._end_turn(None, event)  # noqa: SLF001
        notify.assert_called_once()
        self.assertNotIn("status", notify.call_args.kwargs)


class GatewayEnvTest(unittest.TestCase):
    KEY = "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"

    def marker(self, provider="generic", **env_vars):
        return {"kind": "api", "provider": provider, "base_url": "https://gateway.example.com:8080",
                "env": dict(env_vars), "proxy": "none"}

    def test_gateway_markers_get_it(self):
        self.assertEqual(store.api_env_vars(self.marker())[self.KEY], "330000")
        discovery = self.marker(CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="1")
        self.assertEqual(store.api_env_vars(discovery)[self.KEY], "330000")

    def test_external_generic_endpoint_is_not_gateway(self):
        for url in ("https://api.other-vendor.net/anthropic", "https://evilexample.com", ""):
            marker = {**self.marker(), "base_url": url}
            self.assertNotIn(self.KEY, store.api_env_vars(marker))

    def test_direct_providers_do_not(self):
        for provider in ("deepseek", "openrouter"):
            self.assertNotIn(self.KEY, store.api_env_vars(self.marker(provider)))
        # openrouter 预设自带 GATEWAY_MODEL_DISCOVERY，也不算网关号
        discovery = self.marker("openrouter", CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="1")
        self.assertNotIn(self.KEY, store.api_env_vars(discovery))

    def test_marker_env_not_overridden(self):
        got = store.api_env_vars(self.marker(**{self.KEY: "100000"}))
        self.assertEqual(got[self.KEY], "100000")

    def test_write_settings_env_injects_and_preserves(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            profile = Path(tmp) / ".claude-accounts" / "gem"
            profile.mkdir(parents=True)
            self.assertEqual(store.write_settings_env("gem", self.marker())["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"],
                             "330000")
            settings = json.loads((profile / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(settings["env"][self.KEY], "330000")
            # 清单是唯一真相：settings.json 里残留的旧值会跟着清单（含默认）同步，不会冻结
            settings["env"][self.KEY] = "250000"
            (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            self.assertEqual(store.write_settings_env("gem", self.marker())[self.KEY], "330000")
            self.assertEqual(store.write_settings_env("gem", self.marker(**{self.KEY: "400000"}))[self.KEY],
                             "400000")

    def build(self, kind, marker, inherited=None):
        with mock.patch.object(env, "tui_mode", return_value="default"), \
                mock.patch.object(env, "profile_kind", return_value=kind), \
                mock.patch.object(env, "read_marker", return_value=(marker, "")), \
                mock.patch.object(env, "proxy_settings", return_value=(None, False)), \
                mock.patch.dict(os.environ, inherited or {}, clear=False):
            return env.build_env("gem")

    def test_build_env_paths(self):
        self.assertEqual(self.build("api", self.marker())[self.KEY], "330000")
        self.assertNotIn(self.KEY, self.build("api", self.marker("deepseek")))
        self.assertNotIn(self.KEY, self.build("oauth", None))
        self.assertEqual(self.build("api", self.marker(**{self.KEY: "1"}))[self.KEY], "1")
        # shell 里自己设的值优先于网关默认，清单 env 写了则以清单为准
        self.assertEqual(self.build("api", self.marker(), {self.KEY: "600000"})[self.KEY], "600000")
        self.assertEqual(self.build("api", self.marker(**{self.KEY: "1"}), {self.KEY: "600000"})[self.KEY], "1")


if __name__ == "__main__":
    unittest.main()
