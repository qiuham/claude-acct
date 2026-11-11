"""cca resume-bg：照 stop-bg --all 记下的清单在原号续上（临时 HOME，不起真 claude）。"""

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

from cca.session import push, takeover

RUNNING = "aaaa1111-0000-0000-0000-000000000000"
PARKED = "bbbb2222-0000-0000-0000-000000000000"


class ResumeBgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        self.env.start()
        push._record_stopped("b005", [  # noqa: SLF001
            {"id": RUNNING[:8], "full": RUNNING, "name": "train", "cwd": "/x", "state": "working"},
            {"id": PARKED[:8], "full": PARKED, "name": "idle", "cwd": "/x", "state": "blocked"},
        ])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def manifest(self) -> dict:
        path = Path(self.tmp.name) / ".cca" / "stopped-bg.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def run_resume(self, *, resume_error=None, registered=False):
        with mock.patch.object(takeover, "registry_entry", return_value={"pid": 1} if registered else {}), \
                mock.patch.object(takeover, "resume_quietly",
                                  side_effect=lambda account, sid, cwd="": (resume_error or {}).get(sid, "")) as wake, \
                mock.patch.object(takeover, "deliver_after_resume", return_value="") as deliver:
            code = push.cmd_resume_bg([])
        return code, wake, deliver

    def test_dry_run_lists_without_touching(self):
        with mock.patch.object(takeover, "resume_quietly") as wake:
            self.assertEqual(push.cmd_resume_bg(["--dry-run"]), 0)
        wake.assert_not_called()
        self.assertEqual(set(self.manifest()), {RUNNING, PARKED})

    def test_running_gets_nudge_parked_only_reattached(self):
        code, wake, deliver = self.run_resume()
        self.assertEqual(code, 0)
        self.assertEqual({c.args[1] for c in wake.call_args_list}, {RUNNING, PARKED})
        self.assertEqual([c.args[1] for c in deliver.call_args_list], [RUNNING])   # 挂着的不跑模型
        self.assertEqual(self.manifest(), {})                                       # 全续上，清单删掉

    def test_failure_stays_in_manifest(self):
        code, _wake, _deliver = self.run_resume(resume_error={PARKED: "起不来"})
        self.assertEqual(code, 1)
        self.assertEqual(set(self.manifest()), {PARKED})

    def test_already_registered_is_skipped(self):
        code, wake, deliver = self.run_resume(registered=True)
        self.assertEqual(code, 0)
        wake.assert_not_called()
        deliver.assert_not_called()
        self.assertEqual(self.manifest(), {})

    def test_record_merges_instead_of_overwriting(self):
        push._record_stopped("ds", [{"id": "cccc3333", "full": "cccc3333-0000-0000-0000-000000000000",  # noqa: SLF001
                                     "name": "x", "cwd": "", "state": "done"}])
        self.assertEqual(len(self.manifest()), 3)


if __name__ == "__main__":
    unittest.main()
