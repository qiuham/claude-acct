"""跨号sideagent：只管参数解析和事件解析，真起 claude 的链路用人工验证。

（真链路要联网、要账号，放单元测试里又慢又不稳。）
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import roster, sideagent, sideagent_cli  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from sidehelp import use_temp_root, write_roster, write_state  # noqa: E402

os.environ.setdefault("CCA_WATCHDOG_AUTOSTART", "0")   # 子进程里跑 cca 时别真的拉起换号常驻进程

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


@contextlib.contextmanager
def _env(name: str, value: str):
    saved = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


class SplitTest(unittest.TestCase):
    def test_name_then_text(self):
        name, rest, flags = sideagent._split(["002", "看下", "tests"])  # noqa: SLF001
        self.assertEqual(name, "002")
        self.assertEqual(rest, ["看下", "tests"])
        self.assertEqual(flags, {})

    def test_own_flags_with_space_and_equals(self):
        name, rest, flags = sideagent._split(  # noqa: SLF001
            ["002", "--cwd", "/工作区/a", "--idle=5", "--timeout", "60"],
        )
        self.assertEqual(name, "002")
        self.assertEqual(rest, [])
        self.assertEqual(flags["--cwd"], "/工作区/a")
        self.assertEqual(flags["--idle"], "5")
        self.assertEqual(flags["--timeout"], "60")

    def test_claude_flags_pass_through(self):
        name, rest, _flags = sideagent._split(  # noqa: SLF001
            ["002", "--model", "claude-opus-5-5", "干活"],
        )
        self.assertEqual(name, "002")
        self.assertEqual(rest, ["--model", "claude-opus-5-5", "干活"])


class EventTest(unittest.TestCase):
    def test_parse_ignores_garbage(self):
        self.assertIsNone(sideagent._parse(b"not json"))  # noqa: SLF001
        self.assertIsNone(sideagent._parse(b"[]"))  # noqa: SLF001

    def test_extracts_text_blocks_only(self):
        event = {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "内部"},
            {"type": "text", "text": "收到"},
            {"type": "text", "text": "。"},
        ]}}
        self.assertEqual(sideagent._text_of(event), "收到。")  # noqa: SLF001

    def test_result_is_terminal(self):
        printer = sideagent._Printer(raw=True)  # noqa: SLF001
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(printer.feed(b'{"type":"result"}'))
            self.assertFalse(printer.feed(b'{"type":"assistant"}'))

    def test_streams_text_deltas(self):
        printer = sideagent._Printer(raw=False)  # noqa: SLF001
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            printer.feed('{"type":"stream_event","event":{"type":"content_block_delta",'
                         '"delta":{"type":"text_delta","text":"收"}}}'.encode())
            printer.feed('{"type":"stream_event","event":{"type":"content_block_delta",'
                         '"delta":{"type":"text_delta","text":"到"}}}'.encode())
        self.assertEqual(out.getvalue(), "收到")

    def test_announces_tool_start(self):
        printer = sideagent._Printer(raw=False)  # noqa: SLF001
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            printer.feed(b'{"type":"stream_event","event":{"type":"content_block_start",'
                         b'"content_block":{"type":"tool_use","name":"Bash"}}}')
        self.assertIn("→ 工具 Bash", out.getvalue())


class CallerDirTest(unittest.TestCase):
    def test_no_config_dir_is_unmanaged_session(self):
        # sideagent 只在 cca 起的会话里用；没有 CLAUDE_CONFIG_DIR 就明确报错，不猜是哪个号
        saved = os.environ.pop("CLAUDE_CONFIG_DIR", None)
        try:
            with self.assertRaises(SystemExit):
                sideagent._caller_sessions()  # noqa: SLF001
        finally:
            if saved is not None:
                os.environ["CLAUDE_CONFIG_DIR"] = saved

    def test_reads_session_dir(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/cfg/a"}):
            self.assertEqual(str(sideagent._caller_sessions()), "/cfg/a/sessions")  # noqa: SLF001


class AccountLabelTest(unittest.TestCase):
    """号可以带标签：ds:2 是 ds 号上的第二个sideagent。"""

    def test_plain_name(self):
        self.assertEqual(sideagent._account_of("ds"), "ds")  # noqa: SLF001

    def test_hash_keeps_account(self):
        self.assertEqual(sideagent._account_of("ds#a1b2c3"), "ds")  # noqa: SLF001

    def test_hash_names_are_unique(self):
        first = sideagent._oneshot_name("ds")  # noqa: SLF001
        second = sideagent._oneshot_name("ds")  # noqa: SLF001
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("ds#"))


class TaskEventTest(unittest.TestCase):
    """Claude Code 的后台任务事件记成 bgtask；样例是真机 stream-json 抓下来的。"""

    def test_real_stream_lines(self):
        lines = (Path(__file__).parent / "stream-task-events.jsonl").read_text(encoding="utf-8").splitlines()
        got = [sideagent.task_event(json.loads(line)) for line in lines]
        started = [one for one in got if one["subtype"] == "task_started"]
        self.assertEqual({one["task_type"] for one in started}, {"local_bash", "local_agent"})
        agent = next(one for one in started if one["task_type"] == "local_agent")
        self.assertEqual(agent["description"], "say hello")
        self.assertTrue(agent["tool_use_id"].startswith("toolu_"))
        self.assertNotIn("prompt", agent)
        updated = [one for one in got if one["subtype"] == "task_updated"]
        self.assertTrue(all(one["status"] == "completed" for one in updated))
        done = [one for one in got if one["subtype"] == "task_notification"]
        self.assertTrue(all(one["status"] == "completed" and one["tool_use_id"] for one in done))
        self.assertTrue(all(one["kind"] == "bgtask" for one in got))

    def test_progress_and_other_system_events(self):
        got = sideagent.task_event({"type": "system", "subtype": "task_progress", "task_id": "w1",
                                    "usage": {"total_tokens": 1200, "tool_uses": 3, "duration_ms": 5},
                                    "last_tool_name": "Bash", "workflow_progress": [{"phase": "Review", "x": {}}]})
        self.assertEqual(got["usage"], {"total_tokens": 1200, "tool_uses": 3, "duration_ms": 5})
        self.assertEqual(got["workflow_progress"], [{}])
        self.assertIsNone(sideagent.task_event({"type": "system", "subtype": "init"}))

    def test_workflow_progress_kept_whole_and_counted(self):
        """清单超过 30 项也全留（以前只留前 30 项，后面阶段的子代理面板看不到）；大字段砍掉。"""
        items = [{"type": "workflow_phase", "index": 1, "title": "Generate"},
                 {"type": "workflow_phase", "index": 2, "title": "Verify"}]
        items += [{"type": "workflow_agent", "index": i, "label": f"gen:{i}", "phaseIndex": 1,
                   "phaseTitle": "Generate", "state": "done", "promptPreview": "x" * 500} for i in range(1, 41)]
        items += [{"type": "workflow_agent", "index": 41, "label": "verify:1", "phaseIndex": 2,
                   "phaseTitle": "Verify", "state": "progress"},
                  {"type": "workflow_agent", "index": 42, "label": "verify:2", "phaseIndex": 2,
                   "phaseTitle": "Verify", "state": "error", "error": "boom"}]
        got = sideagent.task_event({"type": "system", "subtype": "task_progress", "task_id": "w1",
                                    "workflow_progress": items})
        self.assertEqual(len(got["workflow_progress"]), 44)
        self.assertNotIn("promptPreview", got["workflow_progress"][2])
        self.assertEqual(got["workflow_counts"], {"total": 42, "done": 40, "error": 1, "running": 1,
                                                  "phase": "Verify", "phases": ["Generate", "Verify"]})

    def test_workflow_counts_all_done_uses_last_phase(self):
        items = [{"type": "workflow_agent", "index": 1, "phaseIndex": 1, "phaseTitle": "A", "state": "done"},
                 {"type": "workflow_agent", "index": 2, "phaseIndex": 2, "phaseTitle": "B", "state": "done"}]
        self.assertEqual(sideagent.workflow_counts(items)["phase"], "B")
        self.assertEqual(sideagent.workflow_counts([]), {"total": 0, "done": 0, "error": 0, "running": 0})

    def test_workflow_path_is_safe(self):
        path = sideagent.workflow_path("ds#abc", "../w 1")
        self.assertEqual(path.name, "w1.json")
        self.assertEqual(path.parent.name, "workflows")


class ToolSummaryTest(unittest.TestCase):
    """进度行只写最短的一句说明。"""

    def test_prefers_command(self):
        self.assertEqual(sideagent._tool_summary({"command": "  ls  -la  "}), "ls -la")  # noqa: SLF001

    def test_uses_file_path(self):
        self.assertEqual(sideagent._tool_summary({"file_path": "/a/b.py"}), "/a/b.py")  # noqa: SLF001

    def test_empty_input(self):
        self.assertEqual(sideagent._tool_summary(None), "")  # noqa: SLF001
        self.assertEqual(sideagent._tool_summary({}), "")  # noqa: SLF001

    def test_tool_summary_skill(self):
        got = sideagent._tool_summary({"skill": "code-review", "args": "--max-findings  all --high"})  # noqa: SLF001
        self.assertEqual(got, "/code-review --max-findings all --high")


class InboundSettingTest(unittest.TestCase):
    """跨会话消息自动接收：缺就补，用户写了别的值就不动。"""

    def _write(self, folder: Path, data: dict) -> None:
        (folder / "settings.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _read(self, folder: Path) -> dict:
        return json.loads((folder / "settings.json").read_text(encoding="utf-8"))

    def test_adds_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp, _env("CLAUDE_CONFIG_DIR", tmp):
            self._write(Path(tmp), {"model": "opus"})
            sideagent._ensure_inbound_setting()  # noqa: SLF001
            data = self._read(Path(tmp))
            self.assertEqual(data["crossSessionInbound"], "accept")
            self.assertEqual(data["model"], "opus")

    def test_creates_file_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp, _env("CLAUDE_CONFIG_DIR", tmp):
            sideagent._ensure_inbound_setting()  # noqa: SLF001
            self.assertEqual(self._read(Path(tmp))["crossSessionInbound"], "accept")

    def test_keeps_explicit_other_value(self):
        with tempfile.TemporaryDirectory() as tmp, _env("CLAUDE_CONFIG_DIR", tmp):
            self._write(Path(tmp), {"crossSessionInbound": "ask"})
            sideagent._ensure_inbound_setting()  # noqa: SLF001
            self.assertEqual(self._read(Path(tmp))["crossSessionInbound"], "ask")

    def test_accept_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp, _env("CLAUDE_CONFIG_DIR", tmp):
            self._write(Path(tmp), {"crossSessionInbound": "accept"})
            before = (Path(tmp) / "settings.json").stat().st_mtime_ns
            sideagent._ensure_inbound_setting()  # noqa: SLF001
            after = (Path(tmp) / "settings.json").stat().st_mtime_ns
            self.assertEqual(before, after)


class PublishTest(unittest.TestCase):
    """登记表镜像：只拷内容，不建软链。"""

    def test_copies_json_and_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = Path(tmp) / "src", Path(tmp) / "dst"
            source.mkdir()
            dest.mkdir()
            (source / "123.json").write_text("{}", encoding="utf-8")
            (source / "123.key").write_text("k", encoding="utf-8")
            server = sideagent._SideagentServer("x", "/工作区", 60, [])  # noqa: SLF001
            server.mirrored = set()
            server._publish(source, dest, 123)  # noqa: SLF001
            self.assertTrue((dest / "123.json").is_file())
            self.assertTrue((dest / "123.key").is_file())
            server._unmirror()  # noqa: SLF001
            self.assertFalse((dest / "123.json").exists())


class StateTest(unittest.TestCase):
    def test_not_running_when_no_state(self):
        self.assertFalse(sideagent.running("没有这个号"))


if __name__ == "__main__":
    unittest.main()


class OwnerTest(unittest.TestCase):
    """sideagent 归谁：面板按会话编号隔离，会话结束一并停掉。"""

    def test_owner_is_the_main_claude_process(self):
        entry = {"pid": 42, "procStart": "p42", "sessionId": "会话-1", "name": "x"}
        self.assertEqual(sideagent.owner_of(entry), {"pid": 42, "proc_start": "p42", "session": "会话-1"})
        with _env("CCA_OWNER_ACCOUNT", "002"):
            self.assertEqual(sideagent._owner_account(), "002")  # noqa: SLF001

    def test_account_from_config_dir(self):
        folder = str(Path.home() / ".claude-accounts" / "003")
        with contextlib.ExitStack() as stack:
            stack.enter_context(_env("CLAUDE_CONFIG_DIR", folder))
            os.environ.pop("CCA_OWNER_ACCOUNT", None)
            self.assertEqual(sideagent._account_from_config(), "003")  # noqa: SLF001

    def test_config_dir_outside_accounts_is_rejected(self):
        with _env("CLAUDE_CONFIG_DIR", "/tmp/别的目录"), self.assertRaises(SystemExit):  # noqa: S108 - 只是字符串
            sideagent._account_from_config()  # noqa: SLF001

    def test_stop_owned_stops_only_that_session(self):
        # 格式改了：按会话名单认（sessions/<编号>.json），不再扫状态文件看 owner_session
        use_temp_root(self)
        for name in ("ds#a", "ds#b", "ds#c", "ds#done"):
            write_state(name, kind="spawned")
        write_state("003#bg-x", kind="attached")
        sideagent.mark_finished("ds#done")
        write_roster("会话-1", {"ds#a": "spawned", "ds#done": "spawned", "003#bg-x": "attached"})
        write_roster("会话-2", {"ds#b": "spawned"})
        stopped = []
        with mock.patch.object(sideagent, "_stop", side_effect=lambda args: stopped.append(args[0])):
            sideagent.stop_owned("会话-1")
        # 接管的不停（只当作解开），已经停了的不再停一遍
        self.assertEqual(stopped, ["ds#a"])

    def test_roster_entry_round_trip(self):
        # 原来的 .owner 纸条：横条名字、描述、哪个子代理派的，现在记在主会话名单的那一条里
        use_temp_root(self)
        owner = {"session": "会话-1", "account": "003", "pid": 7, "proc_start": "p7", "instance_socket": "/m.sock"}
        roster.add(owner, "ds#a", roster.SPAWNED, label="查日志", description="看看", caller_agent="a1")
        data = roster.read("会话-1")
        self.assertEqual((data["account"], data["pid"], data["instance_socket"]), ("003", 7, "/m.sock"))
        entry = data["agents"]["ds#a"]
        self.assertEqual((entry["kind"], entry["label"], entry["description"], entry["caller_agent"]),
                         ("spawned", "查日志", "看看", "a1"))
        since = entry["since"]
        # 再加一次只更新给了值的字段，since 不变
        roster.add(owner, "ds#a", roster.SPAWNED, label="新名字")
        entry = roster.read("会话-1")["agents"]["ds#a"]
        self.assertEqual((entry["label"], entry["description"], entry["since"]), ("新名字", "看看", since))

    def test_cleanup_removes_only_expired_folders(self):
        # 原来按 .progress 的修改时间清（留 1 天）；现在按 state.json 的 finished_at，留 SIDEAGENT_KEEP_DAYS 天
        root = use_temp_root(self)
        day = 86400
        now = time.time()
        write_state("ds#old", kind="spawned", finished_at=now - (roster.SIDEAGENT_KEEP_DAYS + 1) * day)
        (root / "ds#old" / "events.jsonl").write_text("x", encoding="utf-8")
        write_state("ds#fresh", kind="spawned", finished_at=now - day)
        write_state("ds#alive", kind="spawned")
        roster.cleanup(force=True)
        self.assertFalse((root / "ds#old").exists())
        self.assertTrue((root / "ds#fresh" / "state.json").exists())
        self.assertTrue((root / "ds#alive" / "state.json").exists())


# ---- 下面是打断、插话、停掉、回报、词元这些新功能的测试 ----

import socket  # noqa: E402
import threading  # noqa: E402


class _FakeStdin:
    """假的 claude 标准输入：记下写进去的每一行。"""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.extend(json.loads(line) for line in text.splitlines() if line.strip())

    def flush(self):
        pass


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.pid = 999999

    def poll(self):
        return None


class _ServerCase(unittest.TestCase):
    """在临时目录里造一个不起 claude 的 _SideagentServer。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "sideagents"
        self.root.mkdir()
        patcher = mock.patch.object(sideagent, "SIDEAGENT_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = sideagent._SideagentServer("ds#t1", str(self.root), 60, [])  # noqa: SLF001
        self.server.process = _FakeProcess()
        sideagent._write_state("ds#t1", {"pid": 1})  # noqa: SLF001

    def pair(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        return ours, theirs

    def reply_of(self, sock):
        sock.settimeout(2)
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.split(b"\n")[0]) if data.strip() else {}


class WorkflowEventServerTest(_ServerCase):
    def test_workflow_progress_goes_to_its_own_file(self):
        """完整清单覆盖写 workflows/<任务编号>.json；事件文件里只留几个数。"""
        items = [{"type": "workflow_agent", "index": 1, "phaseTitle": "A", "phaseIndex": 1, "state": "done"}]
        entry = sideagent.task_event({"type": "system", "subtype": "task_progress", "task_id": "w9",
                                      "task_type": "local_workflow", "workflow_progress": items})
        self.server._note_event(entry)  # noqa: SLF001
        self.server._note_event(entry)  # noqa: SLF001
        saved = json.loads(sideagent.workflow_path("ds#t1", "w9").read_text(encoding="utf-8"))
        self.assertEqual(saved["items"][0]["state"], "done")
        lines = [json.loads(line) for line in
                 sideagent._events_path("ds#t1").read_text(encoding="utf-8").splitlines()]  # noqa: SLF001
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("workflow_progress" not in line for line in lines))
        self.assertEqual(lines[0]["workflow_counts"]["done"], 1)


