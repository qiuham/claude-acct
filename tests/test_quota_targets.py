"""quota-hint --json 里的 targets：/cca handoff 挑选框用，和自动换号同一套排序。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录

from cca.session import quota_hint


def row(name: str, five: int, seven: int, *, kind: str = "oauth") -> dict:
    later = time.time() + 3600
    return {"profile": name, "kind": kind, "logged_in": True,
            "usage": {"windows": {"five_hour": {"percent": five, "resets_at": later},
                                  "seven_day": {"percent": seven, "resets_at": later + 86400}}}}


class TargetChoicesTest(unittest.TestCase):
    def test_ranked_like_auto_and_marks_the_auto_pick(self):
        rows = [row("b001", 99, 99), row("b006", 3, 0), row("001", 40, 90), row("ds", 0, 0, kind="api"),
                row("b002", 99, 99)]
        got = quota_hint.target_choices(rows, "b001", shares=lambda _name: True)
        self.assertEqual([one["profile"] for one in got], ["b006", "001"])   # 本号、API 号、到阈值的都不列
        self.assertEqual(got[0], {"profile": "b006", "five_hour": 3, "seven_day": 0, "roomy": True, "auto": True})
        self.assertEqual((got[1]["roomy"], got[1]["auto"]), (False, False))

    def test_isolated_accounts_are_not_choices(self):
        rows = [row("b001", 99, 99), row("b006", 3, 0)]
        self.assertEqual(quota_hint.target_choices(rows, "b001", shares=lambda name: name != "b006"), [])


if __name__ == "__main__":
    unittest.main()
