"""对话被 Claude Code 挪到后台换了编号（continued-in）：sideagent 跟到最新那一份。"""

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


class SessionChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects" / "-home-ham"
        self.projects.mkdir(parents=True)
        self.sessions = root / "sessions"
        self.sessions.mkdir()
        self.alive = set()
        self.patches = [
            mock.patch.object(sideagent, "TRANSCRIPTS_DIR", root / "projects"),
            mock.patch.object(sideagent, "_pid_alive", lambda pid: pid in self.alive),
            mock.patch.object(sideagent, "_session_dirs", lambda first: [first]),
        ]
        for patcher in self.patches:
            patcher.start()
        sideagent._continued_cache.clear()  # noqa: SLF001

    def tearDown(self):
        for patcher in self.patches:
            patcher.stop()
        self.tmp.cleanup()

    def transcript(self, session, continued=""):
        lines = [json.dumps({"type": "user", "sessionId": session})]
        if continued:
            lines.append(json.dumps({"type": "continued-in", "sessionId": session, "continuedInSessionId": continued}))
        (self.projects / f"{session}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def register(self, pid, session, start="s"):
        (self.sessions / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": session, "procStart": start}), encoding="utf-8")

    def test_chain_follows_continued_in(self):
        self.transcript("a", "b")
        self.transcript("b", "c")
        self.transcript("c")
        self.assertEqual(sideagent.session_chain("a"), ["a", "b", "c"])
        self.assertEqual(sideagent.latest_session("a"), "c")
        self.assertEqual(sideagent.latest_session("zzz"), "zzz")

    def test_owner_moves_to_bg_copy_even_if_original_still_open(self):
        # 9d5254fa 在前台 111240 里 /bg，复制成 cd89762a 在 843744 里跑；前台进程还开着
        self.transcript("9d5254fa", "cd89762a")
        self.transcript("cd89762a")
        self.register(111240, "9d5254fa", "p1")
        self.register(843744, "cd89762a", "p2")
        self.alive = {111240, 843744}
        owner = {"pid": 111240, "proc_start": "p1", "session": "9d5254fa"}
        self.assertEqual(sideagent.resolve_owner(self.sessions, owner)["pid"], 843744)

    def test_parked_job_is_the_fallback_without_continued_in(self):
        self.register(111240, "9d5254fa", "p1")
        entry = json.loads((self.sessions / "111240.json").read_text())
        entry["parkedJobId"] = "cd89762a"
        (self.sessions / "111240.json").write_text(json.dumps(entry), encoding="utf-8")
        (self.sessions / "843744.json").write_text(
            json.dumps({"pid": 843744, "sessionId": "cd89762a-full", "jobId": "cd89762a"}), encoding="utf-8")
        self.alive = {111240, 843744}
        owner = {"pid": 111240, "proc_start": "p1", "session": "9d5254fa"}
        self.assertEqual(sideagent.resolve_owner(self.sessions, owner)["pid"], 843744)

    def test_cache_skips_unchanged_file(self):
        self.transcript("a", "b")
        self.assertEqual(sideagent.continued_in("a"), "b")
        with mock.patch.object(Path, "open", side_effect=AssertionError("不该重读")):
            self.assertEqual(sideagent.continued_in("a"), "b")


if __name__ == "__main__":
    unittest.main()


class CcaLineageTest(unittest.TestCase):
    def test_watchdog_lineage_extends_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "projects").mkdir()
            side = root / "side"
            side.mkdir()
            (side / "lineage.jsonl").write_text(
                json.dumps({"from": "old", "to": "new", "by": "watchdog"}) + "\n"
                + json.dumps({"from": "same", "to": "same"}) + "\n", encoding="utf-8")
            with mock.patch.object(sideagent, "TRANSCRIPTS_DIR", root / "projects"), \
                    mock.patch.object(sideagent, "SIDEAGENT_DIR", side):
                sideagent._continued_cache.clear()  # noqa: SLF001
                self.assertEqual(sideagent.session_chain("old"), ["old", "new"])
                self.assertEqual(sideagent.session_chain("same"), ["same"])
