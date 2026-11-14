"""`cca <号> --bg -r <编号> "<提示>"`：有名字的会话保住原名，提示续上后再送。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录

from cca import cli

FULL = "8ae9ae91-83df-4075-a8b4-10b9c02f66bd"


class TrailingPromptTest(unittest.TestCase):
    def test_prompt_after_bool_flag_or_value(self):
        self.assertEqual(cli._trailing_prompt(["-r", FULL, "--bg", "做事"]), "做事")  # noqa: SLF001
        self.assertEqual(cli._trailing_prompt(["--bg", "-r", FULL, "做事"]), "做事")  # noqa: SLF001
        self.assertEqual(cli._trailing_prompt(["--bg", "--model", "opus", "做事"]), "做事")  # noqa: SLF001

    def test_value_of_an_option_is_not_a_prompt(self):
        self.assertEqual(cli._trailing_prompt(["--bg", "-r", FULL]), "")  # noqa: SLF001
        self.assertEqual(cli._trailing_prompt(["--bg", "--name", "x"]), "")  # noqa: SLF001
        self.assertEqual(cli._trailing_prompt(["--bg", "--resume"]), "")  # noqa: SLF001


class KeepNameTest(unittest.TestCase):
    def run_keep(self, args, *, name="closed-loop-engine", found=True):
        path = Path(f"/x/{FULL}.jsonl") if found else None
        launch = ([f"/rename {name}"], "做事") if name else (["做事"], "")
        with mock.patch("cca.session.transcript.find_transcript", return_value=path), \
             mock.patch("cca.session.attach.resume_launch", return_value=launch):
            return cli._keep_name_on_bg_resume("b009", args)  # noqa: SLF001

    def test_named_bg_resume_swaps_prompt_for_rename(self):
        args, pending = self.run_keep(["-r", FULL[:8], "--bg", "做事"])
        self.assertEqual(args, ["-r", FULL[:8], "--bg", "/rename closed-loop-engine"])
        self.assertEqual(pending, (FULL, "做事"))

    def test_unnamed_or_foreground_or_no_prompt_untouched(self):
        for args, kw in ((["-r", FULL, "--bg", "做事"], {"name": ""}),
                         (["-r", FULL, "做事"], {}),
                         (["--bg", "-r", FULL], {}),
                         (["-r", FULL, "--bg", "做事"], {"found": False}),
                         (["-r", FULL, "--bg", "/rename x"], {})):
            self.assertEqual(self.run_keep(args, **kw), (args, ()))


class DeliverTest(unittest.TestCase):
    def test_delivers_after_successful_start(self):
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=0)), \
             mock.patch("cca.session.attach.deliver_after_resume", return_value="") as deliver:
            rc = cli._bg_resume_then_deliver("b009", "claude", ["--bg"], {}, (FULL, "做事"))  # noqa: SLF001
        self.assertEqual(rc, 0)
        deliver.assert_called_once_with("b009", FULL, "做事")

    def test_failed_start_does_not_deliver(self):
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=3)), \
             mock.patch("cca.session.attach.deliver_after_resume") as deliver:
            self.assertEqual(cli._bg_resume_then_deliver("b009", "claude", [], {}, (FULL, "x")), 3)  # noqa: SLF001
        deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
