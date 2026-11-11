"""接管会话的模型：打断、出错时的合成消息（<synthetic>）不算真用的模型。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import takeover  # noqa: E402


def assistant(model: str) -> str:
    return json.dumps({"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": "x"}]}})


class ScanModelTest(unittest.TestCase):
    def test_synthetic_keeps_real_model(self):
        _events, _notes, acc = takeover._scan([assistant("deepseek-flash"), assistant("<synthetic>")], {})  # noqa: SLF001
        self.assertEqual(acc["model"], "deepseek-flash")

    def test_stored_synthetic_is_dropped(self):
        _events, _notes, acc = takeover._scan([], {"model": "<synthetic>"})  # noqa: SLF001
        self.assertEqual(acc["model"], "")


if __name__ == "__main__":
    unittest.main()


class CardTest(unittest.TestCase):
    def test_card_has_recap_ask_and_last(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects" / "-home-ham"
            projects.mkdir(parents=True)
            lines = [
                {"type": "user", "message": {"role": "user", "content": "把 152.152 加进集群"}},
                {"type": "user", "message": {"role": "user", "content": "<command-name>/reload-plugins</command-name>"}},
                {"type": "system", "subtype": "away_summary", "content": "在加节点，卡在 Cilium 镜像"},
                {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "镜像拉不下来"}]}},
            ]
            (projects / "abc12345-full.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            with mock.patch.object(takeover, "PROJECTS_DIR", Path(tmp) / "projects"):
                text = takeover.card("abc12345-full", account="002", title="ham-b3", state="idle")
        self.assertIn("接管了 002/abc12345「ham-b3」（idle）", text)
        self.assertIn("回顾：在加节点，卡在 Cilium 镜像", text)
        self.assertIn("  问：把 152.152 加进集群", text)
        self.assertIn("  答：镜像拉不下来", text)


class StoppedHiddenTest(unittest.TestCase):
    def test_picker_skips_stopped(self):
        from unittest import mock
        rows = [{"account": "ds", "id": "a", "session_id": "a-full", "name": "x", "kind": "background", "cwd": "", "state": "stopped", "running": False},
                {"account": "ds", "id": "b", "session_id": "b-full", "name": "y", "kind": "background", "cwd": "", "state": "done", "running": False},
                {"account": "ds", "id": "c", "session_id": "c-full", "name": "z", "kind": "background", "cwd": "", "state": "failed", "running": False}]
        # 名单和 sideagent 目录换到空的临时目录：不读真实的 ~/.cca
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(takeover.sideagent, "SIDEAGENT_DIR", Path(tmp) / "sideagents"), \
                mock.patch.object(takeover, "candidates", return_value=rows), \
                mock.patch.object(takeover, "last_line", return_value=""), mock.patch.object(takeover, "recap", return_value=""):
            got = takeover.picker_rows(include_running=True, owner_session="me")
        self.assertEqual([row["id"] for row in got], ["b"])


class PickerOnePerSessionTest(unittest.TestCase):
    """挑选框：同一个会话只列一行（在跑的优先），带上人最后交给它的话。"""

    def test_dedupe_prefers_running_and_carries_ask(self):
        from unittest import mock
        rows = [{"account": "003", "id": "a", "session_id": "a-full", "name": "任务", "kind": "background", "cwd": "", "state": "done", "running": False},
                {"account": "005", "id": "a", "session_id": "a-full", "name": "任务", "kind": "interactive", "cwd": "", "state": "busy", "running": True},
                {"account": "ds", "id": "b", "session_id": "b-full", "name": "别的", "kind": "background", "cwd": "", "state": "idle", "running": False},
                # 派出的 sideagent 自己的会话：不列
                {"account": "ds", "id": "c", "session_id": "c-full", "name": "派出的", "kind": "interactive", "cwd": "", "state": "busy", "running": True}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(takeover.sideagent, "SIDEAGENT_DIR", Path(tmp) / "sideagents"), \
                mock.patch.object(takeover, "candidates", return_value=rows), \
                mock.patch.object(takeover, "_process_account", return_value=False), \
                mock.patch.object(takeover, "_spawned_sessions", return_value={"c-full"}), \
                mock.patch.object(takeover, "last_line", return_value=""), \
                mock.patch.object(takeover, "last_ask", side_effect=lambda sid: "交代给" + sid):
            got = takeover.picker_rows(include_running=True, owner_session="me")
        self.assertEqual(sorted((row["id"], row["account"]) for row in got), [("a", "005"), ("b", "ds")])
        self.assertEqual({row["id"]: row["ask"] for row in got}, {"a": "交代给a-full", "b": "交代给b-full"})

    def test_same_state_prefers_the_process_account(self):
        from unittest import mock
        rows = [{"account": "ds", "id": "a", "session_id": "a-full", "state": "busy"},
                {"account": "005", "id": "a", "session_id": "a-full", "state": "busy"}]
        with mock.patch.object(takeover, "_process_account", side_effect=lambda row: row["account"] == "005"):
            self.assertEqual([row["account"] for row in takeover.one_per_session(rows)], ["005"])
