"""sideagent 目录和 M 对 N 接管（docs/specs/sideagent-mn.md 第六节要的测试）。

全在临时目录里：SIDEAGENT_DIR 换成 <临时>/sideagents，名单在 <临时>/sessions，不碰真实的 ~/.cca。
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from sidehelp import use_temp_root, write_roster, write_state  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca import mcp_sideagent  # noqa: E402
from cca.config import SIDEAGENT_KEEP_DAYS  # noqa: E402
from cca.session import backends, roster, sideagent, takeover  # noqa: E402

DAY = 86400
SESSION = "6b6dd1ed-90aa-433f-b4ce-95584e83d015"
NAME = "003#bg-6b6dd1ed"


def _owner(session: str, pid: int) -> dict:
    return {"session": session, "pid": pid, "proc_start": f"p{pid}", "account": "005",
            "instance_socket": f"/tmp/{pid}.sock"}  # noqa: S108


class TakeoverMNTest(unittest.TestCase):
    """两个主会话接管同一个原生会话。"""

    def setUp(self):
        self.root = use_temp_root(self)
        self.transcript = self.root.parent / "t.jsonl"
        self.transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "干活"}})
                                   + "\n", encoding="utf-8")
        row = {"account": "003", "id": "6b6dd1ed", "session_id": SESSION, "name": "exp-a",
               "kind": "background", "cwd": "/w", "state": "idle", "running": True}
        self.alive = {"主会话-A", "主会话-B"}
        for patcher in (mock.patch.object(takeover, "candidates", return_value=[row]),
                        mock.patch.object(takeover, "registry_entry",
                                          return_value={"sessionId": SESSION, "status": "idle", "jobId": ""}),
                        mock.patch.object(takeover, "transcript", return_value=self.transcript),
                        mock.patch.object(takeover, "card", return_value="卡片"),
                        mock.patch.object(roster, "owner_alive",
                                          side_effect=lambda data: data.get("session") in self.alive)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_two_owners_take_over_the_same_one(self):
        first = takeover.takeover(SESSION, _owner("主会话-A", 11))
        self.assertEqual(first["name"], NAME)
        state = sideagent._read_state(NAME)  # noqa: SLF001
        self.assertEqual(state["kind"], "attached")
        self.assertNotIn("owner_session", state)          # 谁用着它不记在 state.json
        taken_at = state["taken_over_at"]
        events_before = sideagent._events_path(NAME).read_text(encoding="utf-8")  # noqa: SLF001
        second = takeover.takeover(SESSION, _owner("主会话-B", 22))
        self.assertEqual(second["name"], NAME)
        # 第二个主人：不重建、不覆盖（同一个文件夹、同一份同步），只在自己的名单里加一条
        self.assertEqual(sideagent._read_state(NAME)["taken_over_at"], taken_at)  # noqa: SLF001
        self.assertEqual(sideagent._events_path(NAME).read_text(encoding="utf-8"), events_before)  # noqa: SLF001
        self.assertEqual([path.name for path in self.root.iterdir() if path.is_dir()], [NAME])
        for session in ("主会话-A", "主会话-B"):
            self.assertEqual(roster.read(session)["agents"][NAME]["kind"], "attached")
        self.assertEqual(sorted(h["session"] for h in roster.holders(NAME)), ["主会话-A", "主会话-B"])
        # 名单顶上是主人那部分
        top = roster.read("主会话-B")
        self.assertEqual((top["pid"], top["proc_start"], top["instance_socket"]), (22, "p22", "/tmp/22.sock"))  # noqa: S108

    def test_one_releases_the_other_keeps_it(self):
        takeover.takeover(SESSION, _owner("主会话-A", 11))
        takeover.takeover(SESSION, _owner("主会话-B", 22))
        self.assertEqual(takeover.release(NAME, "主会话-A"), 1)
        self.assertNotIn(NAME, roster.read("主会话-A")["agents"])
        self.assertIn(NAME, roster.read("主会话-B")["agents"])
        self.assertFalse(sideagent._read_state(NAME).get("finished_at"))  # noqa: SLF001
        # 还挂着它的那个主会话照样同步它
        with mock.patch.object(backends.TakenOverBackend, "sync",
                               lambda _self, name, **_kw: {"name": name}) as _sync:
            self.assertEqual(backends.sync_owned("主会话-B"), 1)
            self.assertEqual(backends.sync_owned("主会话-A"), 0)
        # 最后一个也放手：删 run/、写 finished_at，文件夹留着
        self.assertEqual(takeover.release(NAME, "主会话-B"), 0)
        self.assertGreater(sideagent._read_state(NAME)["finished_at"], 0)  # noqa: SLF001
        self.assertTrue(sideagent._events_path(NAME).exists())  # noqa: SLF001
        # 停了以后又有人接管：接着用原来的文件夹，去掉 finished_at
        takeover.takeover(SESSION, _owner("主会话-A", 11))
        self.assertNotIn("finished_at", sideagent._read_state(NAME))  # noqa: SLF001

    def test_release_tool_only_removes_my_entry(self):
        takeover.takeover(SESSION, _owner("主会话-A", 11))
        takeover.takeover(SESSION, _owner("主会话-B", 22))
        me = {"pid": 11, "procStart": "p11", "sessionId": "主会话-A"}
        with mock.patch.object(sideagent, "caller_now", return_value=me):
            text = mcp_sideagent._tool_release({"name": NAME})  # noqa: SLF001
        self.assertIn("还有 1 个会话接管着它", text)
        with mock.patch.object(sideagent, "caller_now", return_value=me):
            self.assertIn("不是本会话接管的", mcp_sideagent._tool_release({"name": NAME}))  # noqa: SLF001

    def test_cannot_take_over_someone_elses_spawned_sideagent(self):
        # 目标会话其实是 主会话-A 派出的 sideagent（它自己的 claude 会话）：只有一个主人，不能接管
        write_state("ds#a1b2c3", kind="spawned", session_id=SESSION)
        write_roster("主会话-A", {"ds#a1b2c3": "spawned"})
        with mock.patch.object(takeover, "session_name", return_value=""):
            result = takeover.takeover(SESSION, _owner("主会话-B", 22))
        self.assertEqual(result["error"], "这是 主会话-A 派出的 sideagent，不能接管")
        self.assertFalse(sideagent._state_path(NAME).exists())  # noqa: SLF001
        self.assertEqual(roster.read("主会话-B"), {})


class DeadOwnerTest(unittest.TestCase):
    """主人死了的名单不算；清理时接管的那条删掉（只当作解开），派出的留着。"""

    def setUp(self):
        self.root = use_temp_root(self)
        write_state(NAME, kind="attached", session_id=SESSION)
        write_state("ds#a1", kind="spawned")
        write_roster("活着的", {"ds#a1": "spawned"}, pid=11, proc_start="p11")
        write_roster("死了的", {NAME: "attached", "ds#a1": "spawned"}, pid=22, proc_start="p22")
        patcher = mock.patch.object(roster, "owner_alive", side_effect=lambda data: data.get("session") == "活着的")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dead_owner_is_not_a_holder(self):
        self.assertEqual(roster.holders(NAME), [])
        self.assertEqual([h["session"] for h in roster.holders(NAME, alive_only=False)], ["死了的"])

    def test_cleanup_unties_attached_and_finishes_it(self):
        roster.cleanup(force=True)
        # 没有活着的主人：接管的写 finished_at，死了的名单里那一条删掉；派出的条目留着（以后还能接着开）
        self.assertGreater(sideagent._read_state(NAME)["finished_at"], 0)  # noqa: SLF001
        self.assertEqual(roster.read("死了的")["agents"], {"ds#a1": {"kind": "spawned", "since": 1.0}})

    def test_cleanup_deletes_dead_empty_rosters_and_expired_folders(self):
        old = time.time() - (SIDEAGENT_KEEP_DAYS + 1) * DAY
        sideagent.mark_finished("ds#a1", old)
        write_roster("死了也空的", {})
        roster.cleanup(force=True)
        self.assertFalse((self.root / "ds#a1").exists())
        self.assertFalse(roster.roster_path("死了也空的").exists())
        # 文件夹删了，名单里指向它的条目也删；死了的名单删空了就整个删掉，活着的留着空名单
        self.assertFalse(roster.roster_path("死了的").exists())
        self.assertEqual(roster.read("活着的")["agents"], {})

    def test_cleanup_removes_stale_run_dir_of_finished(self):
        """进程被硬杀，state 写了 finished_at 但 run/sock 还在：清理时删掉 run/，免得被当成在跑。"""
        run = self.root / "ds#a1" / "run"
        run.mkdir(parents=True)
        (run / "sock").write_text("", encoding="utf-8")
        state = sideagent._read_state("ds#a1")  # noqa: SLF001
        sideagent._write_state("ds#a1", {**state, "finished_at": time.time()})  # noqa: SLF001
        roster.cleanup(force=True)
        self.assertFalse(run.exists())
        self.assertTrue((self.root / "ds#a1" / "state.json").exists())

    def test_cleanup_waits_for_its_interval(self):
        roster.cleanup(force=True)
        sideagent._write_state(NAME, {"kind": "attached"})  # noqa: SLF001
        with mock.patch.object(roster, "holders") as holders:
            roster.cleanup()
        holders.assert_not_called()


class ChainTest(unittest.TestCase):
    """主会话换了编号（/bg 等）：一串编号的名单合起来算；写到最新编号，旧文件并过来删掉。"""

    def setUp(self):
        use_temp_root(self)
        patcher = mock.patch.object(sideagent, "session_chain",
                                    side_effect=lambda sid: ["旧", "新"] if sid == "旧" else [sid])
        patcher.start()
        self.addCleanup(patcher.stop)
        roster._chain_cache.clear()  # noqa: SLF001

    def test_read_merges_and_write_moves_to_latest(self):
        write_roster("旧", {"ds#a": "spawned"})
        write_roster("新", {"ds#b": "spawned"})
        write_roster("别人", {"ds#c": "spawned"})
        self.assertEqual(sorted(roster.mine("新")), ["ds#a", "ds#b"])
        roster.add(_owner("新", 33), "ds#d", roster.SPAWNED)
        self.assertFalse(roster.roster_path("旧").exists())
        self.assertEqual(sorted(roster.read("新")["agents"]), ["ds#a", "ds#b", "ds#d"])
        self.assertTrue(roster.roster_path("别人").exists())

    def test_adopt_refreshes_owner_and_skips_unused_sessions(self):
        # MCP 后台线程定时调：没用过 sideagent 的会话不建空名单；用过的并到最新编号、刷新进程号
        self.assertEqual(roster.adopt(_owner("新", 33)), {})
        self.assertFalse(roster.roster_path("新").exists())
        write_roster("旧", {"ds#a": "spawned"}, pid=5, proc_start="p5")
        data = roster.adopt(_owner("新", 33))
        self.assertEqual((data["pid"], data["proc_start"], sorted(data["agents"])), (33, "p33", ["ds#a"]))
        self.assertFalse(roster.roster_path("旧").exists())

    def test_same_process_counts_as_mine(self):
        # 同一个进程里换了编号、又没有接力记录（例如 /resume 了别的对话）：按进程认，照旧算我的
        write_roster("另一段", {"ds#e": "spawned"}, pid=44, proc_start="p44")
        self.assertEqual(roster.mine("新"), {})
        self.assertIn("ds#e", roster.mine("新", pid=44, proc_start="p44"))


class OwnershipTest(unittest.TestCase):
    """MCP 工具按名单认“本会话的”。"""

    def setUp(self):
        use_temp_root(self)
        me = {"pid": 11, "procStart": "p11", "sessionId": "会话-1"}
        for patcher in (mock.patch.object(sideagent, "caller_now", return_value=me),
                        mock.patch.object(roster, "owner_alive", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_instances_follow_the_roster(self):
        for name in ("ds#mine", "ds#theirs", "ds#done"):
            write_state(name, kind="spawned")
        sideagent.mark_finished("ds#done")
        write_roster("会话-1", {"ds#mine": "spawned", "ds#done": "spawned"})
        write_roster("会话-2", {"ds#theirs": "spawned"})
        # 停了的不算“在跑的实例”，别人名单里的看不到
        self.assertEqual(mcp_sideagent._instances("ds"), ["ds#mine"])  # noqa: SLF001
        self.assertEqual(mcp_sideagent._instances("ds#theirs"), [])  # noqa: SLF001
        self.assertEqual(mcp_sideagent._instances("ds#done"), ["ds#done"])  # noqa: SLF001

    def test_spawn_writes_state_folder_and_roster(self):
        def fake_popen(_args, **_kwargs):
            msg = "不真起"
            raise OSError(msg)

        with mock.patch.object(sideagent, "profile_dir", return_value=Path("/")), \
                mock.patch.object(sideagent, "_ensure_inbound_setting"), \
                mock.patch.object(sideagent, "running", return_value=False), \
                mock.patch.object(sideagent.subprocess, "Popen", side_effect=fake_popen), \
                self.assertRaises(OSError):
            sideagent._ensure_started("ds#n1", "/w", 60, [], label="查日志")  # noqa: SLF001
        self.assertTrue(sideagent.agent_dir("ds#n1").is_dir())
        entry = roster.read("会话-1")["agents"]["ds#n1"]
        self.assertEqual((entry["kind"], entry["label"]), ("spawned", "查日志"))


# 迁移是单独的脚本（运行时不认老格式），按文件路径加载
_SPEC = importlib.util.spec_from_file_location("migrate_sideagent_layout",
                                               REPO / "scripts" / "migrate_sideagent_layout.py")
MIGRATE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MIGRATE)


class MigrateTest(unittest.TestCase):
    """scripts/migrate_sideagent_layout.py：老的平铺文件 → 一个 sideagent 一个文件夹 + 主会话名单。"""

    def setUp(self):
        self.root = use_temp_root(self)

    def _flat(self, filename: str, data, *, age_days: float = 0.0) -> Path:
        path = self.root / filename
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        if age_days:
            stamp = time.time() - age_days * DAY
            os.utime(path, (stamp, stamp))
        return path

    def test_migrate_moves_files_and_builds_rosters(self):
        # 派出的、已经停了：状态文件早删了，留着结果记录、事件、进度、归属纸条、死掉的套接字
        self._flat("ds#a1.last.json", {"account": "ds#a1", "session_id": "s-1", "jobs": [{"id": "j1"}]})
        self._flat("ds#a1.events", '{"kind": "task"}\n')
        self._flat("ds#a1.progress", "RESULT 好\n")
        self._flat("ds#a1.summary.log", "ok\n")
        self._flat("ds#a1.log", "log\n")
        self._flat("ds#a1.live", "")
        self._flat("ds#a1.last.lock", "")
        self._flat("ds#a1.owner", {"session": "会话-1", "account": "005", "label": "查日志",
                                   "caller_agent": "a7", "instance_socket": "/x/a1.sock"})
        # 接管的（老格式 kind=bg）
        self._flat(f"{NAME}.json", {"kind": "bg", "external": True, "session_id": SESSION,
                                    "owner_session": "会话-1", "owner_pid": 9, "owner_proc_start": "p9"})
        self._flat(f"{NAME}.events", "")
        # 只有结果记录的老记录：40 天前的删，5 天前的搬
        self._flat("ds#old.last.json", {"account": "ds#old"}, age_days=SIDEAGENT_KEEP_DAYS + 10)
        self._flat("ds#recent.last.json", {"account": "ds#recent"}, age_days=5)
        # 不是 sideagent 的照旧留着
        for keep in ("lineage.jsonl", "notices.jsonl", "panel-debug.log", "main-session"):
            self._flat(keep, "x")

        self.assertEqual(MIGRATE.migrate(), (4, []))

        folder = self.root / "ds#a1"
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["events.jsonl", "log", "progress", "result.json", "state.json", "summary.log"])
        state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((state["kind"], state["session_id"], state["instance_socket"]),
                         ("spawned", "s-1", "/x/a1.sock"))
        self.assertGreater(state["finished_at"], 0)
        attached = json.loads((self.root / NAME / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(attached["kind"], "attached")
        self.assertNotIn("external", attached)
        self.assertNotIn("owner_session", attached)
        self.assertFalse(attached.get("finished_at"))
        self.assertFalse((self.root / "ds#old").exists())
        self.assertFalse((self.root / "ds#old.last.json").exists())
        self.assertTrue((self.root / "ds#recent" / "result.json").exists())
        for keep in ("lineage.jsonl", "notices.jsonl", "panel-debug.log", "main-session"):
            self.assertTrue((self.root / keep).exists())
        self.assertEqual(sorted(path.name for path in self.root.glob("ds#a1.*")), [])
        data = roster.read("会话-1")
        self.assertEqual((data["pid"], data["proc_start"], data["account"]), (9, "p9", "005"))
        self.assertEqual(data["agents"]["ds#a1"]["kind"], "spawned")
        self.assertEqual((data["agents"]["ds#a1"]["label"], data["agents"]["ds#a1"]["caller_agent"]),
                         ("查日志", "a7"))
        self.assertEqual(data["agents"][NAME]["kind"], "attached")
        # 再跑一次没有活了
        self.assertEqual(MIGRATE.migrate(), (0, []))

    def test_running_old_daemon_is_skipped_until_it_stops(self):
        # 在跑的老守护进程（套接字能连上）是老代码，还在写平铺文件：先跳过并报出来，停了再跑一次脚本
        self._flat("ds#run.json", {"owner_session": "会话-1", "pid": os.getpid()})
        self._flat("ds#run.progress", "TOOL Bash\n")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.root / "ds#run.sock"))
        sock.listen(1)
        self.addCleanup(sock.close)
        self.assertEqual(MIGRATE.migrate(), (0, ["ds#run"]))
        self.assertTrue((self.root / "ds#run.json").exists())
        self.assertFalse((self.root / "ds#run").exists())
        self.assertEqual(roster.read("会话-1"), {})
        # 它停了（老代码退出时删状态文件和套接字）：下一次进来就搬
        sock.close()
        (self.root / "ds#run.sock").unlink()
        (self.root / "ds#run.json").unlink()
        self.assertEqual(MIGRATE.migrate(), (1, []))
        self.assertTrue((self.root / "ds#run" / "progress").exists())


if __name__ == "__main__":
    unittest.main()
