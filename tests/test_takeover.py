"""接管原生后台会话：对话记录翻译、增量同步、发话、总控退出不停。夹具是实测抓下来的对话记录。"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca import mcp_sideagent  # noqa: E402
from cca.session import roster, sideagent, takeover  # noqa: E402

sys.path.insert(0, str(REPO / "tests"))
from sidehelp import write_roster  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures"


def _items(name: str) -> list:
    return [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line]


class EventsTest(unittest.TestCase):
    def test_transcript_to_panel_events(self):
        kinds = []
        notes = []
        for item in _items("bg-transcript.jsonl"):
            found, noted = takeover.events_of(item)
            kinds += [event["kind"] for event in found]
            notes += noted
        # 实测：派活 → Bash → 结果 → 说 OK；签名占位的空思考不算
        self.assertEqual(kinds[:4], ["task", "tool", "tool_result", "say"])
        self.assertIn("TOOL Bash · ls /tmp/cca-peer-test", notes)
        self.assertIn("SAY OK", notes)

    def test_peer_message_becomes_task_with_sender(self):
        tasks = [event for item in _items("bg-transcript-peer.jsonl")
                 for event in takeover.events_of(item)[0] if event["kind"] == "task"]
        peer = [task for task in tasks if task.get("from")]
        self.assertEqual(peer[0]["from"], "ham-76")
        self.assertIn("BG-GOT-IT", peer[0]["text"])

    def test_sidechain_is_skipped(self):
        item = {"type": "assistant", "isSidechain": True,
                "message": {"content": [{"type": "text", "text": "子代理说的"}]}}
        self.assertEqual(takeover.events_of(item), ([], []))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.side = root / "sideagents"
        self.side.mkdir()
        self.account_dir = root / "acct"
        (self.account_dir / "sessions").mkdir(parents=True)
        self.transcript = root / "t.jsonl"
        self.lines = (FIXTURES / "bg-transcript.jsonl").read_text(encoding="utf-8").splitlines()
        self.patches = [mock.patch.object(sideagent, "SIDEAGENT_DIR", self.side),
                        mock.patch.object(takeover, "profile_dir", return_value=self.account_dir)]
        for patch in self.patches:
            patch.start()
        self.name = "003#bg-36709e4a"
        # 格式改了：kind=attached，谁接管的记在主会话名单 sessions/owner.json 里
        sideagent._write_state(self.name, {  # noqa: SLF001
            "kind": "attached", "account": "003", "session_id": "s-1", "transcript": str(self.transcript),
            "offset": 0, "busy": False, "started": 0})
        write_roster("owner", {self.name: "attached"})

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def _registry(self, status: str, waiting: str = ""):
        entry = {"pid": os.getpid(), "sessionId": "s-1", "status": status, "name": "exp-a",
                 "messagingSocketPath": "/nonexistent.sock"}
        if waiting:
            entry["waitingFor"] = waiting
        (self.account_dir / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(entry), encoding="utf-8")

    def test_busy_then_idle_writes_result_once(self):
        half = len(self.lines) // 2
        # 半行没写完：只读到最后一个完整行
        self.transcript.write_text("\n".join(self.lines[:half]) + "\n" + self.lines[half][:20], encoding="utf-8")
        self._registry("busy")
        state = takeover.sync_one(self.name)
        self.assertTrue(state["busy"])
        first_offset = state["offset"]
        self.assertLess(first_offset, self.transcript.stat().st_size)
        self.transcript.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        self._registry("idle")
        state = takeover.sync_one(self.name)
        self.assertFalse(state["busy"])
        self.assertEqual(state["model"], "claude-haiku-5-5")
        self.assertGreater(state["token_count"], 0)
        events = [json.loads(line) for line in sideagent._events_path(self.name).read_text().splitlines()]  # noqa: SLF001
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds.count("result"), 1)
        self.assertEqual(events[-1]["text"], "OK")
        self.assertIn("RESULT OK", sideagent._progress_path(self.name).read_text())  # noqa: SLF001
        # 没有新内容、状态没变：再同步一次不重复记
        takeover.sync_one(self.name)
        events = sideagent._events_path(self.name).read_text().splitlines()  # noqa: SLF001
        self.assertEqual(sum('"result"' in line for line in events), 1)

    def test_waiting_is_marked(self):
        self.transcript.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        self._registry("waiting", "permission prompt")
        state = takeover.sync_one(self.name)
        self.assertTrue(state["busy"])
        self.assertEqual((state["status"], state["waiting_for"]), ("waiting", "permission prompt"))

    def test_gone_session_is_ended(self):
        self.transcript.write_text("", encoding="utf-8")
        state = takeover.sync_one(self.name)
        self.assertEqual(state["status"], "ended")
        self.assertFalse(state["busy"])

    def test_stop_owned_leaves_bg(self):
        with mock.patch.object(sideagent, "_stop") as stop:
            sideagent.stop_owned("owner")
        stop.assert_not_called()
        self.assertTrue(sideagent._state_path(self.name).exists())  # noqa: SLF001
        self.assertFalse(sideagent._read_state(self.name).get("finished_at"))  # noqa: SLF001

    def test_detach_removes_record_only(self):
        # 格式改了：放手只从我的名单里删；没有别的主人了才写 finished_at，文件夹留着（过期再清）
        self.transcript.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        takeover.sync_one(self.name)
        with mock.patch.object(roster, "owner_alive", return_value=True):
            self.assertEqual(takeover.release(self.name, "owner"), 0)
        self.assertNotIn(self.name, roster.read("owner")["agents"])
        state = sideagent._read_state(self.name)  # noqa: SLF001
        self.assertGreater(state["finished_at"], 0)
        self.assertTrue(sideagent._events_path(self.name).exists())  # noqa: SLF001
        # 停了的不再同步
        self.assertEqual(takeover.sync_one(self.name)["finished_at"], state["finished_at"])
        self.assertEqual(takeover.release("ds#not-attached", "owner"), -1)


class SubagentSyncTest(SyncTest):
    """接管会话里它自己派的子代理：读 <会话>/subagents/agent-*.jsonl，事件带上 parent。"""

    def test_subagent_events_carry_parent(self):
        self.transcript.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        folder = takeover.subagent_dir(self.transcript)
        folder.mkdir(parents=True)
        # 照实际格式：子代理记录每行 isSidechain、agentId；meta 里 toolUseId 是派它的 Agent 调用
        (folder / "agent-a1.meta.json").write_text(json.dumps({"toolUseId": "toolu_P", "description": "查文档"}),
                                                   encoding="utf-8")
        rows = [
            {"isSidechain": True, "agentId": "a1", "type": "user",
             "message": {"role": "user", "content": "去查文档"}},
            {"isSidechain": True, "agentId": "a1", "type": "assistant",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 2},
                         "content": [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "ls"}}]}},
        ]
        (folder / "agent-a1.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self._registry("busy")
        state = takeover.sync_one(self.name)
        events = [json.loads(line) for line in sideagent._events_path(self.name).read_text().splitlines()]  # noqa: SLF001
        child = [event for event in events if event.get("parent") == "toolu_P"]
        # 交办那句不重复记（详情页用 Agent 调用的描述），工具调用带上 parent
        self.assertEqual([(event["kind"], event.get("name")) for event in child], [("tool", "Bash")])
        self.assertGreater(state["sub_offsets"]["a1"], 0)
        # 子代理的话不当成主会话的最后一句
        self.assertNotEqual(state.get("last_say", {}).get("parent"), "toolu_P")
        # 再同步一次，没有新行就不重复记
        takeover.sync_one(self.name)
        again = sideagent._events_path(self.name).read_text()  # noqa: SLF001
        self.assertEqual(again.count('"toolu_P"'), 1)


class SummaryTest(SyncTest):
    """接管会话的进度短语：在忙才写，用原会话那个号配置的小模型，写进状态和进度行。"""

    def test_busy_session_gets_a_label(self):
        self.transcript.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        self._registry("busy")
        state = takeover.sync_one(self.name)
        self.assertTrue(takeover.summary_due(state, 10**10))
        calls = []

        def runner(model, prompt, env, cwd):
            calls.append((model, env.get("CLAUDE_CONFIG_DIR")))
            return {"ok": True, "text": "Running the bash command"}

        with mock.patch("cca.session.takeover.summary.configured_model", return_value="haiku"):
            self.assertEqual(takeover.summarize_once(self.name, runner), "ok")
        state = sideagent._read_state(self.name)  # noqa: SLF001
        self.assertEqual(state["summary"], "Running the bash command")
        self.assertEqual(calls, [("haiku", str(self.account_dir))])
        self.assertIn("SUMMARY Running the bash command", sideagent._progress_path(self.name).read_text())  # noqa: SLF001
        # 同步不会把短语盖掉
        state = takeover.sync_one(self.name)
        self.assertEqual(state["summary"], "Running the bash command")
        self.assertFalse(takeover.summary_due(state, state["summary_at"] + 1))

    def test_idle_or_off_skips(self):
        self.assertEqual(takeover.summarize_once(self.name, lambda *a: {}), "skip idle")
        self.transcript.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        self._registry("busy")
        takeover.sync_one(self.name)
        with mock.patch("cca.session.takeover.summary.configured_model", return_value=""):
            self.assertEqual(takeover.summarize_once(self.name, lambda *a: {}), "skip off")


class TellTest(unittest.TestCase):
    def test_frame_reaches_session_socket(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "s.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            got = []

            def accept():
                conn, _ = server.accept()
                got.append(conn.recv(65536).decode("utf-8"))
                conn.close()

            thread = threading.Thread(target=accept)
            thread.start()
            with mock.patch.object(sideagent, "_read_state", return_value={"account": "003", "session_id": "s"}), \
                 mock.patch.object(takeover, "registry_entry", return_value={"messagingSocketPath": path}):
                error = takeover.tell("003#bg-x", "run: echo A > b", {"messagingSocketPath": "/me.sock",
                                                                    "name": "ham-76"})
            thread.join(5)
            server.close()
        self.assertEqual(error, "")
        frame = json.loads(got[0])
        self.assertEqual(frame["from"], "uds:/me.sock")
        # 不转义：对方看到的就是原话
        self.assertIn("run: echo A > b", frame["message"]["content"])
        self.assertIn('from-name="ham-76"', frame["message"]["content"])

    def test_not_running_says_so(self):
        with mock.patch.object(sideagent, "_read_state", return_value={"account": "003", "session_id": "s"}), \
             mock.patch.object(takeover, "registry_entry", return_value={}):
            self.assertIn("没在跑", takeover.tell("003#bg-x", "hi", {}))


ROWS = (
    {"account": "003", "id": "36709e4a", "session_id": "36709e4a-9666", "name": "exp-a"},
    {"account": "006", "id": "36700000", "session_id": "36700000-1111", "name": "train"},
)


class FindTest(unittest.TestCase):

    def test_exact_name_and_prefix(self):
        self.assertEqual([row["account"] for row in takeover.find("train", list(ROWS))], ["006"])
        self.assertEqual([row["id"] for row in takeover.find("36709", list(ROWS))], ["36709e4a"])
        self.assertEqual(len(takeover.find("367", list(ROWS))), 2)
        self.assertEqual(takeover.find("", list(ROWS)), [])

    def test_mcp_lists_takeover_tools(self):
        names = [tool["name"] for tool in mcp_sideagent.TOOLS]
        self.assertIn("sideagent_takeover", names)
        self.assertIn("sideagent_release", names)


if __name__ == "__main__":
    unittest.main()


class PickerTest(unittest.TestCase):
    """接管挑选框的数据：默认只列没在跑的、不列自己、带上最后一句和是否已接管。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        projects = root / "projects" / "-w"
        projects.mkdir(parents=True)
        (projects / "s-idle.jsonl").write_text((FIXTURES / "bg-transcript.jsonl").read_text(encoding="utf-8"),
                                               encoding="utf-8")
        rows = [
            {"account": "006", "id": "s-idle", "session_id": "s-idle", "name": "", "kind": "background",
             "cwd": "/w", "state": "done", "running": False},
            {"account": "006", "id": "s-busy", "session_id": "s-busy", "name": "train", "kind": "background",
             "cwd": "/w", "state": "working", "running": True},
            {"account": "005", "id": "me", "session_id": "me", "name": "总控", "kind": "interactive",
             "cwd": "/w", "state": "idle", "running": True},
        ]
        side = root / "sideagents"
        side.mkdir()
        self.patches = [mock.patch.object(takeover, "PROJECTS_DIR", root / "projects"),
                        mock.patch.object(takeover, "candidates", return_value=rows),
                        mock.patch.object(sideagent, "SIDEAGENT_DIR", side)]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self.tmp.cleanup)
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def test_default_lists_idle_ones_but_not_myself(self):
        rows = takeover.picker_rows(owner_session="me")
        self.assertEqual([row["id"] for row in rows], ["s-idle"])
        # 最后一句从对话记录末尾取（夹具里它最后说的是 OK）
        self.assertEqual(rows[0]["last"], "OK")
        self.assertFalse(rows[0]["taken_over"])
        self.assertGreater(rows[0]["active_at"], 0)   # 有对话记录就带上最后活动时间

    def test_all_includes_running_and_marks_taken_over(self):
        # 格式改了：被我接管 = 我的名单里有它（kind=attached），不再看 state.json 的 owner_session
        sideagent._write_state("006#bg-s-busy", {"kind": "attached", "session_id": "s-busy"})  # noqa: SLF001
        write_roster("me", {"006#bg-s-busy": "attached"})
        with mock.patch.object(roster, "owner_alive", return_value=True):
            rows = {row["id"]: row for row in takeover.picker_rows(include_running=True, owner_session="me")}
        self.assertEqual(set(rows), {"s-idle", "s-busy"})
        self.assertTrue(rows["s-busy"]["taken_over"])
        self.assertEqual(rows["s-busy"]["taken_by"], [])

    def test_taken_by_lists_other_owners(self):
        # M 对 N：别的会话接管的也列出来（仍然能选），带上那个会话的标题或短编号；主人不在了的不算
        sideagent._write_state("006#bg-s-idle", {"kind": "attached", "session_id": "s-idle"})  # noqa: SLF001
        write_roster("other-session-1234", {"006#bg-s-idle": "attached"})
        write_roster("dead-session-5678", {"006#bg-s-idle": "attached"})
        alive = mock.patch.object(roster, "owner_alive", side_effect=lambda data: data["session"].startswith("other"))
        with alive:
            rows = {row["id"]: row for row in takeover.picker_rows(owner_session="me")}
        self.assertFalse(rows["s-idle"]["taken_over"])
        self.assertEqual(rows["s-idle"]["taken_by"], ["other-se"])


