"""跨家族搬家前清掉 tool_use.signature：只动这个字段，thinking 的签名不动。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import transcript


def record(bid: str, blocks: list) -> str:
    return json.dumps({"type": "assistant", "message": {"id": bid, "role": "assistant", "content": blocks}},
                      ensure_ascii=False) + "\n"


class StripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.jsonl"
        self.path.write_text(
            record("m1", [
                {"type": "thinking", "thinking": "想", "signature": "KEEP-think"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}, "signature": "DROP-tool"},
            ])
            + json.dumps({"type": "last-prompt", "lastPrompt": "hi"}) + "\n", encoding="utf-8")

    def test_strips_only_tool_use_signature(self):
        self.assertEqual(transcript.strip_tool_use_signature(self.path), 1)
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        blocks = rows[0]["message"]["content"]
        self.assertEqual(blocks[0]["signature"], "KEEP-think")   # thinking 的留着
        self.assertNotIn("signature", blocks[1])
        self.assertEqual(rows[1]["lastPrompt"], "hi")            # 其他行原样

    def test_second_run_is_noop(self):
        self.assertEqual(transcript.strip_tool_use_signature(self.path), 1)
        before = self.path.read_text(encoding="utf-8")
        self.assertEqual(transcript.strip_tool_use_signature(self.path), 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)   # 没再改写

    def test_backup_kept(self):
        transcript.strip_tool_use_signature(self.path)
        backups = list(self.path.parent.glob("*.bak-signature-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("DROP-tool", backups[0].read_text(encoding="utf-8"))

    def test_missing_or_broken_is_safe(self):
        self.assertEqual(transcript.strip_tool_use_signature(None), 0)
        self.assertEqual(transcript.strip_tool_use_signature(Path(self.tmp.name) / "nope.jsonl"), 0)
        bad = Path(self.tmp.name) / "bad.jsonl"
        bad.write_text('{"message": {"content": [{"type": "tool_use", "signature": "x"}]}\n', encoding="utf-8")
        self.assertEqual(transcript.strip_tool_use_signature(bad), 0)   # 解析不了就不动


class ResumeIdTest(unittest.TestCase):
    def test_reads_all_forms(self):
        self.assertEqual(transcript.resume_id(["--bg", "--resume", "abc"]), "abc")
        self.assertEqual(transcript.resume_id(["--resume=abc"]), "abc")
        self.assertEqual(transcript.resume_id(["-r", "abc"]), "abc")     # README 里的短参数
        self.assertEqual(transcript.resume_id(["-r=abc"]), "abc")
        self.assertEqual(transcript.resume_id(["--resume"]), "")
        self.assertEqual(transcript.resume_id([]), "")


class SafetyTest(unittest.TestCase):
    def test_keeps_original_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text(record("m1", [{"type": "tool_use", "signature": "x"}]), encoding="utf-8")
            path.chmod(0o600)
            transcript.strip_tool_use_signature(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)      # 0600 是 CLI 的约定
            backup = next(path.parent.glob("*.bak-signature-*"))
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)    # 备份也保留

    def test_u2028_inside_json_is_not_a_line_break(self):
        sep = chr(0x2028)               # JSON 字符串里可以合法出现，splitlines() 会误当换行
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            blocks = [{"type": "tool_use", "signature": "x"},
                      {"type": "text", "text": f"a{sep}b"}]
            path.write_text(record("m1", blocks), encoding="utf-8")
            self.assertEqual(transcript.strip_tool_use_signature(path), 1)
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]
            self.assertNotIn("signature", rows[0]["message"]["content"][0])
            self.assertEqual(rows[0]["message"]["content"][1]["text"], f"a{sep}b")

    def test_unwritable_file_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text(record("m1", [{"type": "tool_use", "signature": "x"}]), encoding="utf-8")
            with mock.patch.object(transcript.shutil, "copy2", side_effect=OSError("disk full")):
                self.assertEqual(transcript.strip_tool_use_signature(path), 0)
            self.assertIn("signature", path.read_text(encoding="utf-8"))   # 原文没动


class GatingTest(unittest.TestCase):
    def kind(self, kind: str, marker: dict | None = None):
        with mock.patch("cca.account.store.profile_kind", return_value=kind), \
                mock.patch("cca.account.store.read_marker", return_value=(marker, "")):
            return transcript.target_needs_strip("x")

    def test_subscription_always_strips(self):
        self.assertTrue(self.kind("oauth"))

    def test_gemini_api_keeps_signature(self):
        self.assertFalse(self.kind("api", {"provider": "gemini", "env": {}}))
        self.assertFalse(self.kind("api", {"env": {"ANTHROPIC_MODEL": "gemini-4-argon-high"}}))

    def test_other_api_strips(self):
        self.assertTrue(self.kind("api", {"provider": "codex", "env": {"ANTHROPIC_MODEL": "gpt-6-astra"}}))


if __name__ == "__main__":
    unittest.main()


class StripSessionTest(unittest.TestCase):
    """主记录和 `<id>/subagents/*.jsonl` 一起清。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sid = "aaaa1111-2222-3333-4444-555555555555"
        self.proj = self.root / "proj"          # 共享池根：<池>/<项目目录>/<会话id>.jsonl
        self.project = self.proj / "-mnt-work-src"
        self.main = self.project / f"{self.sid}.jsonl"
        self.main.parent.mkdir(parents=True)
        self.main.write_text(record("m1", [{"type": "tool_use", "signature": "x"}]), encoding="utf-8")
        sub = self.project / self.sid / "subagents" / "agent-1.jsonl"
        sub.parent.mkdir(parents=True)
        sub.write_text(record("m2", [{"type": "tool_use", "signature": "y"}]), encoding="utf-8")

    def test_strips_main_and_sidecars(self):
        self.assertEqual(transcript.strip_session(self.sid, self.proj), 2)
        self.assertNotIn('"signature"', self.main.read_text(encoding="utf-8"))
        sidecar = next((self.project / self.sid / "subagents").glob("*.jsonl"))
        self.assertNotIn('"signature"', sidecar.read_text(encoding="utf-8"))

    def test_short_id_prefix_works_when_unique(self):
        self.assertEqual(transcript.strip_session("aaaa1111", self.proj), 2)

    def test_ambiguous_prefix_is_left_alone(self):
        other = self.project / "aaaa1111-9999-8888-7777-666666666666.jsonl"
        other.write_text(record("m3", [{"type": "tool_use", "signature": "z"}]), encoding="utf-8")
        self.assertEqual(transcript.strip_session("aaaa1111", self.proj), 0)
        self.assertIn('"signature"', other.read_text(encoding="utf-8"))


class SessionLiveTest(unittest.TestCase):
    def test_matches_short_prefix_and_live_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "001" / "sessions").mkdir(parents=True)
            (root / "001" / "sessions" / "1.json").write_text(
                json.dumps({"sessionId": "aaaa1111-2222", "pid": os.getpid()}), encoding="utf-8")
            with mock.patch("cca.paths.accounts_root", return_value=root):
                self.assertTrue(transcript.session_live("aaaa1111"))
                self.assertTrue(transcript.session_live("aaaa1111-2222"))
                self.assertFalse(transcript.session_live("bbbb2222"))

    def test_dead_pid_is_not_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "001" / "sessions").mkdir(parents=True)
            (root / "001" / "sessions" / "1.json").write_text(
                json.dumps({"sessionId": "aaaa1111", "pid": 999999}), encoding="utf-8")
            with mock.patch("cca.paths.accounts_root", return_value=root):
                self.assertFalse(transcript.session_live("aaaa1111"))
