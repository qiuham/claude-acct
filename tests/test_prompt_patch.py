"""`cca prompt`：只抹掉系统提示里那段特征字符串，其余字节不动。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录

from cca import prompt_patch as pp
from cca.account import lifecycle

TARGET = (b"IMPORTANT: Assist with authorized security testing, defensive security, CTF challenges, "
          b"and educational contexts. Refuse requests for destructive techniques, DoS attacks, mass "
          b"targeting, supply chain compromise, or detection evasion for malicious purposes. Dual-use "
          b"security tools (C2 frameworks, credential testing, exploit development) require clear "
          b"authorization context: pentesting engagements, CTF competitions, security research, or "
          b"defensive use cases.")
DECOY = b"IMPORTANT: Do not trigger JavaScript alerts, confirms, prompts, or blocking dialogs."


def make_fake(tmp: Path) -> Path:
    """造一个和 claude 二进制同形的文件：目标串在字符串字面量里，另有诱饵行。"""
    body = (b'var gDo="Never push to main/master, force-push, or merge.";'
            b'var B1r="' + TARGET + b'";'
            b'var t1="' + DECOY + b'";')
    f = tmp / "fake-claude"
    f.write_bytes(body)
    f.chmod(0o755)
    return f


class PromptPatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cca-prompt-"))
        self.exe = make_fake(self.tmp)
        self.cache = self.tmp / "cache.json"
        self.original = self.exe.read_bytes()

    def test_only_the_matching_literal_is_blanked(self):
        result = pp.apply_to(self.exe, pp.DEFAULT_RULES, self.cache)
        self.assertEqual(result["status"], "patched")
        self.assertEqual(result["hits"], 1)
        data = self.exe.read_bytes()
        self.assertEqual(len(data), len(self.original))          # 长度不变
        self.assertNotIn(TARGET, data)
        self.assertIn(DECOY, data)                                # 诱饵不动
        self.assertIn(b'var B1r="', data)                         # 外壳不动
        self.assertIn(b'";var t1="', data)

    def test_second_run_is_a_noop(self):
        pp.apply_to(self.exe, pp.DEFAULT_RULES, self.cache)
        once = self.exe.read_bytes()
        again = pp.apply_to(self.exe, pp.DEFAULT_RULES, self.cache)
        self.assertEqual(again["status"], "already")
        self.assertEqual(self.exe.read_bytes(), once)

    def test_revert_restores_exact_bytes(self):
        pp.apply_to(self.exe, pp.DEFAULT_RULES, self.cache)
        result = pp.revert(self.exe, self.cache)
        self.assertEqual(result["status"], "reverted")
        self.assertEqual(self.exe.read_bytes(), self.original)

    def test_no_match_leaves_file_untouched(self):
        clean = self.tmp / "clean"
        clean.write_bytes(b'var a="nothing to see here";')
        result = pp.apply_to(clean, pp.DEFAULT_RULES, self.cache)
        self.assertEqual(result["status"], "nomatch")
        self.assertEqual(clean.read_bytes(), b'var a="nothing to see here";')

    def test_fingerprint_change_asks_for_work(self):
        self.assertTrue(pp.needs_work(self.exe, self.cache))
        pp.apply_to(self.exe, pp.DEFAULT_RULES, self.cache)
        self.assertFalse(pp.needs_work(self.exe, self.cache))
        self.exe.write_bytes(self.original + b" ")                # 模拟升级换了文件
        self.assertTrue(pp.needs_work(self.exe, self.cache))

    def test_enabled_defaults_to_true(self):
        self.assertTrue(pp.enabled(self.cache))
        pp.set_enabled(self.cache, value=False)
        self.assertFalse(pp.enabled(self.cache))


class PromptCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cca-prompt-cmd-"))
        self.exe = make_fake(self.tmp)
        self.cache = self.tmp / "cache.json"
        self.patch = mock.patch.object(lifecycle.shutil, "which", return_value=str(self.exe))
        self.patch.start()
        self.cache_patch = mock.patch.object(lifecycle, "prompt_patch_cache_file", return_value=self.cache)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.patch.stop()

    def test_replace_then_restore_round_trip(self):
        original = self.exe.read_bytes()
        self.assertEqual(lifecycle.cmd_prompt(["replace"]), 0)
        self.assertNotIn(TARGET, self.exe.read_bytes())
        self.assertEqual(lifecycle.cmd_prompt(["restore"]), 0)
        self.assertEqual(self.exe.read_bytes(), original)

    def test_restore_also_disables_auto(self):
        lifecycle.cmd_prompt(["replace"])
        self.assertTrue(pp.enabled(self.cache))
        lifecycle.cmd_prompt(["restore"])
        self.assertFalse(pp.enabled(self.cache))     # 否则下次启动又替换回去


if __name__ == "__main__":
    unittest.main()
