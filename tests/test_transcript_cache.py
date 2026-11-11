"""会话记录扫描缓存：文件没变不重扫，变了重扫；进程退出写回，文件没了的条目删掉。"""

from __future__ import annotations

import json
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

from cca.session import push  # noqa: E402


class TranscriptCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache = root / "cache.json"
        self.path = root / "s.jsonl"
        self.write("第一个", "m-1")
        for patch in (mock.patch.object(push, "transcript_cache_file", return_value=self.cache),
                      mock.patch.object(push, "_transcript_cache", None),
                      mock.patch.object(push, "_transcript_cache_dirty", new=False),
                      mock.patch.object(push.atexit, "register")):
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, title, model):
        lines = [{"type": "custom-title", "customTitle": title, "cwd": "/w"},
                 {"type": "assistant", "message": {"model": model}}]
        self.path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

    def test_unchanged_file_is_not_rescanned(self):
        self.assertEqual(push._scan_transcript(self.path)["title"], "第一个")  # noqa: SLF001
        self.assertEqual(push.last_session_model(self.path), "m-1")
        with mock.patch.object(push, "_scan_transcript_uncached") as scan, \
                mock.patch.object(push, "_last_session_model_uncached") as model:
            self.assertEqual(push._scan_transcript(self.path)["title"], "第一个")  # noqa: SLF001
            self.assertEqual(push.last_session_model(self.path), "m-1")
        scan.assert_not_called()
        model.assert_not_called()

    def test_changed_file_is_rescanned(self):
        push._scan_transcript(self.path)  # noqa: SLF001
        self.write("第二个，长一点", "m-2")
        os.utime(self.path, ns=(1, 2))
        self.assertEqual(push._scan_transcript(self.path)["title"], "第二个，长一点")  # noqa: SLF001
        self.assertEqual(push.last_session_model(self.path), "m-2")

    def test_saved_and_reloaded_and_gone_files_dropped(self):
        push._scan_transcript(self.path)  # noqa: SLF001
        gone = self.path.with_name("gone.jsonl")
        gone.write_text("{}\n", encoding="utf-8")
        push._scan_transcript(gone)  # noqa: SLF001
        gone.unlink()
        push._save_transcript_cache()  # noqa: SLF001
        saved = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertEqual(list(saved), [str(self.path)])
        with mock.patch.object(push, "_transcript_cache", None), \
                mock.patch.object(push, "_scan_transcript_uncached") as scan:
            self.assertEqual(push._scan_transcript(self.path)["title"], "第一个")  # noqa: SLF001
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
