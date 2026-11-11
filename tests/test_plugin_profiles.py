"""cca plugin / mcp install --all 列号，以及旧插件键清理：临时 HOME，不跑真 claude。"""

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

from cca import mcp_sideagent, plugin  # noqa: E402


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude").mkdir()
        accounts = self.home / ".claude-accounts"
        # 号：002、ds；不是号：.cache、wait、_xx、001（001 只认 ~/.claude，别算两遍）
        for name in ("002", "ds", ".cache", "wait", "_peer", "001"):
            (accounts / name).mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_all_matches_cca_ls(self):
        from cca.account.model import discover_profiles  # noqa: PLC0415

        expected = discover_profiles()
        self.assertNotIn(".cache", expected)
        self.assertNotIn("wait", expected)
        self.assertEqual(expected.count("001"), 1)
        self.assertEqual(plugin.profiles(["--all"]), expected)
        self.assertEqual(mcp_sideagent._target_profiles(["--all"]), expected)  # noqa: SLF001

    def test_register_is_idempotent_and_drops_legacy(self):
        path = self.home / ".claude-accounts" / "002" / ".claude.json"
        path.write_text(json.dumps({
            "keep": 1,
            "mcpServers": {"cca-sideagents": {"command": "old"}, "other": {"command": "x"}},
        }), encoding="utf-8")
        with mock.patch.object(mcp_sideagent.shutil, "which", return_value="/usr/bin/cca"):
            mcp_sideagent.register("002")
            mcp_sideagent.register("002")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["keep"], 1)
        self.assertEqual(data["mcpServers"]["other"], {"command": "x"})
        self.assertNotIn("cca-sideagents", data["mcpServers"])
        self.assertEqual(data["mcpServers"]["sideagents"],
                         {"command": "/usr/bin/cca", "args": ["mcp", "serve"]})

    def test_drop_legacy_settings_removes_old_keys_only(self):
        path = self.home / ".claude-accounts" / "002" / "settings.json"
        path.write_text(json.dumps({
            "enabledPlugins": {"cca-sideagents-panel@cca-mods": True, "cca-peers-panel@cca-mods": True,
                               "sideagents-panel@mods": True, "other@else": True},
            "extraKnownMarketplaces": {"cca-mods": {"source": "x"}, "mods": {"source": "y"}},
        }), encoding="utf-8")
        self.assertTrue(plugin._drop_legacy_settings("002"))  # noqa: SLF001
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(data["enabledPlugins"]), {"sideagents-panel@mods", "other@else"})
        self.assertEqual(set(data["extraKnownMarketplaces"]), {"mods"})
        # 再跑一次没有可删的：不改文件
        self.assertFalse(plugin._drop_legacy_settings("002"))  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()


class UpdateScopesTest(unittest.TestCase):
    """install 要把每个装过的范围都更新：项目范围的旧版会盖住 user 范围的新版。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".claude-accounts" / "003" / "plugins").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_updates_user_and_project_scopes(self):
        package = f"{plugin.PLUGIN_NAME}@{plugin.PLUGIN_MARKETPLACE}"
        record = {"plugins": {package: [
            {"scope": "user", "version": "0.7.8"},
            {"scope": "project", "projectPath": "/home/ham", "version": "0.6.9"},
        ]}}
        path = self.home / ".claude-accounts" / "003" / "plugins" / "installed_plugins.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        calls = []

        def fake_run(name, args, cwd=None):
            calls.append((args, cwd))
            return True, ""

        with mock.patch.object(plugin, "_run", fake_run), \
                mock.patch.object(plugin, "_drop_legacy", lambda name: None):
            self.assertTrue(plugin.install("003"))
        updates = [(args[3], cwd) for args, cwd in calls if args[:2] == ["plugin", "update"]]
        self.assertEqual(updates, [("user", None), ("project", "/home/ham")])

    def test_unreadable_record_updates_user_only(self):
        package = f"{plugin.PLUGIN_NAME}@{plugin.PLUGIN_MARKETPLACE}"
        self.assertEqual(plugin._installed_scopes("003", package), [("user", None)])
