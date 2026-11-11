"""resolve_owner：换号重开后跟到新号下的新进程；死掉的进程留下的登记文件不算。"""

from __future__ import annotations

import json
import os
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


class ResolveOwnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        for name in ("002", "003"):
            (self.home / ".claude-accounts" / name / "sessions").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        self.alive = set()
        self.live = mock.patch.object(sideagent, "_pid_alive", lambda pid: pid in self.alive)
        self.live.start()

    def tearDown(self):
        self.live.stop()
        self.env.stop()
        self.tmp.cleanup()

    def register(self, account, pid, session, start):
        path = self.home / ".claude-accounts" / account / "sessions" / f"{pid}.json"
        path.write_text(json.dumps({"pid": pid, "sessionId": session, "procStart": start}), encoding="utf-8")

    def sessions(self, account):
        return self.home / ".claude-accounts" / account / "sessions"

    def test_follows_reopened_session_into_another_account(self):
        # 003 派的活；主会话换到 002 重开，老进程的登记文件还留在 003 下
        self.register("003", 3444840, "de9cdb1b", "28409965")
        self.register("002", 109748, "de9cdb1b", "531028")
        self.alive = {109748}
        owner = {"pid": 3444840, "proc_start": "28409965", "session": "de9cdb1b"}
        self.assertEqual(sideagent.resolve_owner(self.sessions("003"), owner)["pid"], 109748)

    def test_same_process_still_wins(self):
        self.register("003", 3444840, "de9cdb1b", "28409965")
        self.alive = {3444840}
        owner = {"pid": 3444840, "proc_start": "28409965", "session": "de9cdb1b"}
        self.assertEqual(sideagent.resolve_owner(self.sessions("003"), owner)["pid"], 3444840)

    def test_dead_leftover_is_not_the_owner(self):
        self.register("003", 3444840, "de9cdb1b", "28409965")
        owner = {"pid": 3444840, "proc_start": "28409965", "session": "de9cdb1b"}
        self.assertEqual(sideagent.resolve_owner(self.sessions("003"), owner), {})


if __name__ == "__main__":
    unittest.main()
