"""进度短语：照 Claude Code 的 AgentSummary。模型用假的回复，不真调。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import sideagent  # noqa: E402

_CALLER = None


def setUpModule():
    """测试不碰真的登记表：父进程链上可能就是正在跑的主会话 claude。"""
    global _CALLER  # noqa: PLW0603
    _CALLER = mock.patch.object(sideagent, "caller_now", return_value={})
    _CALLER.start()


def tearDownModule():
    _CALLER.stop()
from cca.session import summary  # noqa: E402

TURN = [
    {"kind": "task", "text": "跑一遍测试，修好失败的"},
    {"kind": "say", "text": "先跑测试。"},
    {"kind": "tool", "name": "Bash", "id": "t1", "input": {"command": "make test", "description": "Run tests"}},
    {"kind": "tool_result", "id": "t1", "text": "1 failed", "error": True},
    {"kind": "tool", "name": "Read", "id": "t2", "input": {"file_path": "/w/src/app.py"}},
    {"kind": "tool_result", "id": "t2", "text": "..."},
    {"kind": "tool", "name": "Edit", "id": "t3", "input": {"file_path": "/w/src/app.py"}},
]


class NotesTest(unittest.TestCase):
    def test_notes_shape_like_claude_code(self):
        notes = summary.build_notes(TURN, "/w")
        self.assertEqual(notes, (
            "<task>\n跑一遍测试，修好失败的\n</task>\n"
            "<recent_steps>\n"
            "said: 先跑测试。\n"
            "did: Bash - make test (described as: Run tests) (failed)\n"
            "did: Read - Reading src/app.py\n"
            "</recent_steps>\n"
            "<started_not_finished>\n"
            "Edit - Editing src/app.py\n"
            "</started_not_finished>"))

    def test_only_this_turn_and_not_subagents(self):
        events = [{"kind": "task", "text": "上一轮"}, {"kind": "say", "text": "旧的话"},
                  *TURN, {"kind": "tool", "name": "Grep", "id": "x", "parent": "a1", "input": {"pattern": "p"}}]
        notes = summary.build_notes(events, "/w")
        self.assertNotIn("旧的话", notes)
        self.assertNotIn("Grep", notes)

    def test_interject_does_not_start_a_new_turn(self):
        events = [*TURN, {"kind": "task", "text": "顺便看下 README", "interject": True}]
        self.assertIn("跑一遍测试", summary.build_notes(events, "/w"))

    def test_nothing_to_describe(self):
        self.assertEqual(summary.build_notes([{"kind": "task", "text": "x"}]), "")

    def test_budget_keeps_latest_steps(self):
        many = [{"kind": "task", "text": "t"}] + [
            {"kind": "say", "text": f"第 {index} 步 " + "很长的话" * 40} for index in range(50)]
        notes = summary.build_notes(many)
        self.assertIn("第 49 步", notes)
        self.assertNotIn("第 0 步", notes)
        self.assertLessEqual(summary.estimate(notes), summary.NOTES_BUDGET + 50)

    def test_xml_escaped(self):
        notes = summary.build_notes([{"kind": "task", "text": "t"}, {"kind": "say", "text": "a <b> & c"}])
        self.assertIn("a &lt;b&gt; &amp; c", notes)


class PromptTest(unittest.TestCase):
    def test_user_prompt_has_previous_and_language_rule(self):
        text = summary.user_prompt("<recent_steps>\nx\n</recent_steps>", "Running tests")
        self.assertIn("Previous: Running tests — keep it if the agent is still doing that; "
                      "otherwise say what is new.", text)
        self.assertIn("Describe the agent's most recent action in 3-5 words", text)
        self.assertTrue(text.endswith(summary.LANGUAGE_DEFAULT))
        self.assertNotIn("Previous:", summary.user_prompt("n"))
        self.assertTrue(summary.SYSTEM_PROMPT.startswith("You write a 3-5 word progress label"))

    def test_accept_rejects_bad_replies(self):
        self.assertEqual(summary.accept("Fixing null check in app.py"), "Fixing null check in app.py")
        self.assertEqual(summary.accept('"Running auth tests"'), "Running auth tests")
        self.assertEqual(summary.accept("`Reading app.py`"), "Reading app.py")
        self.assertEqual(summary.accept("<label>Reading app.py</label>"), "Reading app.py")
        self.assertIsNone(summary.accept("Line one\nLine two"))
        self.assertIsNone(summary.accept("x" * 101))
        self.assertIsNone(summary.accept("```Reading```"))
        self.assertIsNone(summary.accept("# Reading"))
        self.assertIsNone(summary.accept("   "))


class ModelConfigTest(unittest.TestCase):
    def test_default_file_env_and_off(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {}, clear=False):
            profile = Path(folder)
            import os  # noqa: PLC0415
            os.environ.pop("CCA_SIDEAGENT_SUMMARY_MODEL", None)
            self.assertEqual(summary.configured_model(profile), "haiku")
            summary.model_file(profile).write_text("claude-haiku-4-5\n", encoding="utf-8")
            self.assertEqual(summary.configured_model(profile), "claude-haiku-4-5")
            summary.model_file(profile).write_text("off\n", encoding="utf-8")
            self.assertEqual(summary.configured_model(profile), "")
            with mock.patch.dict("os.environ", {"CCA_SIDEAGENT_SUMMARY_MODEL": "sonnet"}):
                self.assertEqual(summary.configured_model(profile), "sonnet")

    def test_run_model_args_and_parse(self):
        done = mock.Mock(returncode=0, stdout=json.dumps({
            "result": "Running tests", "is_error": False,
            "usage": {"input_tokens": 300, "cache_read_input_tokens": 0, "output_tokens": 6}}), stderr="")
        with mock.patch("shutil.which", return_value="/bin/claude"), \
                mock.patch.object(summary.subprocess, "run", return_value=done) as run:
            out = summary.run_model("haiku", "提示", {"X": "1"}, "/w")
        self.assertEqual(out, {"ok": True, "text": "Running tests", "tokens_in": 300, "tokens_out": 6})
        args = run.call_args.args[0]
        for flag in ("--model", "--system-prompt", "--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--effort") + 1], "low")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["DISABLE_PROMPT_CACHING"], "1")
        self.assertEqual(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "2108")
        self.assertEqual(env["X"], "1")

    def test_run_model_failure(self):
        done = mock.Mock(returncode=1, stdout=json.dumps({"result": "model not found", "is_error": True}),
                         stderr="")
        with mock.patch("shutil.which", return_value="/bin/claude"), \
                mock.patch.object(summary.subprocess, "run", return_value=done):
            out = summary.run_model("nope", "p", {}, "")
        self.assertFalse(out["ok"])
        self.assertIn("model not found", out["error"])


class ServerSummaryTest(unittest.TestCase):
    """服务端每 30 秒那一次：用假的模型回复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for patcher in (mock.patch.object(sideagent, "SIDEAGENT_DIR", self.root),
                        mock.patch.object(sideagent, "profile_dir", return_value=self.root / "profile"),
                        mock.patch.dict("os.environ", {"CCA_SIDEAGENT_SUMMARY_MODEL": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = sideagent._SideagentServer("ds#t1", "/w", 60, [])  # noqa: SLF001
        sideagent._write_state("ds#t1", {"pid": 1})  # noqa: SLF001
        self.replies = []
        self.prompts = []

        def fake(model, prompt, env, cwd):
            self.prompts.append((model, prompt, cwd))
            return self.replies.pop(0)
        self.server.summary_runner = fake
        self.server.busy = True

    def write_events(self, events):
        sideagent._events_path("ds#t1").write_text(  # noqa: SLF001
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")

    def test_writes_label_and_skips_when_unchanged(self):
        self.write_events(TURN)
        self.replies = [{"ok": True, "text": "Fixing failing test in app.py"}]
        self.assertEqual(self.server.summarize_once(), "ok")
        state = sideagent._read_state("ds#t1")  # noqa: SLF001
        self.assertEqual(state["summary"], "Fixing failing test in app.py")
        self.assertEqual(state["summary_calls"], 1)
        self.assertEqual(self.prompts[0][0], "haiku")
        # 最近几步没变：不调
        self.assertEqual(self.server.summarize_once(), "skip unchanged")
        # 有了新的一步：调，并带上上一句
        self.write_events([*TURN, {"kind": "tool_result", "id": "t3", "text": "ok"}])
        self.replies = [{"ok": True, "text": "Line one\nLine two"}]
        self.assertEqual(self.server.summarize_once(), "unusable")
        self.assertIn("Previous: Fixing failing test in app.py", self.prompts[1][1])
        # 不合格的回复丢掉，沿用上一句
        self.assertEqual(sideagent._read_state("ds#t1")["summary"], "Fixing failing test in app.py")  # noqa: SLF001
        log = sideagent._summary_log_path("ds#t1").read_text(encoding="utf-8")  # noqa: SLF001
        self.assertIn(" ok ", log)
        self.assertIn(" unusable ", log)

    def test_failure_keeps_previous_and_records_error(self):
        self.write_events(TURN)
        self.replies = [{"ok": False, "error": "model not found"}]
        self.assertEqual(self.server.summarize_once(), "fail")
        state = sideagent._read_state("ds#t1")  # noqa: SLF001
        self.assertEqual(state.get("summary", ""), "")
        self.assertEqual(state["summary_error"], "model not found")

    def test_idle_or_off_does_not_call(self):
        self.write_events(TURN)
        self.server.busy = False
        self.assertEqual(self.server.summarize_once(), "skip idle")
        self.server.busy = True
        (self.root / "profile").mkdir()
        summary.model_file(self.root / "profile").write_text("off", encoding="utf-8")
        self.assertEqual(self.server.summarize_once(), "skip off")
        self.assertEqual(self.prompts, [])

    def test_new_turn_keeps_label_as_previous(self):
        # 照 Claude Code：做完、闲着、再接活时都显示上一句；记录重新数，下一次它就是 Previous
        self.server.summary_text = "Old label"
        self.server.summary_notes = "x"
        self.server._start_turn_books()  # noqa: SLF001
        self.assertEqual((self.server.summary_text, self.server.summary_notes), ("Old label", ""))

    def test_label_is_written_to_progress(self):
        self.write_events(TURN)
        self.replies = [{"ok": True, "text": "Fixing app.py"}]
        self.server.summarize_once()
        self.assertIn("SUMMARY Fixing app.py", self.server.progress.read_text(encoding="utf-8"))

    def test_status_carries_summary(self):
        sideagent._write_state("ds#t1", {"pid": 1, "busy": True, "summary": "Running tests"})  # noqa: SLF001
        self.assertEqual(sideagent.status("ds#t1")["summary"], "Running tests")


if __name__ == "__main__":
    unittest.main()
