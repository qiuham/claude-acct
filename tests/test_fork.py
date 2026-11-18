"""cca fork：从当前会话分出一条独立的后台会话。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录

from cca.session import fork

PARENT = "5a5d353a-3f74-4e47-80ac-74d617c31d70"
BG_OUT = "backgrounded · 1b56879d\n  claude attach 1b56879d    open in this terminal\n"


class ForkTest(unittest.TestCase):
    def run_fork(self, args, *, target="b006", shares=True, taken="", out=BG_OUT, code=0):  # noqa: PLR0913
        ran = []

        def fake_run(argv, account, **_kw):
            ran.append((account, argv))
            return mock.Mock(returncode=code, stdout=out, stderr="")

        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": PARENT}), \
             mock.patch.object(fork, "resolve_source", return_value=(PARENT, "portfolio-base", "/srv/repo")), \
             mock.patch.object(fork, "_name_taken", return_value=taken), \
             mock.patch.object(fork, "pick_account", return_value=target), \
             mock.patch("cca.report._fetch_list_rows", return_value=[]), \
             mock.patch("cca.account.model.discover_profiles", return_value=[]), \
             mock.patch("cca.account.model.require_profile", side_effect=lambda name, **_k: name), \
             mock.patch("cca.session.push.shares_sessions", return_value=shares), \
             mock.patch("cca.session.push._run_claude", side_effect=fake_run), \
             mock.patch("cca.api.client.api_launch_args", side_effect=lambda _a, argv: argv):
            rc = fork.cmd_fork(args)
        return rc, ran

    def test_forks_current_session_with_name_and_identity_header(self):
        rc, ran = self.run_fork(["planner-export", "实现规划器输出"])
        self.assertEqual(rc, 0)
        account, argv = ran[0]
        self.assertEqual(account, "b006")
        self.assertEqual(argv[:6], ["--bg", "--resume", PARENT, "--fork-session", "-n", "planner-export"])
        self.assertIn("「portfolio-base」", argv[-1])
        self.assertIn('sideagent_send {session: "portfolio-base"}', argv[-1])
        self.assertTrue(argv[-1].endswith("实现规划器输出"))

    def test_space_separated_to_and_fresh(self):
        _rc, ran = self.run_fork(["x", "--to", "gpt", "--fresh", "活"])
        account, argv = ran[0]
        self.assertEqual(account, "gpt")
        self.assertNotIn("--fork-session", argv)
        self.assertNotIn("上面是分出来之前的完整对话", argv[-1])

    def test_refuses_taken_name_isolated_target_and_failed_start(self):
        for kwargs, args in (({"taken": "b001/aaaa1111"}, ["x", "活"]),
                             ({"shares": False}, ["x", "--to", "gpt-iso", "活"]),
                             ({"out": "Workspace not trusted", "code": 1}, ["x", "活"]),
                             ({"target": ""}, ["x", "活"])):
            with self.assertRaises(SystemExit):
                self.run_fork(args, **kwargs)

    def test_message_may_start_with_dash(self):
        name, message, flags = fork.parse_args(["x", "--to", "gpt", "- 目标：实现输出"])
        self.assertEqual((name, message, flags), ("x", "- 目标：实现输出", {"--to": "gpt"}))
        self.assertEqual(fork.parse_args(["--fresh", "x", "--from=abc", "活"])[2], {"--fresh": True, "--from": "abc"})
        with self.assertRaises(SystemExit):
            fork.parse_args(["x", "活", "多余"])

    def test_child_starts_in_the_source_session_cwd(self):
        seen = {}
        with mock.patch.object(fork, "resolve_source", return_value=(PARENT, "p", "/srv/repo")), \
             mock.patch.object(fork, "_name_taken", return_value=""), \
             mock.patch.object(fork, "pick_account", return_value="b006"), \
             mock.patch("cca.report._fetch_list_rows", return_value=[]), \
             mock.patch("cca.account.model.discover_profiles", return_value=[]), \
             mock.patch("cca.session.push.shares_sessions", return_value=True), \
             mock.patch("cca.session.push._run_claude",
                        side_effect=lambda _a, _n, **kw: seen.update(kw) or mock.Mock(returncode=0, stdout=BG_OUT, stderr="")), \
             mock.patch("cca.api.client.api_launch_args", side_effect=lambda _a, argv: argv):
            fork.cmd_fork(["x", "活"])
        self.assertEqual(seen["cwd"], "/srv/repo")

    def test_from_accepts_a_session_name(self):
        rows = [{"session_id": PARENT, "id": PARENT[:8], "name": "portfolio-base", "account": "b004", "running": True}]
        with mock.patch("cca.session.attach.candidates", return_value=rows):
            self.assertEqual(fork.session_id_for("portfolio-base"), PARENT)
            self.assertEqual(fork.session_id_for("deadbeef"), "deadbeef")   # 找不到名字按编号处理

    def test_terminal_without_from_says_how(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}), self.assertRaises(SystemExit):
            fork.resolve_source("")

    def test_message_from_stdin(self):
        with mock.patch.object(sys, "stdin", mock.Mock(read=lambda: "很长的交办")):
            _rc, ran = self.run_fork(["x", "-"])
        self.assertTrue(ran[0][1][-1].endswith("很长的交办"))


if __name__ == "__main__":
    unittest.main()
