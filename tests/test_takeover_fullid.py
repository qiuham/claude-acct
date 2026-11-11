"""接管时必须用完整会话编号：只给前 8 位，claude --resume 会复制出新会话。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import takeover  # noqa: E402


class FullIdTest(unittest.TestCase):
    def test_short_id_is_refused_without_running_claude(self):
        row = {"session_id": "e20732a0", "kind": "background", "account": "003", "cwd": ""}
        with mock.patch.object(takeover.subprocess, "run") as run:
            error = takeover._wake(row)  # noqa: SLF001
        self.assertIn("不完整", error)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
