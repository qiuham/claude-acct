"""handoff 的 JSON 后端：候选清单与 --json 搬运（全部用桩，不跑真 claude）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.account import lifecycle  # noqa: E402
from cca.session import push  # noqa: E402


def session(short, *, kind="background", state="done", name="job", cwd="/proj/x",  # noqa: PLR0913 - 测试造数据
            full=None, pid=None):
    """构造一条 `claude agents --json --all` 的已归一化记录。"""
    return {
        "id": short,
        "full": full or f"{short}-0000-0000-0000-000000000000",
        "job": short,
        "kind": kind,
        "pid": pid,
        "name": name,
        "cwd": cwd,
        "state": state,
    }


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def candidates(self, sessions, *, current="", transcript=None, include_interactive=False,  # noqa: PLR0913
                   registries=None, alive=False):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}), \
                mock.patch.object(push, "_agents_sessions", return_value=sessions), \
                mock.patch.object(push, "session_transcript", return_value=transcript), \
                mock.patch.object(push, "_registry_by_pid", return_value=registries or {}), \
                mock.patch.object(push, "_pid_alive", return_value=alive), \
                mock.patch.object(push, "work_dir", return_value=self.root / "llm"):
            return push.handoff_candidates(
                "001", current=current, include_interactive=include_interactive
            )

    def test_completed_sessions_are_listed(self):
        """done / stopped 也要出现，并标 completed=True；否则面板永远搬不了它们。"""
        items = self.candidates([
            session("aaaa1111", state="done"),
            session("bbbb2222", state="stopped"),
            session("cccc3333", state="busy"),
        ])
        by_id = {item["id"]: item for item in items}
        self.assertEqual(len(items), 3)
        self.assertTrue(by_id["aaaa1111"]["completed"])
        self.assertFalse(by_id["aaaa1111"]["running"])
        self.assertTrue(by_id["bbbb2222"]["completed"])
        self.assertFalse(by_id["cccc3333"]["completed"])
        self.assertTrue(by_id["cccc3333"]["running"])

    def test_sorted_by_last_activity_desc(self):
        """按最后一次活动倒序（和接管挑选框一致），不是按名字、也不是在跑的最后。"""
        files = {}
        for short, age in (("old11111", 300), ("new11111", 10), ("mid11111", 120)):
            path = self.root / f"{short}.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
            files[short] = path
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}), \
                mock.patch.object(push, "_agents_sessions", return_value=[
                    session("old11111", name="a-最早"),
                    session("new11111", name="z-最近"),
                    session("mid11111", name="m-中间"),
                ]), \
                mock.patch.object(push, "session_transcript", side_effect=lambda _cfg, short: files.get(short)), \
                mock.patch.object(push, "_registry_by_pid", return_value={}), \
                mock.patch.object(push, "_pid_alive", return_value=False), \
                mock.patch.object(push, "work_dir", return_value=self.root / "llm"):
            items = push.handoff_candidates("001")
        self.assertEqual([item["id"] for item in items], ["new11111", "mid11111", "old11111"])

    def test_duplicate_rows_are_merged_by_full_id(self):
        """同一段对话的后台登记 + 交互登记只出一行。"""
        full = "aaaa1111-2222-3333-4444-555555555555"
        items = self.candidates([
            session("aaaa1111", full=full, state="blocked", name="后台名"),
            session("aaaa1111", full=full, kind="interactive", state="busy", name="ham-39", pid=111),
        ], include_interactive=True, alive=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["full"], full)
        self.assertEqual(items[0]["name"], "后台名")

    def test_live_terminal_blocks_move(self):
        """后台已结束、终端还开着这段对话：整条不能搬，并说清在哪个终端。"""
        full = "aaaa1111-2222-3333-4444-555555555555"
        items = self.candidates([
            session("aaaa1111", full=full, state="failed", name="旧后台"),
            session("aaaa1111", full=full, kind="interactive", state="busy", name="ham-39", pid=111),
        ], include_interactive=True, alive=True)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["movable"])
        self.assertIn("ham-39", items[0]["reason"])
        self.assertIn("正在终端", items[0]["reason"])

    def test_attach_view_keeps_background_row(self):
        """后台还活着时，同 id 的终端是 attach 视图：按后台那行，仍可搬。"""
        (self.root / "llm").mkdir()
        full = "aaaa1111-2222-3333-4444-555555555555"
        items = self.candidates([
            session("aaaa1111", full=full, state="blocked", name="后台名", cwd=str(self.root)),
            session("aaaa1111", full=full, kind="interactive", state="busy", name="ham-39", pid=111),
        ], include_interactive=True, alive=True)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["movable"])
        self.assertEqual(items[0]["state"], "blocked")
        self.assertEqual(items[0]["name"], "后台名")

    def test_parked_terminal_does_not_block_move(self):
        """按过 ← 的终端（registry 有 parkedJobId）不再占用对话，不挡搬。"""
        full = "aaaa1111-2222-3333-4444-555555555555"
        items = self.candidates([
            session("aaaa1111", full=full, state="failed", name="旧后台"),
            session("aaaa1111", full=full, kind="interactive", state="idle", name="ham-39", pid=111),
        ], include_interactive=True, alive=True, registries={111: {"parkedJobId": "aaaa1111"}})
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["movable"])

    def test_home_directory_moves_via_work_dir(self):
        """家目录的会话能搬：它会改到 ~/llm 接着跑，note 里说明。"""
        (self.root / "llm").mkdir()
        items = self.candidates([session("aaaa1111", cwd=str(self.root))])
        self.assertTrue(items[0]["movable"])
        self.assertEqual(items[0]["reason"], "")
        self.assertIn("接着跑", items[0]["note"])
        self.assertIn("llm", items[0]["note"])

    def test_home_directory_without_work_dir_is_not_movable(self):
        """~/llm 还没建：搬不了，原因写清楚。"""
        items = self.candidates([session("aaaa1111", cwd=str(self.root))])
        self.assertFalse(items[0]["movable"])
        self.assertEqual(items[0]["reason"], push.HOME_DIR_REASON)
        self.assertIn("工作目录", items[0]["reason"])

    def test_interactive_session_is_not_movable(self):
        """面板清单要列出交互会话，但灰掉并说明它开在哪个终端。"""
        items = self.candidates(
            [session("aaaa1111", kind="interactive", state="idle", name="ham-39", pid=111)],
            include_interactive=True, alive=True,
        )
        self.assertFalse(items[0]["movable"])
        self.assertIn("ham-39", items[0]["reason"])
        self.assertIn("正在终端", items[0]["reason"])

    def test_plain_handoff_still_skips_interactive(self):
        """普通 handoff 的老行为不变：交互会话不进候选。"""
        items = self.candidates([session("aaaa1111", kind="interactive", state="idle")])
        self.assertEqual(items, [])

    def test_project_directory_session_is_movable(self):
        project = self.root / "proj"
        project.mkdir()
        items = self.candidates([session("aaaa1111", cwd=str(project))])
        self.assertTrue(items[0]["movable"])
        self.assertEqual(items[0]["reason"], "")
        self.assertEqual(items[0]["note"], "")

    def test_current_matches_full_or_short_id(self):
        full = "aaaa1111-2222-3333-4444-555555555555"
        items = self.candidates([session("aaaa1111", full=full)], current=full)
        self.assertTrue(items[0]["current"])
        items = self.candidates([session("aaaa1111", full=full)], current="aaaa1111")
        self.assertTrue(items[0]["current"])
        items = self.candidates([session("aaaa1111", full=full)], current="other")
        self.assertFalse(items[0]["current"])

    def test_recap_last_and_active_at_come_from_transcript(self):
        """recap 取最后的 away_summary；last 取最后一句 assistant 文本。"""
        path = self.root / "aaaa1111-0000-0000-0000-000000000000.jsonl"
        path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
            {"type": "user", "cwd": str(self.root)},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "第一句"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "最后 一句"}]}},
            {"type": "system", "subtype": "away_summary", "content": "离开期间 做完 A"},
        ]) + "\n", encoding="utf-8")
        stamp = time.time() - 3600
        os.utime(path, (stamp, stamp))
        items = self.candidates([session("aaaa1111", cwd="/proj/x")], transcript=path)
        self.assertEqual(items[0]["last"], "最后 一句")
        self.assertEqual(items[0]["recap"], "离开期间 做完 A")
        self.assertTrue(items[0]["active_at"].endswith("+00:00"))
        self.assertEqual(items[0]["cwd"], str(self.root))   # transcript 里的起始目录优先

    def test_agents_all_is_requested(self):
        """候选必须用 `claude agents --json --all`，否则已结束的会话不出现。"""
        daemon = self.root / "daemon"
        daemon.mkdir()
        completed = mock.Mock(returncode=0, stdout="[]", stderr="")
        with mock.patch.object(push, "profile_dir", return_value=self.root), \
                mock.patch.object(push.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(push.subprocess, "run", return_value=completed) as run:
            self.assertEqual(push._agents_sessions("001", include_all=True), [])  # noqa: SLF001 - 测的就是它
        self.assertEqual(run.call_args.args[0], ["/bin/claude", "agents", "--json", "--all"])

        with mock.patch.object(push, "profile_dir", return_value=self.root), \
                mock.patch.object(push.shutil, "which", return_value="/bin/claude"), \
                mock.patch.object(push.subprocess, "run", return_value=completed) as run:
            push._agents_sessions("001")  # noqa: SLF001 - 测的就是它
        self.assertEqual(run.call_args.args[0], ["/bin/claude", "agents", "--json"])


class RunHandoffTest(unittest.TestCase):
    """run_handoff 的两条新规则：已结束跳过 stop；搬不了的一条都不动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ok = mock.Mock(returncode=0, stdout="", stderr="")

    def item(self, **overrides):
        base = {
            "profile": "001",
            "id": "aaaa1111",
            "full": "aaaa1111-0000-0000-0000-000000000000",
            "state": "done",
            "name": "job",
            "cwd": str(self.root),
            "running": False,
            "completed": True,
            "movable": True,
            "reason": "",
        }
        base.update(overrides)
        return base

    def run_handoff(self, item, *, live=None, shared=True):
        """live: {号: {完整 id}}，模拟各号 roster 里进程还活着的会话；stop 以后源号那条就不活了。"""
        live = {name: set(ids) for name, ids in (live or {}).items()}
        calls = []

        def fake_run(args, name, **_kw):
            calls.append((name, list(args)))
            if args[:1] == ["stop"]:
                live.get(name, set()).discard(item["full"])
            return self.ok

        with mock.patch.object(lifecycle, "profile_kind", return_value="oauth"), \
                mock.patch.object(lifecycle, "api_launch_args", side_effect=lambda _n, a: a), \
                mock.patch.object(lifecycle, "copy_trust", return_value=False), \
                mock.patch.object(lifecycle, "HANDOFF_LOCK_DIR", self.root / "locks"), \
                mock.patch.object(lifecycle, "shares_sessions", return_value=shared), \
                mock.patch.object(lifecycle, "live_session_ids", side_effect=lambda n: set(live.get(n, set()))), \
                mock.patch.object(lifecycle, "_run_claude", side_effect=fake_run):
            taken, failures = lifecycle.run_handoff("002", [item], {1})
        stops = [c for c in calls if c[1][:1] == ["stop"]]
        resumes = [c for c in calls if "--resume" in c[1]]
        return taken, failures, stops, resumes

    def test_completed_session_skips_stop(self):
        """已结束的会话不用先 stop 源号，直接在目标号 --bg --resume。"""
        taken, failures, stops, resumes = self.run_handoff(self.item())
        self.assertEqual((len(taken), failures), (1, []))
        self.assertEqual(stops, [])
        self.assertEqual(len(resumes), 1)

    def test_blocked_session_still_stops_source_first(self):
        """没结束也没在跑的会话仍要先停源号，否则 CLI 只会 fork 副本。"""
        taken, failures, stops, _resumes = self.run_handoff(self.item(state="blocked", completed=False))
        self.assertEqual((len(taken), failures), (1, []))
        self.assertEqual(stops, [("001", ["stop", "aaaa1111"])])

    def test_unmovable_session_is_not_touched(self):
        """家目录没建工作目录 / 交互会话：不进失败列表前先停，而是一步都不动。"""
        item = self.item(movable=False, reason=push.HOME_DIR_REASON)
        taken, failures, stops, resumes = self.run_handoff(item)
        self.assertEqual(taken, [])
        self.assertEqual(failures, [f"aaaa1111: {push.HOME_DIR_REASON}"])
        self.assertEqual((stops, resumes), ([], []))

    def test_done_but_alive_is_stopped_first(self):
        """说完一轮的 done 进程还在（面板里搬自己就是这样）：照样先停，等它退出再 resume，不然只会复制一份。"""
        item = self.item()
        taken, failures, stops, resumes = self.run_handoff(item, live={"001": {item["full"]}})
        self.assertEqual((len(taken), failures), (1, []))
        self.assertEqual(stops, [("001", ["stop", "aaaa1111"])])
        self.assertEqual(len(resumes), 1)

    def test_already_running_on_target_is_refused(self):
        """目标号上已经在跑（比如连按了两次 y）：不再 resume。"""
        item = self.item()
        taken, failures, stops, resumes = self.run_handoff(item, live={"002": {item["full"]}})
        self.assertEqual(taken, [])
        self.assertEqual(failures, ["aaaa1111: 它已经在 002 上跑着了"])
        self.assertEqual((stops, resumes), ([], []))

    def run_with_rm(self, item, *, state=None, rm_code=0, resume_code=0):
        """搬一条，记录所有 claude 调用；state 给了就写成源号 jobs/<id>/state.json。返回 (taken, failures, calls)。"""
        if state is not None:
            folder = self.root / item["profile"] / "jobs" / item["id"]
            folder.mkdir(parents=True)
            (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
        calls = []

        def fake_run(args, name, **_kw):
            calls.append((name, list(args)))
            code = {"rm": rm_code}.get(args[0], resume_code if "--resume" in args else 0)
            return mock.Mock(returncode=code, stdout="", stderr="boom" if code else "")

        with mock.patch.object(lifecycle, "profile_kind", return_value="oauth"), \
                mock.patch.object(lifecycle, "api_launch_args", side_effect=lambda _n, a: a), \
                mock.patch.object(lifecycle, "copy_trust", return_value=False), \
                mock.patch.object(lifecycle, "HANDOFF_LOCK_DIR", self.root / "locks"), \
                mock.patch.object(lifecycle, "shares_sessions", return_value=True), \
                mock.patch.object(lifecycle, "profile_dir", side_effect=lambda n: self.root / n), \
                mock.patch.object(lifecycle, "live_session_ids", return_value=set()), \
                mock.patch.object(lifecycle, "_run_claude", side_effect=fake_run), \
                mock.patch.object(lifecycle, "say") as said:
            taken, failures = lifecycle.run_handoff("002", [item], {1})
        return taken, failures, calls, said

    def test_successful_move_removes_the_source_panel_entry(self):
        """搬完在源号上 `claude rm <短编号>`：旧号面板不再有这条，人就点不出副本。"""
        taken, failures, calls, said = self.run_with_rm(self.item(), state={"cwd": "/w"})
        self.assertEqual((len(taken), failures), (1, []))
        self.assertEqual(calls[-1], ("001", ["rm", "aaaa1111"]))
        self.assertEqual(sum(1 for c in calls if c[1][0] == "rm"), 1)
        # rm 在目标号续上之后才做
        self.assertLess(next(i for i, c in enumerate(calls) if "--resume" in c[1]),
                        next(i for i, c in enumerate(calls) if c[1][0] == "rm"))
        self.assertTrue(any("已清 001 面板里的 aaaa1111" in str(call) for call in said.call_args_list))

    def test_job_with_a_worktree_is_not_removed(self):
        """job 有 worktree：rm 会把续跑还要用的 worktree 删掉，跳过并说明原因；搬迁照样算成功。"""
        taken, failures, calls, said = self.run_with_rm(self.item(), state={"worktreePath": "/w/.claude/worktrees/x"})
        self.assertEqual((len(taken), failures), (1, []))
        self.assertFalse(any(c[1][0] == "rm" for c in calls))
        self.assertTrue(any("worktree" in str(call) and "worktreePath" in str(call) for call in said.call_args_list))

    def test_unreadable_job_state_is_not_removed_either(self):
        item = self.item()
        folder = self.root / "001" / "jobs" / item["id"]
        folder.mkdir(parents=True)
        (folder / "state.json").write_text("{坏的", encoding="utf-8")
        with mock.patch.object(lifecycle, "profile_dir", side_effect=lambda n: self.root / n):
            self.assertIn("无法确认", lifecycle.job_worktree("001", item["id"]))

    def test_rm_failure_does_not_undo_a_successful_move(self):
        taken, failures, calls, said = self.run_with_rm(self.item(), rm_code=1)
        self.assertEqual((len(taken), failures), (1, []))
        self.assertTrue(any(c[1][0] == "rm" for c in calls))
        self.assertTrue(any("失败" in str(call) and "boom" in str(call) for call in said.call_args_list))

    def test_rm_is_not_run_when_the_resume_failed(self):
        taken, failures, calls, _said = self.run_with_rm(self.item(), resume_code=1)
        self.assertEqual((taken, len(failures)), ([], 1))
        self.assertFalse(any(c[1][0] == "rm" for c in calls))

    def test_rm_is_skipped_while_the_source_process_is_still_alive(self):
        with mock.patch.object(lifecycle, "live_session_ids", return_value={"aaaa1111-0000"}), \
                mock.patch.object(lifecycle, "_run_claude") as run:
            result = lifecycle.drop_entry("001", "aaaa1111", "aaaa1111-0000", why="x")
        self.assertIn("进程还活着", result)
        run.assert_not_called()

    def test_still_alive_after_stop_is_not_resumed(self):
        """停了进程还不退：宁可不搬，也不复制一份。"""
        item = self.item()
        with mock.patch.object(lifecycle, "HANDOFF_STOP_WAIT_SECONDS", 0), \
                mock.patch.object(lifecycle, "_wait_stopped", return_value=False):
            taken, failures, _stops, resumes = self.run_handoff(item, live={"001": {item["full"]}})
        self.assertEqual(taken, [])
        self.assertEqual(len(failures), 1)
        self.assertEqual(resumes, [])

    def test_isolated_target_is_refused(self):
        """隔离号的对话目录自己一份，看不到这段对话。"""
        taken, failures, stops, resumes = self.run_handoff(self.item(), shared=False)
        self.assertEqual(taken, [])
        self.assertEqual(failures, ["002 是隔离号（对话目录不共用），搬不过去"])
        self.assertEqual((stops, resumes), ([], []))

    def test_second_handoff_of_same_session_is_refused(self):
        """同一段对话正在搬，第二个直接拒绝。"""
        item = self.item()
        with mock.patch.object(lifecycle, "HANDOFF_LOCK_DIR", self.root / "locks"), \
                lifecycle._handoff_lock(item["full"]) as first:  # noqa: SLF001
            self.assertTrue(first)
            with lifecycle._handoff_lock(item["full"]) as second:  # noqa: SLF001
                self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
