"""收件箱：一件活结束只通知一次；推不到就留着，面板来取；认领的人死了自动放回。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录

from cca.session import inbox, sideagent  # noqa: E402


class InboxCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in ((inbox, Path(self.tmp.name) / "inbox"), (sideagent, Path(self.tmp.name) / "agents")):
            attr = "INBOX_DIR" if target is inbox else "SIDEAGENT_DIR"
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def finalize(self, job="j1", **kw):
        return inbox.finalize("ds#a", job, session="s1", status=kw.pop("status", "completed"),
                              notice={"text": kw.pop("text", "做完了")}, **kw)


class PostTest(InboxCase):
    def test_same_job_only_once(self):
        first = self.finalize()
        self.assertIsNotNone(first)
        self.assertIsNone(self.finalize())
        # 送完删掉以后，再来一次也不会再存（任务记录里记着通知过）
        first.unlink()
        self.assertIsNone(self.finalize(status="failed"))
        self.assertEqual(inbox.pending(["s1"]), [])

    def test_different_jobs_each_notify(self):
        self.finalize("j1")
        self.finalize("j2")
        self.assertEqual(len(inbox.pending(["s1"])), 2)

    def test_settle_marks_running_job_finished(self):
        sideagent.write_job("ds#a", {"id": "j1", "state": "running"})
        self.finalize(status="killed", settle=True)
        job = sideagent.jobs_of(json.loads(sideagent.job_path("ds#a").read_text(encoding="utf-8")))[0]
        self.assertEqual(job["state"], "stopped")
        self.assertTrue(job["noticed"])

    def test_without_settle_leaves_state_to_the_caller(self):
        sideagent.write_job("ds#a", {"id": "j1", "state": "running"})
        self.finalize()
        job = sideagent.jobs_of(json.loads(sideagent.job_path("ds#a").read_text(encoding="utf-8")))[0]
        self.assertEqual(job["state"], "running")

    def test_no_notify_records_but_posts_nothing(self):
        self.assertIsNone(self.finalize(notify=False))
        self.assertEqual(inbox.pending(["s1"]), [])


class ClaimTest(InboxCase):
    def test_only_one_taker(self):
        path = self.finalize()
        first = inbox.claim(path, 111)
        self.assertIsNotNone(first)
        self.assertIsNone(inbox.claim(path, 222))
        inbox.done(first)
        self.assertEqual(inbox.pending(["s1"]), [])

    def test_release_puts_it_back(self):
        claimed = inbox.claim(self.finalize(), 111)
        inbox.release(claimed)
        self.assertEqual(len(inbox.pending(["s1"])), 1)

    def test_dead_taker_claim_returns(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        inbox.claim(self.finalize(), proc.pid)
        # 认领它的进程已经没了：下次来取时放回待送
        taken = inbox.take(["s1"], 999999)
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0]["notice"]["text"], "做完了")

    def test_live_taker_claim_stays(self):
        inbox.claim(self.finalize(), os.getpid())
        self.assertEqual(inbox.take(["s1"], 1), [])


class PushTest(InboxCase):
    def test_push_delivers_and_deletes(self):
        path = self.finalize()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        sock_path = str(Path(self.tmp.name) / "main.sock")
        server.bind(sock_path)
        server.listen(1)
        self.assertTrue(inbox.push(path, sock_path))
        conn, _ = server.accept()
        frame = json.loads(conn.recv(65536).split(b"\n")[0])
        conn.close()
        self.assertIn("做完了", frame["message"]["content"])
        self.assertEqual(inbox.pending(["s1"]), [])

    def test_push_fails_keeps_it(self):
        path = self.finalize()
        self.assertFalse(inbox.push(path, str(Path(self.tmp.name) / "没有.sock")))
        self.assertEqual(len(inbox.pending(["s1"])), 1)


class NotificationTextTest(unittest.TestCase):
    def test_statuses(self):
        self.assertIn("was stopped", inbox.notification_text("ds#a", "查", status="killed"))
        self.assertIn("was interrupted", inbox.notification_text("ds#a", "查", status="interrupted"))
        self.assertIn("failed: 卡住", inbox.notification_text("ds#a", "查", status="failed", error="卡住"))
        self.assertIn("&lt;py&gt;", inbox.notification_text("ds#a", "查", status="completed", text="<py>"))


if __name__ == "__main__":
    unittest.main()
