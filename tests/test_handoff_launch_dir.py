"""换号接着跑的目录：家目录不能长期信任，改到工作目录（~/llm）。"""

from __future__ import annotations

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

from cca.account import lifecycle  # noqa: E402


class LaunchDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        os.environ.pop("CCA_WORK_DIR", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_home_goes_to_work_dir(self):
        (self.home / "llm").mkdir()
        self.assertEqual(lifecycle.launch_dir(str(self.home)), str(self.home / "llm"))

    def test_project_dir_unchanged(self):
        (self.home / "llm").mkdir()
        project = self.home / "event-ws"
        project.mkdir()
        self.assertEqual(lifecycle.launch_dir(str(project)), str(project))

    def test_home_kept_when_no_work_dir(self):
        self.assertEqual(lifecycle.launch_dir(str(self.home)), str(self.home))

    def test_work_dir_env_override(self):
        other = self.home / "w"
        other.mkdir()
        with mock.patch.dict(os.environ, {"CCA_WORK_DIR": str(other)}):
            self.assertEqual(lifecycle.launch_dir(str(self.home)), str(other))


if __name__ == "__main__":
    unittest.main()