class CardTest(unittest.TestCase):
    """接管卡片：有没有 recap 都带上最近几轮问答的原话，再附对话记录路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name) / "projects" / "-w"
        folder.mkdir(parents=True)
        self.path = folder / "s1.jsonl"
        patch = mock.patch.object(takeover, "PROJECTS_DIR", Path(self.tmp.name) / "projects")
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, records):
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    @staticmethod
    def ask(text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    @staticmethod
    def say(text):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    def test_last_rounds_keep_final_answer_and_skip_noise(self):
        self.write([
            self.ask("第一件"), self.say("一的答"),
            self.ask("第二件"), self.say("先看看"), self.say("二的答"),
            self.ask("<command-name>/reload-plugins</command-name>"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "元信息"}},
            self.ask("第三件"), self.say("三的答"),
            self.ask("第四件"),
        ])
        self.assertEqual(takeover.recent_rounds("s1", 3),
                         [("第二件", "二的答"), ("第三件", "三的答"), ("第四件", "")])

    def test_card_has_rounds_and_path_without_recap(self):
        self.write([self.ask("做 A"), self.say("A 做完了")])
        text = takeover.card("s1", account="003", title="任务")
        self.assertIn("接管了 003/s1「任务」", text)
        self.assertNotIn("回顾：", text)
        self.assertIn("  问：做 A", text)
        self.assertIn("  答：A 做完了", text)
        self.assertTrue(text.endswith(f"对话记录：{self.path}"))

    def test_card_is_capped(self):
        self.write([r for i in range(5) for r in (self.ask("问" * 500), self.say("答" * 500))])
        with mock.patch.object(takeover, "TAKEOVER_CARD_TOTAL_CHARS", 400):
            text = takeover.card("s1", account="003")
        self.assertLessEqual(len(text.split("\n对话记录：")[0]), 401)


class AskTextTest(unittest.TestCase):
    def test_system_injected_user_text_is_not_an_ask(self):
        for text in ("<task-notification>x</task-notification>", "<cross-session-message>hi",
                     "This session is being continued from a previous conversation that ran out of context."):
            self.assertEqual(takeover._ask_text({"type": "user", "message": {"content": text}}), "")  # noqa: SLF001
        self.assertEqual(takeover._ask_text({"type": "user", "message": {"content": " 查一下 "}}), "查一下")  # noqa: SLF001


SID = "3339d1d0-d064-473e-a682-41b731967700"


def _row(account: str, state: str, started: float, *, running: bool = False, kind: str = "background") -> dict:
    return {"account": account, "id": SID[:8], "session_id": SID, "name": "enhance-pure", "kind": kind,
            "cwd": "", "state": state, "running": running, "started_at": started}


class MessageTest(unittest.TestCase):
    """sideagent_send {session}：不接管，自动找它现在挂的号；没在跑就唤醒再送。"""

    SENDER = {"sessionId": "me-0000", "messagingSocketPath": "/me.sock",  # noqa: RUF012 - 只读夹具
              "name": "train", "account": "b004"}

    def send(self, rows, *, live=None, usable=None, wake_error="", account=""):
        live = dict(live or {})
        woke, sent = [], []

        def registry(acct, _sid):
            return {"messagingSocketPath": f"/{acct}.sock"} if acct in live else {}

        def wake(row):
            woke.append((row["account"], row["kind"]))
            if not wake_error:
                live[row["account"]] = True
            return wake_error

        def deliver(entry, text, sender):
            sent.append((entry.get("messagingSocketPath"), text, sender["name"]))
            return ""

        with mock.patch.object(takeover, "candidates", return_value=[dict(r) for r in rows]), \
             mock.patch.object(takeover, "registry_entry", side_effect=registry), \
             mock.patch.object(takeover, "_wake", side_effect=wake), \
             mock.patch.object(takeover, "deliver", side_effect=deliver), \
             mock.patch.object(takeover, "_spawned_owner", return_value=""), \
             mock.patch.object(takeover, "SOCKET_WAIT", 0):
            result = takeover.message("enhance-pure", "跑 300/500 判官", self.SENDER,
                                      account=account, usable=usable)
        return result, woke, sent

    def test_running_session_gets_message_without_wake(self):
        result, woke, sent = self.send([_row("001", "done", 1), _row("b002", "working", 2, running=True)],
                                       live={"b002": True})
        self.assertEqual((result["account"], result["woke"]), ("b002", False))
        self.assertEqual(woke, [])
        self.assertEqual(sent, [("/b002.sock", "跑 300/500 判官", "train")])

    def test_done_session_is_woken_on_newest_registration(self):
        result, woke, sent = self.send([_row("001", "done", 1), _row("b002", "done", 5)])
        self.assertEqual(woke, [("b002", "background")])
        self.assertTrue(result["woke"])
        self.assertEqual(sent[0][0], "/b002.sock")

    def test_stopped_registration_is_the_last_resort(self):
        """handoff 把源号登记标成 stopped：即使它更新，也排在没停的那条后面。"""
        result, woke, _ = self.send([_row("001", "stopped", 9), _row("b002", "done", 1)])
        self.assertEqual(woke, [("b002", "background")])
        self.assertEqual(result["account"], "b002")

    def test_only_stopped_still_wakes(self):
        result, woke, _ = self.send([_row("001", "stopped", 9)])
        self.assertEqual(woke, [("001", "background")])
        self.assertTrue(result["woke"])

    def test_full_account_is_skipped(self):
        usable = {"001": "额度用满，等重置"}.get
        result, woke, _ = self.send([_row("001", "done", 9), _row("b002", "stopped", 1)],
                                    usable=lambda name: usable(name, ""))
        self.assertEqual(woke, [("b002", "background")])
        self.assertEqual(result["account"], "b002")

    def test_falls_back_to_my_account_as_plain_session(self):
        result, woke, _ = self.send([_row("001", "done", 9)],
                                    usable=lambda name: "额度用满，等重置" if name == "001" else "")
        self.assertEqual(woke, [("b004", "session")])
        self.assertEqual(result["account"], "b004")

    def test_nowhere_to_wake_explains_why(self):
        result, woke, sent = self.send([_row("001", "done", 9)], usable=lambda name: "额度用满，等重置")
        self.assertIn("001（额度用满，等重置）", result["error"])
        self.assertIn("b004（额度用满，等重置）", result["error"])
        self.assertEqual((woke, sent), ([], []))

    def test_explicit_account_does_not_fall_back(self):
        result, woke, _ = self.send([_row("001", "done", 9)], account="001",
                                    usable=lambda name: "额度用满，等重置")
        self.assertIn("001（额度用满，等重置）", result["error"])
        self.assertNotIn("b004", result["error"])
        self.assertEqual(woke, [])

    def test_wake_failure_is_reported(self):
        result, _, sent = self.send([_row("001", "done", 9)], wake_error="起不来")
        self.assertIn("唤醒失败：起不来", result["error"])
        self.assertEqual(sent, [])

    def test_refuses_myself_and_spawned_sideagents(self):
        mine = [{**_row("b004", "working", 1, running=True), "session_id": "me-0000"}]
        with mock.patch.object(takeover, "candidates", return_value=mine):
            self.assertEqual(takeover.message("me-0000", "hi", self.SENDER)["error"], "这是你自己")
        with mock.patch.object(takeover, "candidates", return_value=[_row("ds", "done", 1)]), \
             mock.patch.object(takeover, "_spawned_owner", return_value="cca-skill"):
            self.assertIn("派出的 sideagent", takeover.message("enhance-pure", "hi", self.SENDER)["error"])

    def test_mcp_send_with_session_does_not_attach(self):
        entry = {"sessionId": "me-0000", "messagingSocketPath": "/me.sock", "name": "train", "pid": 1}
        with mock.patch.object(sideagent, "caller_now", return_value=entry), \
             mock.patch.object(sideagent, "_owner_account", return_value="b004"), \
             mock.patch.object(takeover, "message", return_value={
                 "account": "b002", "session_id": SID, "label": "enhance-pure", "woke": True}) as sent, \
             mock.patch.object(takeover, "takeover") as attach:
            reply = mcp_sideagent._tool_send({"session": "enhance-pure", "message": "hi"})  # noqa: SLF001
        self.assertIn("b002/3339d1d0（enhance-pure）没在跑，已唤醒并送到", reply)
        self.assertEqual(sent.call_args.args[2]["account"], "b004")
        attach.assert_not_called()


class TakeoverPicksCurrentAccountTest(unittest.TestCase):
    def test_stale_stopped_registration_is_not_taken_over(self):
        """handoff 后旧号上是 stopped：不写 account 时接的是新号上那条，不去唤醒旧副本。"""
        rows = [_row("001", "stopped", 9), _row("b004", "done", 1)]
        woke = []
        with mock.patch.object(takeover, "candidates", return_value=rows), \
             mock.patch.object(takeover, "_spawned_owner", return_value=""), \
             mock.patch.object(takeover, "registry_entry", return_value={}), \
             mock.patch.object(takeover, "_wake", side_effect=lambda row: woke.append(row["account"]) or "停在这"):
            result = takeover.takeover("enhance-pure", {"session": "me", "account": "005"})
        self.assertEqual(woke, ["b004"])
        self.assertIn("b004/", result["error"])
