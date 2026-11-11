"""接管会话的确认转发钩子：临时目录，不跑真 claude。"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(REPO / "tests"))

from sidehelp import use_temp_root, write_roster, write_state  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import permission, roster, sideagent  # noqa: E402

SESSION = "6b6dd1ed-90aa-433f-b4ce-95584e83d015"
NAME = "ds2#bg-6b6dd1ed"
# 实测抓到的 PermissionRequest 输入（2026-11-02，后台会话，权限模式 default）；路径只是字符串
TEST_DIR = "/tmp/cca-peer-test"  # noqa: S108
HOOK_INPUT = json.dumps({
    "session_id": SESSION, "cwd": TEST_DIR, "permission_mode": "default",
    "hook_event_name": "PermissionRequest", "tool_name": "Bash",
    "tool_input": {"command": f"echo A > {TEST_DIR}/a.txt", "description": "Write A to a.txt test file"},
    "permission_suggestions": [{"type": "addDirectories", "directories": [TEST_DIR],
                                "destination": "session"}],
})


class HookTest(unittest.TestCase):
    def setUp(self):
        root = use_temp_root(self)
        # 主人在不在：名单里写的进程号这里都是假的，按 alive 集合认
        self.alive = {"master-1", "master-2"}
        self.patches = [mock.patch.object(permission, "PERM_DIR", root / "perm"),
                        mock.patch.object(permission, "POLL_SECONDS", 0.02),
                        mock.patch.object(roster, "owner_alive",
                                          side_effect=lambda data: data.get("session") in self.alive)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.root = root

    def _take_over(self, owner="master-1"):
        # 格式改了：接管记录在 sideagents/<名字>/state.json（kind=attached），主人记在 sessions/<主人>.json
        write_state(NAME, kind="attached", session_id=SESSION)
        write_roster(owner, {NAME: "attached"})

    def _run_hook(self, wait=2.0):
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("text", permission.hook(HOOK_INPUT, wait_seconds=wait)))
        thread.start()
        return thread, out

    def _wait_request(self):
        for _ in range(200):
            rows = permission.pending(NAME)
            if rows:
                return rows[0]
            time.sleep(0.01)
        self.fail("钩子没写出待确认请求")
        return {}

    def test_not_taken_over_stays_silent(self):
        # 没被接管：不表态，Claude Code 照常弹原生确认框
        self.assertEqual(permission.hook(HOOK_INPUT, wait_seconds=1), "")

    def test_bad_input_stays_silent(self):
        self.assertEqual(permission.hook("not json", wait_seconds=1), "")

    def test_allow_from_panel(self):
        self._take_over()
        thread, out = self._run_hook()
        request = self._wait_request()
        self.assertEqual(request["summary"], "Bash(echo A > /tmp/cca-peer-test/a.txt)")
        self.assertEqual(permission.answer(NAME, "allow"), "")
        thread.join(5)
        decision = json.loads(out["text"])["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PermissionRequest")
        self.assertEqual(decision["decision"], {"behavior": "allow"})
        # 用完就清掉，不留请求文件
        self.assertEqual(permission.pending(NAME), [])
        self.assertEqual(list((self.root / "perm" / NAME).iterdir()), [])

    def test_deny_from_panel(self):
        self._take_over()
        thread, out = self._run_hook()
        self._wait_request()
        permission.answer(NAME, "deny")
        thread.join(5)
        decision = json.loads(out["text"])["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("拒绝", decision["message"])

    def test_timeout_denies_with_reason(self):
        # 没人点：我们自己到点回 deny（Claude Code 杀钩子会退回原生确认框，所以不能靠它的超时）
        self._take_over()
        text = permission.hook(HOOK_INPUT, wait_seconds=0.2)
        decision = json.loads(text)["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("没人确认", decision["message"])

    def test_detach_while_waiting_falls_back(self):
        self._take_over()
        thread, out = self._run_hook()
        self._wait_request()
        # 所有主人都放手了：state.json 写了 finished_at
        sideagent.mark_finished(NAME)
        thread.join(5)
        self.assertEqual(out["text"], "")

    def test_answer_without_request(self):
        self.assertIn("没有等确认", permission.answer(NAME, "allow"))
        self.assertIn("allow 或 deny", permission.answer(NAME, "yes"))

    def test_hook_timeout_longer_than_own_wait(self):
        self.assertGreater(permission.HOOK_TIMEOUT, permission.WAIT_SECONDS)


    def test_two_owners_one_releases_still_taken(self):
        # M 对 N：两个主会话都接管了它，一个放手，请求照样转到面板（另一个还在）
        self._take_over("master-1")
        write_roster("master-2", {NAME: "attached"})
        self.assertEqual(permission._taken_over_name(SESSION), NAME)  # noqa: SLF001
        roster.remove("master-1", NAME)
        self.assertEqual(permission._taken_over_name(SESSION), NAME)  # noqa: SLF001
        roster.remove("master-2", NAME)
        self.assertEqual(permission._taken_over_name(SESSION), "")  # noqa: SLF001

    def test_dead_owner_does_not_count(self):
        # 主人不在了（进程死了）的名单不算：没人能点，照常弹原生确认框
        self._take_over("master-dead")
        self.assertEqual(permission.hook(HOOK_INPUT, wait_seconds=1), "")

    def test_first_answer_wins(self):
        # 好几个面板都能答：谁先答算谁的。两个面板几乎同时读到同一条请求，后答的被告知已经答过
        folder = self.root / "perm" / NAME
        folder.mkdir(parents=True)
        (folder / "r1.req.json").write_text(json.dumps({"id": "r1", "created": 1.0}), encoding="utf-8")
        seen = permission.pending(NAME)
        with mock.patch.object(permission, "pending", return_value=seen):
            self.assertEqual(permission.answer(NAME, "allow", by="master-1"), "")
            self.assertIn("已经有人答过", permission.answer(NAME, "deny", by="master-2"))
        result = json.loads((folder / "r1.res.json").read_text(encoding="utf-8"))
        self.assertEqual((result["behavior"], result["by"]), ("allow", "master-1"))
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["r1.req.json", "r1.res.json"])


class CliAndSyncTest(HookTest):
    def test_install_needs_explicit_target(self):
        # 先载入 sideagent，sideagent_cli 才不会循环导入（和其他测试一样从 sideagent 拿）
        from cca.session import sideagent  # noqa: PLC0415

        sideagent_cli = sideagent.sys.modules["cca.session.sideagent_cli"]

        # 装钩子影响这个号上每个会话：不写 --profile/--all 就不装
        with self.assertRaises(SystemExit):
            sideagent_cli._hook_command(["install"])  # noqa: SLF001

    def test_sync_exposes_first_pending(self):
        from cca.session import takeover  # noqa: PLC0415

        self._take_over()
        self.assertIsNone(takeover._first_pending(NAME))  # noqa: SLF001
        thread, _out = self._run_hook()
        self._wait_request()
        shown = takeover._first_pending(NAME)  # noqa: SLF001
        self.assertEqual((shown["tool"], shown["count"]), ("Bash", 1))
        permission.answer(NAME, "deny")
        thread.join(5)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/local/bin/other-hook"}]}
        self.path.write_text(json.dumps({"model": "x", "hooks": {"PermissionRequest": [self.other],
                                                                  "Stop": [{"hooks": []}]}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _groups(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["hooks"]["PermissionRequest"]

    def test_install_merges_and_is_idempotent(self):
        command = "/home/x/.local/bin/cca sideagent permission-hook"
        self.assertTrue(permission.install_into(self.path, command))
        groups = self._groups()
        self.assertEqual(groups[0], self.other)
        self.assertEqual(groups[1]["hooks"][0]["timeout"], permission.HOOK_TIMEOUT)
        self.assertFalse(permission.install_into(self.path, command))
        self.assertEqual(len(self._groups()), 2)
        # 先备份
        self.assertTrue(list(self.path.parent.glob("settings.json.bak-permission-hook-*")))

    def test_uninstall_keeps_other_hooks(self):
        permission.install_into(self.path, "cca sideagent permission-hook")
        self.assertTrue(permission.uninstall_from(self.path))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"]["PermissionRequest"], [self.other])
        self.assertIn("Stop", data["hooks"])
        self.assertFalse(permission.uninstall_from(self.path))


if __name__ == "__main__":
    unittest.main()
