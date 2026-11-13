"""MCP 转接层：只测协议处理，不真起sideagent。"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca import mcp_sideagent  # noqa: E402
from cca.session import inbox, jobs, sideagent  # noqa: E402

# 格式改了（一个 sideagent 一个文件夹）：sideagents 目录放在临时目录下一层，名单目录 sessions 和它同级


def _side(tmp) -> Path:
    root = Path(tmp) / "sideagents"
    root.mkdir(exist_ok=True)
    return root


def _rec(root: Path, name: str, part: str = "result.json") -> Path:
    """sideagents/<名字>/<文件>（原来的 <名字>.last.json 等）。"""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder / part

_CALLER = None


_PATCHES = []


def setUpModule():
    """测试不碰真的登记表：父进程链上可能就是正在跑的主会话 claude。"""
    global _CALLER  # noqa: PLW0603
    _CALLER = mock.patch.object(sideagent, "caller_now", return_value={})
    _CALLER.start()
    # 测试里的进程号都是编的：一律当活着；也只看测试自己的登记表，不去扫本机真的各个号
    for patcher in (mock.patch.object(sideagent, "_pid_alive", lambda pid: pid > 0),
                    mock.patch.object(sideagent, "_session_dirs", lambda first: [first])):
        patcher.start()
        _PATCHES.append(patcher)


def tearDownModule():
    _CALLER.stop()
    for patcher in _PATCHES:
        patcher.stop()


class HandleTest(unittest.TestCase):
    def test_initialize(self):
        out = mcp_sideagent.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(out["result"]["serverInfo"]["name"], "sideagents")
        self.assertIn("protocolVersion", out["result"])

    def test_tools_list_names(self):
        out = mcp_sideagent.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in out["result"]["tools"]]
        self.assertEqual(names, ["sideagent_send", "sideagent_result", "sideagent_interrupt",
                                 "sideagent_status", "sideagent_takeover", "sideagent_release",
                                 "sideagent_accounts", "sideagent_stop"])

    def test_unknown_method(self):
        out = mcp_sideagent.handle({"jsonrpc": "2.0", "id": 3, "method": "没有这个"})
        self.assertEqual(out["error"]["code"], -32601)

    def test_notification_has_no_response(self):
        self.assertIsNone(mcp_sideagent.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_tool(self):
        out = mcp_sideagent.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                               "params": {"name": "没有这个工具", "arguments": {}}})
        self.assertEqual(out["error"]["code"], -32602)

    def test_send_requires_message(self):
        out = mcp_sideagent.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "sideagent_send", "arguments": {"account": "x"}}})
        self.assertIn("要给 account 和 message", out["result"]["content"][0]["text"])


class ProgressAndContextTest(unittest.TestCase):
    def test_progress_tail(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sideagent, "SIDEAGENT_DIR", _side(tmp)):
            _rec(_side(tmp), "ds", "progress").write_text("TOOL Bash\nRESULT 完成\n", encoding="utf-8")
            self.assertEqual(mcp_sideagent._progress_tail("ds"), ["TOOL Bash", "RESULT 完成"])  # noqa: SLF001

    def test_caller_context_reads_transcript_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "cfg"
            encoded = "-" + str(Path.cwd()).strip("/").replace("/", "-")
            project = folder / "projects" / encoded
            project.mkdir(parents=True)
            entries = [
                {"message": {"role": "user", "content": [{"type": "text", "text": "看下 README"}]}},
                {"message": {"role": "assistant", "content": [{"type": "text", "text": "好的"}]}},
            ]
            (project / "sid-1.jsonl").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(folder),
                                                "CLAUDE_CODE_SESSION_ID": "sid-1"}):
                text = mcp_sideagent._caller_context()  # noqa: SLF001
            self.assertIn("看下 README", text)
            self.assertIn("好的", text)


class JobTest(unittest.TestCase):
    def test_job_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sideagent, "SIDEAGENT_DIR", _side(tmp)):
            mcp_sideagent._write_job(  # noqa: SLF001
            "ds", {"account": "ds", "state": "running"})
            self.assertEqual(mcp_sideagent._read_job("ds")["state"], "running")  # noqa: SLF001
            self.assertIn('"state": "running"', mcp_sideagent._tool_result({"account": "ds"}))  # noqa: SLF001

    def test_result_without_job(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sideagent, "SIDEAGENT_DIR", _side(tmp)):
            self.assertIn('"none"', mcp_sideagent._tool_result({"account": "nobody"}))  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()


class ResolveTest(unittest.TestCase):
    """一个号下可能有多个任务：查状态、派活都要先认准是哪一个。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _side(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(sideagent, "SIDEAGENT_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 假登记表：本会话是进程 100、编号 会话-1
        sessions = self.root / "sessions"
        sessions.mkdir()
        me = {"pid": 100, "procStart": "p100", "sessionId": "会话-1", "messagingSocketPath": "/m.sock"}
        (sessions / "100.json").write_text(json.dumps(me), encoding="utf-8")
        for patcher in (mock.patch.object(sideagent, "_caller_sessions", return_value=sessions),
                        mock.patch.object(sideagent, "caller_now", return_value=me)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _state(self, name: str, **extra):
        _rec(self.root, name, "state.json").write_text(json.dumps(extra), encoding="utf-8")

    def test_instances_lists_numbered_tasks(self):
        self._state("ds#a1b2c3")
        self._state("ds#d4e5f6")
        self._state("other#zzz")
        self.assertEqual(mcp_sideagent._instances("ds"), ["ds#a1b2c3", "ds#d4e5f6"])  # noqa: SLF001

    def test_status_of_account_lists_every_task(self):
        self._state("ds#a1b2c3", session_id="s1")
        self._state("ds#d4e5f6", session_id="s2")
        with mock.patch.object(sideagent, "running", return_value=True):
            out = json.loads(mcp_sideagent._tool_status({"account": "ds"}))  # noqa: SLF001
        self.assertIsInstance(out, list)
        self.assertEqual([row["name"] for row in out], ["ds#a1b2c3", "ds#d4e5f6"])
        self.assertTrue(out[0]["running"])

    def test_status_single_task_is_an_object(self):
        self._state("ds#a1b2c3", session_id="s1")
        with mock.patch.object(sideagent, "running", return_value=True):
            out = json.loads(mcp_sideagent._tool_status({"account": "ds"}))  # noqa: SLF001
        self.assertEqual(out["name"], "ds#a1b2c3")

    def test_account_name_always_starts_a_new_instance(self):
        # 同号已经有实例在跑，也不塞给它：派新活一律新开（像 Agent）
        self._state("ds#a1b2c3", owner_session="会话-1")
        with mock.patch.object(sideagent, "running", return_value=True), \
                mock.patch.object(sideagent, "prepare", return_value=("ds#new", True)) as prepare:
            self.assertEqual(mcp_sideagent._route("ds", label="查日志", description="查日志",  # noqa: SLF001
                                                  model="sonnet"), ("ds#new", True))
        prepare.assert_called_once_with("ds", context="", label="查日志", description="查日志", model="sonnet",
                                        caller_agent="")

    def test_instance_name_goes_to_that_running_instance(self):
        self._state("ds#a1b2c3", owner_session="会话-1")
        with mock.patch.object(sideagent, "running", return_value=True), \
                mock.patch.object(sideagent, "prepare") as prepare:
            self.assertEqual(mcp_sideagent._route("ds#a1b2c3"), ("ds#a1b2c3", False))  # noqa: SLF001
        prepare.assert_not_called()

    def test_exited_instance_is_resumed_from_its_session(self):
        _rec(self.root, "ds#a1b2c3").write_text(json.dumps({
            "account": "ds#a1b2c3", "session_id": "s-123", "cwd": "/w", "label": "查日志", "model": "opus",
            "caller_agent": "agent-7",
            "owner_session": "会话-1", "jobs": []}), encoding="utf-8")
        with mock.patch.object(sideagent, "running", return_value=False), \
                mock.patch.object(sideagent, "transcript_exists", return_value=True), \
                mock.patch.object(sideagent, "ensure", return_value=True) as ensure:
            self.assertEqual(mcp_sideagent._route("ds#a1b2c3"), ("ds#a1b2c3", True))  # noqa: SLF001
        ensure.assert_called_once_with("ds#a1b2c3", cwd="/w", resume="s-123", label="查日志",
                                       description="", model="opus", caller_agent="agent-7")

    def test_exited_instance_without_record_is_an_error(self):
        with mock.patch.object(sideagent, "running", return_value=False), \
                self.assertRaises(sideagent.SideagentError) as caught:
            mcp_sideagent._route("ds#gone00")  # noqa: SLF001
        self.assertIn("找不到它的会话记录", str(caught.exception))
        _rec(self.root, "ds#gone00").write_text(json.dumps({"session_id": "s-9", "jobs": []}),
                                                       encoding="utf-8")
        with mock.patch.object(sideagent, "running", return_value=False), \
                mock.patch.object(sideagent, "transcript_exists", return_value=False), \
                self.assertRaises(sideagent.SideagentError) as caught:
            mcp_sideagent._route("ds#gone00")  # noqa: SLF001
        self.assertIn("s-9", str(caught.exception))

    def test_route_starts_new_when_nothing_runs(self):
        with mock.patch.object(sideagent, "running", return_value=False), \
                mock.patch.object(sideagent, "prepare", return_value=("ds#new", True)) as prepare:
            self.assertEqual(mcp_sideagent._route("ds"), ("ds#new", True))  # noqa: SLF001
        prepare.assert_called_once()

    def test_all_status_only_shows_this_session(self):
        self._state("mine#a", owner_session="会话-1")
        self._state("other#b", owner_session="会话-2")
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "会话-1"}), \
                mock.patch.object(sideagent, "running", return_value=True):
            text = mcp_sideagent._all_status()  # noqa: SLF001
        self.assertIn("mine#a", text)
        self.assertNotIn("other#b", text)

    def test_other_sessions_tasks_are_invisible(self):
        self._state("ds#mine", owner_session="会话-1")
        self._state("ds#theirs", owner_session="会话-2")
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "会话-1"}):
            self.assertEqual(mcp_sideagent._instances("ds"), ["ds#mine"])  # noqa: SLF001
            self.assertEqual(mcp_sideagent._instances("ds#theirs"), [])  # noqa: SLF001

    def test_route_refuses_another_sessions_task(self):
        self._state("ds#theirs", owner_session="会话-2")
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "会话-1"}), \
                self.assertRaises(sideagent.SideagentError):
            mcp_sideagent._route("ds#theirs")  # noqa: SLF001

    def test_job_from_another_session_is_skipped(self):
        _rec(self.root, "ds").write_text(
            json.dumps({"account": "ds", "state": "finished", "text": "别人的",
                        "owner_session": "会话-2"}), encoding="utf-8")
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "会话-1"}):
            self.assertEqual(mcp_sideagent._read_job("ds")["state"], "none")  # noqa: SLF001

    def test_last_result_files_are_not_agents(self):
        self._state("ds#a1")
        _rec(self.root, "ds#a1").write_text(json.dumps({"account": "ds#a1", "state": "finished"}),
                                                   encoding="utf-8")
        self.assertEqual(mcp_sideagent._instances("ds"), ["ds#a1"])  # noqa: SLF001
        self.assertNotIn("ds#a1.last", mcp_sideagent._all_status())  # noqa: SLF001

    def test_result_prefers_the_running_task(self):
        # 停掉的旧任务（空结果）和正在跑的任务各一条记录
        _rec(self.root, "ds#old").write_text(json.dumps(
            {"account": "ds#old", "jobs": [{"id": "1", "state": "finished", "text": "",
                                            "started": 100.0}]}), encoding="utf-8")
        _rec(self.root, "ds#live").write_text(json.dumps(
            {"account": "ds#live", "jobs": [{"id": "2", "state": "running", "message": "干活",
                                             "started": 200.0}]}), encoding="utf-8")
        with mock.patch.object(sideagent, "running", side_effect=lambda name: name == "ds#live"), \
                mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": ""}):
            data = mcp_sideagent._read_job("ds")  # noqa: SLF001
        self.assertEqual(data["account"], "ds#live")
        self.assertEqual(data["state"], "running")

    def test_jobs_of_one_agent_are_kept_apart(self):
        path = _rec(self.root, "ds")
        write = mcp_sideagent._write_job  # noqa: SLF001
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": ""}):
            write("ds", {"id": "a", "account": "ds", "state": "running",
                         "started": 1.0, "message": "第一件"})
            write("ds", {"id": "b", "account": "ds", "state": "running",
                         "started": 2.0, "message": "第二件"})
            write("ds", {"id": "a", "account": "ds", "state": "finished",
                         "text": "第一件做完了", "started": 1.0})
        jobs = json.loads(path.read_text(encoding="utf-8"))["jobs"]
        self.assertEqual([job["id"] for job in jobs], ["a", "b"])
        self.assertEqual(jobs[0]["text"], "第一件做完了")

    def test_queue_note_says_queued_not_interrupt(self):
        self.assertEqual(mcp_sideagent._queue_note({"busy": False}), "")  # noqa: SLF001
        note = mcp_sideagent._queue_note({"busy": True, "queued": 2})  # noqa: SLF001
        self.assertIn("正忙", note)
        self.assertIn("前面还有 3 条", note)


