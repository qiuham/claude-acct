"""sideagent 后端选择：按状态文件里的 kind 选实现，调用方不再自己判断。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from sidehelp import use_temp_root, write_roster  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import backends, roster, sideagent  # noqa: E402


class BackendForTest(unittest.TestCase):
    def setUp(self):
        use_temp_root(self)

    def test_default_is_process(self):
        self.assertIs(backends.backend_for("ds#a1"), backends.BACKENDS[roster.SPAWNED])
        self.assertIs(backends.backend_for(""), backends.BACKENDS[roster.SPAWNED])

    def test_taken_over_session(self):
        # 接管的写 kind=attached（老格式由迁移脚本转好，运行时不认）
        sideagent._write_state("003#bg-1234abcd", {"kind": "attached"})  # noqa: SLF001
        backend = backends.backend_for("003#bg-1234abcd")
        self.assertIs(backend, backends.BACKENDS[roster.ATTACHED])
        # 接管的会话：主会话退出不停它、不走后台任务、停和打断都拒绝并说明原因
        self.assertTrue(backend.outlives_owner)
        self.assertFalse(backend.takes_jobs)
        self.assertIn("claude stop", backend.stop("003#bg-1234abcd"))
        self.assertIn("打断不了", backend.interrupt("003#bg-1234abcd"))

    def test_process_backend_is_default_for_unknown_kind(self):
        sideagent._write_state("ds#a2", {"kind": "以后的新种类"})  # noqa: SLF001
        self.assertIs(backends.backend_for("ds#a2"), backends.BACKENDS[roster.SPAWNED])


if __name__ == "__main__":
    unittest.main()


class SyncLoopTest(unittest.TestCase):
    def setUp(self):
        use_temp_root(self)
        # 格式改了：谁用着谁记在名单里，不再看 state.json 的 owner_session
        sideagent._write_state("003#bg-aaaa1111", {"kind": "attached"})  # noqa: SLF001
        sideagent._write_state("003#bg-bbbb2222", {"kind": "attached"})  # noqa: SLF001
        sideagent._write_state("ds#c1", {"kind": "spawned"})  # noqa: SLF001
        write_roster("S1", {"003#bg-aaaa1111": "attached", "ds#c1": "spawned"})
        write_roster("别人", {"003#bg-bbbb2222": "attached"})
        for patcher in (mock.patch.object(sideagent, "caller_now", return_value={}),
                        mock.patch.object(roster, "cleanup")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_this_sessions_taken_over_ones_are_synced(self):
        seen = []
        with mock.patch.object(backends.TakenOverBackend, "sync", lambda _self, name, **_kw: seen.append(name) or {}):
            self.assertEqual(backends.sync_owned("S1"), 1)
        self.assertEqual(seen, ["003#bg-aaaa1111"])

    def test_loop_syncs_and_writes_heartbeat_then_stops(self):
        import threading  # noqa: PLC0415

        class OneShot(threading.Event):
            def wait(self, timeout=None):  # 第一轮做完就停
                self.set()
                return True

        seen = []
        with mock.patch.object(backends.TakenOverBackend, "sync", lambda _self, name, **_kw: seen.append(name) or {}):
            backends.sync_loop(OneShot(), lambda: "S1", interval=0)
        self.assertEqual(seen, ["003#bg-aaaa1111"])
        self.assertTrue(backends.beat_path("S1").is_file())
