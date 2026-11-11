"""测试隔离：整套测试跑在临时 HOME 里，不会写到真实的 ~/.claude-accounts / ~/.cca / ~/.claude。"""

from __future__ import annotations

import os
import pwd
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402

from cca import paths  # noqa: E402
from cca.session import watchdog  # noqa: E402


class IsolationTest(unittest.TestCase):
    def test_home_and_accounts_root_are_temporary(self):
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        for path in (paths.home(), paths.accounts_root(), paths.state_dir(), paths.shared_dir()):
            self.assertTrue(path.resolve().is_relative_to(isolate.TEST_HOME.resolve()), path)
            self.assertFalse(path.resolve().is_relative_to(real_home) and not
                             isolate.TEST_HOME.resolve().is_relative_to(real_home), path)
        self.assertTrue(paths.accounts_root().name == ".claude-accounts")

    def test_environment_does_not_point_at_a_real_session(self):
        for key in isolate.LEAKY_ENV:
            self.assertNotIn(key, os.environ)
        self.assertTrue(Path(os.environ["CLAUDE_CONFIG_DIR"]).is_relative_to(isolate.TEST_HOME))
        self.assertEqual(os.environ.get("CCA_WATCHDOG_AUTOSTART"), "0")

    def test_every_test_module_imports_isolate_before_cca(self):
        """新加的测试文件忘了 import isolate，就是把真实状态暴露给它：这里拦下来。"""
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            lines = path.read_text(encoding="utf-8").splitlines()
            first_cca = next((i for i, line in enumerate(lines) if line.startswith(("from cca", "import cca"))), None)
            marker = next((i for i, line in enumerate(lines) if line.startswith("import isolate")), None)
            self.assertIsNotNone(marker, f"{path.name} 没有 import isolate")
            if first_cca is not None:
                self.assertLess(marker, first_cca, f"{path.name}: import isolate 要在第一个 cca 导入之前")

    def test_a_move_request_lands_in_the_temporary_state_only(self):
        """没 mock 的 request_move（真实事故的写入点）也只落在临时目录，会话编号取自环境变量时尤其如此。"""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "isolation-probe-session"
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        watchdog.request_move("b001", "b002", os.environ["CLAUDE_CODE_SESSION_ID"])
        state = watchdog._state_path()  # noqa: SLF001
        self.assertTrue(state.is_relative_to(isolate.TEST_HOME))
        self.assertIn("isolation-probe-session", watchdog.read_intents()["moves"])
        state.unlink()


if __name__ == "__main__":
    unittest.main()