class BackgroundHoldServerTest(_ServerCase):
    """一轮结束时后台还有任务：结果先压着，等后台做完、claude 自己开的那一轮出结果再交。

    后台有哪些任务照 claude 的 background_tasks_changed（整份名单）来，不自己配对开始/结束。
    """

    def setUp(self):
        super().setUp()
        self.live = []

    def feed(self, event: dict) -> None:
        line = json.dumps(event) + "\n"
        self.server._note_progress(line)  # noqa: SLF001
        with self.server.lock:
            target = self.server.current
        self.server._forward(target, line)  # noqa: SLF001

    def result_of(self, sock, wait=2.0):
        """调用方 socket 上收到的结果那一行（前面的其它事件跳过）；没有就 None。"""
        sock.settimeout(wait)
        data = b""
        while True:
            for raw in data.split(b"\n"):
                if raw.strip() and json.loads(raw).get("type") == "result":
                    return json.loads(raw)
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                return None
            if not chunk:
                return None
            data += chunk

    def start_turn(self):
        ours, theirs = self.pair()
        self.server.busy = True
        self.server.current = theirs
        self.server.turn_keep = False
        return ours

    def snapshot(self):
        self.feed({"type": "system", "subtype": "background_tasks_changed", "tasks": list(self.live)})

    def bg_started(self, task="w1", tool="t1", task_type="local_workflow", **flags):
        # claude 的顺序：先进名单，再发 task_started
        self.live.append({"task_id": task, "task_type": task_type, "description": "x", **flags})
        self.snapshot()
        self.feed({"type": "system", "subtype": "task_started", "task_id": task, "tool_use_id": tool,
                   "task_type": task_type})

    def bg_done(self, task="w1", status="completed"):
        self.feed({"type": "system", "subtype": "task_notification", "task_id": task, "status": status})
        self.live = [one for one in self.live if one["task_id"] != task]
        self.snapshot()

    def test_result_waits_for_background_then_final_is_delivered(self):
        ours = self.start_turn()
        self.bg_started()
        self.feed({"type": "result", "result": "工作流已启动"})
        self.assertTrue(self.server.busy)
        self.assertEqual(self.server.exit_at, 0.0)
        self.assertIsNone(self.result_of(ours, wait=0.2))
        self.bg_done()
        self.feed({"type": "result", "result": "最终：30"})
        self.assertEqual(self.result_of(ours)["result"], "最终：30")
        self.assertFalse(self.server.busy)
        self.assertGreater(self.server.exit_at, 0)

    def test_held_tells_the_caller(self):
        ours = self.start_turn()
        self.bg_started()
        self.feed({"type": "result", "result": "已启动"})
        ours.settimeout(1)
        got = b""
        while b"cca_held" not in got:
            got += ours.recv(65536)
        self.assertIsNone(self.result_of(ours, wait=0.2))

    def test_caller_keeps_only_the_final_turns_text(self):
        """调用方那边：被压下那一轮说的话清掉，只留后台做完后那一轮的回复。"""
        collector = sideagent._Collector()  # noqa: SLF001
        def say(text):
            return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
                                                                "delta": {"type": "text_delta", "text": text}}}).encode()
        collector.feed(say("工作流已启动"))
        self.assertFalse(collector.feed(b'{"type": "cca_held", "background": 1}'))
        collector.feed(say("最终：30"))
        self.assertTrue(collector.feed(json.dumps({"type": "result", "result": "最终：30"}).encode()))
        self.assertEqual(collector.text, "最终：30")

    def test_monitor_ambient_and_awaited_do_not_hold(self):
        ours = self.start_turn()
        self.feed({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tm", "name": "Monitor", "input": {}}]}})
        self.bg_started("m1", "tm", task_type="local_bash")
        self.bg_started("e1", "te", task_type="local_bash", ambient=True)
        self.bg_started("a1", "ta", task_type="local_agent", awaited=True)
        self.assertEqual(self.server.background, set())
        self.feed({"type": "result", "result": "完成"})
        self.assertEqual(self.result_of(ours)["result"], "完成")
        self.assertFalse(self.server.busy)

    def test_foreground_backgrounded_later_holds(self):
        """前台命令后来被挪到后台（Ctrl+B 那种）：claude 把它放进名单，照样压着结果。"""
        ours = self.start_turn()
        self.live.append({"task_id": "b1", "task_type": "local_bash", "description": "build"})
        self.snapshot()
        self.feed({"type": "result", "result": "构建挪到后台了"})
        self.assertTrue(self.server.busy)
        self.assertIsNone(self.result_of(ours, wait=0.2))

    def test_no_stall_or_idle_exit_while_background_runs(self):
        self.start_turn()
        self.bg_started()
        self.feed({"type": "result", "result": "已启动"})
        self.server.last_output = self.server.last_activity = 0
        self.assertFalse(self.server._stalled())  # noqa: SLF001
        self.assertFalse(self.server._idle_too_long())  # noqa: SLF001

    def test_interrupt_while_held_stops_background_then_releases(self):
        ours = self.start_turn()
        self.bg_started()
        self.feed({"type": "result", "result": "已启动"})
        self.server._interrupt()  # noqa: SLF001
        sent = [line["request"] for line in self.server.process.stdin.lines]
        self.assertEqual(sent, [{"subtype": "stop_task", "task_id": "w1"}])
        self.assertIsNone(self.result_of(ours, wait=0.2))
        self.bg_done(status="stopped")
        got = self.result_of(ours)
        self.assertEqual(got["result"], "已启动")
        self.assertTrue(got["cca_interrupted"])
        self.assertFalse(self.server.busy)

    def test_interrupt_during_the_final_turn_is_a_normal_interrupt(self):
        ours = self.start_turn()
        self.bg_started()
        self.feed({"type": "result", "result": "已启动"})
        self.bg_done()
        self.server._interrupt()  # noqa: SLF001
        self.assertEqual(self.server.process.stdin.lines[-1]["request"]["subtype"], "interrupt")
        self.feed({"type": "result", "result": "半截"})
        got = self.result_of(ours)
        self.assertEqual(got["result"], "半截")
        self.assertTrue(got["cca_interrupted"])

    def test_failed_turn_is_not_held(self):
        ours = self.start_turn()
        self.bg_started()
        self.feed({"type": "result", "result": "API 出错", "is_error": True})
        self.assertTrue(self.result_of(ours)["is_error"])
        self.assertFalse(self.server.busy)

    def test_held_followup_turn_is_not_dropped_as_empty(self):
        self.start_turn()
        self.server.followup_turn = True
        self.bg_started()
        self.feed({"type": "result", "result": "已启动"})
        self.server.last_output = 0
        self.server._drop_empty_followup()  # noqa: SLF001
        self.assertTrue(self.server.busy)
        self.assertIsNotNone(self.server.held)

    def test_lingering_exit_waits_for_background(self):
        self.server.exit_at = time.time() - 1
        self.server.background = {"w1"}
        self.assertFalse(self.server._done_lingering())  # noqa: SLF001
        self.server.background = set()
        self.assertTrue(self.server._done_lingering())  # noqa: SLF001


class InterruptServerTest(_ServerCase):
    def test_interrupt_when_busy_marks_the_turn(self):
        self.server.busy = True
        self.assertTrue(self.server._interrupt())  # noqa: SLF001
        self.assertTrue(self.server.turn_interrupted)
        sent = self.server.process.stdin.lines[-1]
        self.assertEqual(sent["request"]["subtype"], "interrupt")
        self.assertIn("INTERRUPT", self.server.progress.read_text(encoding="utf-8"))

    def test_interrupt_when_idle_says_not_busy(self):
        self.assertFalse(self.server._interrupt())  # noqa: SLF001
        self.assertFalse(self.server.turn_interrupted)

    def test_handle_interrupt_replies_ack(self):
        self.server.busy = True
        ours, theirs = self.pair()
        ours.sendall(b'{"interrupt": true}\n')
        self.server._handle(theirs)  # noqa: SLF001
        self.assertEqual(self.reply_of(ours), {"type": "cca_ack", "busy": True})

    def test_interrupted_result_is_tagged_for_the_caller(self):
        self.server.busy = True
        self.server.turn_interrupted = True
        ours, theirs = self.pair()
        self.server._forward(theirs, json.dumps({"type": "result", "result": "半截"}) + "\n")  # noqa: SLF001
        self.assertTrue(self.reply_of(ours)["cca_interrupted"])
        self.assertFalse(self.server.busy)


class InjectServerTest(_ServerCase):
    def test_inject_while_busy_writes_stdin_with_uuid(self):
        self.server.busy = True
        self.server.current_job = "job1"
        ours, theirs = self.pair()
        ours.sendall(json.dumps({"text": "顺便看下 README", "interject": True, "job": "job2"}).encode() + b"\n")
        self.server._handle(theirs)  # noqa: SLF001
        ack = self.reply_of(ours)
        self.assertEqual(ack["mode"], "interject")
        self.assertEqual(ack["merged_into"], "job1")
        sent = self.server.process.stdin.lines[-1]
        self.assertEqual(sent["uuid"], ack["uuid"])
        self.assertEqual(sent["message"]["content"][0]["text"], "顺便看下 README")
        # 插话不进排队
        self.assertEqual(len(self.server.queue), 0)

    def test_inject_while_idle_starts_a_turn(self):
        ours, theirs = self.pair()
        ours.sendall(json.dumps({"text": "干活", "interject": True, "detach": True}).encode() + b"\n")
        self.server._handle(theirs)  # noqa: SLF001
        self.assertEqual(self.reply_of(ours), {"type": "cca_ack", "mode": "queue"})
        self.assertTrue(self.server.busy)
        self.assertNotIn("uuid", self.server.process.stdin.lines[-1])

    def test_lifecycle_started_marks_merged(self):
        self.server.busy = True
        self.server.injected["u1"] = {"text": "插一句", "notify": False, "job": "", "started": False}
        self.server._note_control(json.dumps(  # noqa: SLF001
            {"type": "command_lifecycle", "command_uuid": "u1", "state": "started"}))
        self.assertTrue(self.server.injected["u1"]["started"])
        self.assertIn("TASK （插话）插一句", self.server.progress.read_text(encoding="utf-8"))
        # 并进了这一轮：这一轮结束不再另开一轮
        self.server._forward(None, json.dumps({"type": "result", "result": "好"}) + "\n")  # noqa: SLF001
        self.assertFalse(self.server.busy)

    def test_missed_interject_becomes_its_own_turn(self):
        self.server.busy = True
        self.server.queue.append((None, "排队的下一条", False, "job3", True))
        self.server.injected["u2"] = {"text": "插一句", "notify": True, "job": "", "started": False}
        self.server._forward(None, json.dumps({"type": "result", "result": "第一轮"}) + "\n")  # noqa: SLF001
        # claude 会另开一轮跑插话：期间保持忙，不发排队的那一条
        self.assertTrue(self.server.busy)
        self.assertTrue(self.server.followup_turn)
        self.assertEqual(len(self.server.queue), 1)
        self.assertTrue(self.server.notify_turn)

    def test_empty_followup_is_dropped(self):
        self.server.busy = True
        self.server.followup_turn = True
        self.server.last_output = 0
        self.server.queue.append((None, "下一条", False, "job3", True))
        self.server._drop_empty_followup()  # noqa: SLF001
        self.assertFalse(self.server.followup_turn)
        # 放开后马上发了排队的下一条
        self.assertTrue(self.server.busy)
        self.assertEqual(len(self.server.queue), 0)

    def test_interrupt_response_forgets_dropped_injects(self):
        self.server.injected["u3"] = {"text": "x", "notify": False, "job": "", "started": False}
        self.server._note_control(json.dumps({  # noqa: SLF001
            "type": "control_response",
            "response": {"request_id": "int-1", "response": {"still_queued": []}}}))
        self.assertNotIn("u3", self.server.injected)


class TokenTest(_ServerCase):
    def test_counts_input_at_start_and_output_at_delta(self):
        server = self.server
        server._note_stream_event({"type": "message_start", "message": {"usage": {  # noqa: SLF001
            "input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 12000,
            "output_tokens": 1}}})
        server._note_stream_event({"type": "message_delta", "usage": {"output_tokens": 50}})  # noqa: SLF001
        server._note_stream_event({"type": "message_delta", "usage": {"output_tokens": 80}})  # noqa: SLF001
        server._note_stream_event({"type": "message_start", "message": {"usage": {  # noqa: SLF001
            "input_tokens": 5, "cache_read_input_tokens": 200}}})
        server._note_stream_event({"type": "message_delta", "usage": {"output_tokens": 20}})  # noqa: SLF001
        state = sideagent._read_state("ds#t1")  # noqa: SLF001
        self.assertEqual(state["tokens_in"], 12315)
        self.assertEqual(state["tokens_out"], 100)

    def test_result_uses_result_usage(self):
        self.server.latest_in = 12000
        note = self.server._note_result({"result": "好", "duration_ms": 1500, "usage": {  # noqa: SLF001
            "input_tokens": 300, "cache_read_input_tokens": 12000, "output_tokens": 400}})
        self.assertIn("↑12.3k ↓0.4k tok · 12.4k total", note)
        state = sideagent._read_state("ds#t1")  # noqa: SLF001
        self.assertEqual(state["tokens_in"], 12300)
        self.assertEqual(state["token_count"], 12400)

    def test_token_count_like_claude_code_subagent_row(self):
        """最近一次输入（覆盖）+ 累计输出（每条消息取最终 message_delta）+ 正在输出的估计。"""
        server = self.server
        start = server._note_stream_event  # noqa: SLF001
        start({"type": "message_start", "message": {"usage": {
            "input_tokens": 10, "cache_creation_input_tokens": 90, "cache_read_input_tokens": 9900}}})
        state = sideagent._read_state("ds#t1")  # noqa: SLF001
        self.assertEqual(state["token_count"], 10000)
        self.assertFalse(state["acted"])
        start({"type": "content_block_start", "content_block": {"type": "text"}})
        self.assertTrue(sideagent._read_state("ds#t1")["acted"])  # noqa: SLF001
        # 正在输出：800 个字符估 200 个词元
        start({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "字" * 800}})
        self.assertEqual(server.token_count(), 10200)
        self.assertEqual(sideagent._read_state("ds#t1")["token_count"], 10200)  # noqa: SLF001
        # 真的输出数到了，估计清零，按最终值算
        start({"type": "message_delta", "usage": {"output_tokens": 150}})
        start({"type": "message_delta", "usage": {"output_tokens": 180}})
        self.assertEqual(server.token_count(), 10180)
        # 第二次调用：输入是覆盖，不累加；输出接着累加
        start({"type": "message_start", "message": {"usage": {
            "input_tokens": 5, "cache_read_input_tokens": 10500}}})
        start({"type": "message_delta", "usage": {"output_tokens": 20}})
        state = sideagent._read_state("ds#t1")  # noqa: SLF001
        self.assertEqual(state["token_count"], 10505 + 200)
        self.assertEqual(state["tokens_in"], 10000 + 10505)
        self.assertEqual(state["tokens_out"], 200)
        # acted 一旦为真，这一轮里一直为真
        self.assertTrue(state["acted"])

    def test_subagent_stream_is_not_counted(self):
        self.server._note_progress(json.dumps({  # noqa: SLF001
            "type": "stream_event", "parent_tool_use_id": "agent1",
            "event": {"type": "message_start", "message": {"usage": {"input_tokens": 999}}}}))
        self.assertEqual(self.server.token_count(), 0)

    def test_status_has_token_count(self):
        sideagent._write_state("ds#t1", {"pid": 1, "token_count": 4321})  # noqa: SLF001
        self.assertEqual(sideagent.status("ds#t1")["token_count"], 4321)


class ReportTest(_ServerCase):
    def test_finds_caller_by_owner_session(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        (sessions / "1.json").write_text(json.dumps({"sessionId": "旧的", "name": "别人"}), encoding="utf-8")
        (sessions / "2.json").write_text(json.dumps(
            {"sessionId": "会话-1", "name": "主会话", "messagingSocketPath": "/x.sock"}), encoding="utf-8")
        self.server.caller_sessions = sessions
        self.server.owner = {"pid": 0, "proc_start": "", "session": "会话-1"}
        with _env("CLAUDE_CODE_SESSION_ID", "旧的"):
            self.assertEqual(self.server._find_caller_entry()["name"], "主会话")  # noqa: SLF001

    def test_notify_without_socket_writes_reason(self):
        self.server.caller_sessions = self.root / "没有"
        self.server._notify_main("做完了")  # noqa: SLF001
        self.assertIn("NOTIFY skip", self.server.progress.read_text(encoding="utf-8"))

    def test_report_prompt_names_the_caller(self):
        self.server.caller_name = "主会话"
        self.server.caller_socket = "/x.sock"
        text = self.server._report_prompt()  # noqa: SLF001
        self.assertIn("SendMessage", text)
        self.assertIn('"主会话"', text)
        self.assertIn("uds:/x.sock", text)
        self.server.caller_socket = ""
        self.assertEqual(self.server._report_prompt(), "")  # noqa: SLF001


class StatusTest(_ServerCase):
    def test_stale_when_code_version_differs_or_missing(self):
        with mock.patch.object(sideagent, "running", return_value=True):
            self.assertTrue(sideagent.status("ds#t1")["stale"])
            sideagent._write_state("ds#t1", {"pid": 1, "code_version": sideagent.CODE_VERSION})  # noqa: SLF001
            self.assertNotIn("stale", sideagent.status("ds#t1"))

    def test_busy_status_has_doing(self):
        sideagent._write_state("ds#t1", {"pid": 1, "busy": True, "tokens_in": 5, "tokens_out": 2})  # noqa: SLF001
        sideagent._events_path("ds#t1").write_text(  # noqa: SLF001
            json.dumps({"kind": "tool", "name": "Bash", "summary": "make test"}) + "\n", encoding="utf-8")
        info = sideagent.status("ds#t1")
        self.assertEqual(info["doing"], "Bash(make test)")
        self.assertEqual((info["tokens_in"], info["tokens_out"]), (5, 2))

    def test_version_command_prints_hash(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(sideagent.cmd_sideagent(["version"]), 0)
        self.assertIn(sideagent.CODE_VERSION, out.getvalue())


class DoingTest(unittest.TestCase):
    def test_live_text_wins(self):
        self.assertEqual(sideagent.doing_of([{"kind": "think"}], "  正在 写\n结论 "), "正在 写 结论")

    def test_last_top_level_step(self):
        events = [{"kind": "task", "text": "跑测试"},
                  {"kind": "tool", "name": "Bash", "summary": "make test"},
                  {"kind": "tool", "name": "Read", "summary": "x.py", "parent": "agent1"}]
        self.assertEqual(sideagent.doing_of(events), "Bash(make test)")
        self.assertEqual(sideagent.doing_of([{"kind": "think", "text": "嗯"}]), "Thinking…")
        self.assertEqual(sideagent.doing_of([{"kind": "say", "text": "好的\n下一步"}]), "好的 下一步")
        self.assertEqual(sideagent.doing_of([{"kind": "result", "text": "完"}]), "")


class AskTest(unittest.TestCase):
    """ask() 对着一个假的 sideagent 套接字。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.sock"
        patcher = mock.patch.object(sideagent, "_socket_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, lines):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(1)
        self.addCleanup(server.close)
        self.request = {}

        def run():
            conn, _ = server.accept()
            buffer = b""
            while b"\n" not in buffer:
                buffer += conn.recv(4096)
            self.request.update(json.loads(buffer.split(b"\n")[0]))
            for line in lines:
                conn.sendall(json.dumps(line).encode() + b"\n")
            conn.close()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_disconnect_without_result_is_an_error(self):
        thread = self.serve([{"type": "assistant", "message": {"content": [{"type": "text", "text": "半"}]}}])
        with self.assertRaises(sideagent.SideagentError) as caught:
            sideagent.ask("ds#t1", "干活", 5)
        thread.join(2)
        self.assertIn("连接断了", str(caught.exception))

    def test_interrupted_result(self):
        thread = self.serve([{"type": "result", "result": "停在这", "cca_interrupted": True}])
        out = sideagent.ask("ds#t1", "干活", 5, job="j1")
        thread.join(2)
        self.assertTrue(out["interrupted"])
        self.assertEqual(self.request["job"], "j1")

    def test_interject_ack_returns_merged(self):
        thread = self.serve([{"type": "cca_ack", "mode": "interject", "merged_into": "j0"}])
        out = sideagent.ask("ds#t1", "插一句", 5, interject=True)
        thread.join(2)
        self.assertEqual(out, {"merged": True, "merged_into": "j0", "text": "", "turns": None,
                               "duration_ms": None})
        self.assertTrue(self.request["interject"])

    def test_tell_interject_command(self):
        thread = self.serve([{"type": "cca_ack", "mode": "interject", "merged_into": "j0"}])
        self.assertEqual(sideagent.cmd_sideagent(["tell", "--interject", "ds#t1", "看下", "日志"]), 0)
        thread.join(2)
        self.assertEqual(self.request["text"], "看下 日志")
        self.assertTrue(self.request["interject"])
        self.assertTrue(self.request["detach"])


class CallerByPidTest(_ServerCase):
    """主会话被继续、恢复后会话编号会变：按主会话 claude 的进程到登记表现查。"""

    def setUp(self):
        super().setUp()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.server.caller_sessions = self.sessions

    def entry(self, pid, session, sock, start="s1"):
        (self.sessions / f"{pid}.json").write_text(json.dumps(
            {"pid": pid, "procStart": start, "sessionId": session, "name": "主会话",
             "messagingSocketPath": sock}), encoding="utf-8")

    def listen(self, name):
        path = self.root / name
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        server.settimeout(3)
        self.addCleanup(server.close)
        return str(path), server

    def received(self, server):
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\n" not in data:
                data += conn.recv(4096)
        return json.loads(data.split(b"\n")[0])

    def test_walks_up_parent_processes(self):
        self.entry(os.getppid(), "会话-新", "/x.sock")
        found = sideagent.caller_entry_by_pid(self.sessions, os.getpid())
        self.assertEqual(found["sessionId"], "会话-新")
        self.assertEqual(sideagent.caller_entry_by_pid(self.root / "没有", os.getpid()), {})

    def test_session_id_change_still_delivers(self):
        self.server.owner = {"pid": 4242, "proc_start": "s1", "session": "会话-旧"}
        self.server.instance_socket = "/me.sock"
        first, server_a = self.listen("a.sock")
        self.entry(4242, "会话-旧", first)
        self.server._notify_main("第一轮做完了")  # noqa: SLF001
        self.assertIn("第一轮做完了", self.received(server_a)["message"]["content"])
        # 主会话被恢复：编号和 socket 都换了，进程没变
        second, server_b = self.listen("b.sock")
        self.entry(4242, "会话-新", second)
        self.server._notify_main("第二轮做完了")  # noqa: SLF001
        self.assertIn("第二轮做完了", self.received(server_b)["message"]["content"])
        # 归属跟着改成新编号：面板和 MCP 按新编号认得它
        self.assertEqual(self.server.owner_session, "会话-新")
        self.assertEqual(sideagent._read_state("ds#t1")["owner_session"], "会话-新")  # noqa: SLF001
        progress = self.server.progress.read_text(encoding="utf-8")
        self.assertIn("OWNER 会话-旧 -> 会话-新", progress)
        self.assertEqual(progress.count("NOTIFY ok"), 2)

    def test_reopened_main_session_is_followed(self):
        # 主会话关了重开：老进程 4242 没了，新进程 5000 继续同一段对话（同一个编号）
        self.server.owner = {"pid": 4242, "proc_start": "s1", "session": "会话-1"}
        self.server.instance_socket = "/me.sock"
        sock, server = self.listen("c.sock")
        self.entry(5000, "会话-1", sock, start="s2")
        self.server._notify_main("做完了")  # noqa: SLF001
        self.assertIn("做完了", self.received(server)["message"]["content"])
        self.assertEqual(self.server.owner["pid"], 5000)
        self.assertEqual(sideagent._read_state("ds#t1")["owner_pid"], 5000)  # noqa: SLF001

    def test_pid_reused_by_another_program_is_not_the_owner(self):
        self.server.owner = {"pid": 4242, "proc_start": "s1", "session": "会话-1"}
        self.entry(4242, "别人的会话", "/x.sock", start="另一个进程")
        self.server._notify_main("做完了")  # noqa: SLF001
        progress = self.server.progress.read_text(encoding="utf-8")
        self.assertIn("NOTIFY skip", progress)
        self.assertIn("4242", progress)


class LifecycleTest(unittest.TestCase):
    """派新活一律新开；退出后追问用自己的会话接着开；干完过一会儿退出，期间接了新活就不退。"""

    def test_prepare_always_new_instance(self):
        with mock.patch.object(sideagent, "_ensure_started", return_value=True) as start, \
                mock.patch.object(sideagent, "running", return_value=True):
            name, started = sideagent.prepare("ds", label="查日志")
        self.assertTrue(name.startswith("ds#"))
        self.assertTrue(started)
        self.assertEqual(start.call_args.kwargs["label"], "查日志")

    def test_resume_passes_session(self):
        with mock.patch.object(sideagent, "_ensure_started", return_value=True) as start:
            sideagent.ensure("ds#a1", cwd="/w", resume="s-1")
        self.assertEqual(start.call_args.kwargs["resume"], "s-1")

    def test_transcript_exists(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(sideagent, "profile_dir", return_value=Path(folder)):
            (Path(folder) / "projects" / "-w").mkdir(parents=True)
            (Path(folder) / "projects" / "-w" / "s-1.jsonl").write_text("{}", encoding="utf-8")
            self.assertTrue(sideagent.transcript_exists("ds#a1", "s-1"))
            self.assertFalse(sideagent.transcript_exists("ds#a1", "s-2"))

    def test_stop_later_skips_when_new_work_came(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(sideagent, "SIDEAGENT_DIR", Path(folder) / "sideagents"), \
                mock.patch.object(sideagent, "running", return_value=True), \
                mock.patch.object(sideagent, "_stop") as stop:
            sideagent._write_state("ds#a1", {"busy": False})  # noqa: SLF001
            sideagent.stop_later("ds#a1", 0.05)
            sideagent._write_state("ds#a1", {"busy": False, "last_request": time.time() + 1})  # noqa: SLF001
            time.sleep(0.2)
            stop.assert_not_called()
            sideagent._write_state("ds#a1", {"busy": False})  # noqa: SLF001
            sideagent.stop_later("ds#a1", 0.05)
            time.sleep(0.2)
            stop.assert_called_once_with(["ds#a1"])


class StateKeepTest(unittest.TestCase):
    """状态文件写的时候别人在读，读到的不能是空的；实例退出后会话编号、代码版本要留下。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(sideagent, "SIDEAGENT_DIR", Path(self.tmp.name) / "sideagents")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reader_never_sees_empty_state(self):
        sideagent._write_state("ds#a1", {"pid": 1, "session_id": "s-1"})  # noqa: SLF001
        stop = threading.Event()

        def writer():
            index = 0
            while not stop.is_set():
                index += 1
                sideagent._write_state("ds#a1", {"pid": 1, "session_id": "s-1", "n": index})  # noqa: SLF001

        thread = threading.Thread(target=writer)
        thread.start()
        seen = [sideagent._read_state("ds#a1").get("session_id") for _ in range(2000)]  # noqa: SLF001
        stop.set()
        thread.join()
        self.assertEqual(set(seen), {"s-1"})

    def test_stop_keeps_session_and_code_version(self):
        sideagent._write_state("ds#a1", {"session_id": "s-1", "code_version": "abc", "cwd": "/w"})  # noqa: SLF001
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(sideagent, "_serve_pids", return_value=[]):
            sideagent._stop(["ds#a1"])  # noqa: SLF001
        # 格式改了：停了 state.json 留着、写上 finished_at，run/ 删掉（原来是整个状态文件删掉）
        state = sideagent._read_state("ds#a1")  # noqa: SLF001
        self.assertGreater(state["finished_at"], 0)
        self.assertEqual(state["session_id"], "s-1")
        self.assertFalse(sideagent._run_dir("ds#a1").exists())  # noqa: SLF001
        record = json.loads(sideagent.job_path("ds#a1").read_text(encoding="utf-8"))
        self.assertEqual((record["session_id"], record["code_version"], record["cwd"]), ("s-1", "abc", "/w"))

    def test_init_event_is_recorded_right_away(self):
        server = sideagent._SideagentServer("ds#a1", "/w", 60, [])  # noqa: SLF001
        server._remember_session(json.dumps({"type": "system", "subtype": "init",  # noqa: SLF001
                                             "session_id": "s-9"}))
        record = json.loads(sideagent.job_path("ds#a1").read_text(encoding="utf-8"))
        self.assertEqual(record["session_id"], "s-9")
        self.assertEqual(record["code_version"], sideagent.CODE_VERSION)
        self.assertEqual(sideagent._read_state("ds#a1")["session_id"], "s-9")  # noqa: SLF001

    def test_init_event_records_actual_model(self):
        # 派活时没指定模型，也记下 init 里报的实际模型，详情页标题右边要显示
        server = sideagent._SideagentServer("openai#a1", "/w", 60, [])  # noqa: SLF001
        server._remember_session(json.dumps({"type": "system", "subtype": "init",  # noqa: SLF001
                                             "session_id": "s-1", "model": "gpt-6-astra"}))
        record = json.loads(sideagent.job_path("openai#a1").read_text(encoding="utf-8"))
        self.assertEqual(record["model"], "gpt-6-astra")
        self.assertEqual(sideagent._read_state("openai#a1")["model"], "gpt-6-astra")  # noqa: SLF001


class ServePidsTest(unittest.TestCase):
    def test_matches_exact_instance_name_only(self):
        fake = {"11": [sys.executable, "-c", sideagent.SERVE_CODE, "ds#a1", "/w", "60"],
                "12": [sys.executable, "-c", sideagent.SERVE_CODE, "ds#a12", "/w", "60"],
                "13": ["bash", "-c", "echo ds#a1"]}
        with tempfile.TemporaryDirectory() as folder:
            for pid, argv in fake.items():
                (Path(folder) / pid).mkdir()
                (Path(folder) / pid / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
            real = Path
            # 找进程的代码拆到了 sideagent_cli，Path 要在那边换
            with mock.patch.object(sideagent_cli, "Path",
                                   side_effect=lambda value="": real(folder) if value == "/proc" else real(value)):
                self.assertEqual(sideagent._serve_pids("ds#a1"), [11])  # noqa: SLF001


class NotificationTest(_ServerCase):
    """回报照子代理的 task-notification：编号、状态、摘要、结果、用量。"""

    def test_completed_notification_format(self):
        self.server.turn_task = "数一下文件"
        with mock.patch.dict("os.environ", {"CCA_DESCRIPTION": "数文件", "CCA_LABEL": ""}):
            text = self.server._notification("一共 25 个 <py>", status="completed",  # noqa: SLF001
                                             usage={"tokens": 12345, "tool_uses": 3, "duration_ms": 4200})
        self.assertTrue(text.startswith("<task-notification>\n<task-id>ds#t1</task-id>\n<status>completed</status>"))
        self.assertIn('<summary>Agent "数文件" finished</summary>', text)
        self.assertIn("<result>一共 25 个 &lt;py&gt;</result>", text)
        self.assertIn("<usage><subagent_tokens>12345</subagent_tokens><tool_uses>3</tool_uses>"
                      "<duration_ms>4200</duration_ms></usage>", text)
        self.assertIn("sideagent_send 发给 ds#t1", text)

    def test_failed_notification(self):
        with mock.patch.dict("os.environ", {"CCA_DESCRIPTION": "", "CCA_LABEL": "查日志"}):
            text = self.server._notification("", status="failed", usage={}, error="卡住了")  # noqa: SLF001
        self.assertIn('<summary>Agent "查日志" failed: 卡住了</summary>', text)
        self.assertNotIn("<result>", text)

    def test_result_line_carries_tool_uses_and_tokens(self):
        self.server.busy = True
        self.server._forward(None, json.dumps({"type": "assistant", "message": {"content": [  # noqa: SLF001
            {"type": "tool_use", "name": "Bash", "id": "a"}, {"type": "tool_use", "name": "Read", "id": "b"}]}}) + "\n")
        self.server._forward(None, json.dumps({"type": "assistant", "parent_tool_use_id": "x",  # noqa: SLF001
                                               "message": {"content": [{"type": "tool_use", "name": "Grep"}]}}) + "\n")
        self.server.latest_in = 1000
        ours, theirs = self.pair()
        self.server._forward(theirs, json.dumps({"type": "result", "result": "好", "duration_ms": 5}) + "\n")  # noqa: SLF001
        event = self.reply_of(ours)
        self.assertEqual((event["cca_tool_uses"], event["cca_token_count"]), (2, 1000))


class KeepFilesTest(unittest.TestCase):
    def test_folder_kept_until_keep_days_after_exit(self):
        # 原来退出后进度、事件留 1 天；现在整个文件夹留 SIDEAGENT_KEEP_DAYS 天（按 finished_at 算）
        root = use_temp_root(self)
        day = 86400
        write_state("ds#h", kind="spawned", finished_at=time.time() - 3600)
        (root / "ds#h" / "events.jsonl").write_text("x", encoding="utf-8")
        write_state("ds#d", kind="spawned", finished_at=time.time() - (roster.SIDEAGENT_KEEP_DAYS + 1) * day)
        roster.cleanup(force=True)
        self.assertTrue((root / "ds#h" / "events.jsonl").exists())
        self.assertFalse((root / "ds#d").exists())


class CallerAgentTest(_ServerCase):
    """子代理派的 sideagent：记下归哪个子代理，回报带上实例名，面板好转给那个子代理。"""

    def test_notification_carries_instance_name(self):
        self.server.instance_socket = "/me.sock"
        sessions = self.root / "sessions"
        sessions.mkdir()
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = self.root / "main.sock"
        server_sock.bind(str(path))
        server_sock.listen(1)
        server_sock.settimeout(3)
        self.addCleanup(server_sock.close)
        (sessions / "77.json").write_text(json.dumps({"pid": 77, "procStart": "s", "sessionId": "会话-1",
                                                      "messagingSocketPath": str(path)}), encoding="utf-8")
        self.server.caller_sessions = sessions
        self.server.owner = {"pid": 77, "proc_start": "s", "session": "会话-1"}
        self.server._notify_main("好了")  # noqa: SLF001
        conn, _ = server_sock.accept()
        with conn:
            data = conn.recv(65536)
        content = json.loads(data.split(b"\n")[0])["message"]["content"]
        self.assertIn('cca-instance="ds#t1"', content)

    def test_own_socket_is_recorded_for_routing(self):
        self.server.process = _FakeProcess()
        self.server.agent_sessions.mkdir(parents=True, exist_ok=True)
        (self.server.agent_sessions / f"{self.server.process.pid}.json").write_text(
            json.dumps({"name": "ds", "messagingSocketPath": "/x/s1.sock"}), encoding="utf-8")
        self.server._read_own_identity()  # noqa: SLF001
        # 自己的消息 socket 只记在自己的 state.json（原来另外写一份到 .owner 纸条，纸条没了）
        self.assertEqual(sideagent._read_state("ds#t1")["instance_socket"], "/x/s1.sock")  # noqa: SLF001

    def test_caller_agent_goes_to_env_and_owner_file(self):
        captured = {}

        def fake_popen(args, **kwargs):
            captured.update(kwargs["env"])
            msg = "不真起"
            raise OSError(msg)

        caller = {"pid": 77, "procStart": "s", "sessionId": "会话-1"}
        with mock.patch.object(sideagent, "profile_dir", return_value=self.root), \
                mock.patch.object(sideagent, "caller_now", return_value=caller), \
                mock.patch.object(sideagent, "_ensure_inbound_setting"), \
                mock.patch.object(sideagent, "running", return_value=False), \
                mock.patch.object(sideagent.subprocess, "Popen", side_effect=fake_popen), \
                self.assertRaises(OSError):
            sideagent._ensure_started("ds#c1", "/w", 60, [], caller_agent="a1")  # noqa: SLF001
        self.assertEqual(captured["CCA_CALLER_AGENT"], "a1")
        # 原来写进 .owner 纸条；现在记在主会话名单里这个 sideagent 那一条（kind=spawned）
        entry = roster.read("会话-1")["agents"]["ds#c1"]
        self.assertEqual((entry["kind"], entry["caller_agent"]), ("spawned", "a1"))


class CallerLostTest(CallerByPidTest):
    """等结果的调用方半路没了（MCP 服务重启，后台线程跟着死）：sideagent 自己收尾。

    复现 ds#c62b0a：20:21 派活，20:25:19 主会话的 `cca mcp serve` 重启，20:25:37 出结果。
    以前结果只发给那条断了的连接：任务一直是 running，不回报，keep=false 也不退出。
    """

    def start_turn(self, *, keep):
        self.server.owner = {"pid": 4242, "proc_start": "s1", "session": "会话-1"}
        self.server.instance_socket = "/me.sock"
        sock, main = self.listen("main.sock")
        self.entry(4242, "会话-1", sock)
        sideagent.job_path("ds#t1").write_text(json.dumps(
            {"account": "ds#t1", "jobs": [{"id": "j1", "account": "ds#t1", "state": "running",
                                           "message": "查一下"}]}), encoding="utf-8")
        ours, theirs = self.pair()
        ours.sendall(json.dumps({"text": "查一下", "notify": True, "job": "j1",
                                 "keep": keep}).encode() + b"\n")
        self.server._handle(theirs)  # noqa: SLF001
        self.assertIs(self.server.current, theirs)
        return ours, main

    def finish(self, text="查完了：一共 3 处"):
        # 照 _read_claude：每行都发给当前等着的连接
        for line in (json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}),
                     json.dumps({"type": "result", "result": text, "num_turns": 2, "duration_ms": 900})):
            self.server._forward(self.server.current, line + "\n")  # noqa: SLF001

    def job(self):
        data = json.loads(sideagent.job_path("ds#t1").read_text(encoding="utf-8"))
        return next(job for job in data["jobs"] if job["id"] == "j1")

    def test_lost_caller_still_records_notifies_and_exits(self):
        ours, main = self.start_turn(keep=False)
        ours.close()   # MCP 服务重启：等结果的连接断了
        self.finish()
        # 1. 任务记成完成，带正文
        job = self.job()
        self.assertEqual(job["state"], "finished")
        self.assertEqual(job["text"], "查完了：一共 3 处")
        # 2. 直接叫醒主会话，面板不用再叫一遍
        self.assertIn("查完了：一共 3 处", self.received(main)["message"]["content"])
        self.assertTrue(sideagent._read_state("ds#t1")["delivered_to_caller"])  # noqa: SLF001
        # 3. keep=false：留一会儿就自己退出
        self.assertGreater(self.server.exit_at, 0)
        self.server.exit_at = time.time() - 1
        self.server.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.server.server.close)
        self.server.server.bind(str(self.root / "own.sock"))
        self.server.server.listen(1)
        self.server.server.settimeout(0.01)
        self.assertFalse(self.server._accept_once())  # noqa: SLF001

    def test_keep_true_stays_but_still_records(self):
        ours, main = self.start_turn(keep=True)
        ours.close()
        self.finish()
        self.assertEqual(self.job()["state"], "finished")
        self.assertIn("查完了", self.received(main)["message"]["content"])
        self.assertEqual(self.server.exit_at, 0)

    def test_new_request_cancels_exit(self):
        ours, _main = self.start_turn(keep=False)
        self.finish()
        self.assertEqual(json.loads(ours.recv(65536).split(b"\n")[-2])["type"], "result")
        self.assertGreater(self.server.exit_at, 0)
        again, theirs = self.pair()
        again.sendall(json.dumps({"text": "再问一句", "job": "j2", "keep": True}).encode() + b"\n")
        self.server._handle(theirs)  # noqa: SLF001
        self.assertEqual(self.server.exit_at, 0)

    def test_caller_still_there_job_left_to_caller(self):
        ours, _main = self.start_turn(keep=False)
        self.finish()
        # 调用方拿到了结果，任务记录由它写：sideagent 不抢着写
        self.assertEqual(self.job()["state"], "running")
        self.assertIn(b'"type": "result"', ours.recv(65536))