class JobLifecycleTest(unittest.TestCase):
    """任务编号、按编号取结果、打断、停掉、插话：都在临时目录里，不起真的 sideagent。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _side(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for patcher in (mock.patch.object(sideagent, "SIDEAGENT_DIR", self.root),
                        mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "会话-1"}),
                        mock.patch.object(sideagent, "stop_later")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _jobs(self, name="ds#a"):
        return json.loads(_rec(self.root, name).read_text(encoding="utf-8"))["jobs"]

    def _wait_state(self, name, job_id, states):
        import time  # noqa: PLC0415
        for _ in range(200):
            for job in self._jobs(name):
                if job["id"] == job_id and job["state"] in states:
                    return job
            time.sleep(0.01)
        msg = f"{job_id} 没变成 {states}"
        raise AssertionError(msg)

    def _send(self, ask_result, account="ds", *, started=False, running=True, busy=True, **arguments):
        with mock.patch.object(mcp_sideagent, "_route", return_value=("ds#a", started)), \
                mock.patch.object(sideagent, "running", return_value=running), \
                mock.patch.object(sideagent, "status", return_value={"busy": busy, "queued": 0}), \
                mock.patch.object(sideagent, "ask", return_value=ask_result) as ask:
            text = mcp_sideagent._tool_send({"account": account, "message": "干活", **arguments})  # noqa: SLF001
            job_id = re.search(r"任务编号 (\w+)", text).group(1)
            job = self._wait_state("ds#a", job_id, ("finished", "merged", "interrupted"))
        return text, job, ask

    def test_send_returns_job_id_and_result_takes_it(self):
        text, job, _ask = self._send({"text": "做完了", "turns": 2, "duration_ms": 10})
        self.assertIn(job["id"], text)
        self.assertEqual(job["text"], "做完了")
        with mock.patch.object(sideagent, "running", return_value=False):
            out = json.loads(mcp_sideagent._tool_result({"account": "ds", "job_id": job["id"]}))  # noqa: SLF001
            missing = json.loads(mcp_sideagent._tool_result({"account": "ds", "job_id": "没有"}))  # noqa: SLF001
        self.assertEqual(out["text"], "做完了")
        self.assertEqual(missing["state"], "none")

    def test_full_name_defaults_to_interject_like_send_message(self):
        # 给实例全名发消息，不传 mode：照 SendMessage，在它下一个工具调用结束时送到
        text, job, ask = self._send({"merged": True, "merged_into": "j0"}, account="ds#a")
        self.assertIn("在它下一个工具调用结束时送到", text)
        self.assertIn("它要是先做完，会用这条消息接着开、做完再回报一次；等那条回报，别重发", text)
        self.assertEqual(job["state"], "merged")
        self.assertEqual(job["merged_into"], "j0")
        self.assertTrue(ask.call_args.kwargs["interject"])

    def test_reply_texts(self):
        text, _job, _ask = self._send({"text": "好"}, account="ds#a", started=True, running=False)
        self.assertIn("正在用它的会话记录接着开（上下文还在）", text)
        text, _job, _ask = self._send({"text": "好"}, account="ds#a", busy=False)
        self.assertIn("它闲着，马上开始处理", text)
        text, _job, ask = self._send({"text": "好"}, account="ds#a", mode="queue")
        self.assertIn("这一轮干完再单独开一轮", text)
        self.assertFalse(ask.call_args.kwargs["interject"])
        text, _job, _ask = self._send({"text": "好"})
        self.assertIn("已在后台开始", text)

    def test_bad_mode_is_refused(self):
        self.assertIn("mode", mcp_sideagent._tool_send({"account": "ds", "message": "x", "mode": "别的"}))  # noqa: SLF001

    def test_interrupted_turn_is_recorded(self):
        _text, job, _ask = self._send({"text": "停在这", "interrupted": True})
        self.assertEqual(job["state"], "interrupted")
        self.assertEqual(job["text"], "停在这")

    def test_interrupt_tool_marks_running_job(self):
        write = mcp_sideagent._write_job  # noqa: SLF001
        write("ds#a", {"id": "j1", "account": "ds#a", "state": "running", "started": 1.0})
        with mock.patch.object(mcp_sideagent, "_running_instances", return_value=["ds#a"]), \
                mock.patch.object(sideagent, "interrupt", return_value={"type": "cca_ack", "busy": True}) as hit:
            text = mcp_sideagent._tool_interrupt({"account": "ds"})  # noqa: SLF001
        hit.assert_called_once_with("ds#a")
        self.assertIn("interrupted", text)
        self.assertEqual(self._jobs()[0]["state"], "interrupted")
        # 后台线程随后写的 finished 不盖掉 interrupted，但正文留下
        write("ds#a", {"id": "j1", "state": "finished", "text": "半截"})
        self.assertEqual(self._jobs()[0]["state"], "interrupted")
        self.assertEqual(self._jobs()[0]["text"], "半截")

    def test_interrupt_idle_says_nothing_to_do(self):
        with mock.patch.object(mcp_sideagent, "_running_instances", return_value=["ds#a"]), \
                mock.patch.object(sideagent, "interrupt", return_value={"type": "cca_ack", "busy": False}):
            self.assertIn("闲着", mcp_sideagent._tool_interrupt({"account": "ds"}))  # noqa: SLF001

    def test_interrupt_without_instance(self):
        with mock.patch.object(mcp_sideagent, "_running_instances", return_value=[]):
            self.assertIn("没有在跑", mcp_sideagent._tool_interrupt({"account": "ds"}))  # noqa: SLF001

    def test_stop_marks_stopped_and_keeps_it(self):
        write = mcp_sideagent._write_job  # noqa: SLF001
        write("ds#a", {"id": "j1", "account": "ds#a", "state": "running", "started": 1.0})
        write("ds#a", {"id": "j0", "account": "ds#a", "state": "finished", "text": "早的", "started": 0.5})
        _rec(self.root, "ds#a", "state.json").write_text(json.dumps({"owner_session": "会话-1"}), encoding="utf-8")
        with mock.patch.object(sideagent, "_stop") as stop:
            mcp_sideagent._tool_stop({"account": "ds#a"})  # noqa: SLF001
        stop.assert_called_once_with(["ds#a"])
        jobs = {job["id"]: job for job in self._jobs()}
        self.assertEqual(jobs["j1"]["state"], "stopped")
        self.assertEqual(jobs["j0"]["state"], "finished")
        write("ds#a", {"id": "j1", "state": "failed", "error": "连接断了"})
        self.assertEqual({job["id"]: job for job in self._jobs()}["j1"]["state"], "stopped")

    def test_pick_order_within_and_across_instances(self):
        rank = mcp_sideagent._rank  # noqa: SLF001
        self.assertGreater(rank({"state": "running"}, alive=True), rank({"state": "finished", "text": "x"}, alive=True))
        self.assertGreater(rank({"state": "finished", "text": "x"}, alive=True), rank({"state": "interrupted"}, alive=True))
        self.assertGreater(rank({"state": "interrupted"}, alive=True), rank({"state": "stopped"}, alive=True))
        self.assertEqual(rank({"state": "running"}, alive=False), rank({"state": "failed"}, alive=False))
        # 跨实例：新的停掉的空结果不盖过旧的有内容的
        _rec(self.root, "ds#old").write_text(json.dumps({"account": "ds#old", "jobs": [
            {"id": "1", "state": "finished", "text": "有内容", "started": 100.0}]}), encoding="utf-8")
        _rec(self.root, "ds#new").write_text(json.dumps({"account": "ds#new", "jobs": [
            {"id": "2", "state": "stopped", "started": 300.0},
            {"id": "3", "state": "interrupted", "started": 200.0}]}), encoding="utf-8")
        with mock.patch.object(sideagent, "running", return_value=False):
            self.assertEqual(mcp_sideagent._read_job("ds")["id"], "1")  # noqa: SLF001
            self.assertEqual(mcp_sideagent._pick_job("ds#new", [  # noqa: SLF001
                {"id": "2", "state": "stopped", "started": 300.0},
                {"id": "3", "state": "interrupted", "started": 200.0}])["id"], "3")

    def test_result_of_running_job_has_doing_and_stale(self):
        mcp_sideagent._write_job("ds#a", {"id": "j1", "account": "ds#a",  # noqa: SLF001
                                          "state": "running", "started": 1.0})
        status = {"tokens_in": 12300, "tokens_out": 400, "token_count": 9000,
                  "stale": True, "stale_note": "旧版本，需重启"}
        with mock.patch.object(sideagent, "running", return_value=True), \
                mock.patch.object(sideagent, "doing", return_value="Bash(make test)"), \
                mock.patch.object(sideagent, "status", return_value=status):
            out = json.loads(mcp_sideagent._tool_result({"account": "ds#a"}))  # noqa: SLF001
        self.assertEqual(out["doing"], "Bash(make test)")
        self.assertTrue(out["stale"])
        self.assertEqual(out["tokens_in"], 12300)
        self.assertEqual(out["token_count"], 9000)

    def test_busy_old_version_still_says_interjected(self):
        """“旧版”只说明代码改过：插话照样送进正在跑的这一轮，回话不能说成排到这一轮之后。"""
        reply = mcp_sideagent._send_reply("ds#a", "j1", {"busy": True, "stale": True},  # noqa: SLF001
                                          interject=True, new=False, resumed=False)
        self.assertIn("下一个工具调用结束时送到", reply)
        self.assertNotIn("排在后面", reply)

    def test_interject_note(self):
        note = mcp_sideagent._interject_note  # noqa: SLF001
        # 旧版本进程也认插话：照样送进正在跑的这一轮
        self.assertIn("正在跑的这一轮", note({"stale": True, "busy": True}))
        self.assertIn("正在跑的这一轮", note({"busy": True}))
        self.assertIn("闲着", note({"busy": False}))


class MySessionTest(unittest.TestCase):
    """“是不是本会话的”只看一个来源：按归属到登记表找主会话现在的进程，是不是我这个进程。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sessions = Path(self.tmp.name)
        self.me = {"pid": 100, "procStart": "p100", "sessionId": "会话-新"}
        (self.sessions / "100.json").write_text(json.dumps(self.me), encoding="utf-8")
        for patcher in (mock.patch.object(sideagent, "_caller_sessions", return_value=self.sessions),
                        mock.patch.object(sideagent, "caller_now", return_value=self.me),
                        mock.patch.dict("os.environ", {"CLAUDE_CODE_SESSION_ID": "会话-旧"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_same_process_even_after_session_id_changed(self):
        # 派活时编号还是“会话-旧”，后来主会话换了编号：按进程认，还是本会话的
        self.assertTrue(mcp_sideagent._is_mine(  # noqa: SLF001
            {"owner_pid": 100, "owner_proc_start": "p100", "owner_session": "会话-旧"}))
        self.assertEqual(mcp_sideagent._my_session(), "会话-新")  # noqa: SLF001

    def test_reopened_main_session_follows_by_session_id(self):
        # 原来的进程 55 没了，同一段对话在新进程 100 里接着：按最近看到的编号认
        self.assertTrue(mcp_sideagent._is_mine(  # noqa: SLF001
            {"owner_pid": 55, "owner_proc_start": "p55", "owner_session": "会话-新"}))
        # 进程号被别的程序复用（启动时刻对不上），编号也不是我的：不是本会话的
        self.assertFalse(mcp_sideagent._is_mine(  # noqa: SLF001
            {"owner_pid": 100, "owner_proc_start": "别的", "owner_session": "别人的"}))
        self.assertTrue(mcp_sideagent._is_mine({}))  # noqa: SLF001

    def test_new_job_records_owner_process(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sideagent, "SIDEAGENT_DIR", _side(tmp)), \
                mock.patch.object(sideagent, "ask", return_value={"text": "好"}), \
                mock.patch.object(sideagent, "stop_later"):
            job = mcp_sideagent._start_job("ds#a", "干活", {})  # noqa: SLF001
            import time  # noqa: PLC0415
            for _ in range(200):
                data = json.loads(_rec(_side(tmp), "ds#a").read_text(encoding="utf-8"))
                if data["jobs"][0].get("state") == "finished":
                    break
                time.sleep(0.01)
            out = json.loads(mcp_sideagent._tool_result({"account": "ds#a", "job_id": job}))  # noqa: SLF001
        self.assertEqual((out["owner_session"], out["owner_pid"]), ("会话-新", 100))


class LostJobNotifyTest(unittest.TestCase):
    """sideagent 没交结果就断了（被强杀）：它自己没机会通知，派活这边补一条，只补一条。"""

    def run_job(self, tmp, error):
        with mock.patch.object(sideagent, "SIDEAGENT_DIR", _side(tmp)), \
                mock.patch.object(inbox, "INBOX_DIR", Path(tmp) / "inbox"), \
                mock.patch.object(jobs, "me", return_value={"sessionId": "会话-9", "messagingSocketPath": ""}), \
                mock.patch.object(sideagent, "ask", side_effect=error):
            mcp_sideagent._start_job("ds#a", "查一下日志", {})  # noqa: SLF001
            import time  # noqa: PLC0415
            for _ in range(200):
                if inbox.pending(["会话-9"]):
                    break
                time.sleep(0.01)
            return [inbox.read(path)["text"] for path in inbox.pending(["会话-9"])]

    def test_connection_dropped_notifies_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            texts = self.run_job(tmp, sideagent.SideagentError("ds#a: 连接断了，没拿到结果"))
        self.assertEqual(len(texts), 1)
        self.assertIn("failed", texts[0])
        self.assertIn("连接断了", texts[0])


class ExitedRecordTest(unittest.TestCase):
    def test_status_and_result_of_exited_instance_keep_session(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sideagent, "SIDEAGENT_DIR", _side(tmp)), \
                mock.patch.object(sideagent, "running", return_value=False):
            _rec(_side(tmp), "ds#a1").write_text(json.dumps({
                "account": "ds#a1", "session_id": "s-1", "code_version": "abc",
                "jobs": [{"id": "j1", "state": "finished", "text": "好"}]}), encoding="utf-8")
            status = mcp_sideagent._status_of("ds#a1")  # noqa: SLF001
            result = json.loads(mcp_sideagent._tool_result({"account": "ds#a1"}))  # noqa: SLF001
        self.assertEqual((status["session_id"], status["code_version"]), ("s-1", "abc"))
        self.assertEqual((result["session_id"], result["code_version"]), ("s-1", "abc"))


class CallerAgentRouteTest(unittest.TestCase):
    def test_send_passes_caller_agent_to_new_instance(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sideagent, "SIDEAGENT_DIR", _side(tmp)), \
                mock.patch.object(sideagent, "prepare", return_value=("ds#n1", True)) as prepare, \
                mock.patch.object(sideagent, "status", return_value={"busy": False}), \
                mock.patch.object(sideagent, "ask", return_value={"text": "好"}), \
                mock.patch.object(sideagent, "stop_later"):
            mcp_sideagent._tool_send({"account": "ds", "message": "干活", "caller_agent": "a1",  # noqa: SLF001
                                      "background": False})
        self.assertEqual(prepare.call_args.kwargs["caller_agent"], "a1")


class DieReasonTest(unittest.TestCase):
    def test_send_failure_returns_the_reason(self):
        """cca 拒绝执行时把 stderr 里的原因回给模型，不只说“详情见 stderr”。"""
        def boom(_arguments):
            print("错误: profile astra 不存在", file=sys.stderr)  # noqa: T201 - 模拟 die
            raise SystemExit(1)

        with mock.patch.dict(mcp_sideagent.TOOL_HANDLERS, {"sideagent_status": boom}):
            out = mcp_sideagent.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                        "params": {"name": "sideagent_status",
                                                   "arguments": {"account": "astra"}}})
        result = out["result"]
        self.assertTrue(result.get("isError"))
        self.assertIn("astra 不存在", result["content"][0]["text"])


class AccountsTest(unittest.TestCase):
    """sideagent_accounts：照 cca ls 的行，给模型挑号要用的几个字段。"""

    @staticmethod
    def _row(name, kind, **extra):
        row = {"profile": name, "kind": kind, "provider": "", "base_url": "", "api_label": "",
               "logged_in": True, "usage": {"windows": {}}, "background": {"running": 0}}
        row.update(extra)
        return row

    def _run(self, rows, markers, keys):
        with mock.patch("cca.account.model.discover_profiles", return_value=[r["profile"] for r in rows]), \
             mock.patch("cca.report._fetch_list_rows", return_value=rows), \
             mock.patch("cca.account.store.read_marker",
                        side_effect=lambda name: (markers.get(name), None)), \
             mock.patch("cca.account.store.read_api_key", side_effect=lambda name: keys.get(name, "")):
            out = mcp_sideagent.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                        "params": {"name": "sideagent_accounts", "arguments": {}}})
        return {one["name"]: one for one in json.loads(out["result"]["content"][0]["text"])}

    def test_lists_names_family_and_quota(self):
        window = {"percent": 100, "resets_at": 0, "reset_passed": False}
        rows = [
            self._row("003", "oauth", usage={"windows": {"five_hour": {"percent": 45, "resets_at": 0,
                                                                        "reset_passed": False}}}),
            self._row("002", "oauth", usage={"windows": {"seven_day": window}}),
            self._row("ds", "api", provider="deepseek", api_label="api:deepseek", background={"running": 1}),
            self._row("openai", "api", provider="generic", api_label="api:gateway.example.com"),
            self._row("nokey", "api", provider="generic"),
        ]
        markers = {"ds": {"env": {"ANTHROPIC_MODEL": "deepseek-flash[1m]"}},
                   "openai": {"env": {"ANTHROPIC_MODEL": "gpt-6-astra"}}, "nokey": {}}
        got = self._run(rows, markers, {"ds": "k", "openai": "k"})
        self.assertEqual(got["003"]["family"], "Claude")
        self.assertEqual(got["003"]["five_hour_used"], "45%")
        self.assertTrue(got["003"]["usable"])
        self.assertFalse(got["002"]["usable"])
        self.assertIn("额度用满", got["002"]["why"])
        self.assertEqual((got["ds"]["family"], got["ds"]["running"]), ("DeepSeek", 1))
        # GPT 号名字因人而异：按模型名认出家族，不靠号名
        self.assertEqual((got["openai"]["family"], got["openai"]["model"]), ("GPT", "gpt-6-astra"))
        self.assertEqual(got["openai"]["endpoint"], "api:gateway.example.com")
        self.assertFalse(got["nokey"]["usable"])
        self.assertEqual(got["nokey"]["why"], "没配 key")

    def test_no_accounts(self):
        with mock.patch("cca.account.model.discover_profiles", return_value=[]):
            self.assertIn("没有号", mcp_sideagent._tool_accounts({}))  # noqa: SLF001


class AccountPriorityTest(unittest.TestCase):
    """sideagent_accounts 的顺序就是推荐顺序：订阅号按周额度最早重置在前。"""

    def test_subscriptions_by_weekly_reset_then_api_then_unusable(self):
        import time  # noqa: PLC0415

        now = time.time()

        def sub(name: str, hours: float, *, usable: bool = True) -> tuple:
            row = {"usage": {"windows": {"seven_day": {"percent": 50, "resets_at": now + hours * 3600,
                                                       "reset_passed": False}}}}
            return {"name": name, "kind": "claude 订阅", "usable": usable}, row

        api = ({"name": "ds", "kind": "api 按量", "usable": True}, {})
        pairs = [sub("b001", 48), api, sub("001", 70, usable=False), sub("b004", 3), sub("b005", 120)]
        ranked = mcp_sideagent._by_priority(pairs)  # noqa: SLF001
        self.assertEqual([e["name"] for e in ranked], ["b004", "b001", "b005", "ds", "001"])
        self.assertEqual([e.get("priority") for e in ranked], [1, 2, 3, None, None])
