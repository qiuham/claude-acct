"""额度提醒（cca quota-hint）、会话里的入口（cca handoff --current / --stay）与搬迁执行（move_session）：不起真 claude。"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.account import lifecycle
from cca.session import quota_hint

HOUR = 3600


def row(name: str, five: int, seven: int, *, seven_in: float = 72, kind: str = "oauth",  # noqa: PLR0913
        logged_in: bool = True) -> dict:
    now = time.time()
    windows = {} if kind == "api" else {
        "five_hour": {"percent": five, "resets_at": now + 2 * HOUR, "reset_passed": False},
        "seven_day": {"percent": seven, "resets_at": now + seven_in * HOUR, "reset_passed": False}}
    return {"profile": name, "kind": kind, "logged_in": logged_in, "usage": {"windows": windows}}


def everyone_shares(_name: str) -> bool:
    return True


class AdviseTest(unittest.TestCase):
    def advise(self, rows, *, account="b004", kind="bg", env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            return quota_hint.advise(account, "s-full-id", rows=rows, shares=everyone_shares, kind=kind)

    def test_quiet_below_threshold(self):
        self.assertFalse(self.advise([row("b004", 60, 20)])["warn"])

    def test_api_account_is_never_warned(self):
        self.assertFalse(self.advise([row("ds", 0, 0, kind="api")], account="ds")["warn"])

    def test_warns_and_picks_earliest_weekly_reset_with_room(self):
        rows = [row("b004", 97, 20),
                row("b001", 10, 30, seven_in=48),
                row("b002", 5, 10, seven_in=5),        # 周额度最早重置 → 先用它
                row("b003", 96, 10, seven_in=1),       # 5 小时也快满：不当目标
                row("005", 0, 0, seven_in=3, logged_in=False),
                row("ds", 0, 0, kind="api")]
        ranked = quota_hint.ranked_targets(rows, "b004", limits=quota_hint.thresholds(),
                                           shares=everyone_shares, now=time.time())
        self.assertEqual([r["profile"] for r in ranked], ["b002", "b001"])
        result = self.advise(rows)
        self.assertTrue(result["warn"])
        self.assertIn(result["target"], {"b002", "b001"})     # 后台会话会在前几个里分散
        self.assertIn(f"cca 会把会话换到 {result['target']}", result["text"])
        self.assertIn("5 小时额度已用 97%", result["text"])
        self.assertIn("`cca handoff --stay`", result["text"])
        self.assertTrue(result["key"].startswith("b004:five_hour:"))

    def test_weekly_window_also_counts(self):
        result = self.advise([row("b004", 10, 98), row("b001", 0, 0)])
        self.assertIn("7 天额度已用 98%", result["text"])
        self.assertIn(":seven_day:", result["key"])

    def test_weekly_window_waits_until_98(self):
        """周窗口涨得慢：95%~97% 不提醒，98% 才提醒；5 小时窗口照旧 95%。"""
        self.assertFalse(self.advise([row("b004", 10, 97), row("b001", 0, 0)])["warn"])
        self.assertTrue(self.advise([row("b004", 95, 10), row("b001", 0, 0)])["warn"])

    def test_roomy_target_beats_earlier_weekly_reset(self):
        """b003 周额度最早重置但已用 89%（离 98% 只差 9 个点）：一批会话搬过去很快又要搬，排到余量充足的后面。"""
        rows = [row("b005", 46, 98), row("b003", 16, 89, seven_in=60), row("005", 9, 34, seven_in=80),
                row("b004", 6, 38, seven_in=120)]
        result = self.advise(rows, account="b005")
        self.assertIn(result["target"], {"005", "b004"})
        self.assertIn("余量充足", result["text"])

    def test_sessions_of_one_account_spread_over_roomy_targets(self):
        """同一个号上的一批会话一起换号：分散到排前面的几个余量充足的号，同一条会话答案固定。"""
        rows = [row("b005", 46, 98), row("005", 9, 34, seven_in=80), row("b004", 6, 38, seven_in=120),
                row("006", 0, 28, seven_in=121), row("b001", 0, 12, seven_in=160), row("b003", 16, 89, seven_in=60)]
        picks = {}
        for index in range(40):
            sid = f"session-{index}"
            picks[sid] = quota_hint.advise("b005", sid, rows=rows, shares=everyone_shares, kind="bg")["target"]
        self.assertEqual(set(picks.values()), {"005", "b004", "006"})   # 前三个余量充足的，不含 b003 和更晚的 b001
        again = quota_hint.advise("b005", "session-7", rows=rows, shares=everyone_shares, kind="bg")["target"]
        self.assertEqual(again, picks["session-7"])

    def test_person_in_terminal_sees_the_first_choice(self):
        rows = [row("b005", 46, 98), row("005", 9, 34, seven_in=80), row("b004", 6, 38, seven_in=120)]
        self.assertEqual(quota_hint.advise("b005", "", rows=rows, shares=everyone_shares, kind="cli")["target"], "005")

    def test_tight_target_still_offered_when_nothing_is_roomy(self):
        rows = [row("b005", 46, 98), row("b003", 16, 89, seven_in=60), row("b004", 85, 20, seven_in=120)]
        result = self.advise(rows, account="b005")
        self.assertEqual(result["target"], "b003")
        self.assertIn("余量都不多", result["text"])

    def test_target_judged_by_each_windows_own_threshold(self):
        """目标号的周额度 96% 还没到 98%，能当目标；5 小时 96% 已过 95%，不能。"""
        rows = [row("b004", 97, 20), row("b001", 10, 96, seven_in=2), row("b002", 96, 10, seven_in=1)]
        self.assertEqual(self.advise(rows)["target"], "b001")

    def test_no_target_keeps_working_and_asks_user_for_wake(self):
        result = self.advise([row("b004", 97, 20), row("b001", 99, 10)])
        self.assertEqual(result["target"], "")
        self.assertIn("没有可换的号：接着做就行", result["text"])
        # 撞墙后叫醒由常驻的 watchdog 排（wake），不用模型去转告用户
        self.assertIn("撞墙停下后 cca 会在额度重置后叫醒它", result["text"])
        self.assertNotIn("--stay", result["text"])

    def test_account_without_usage_data_is_not_recommended(self):
        blank = row("b001", 0, 0, seven_in=1)
        blank["usage"]["windows"] = {}
        result = self.advise([row("b004", 97, 20), blank])
        self.assertEqual(result["target"], "")

    def test_account_missing_weekly_window_is_not_recommended(self):
        """没有周窗口数据：既不知道余量，也没法按周额度最早重置排。"""
        partial = row("b001", 10, 0, seven_in=1)
        del partial["usage"]["windows"]["seven_day"]
        self.assertEqual(self.advise([row("b004", 97, 20), partial])["target"], "")

    def test_reset_passed_window_reads_zero(self):
        renewed = row("b001", 70, 50, seven_in=1)
        renewed["usage"]["windows"]["five_hour"]["reset_passed"] = True
        result = self.advise([row("b004", 97, 20), renewed])
        self.assertEqual(result["target"], "b001")
        self.assertIn("5 小时已用 0%", result["text"])

    def test_interactive_session_tells_user_instead(self):
        result = self.advise([row("b004", 97, 20), row("b001", 0, 0)], kind="interactive")
        self.assertIn("交互会话", result["text"])
        self.assertIn("cca b001 --resume s-full-id", result["text"])
        self.assertNotIn("--current", result["text"])
        self.assertNotIn("--stay", result["text"])

    def test_background_text_is_a_notice_with_the_stay_escape(self):
        result = self.advise([row("b004", 97, 20), row("b001", 0, 0)])
        self.assertIn("这一轮结束后 cca 会把会话换到 b001", result["text"])
        self.assertIn("编号、名字不变，后台任务会停掉，续上后会收到通知", result["text"])
        self.assertIn("如果这一轮就收尾、之后没活了，运行 `cca handoff --stay` 留在本号", result["text"])
        self.assertNotIn("cca handoff b001 --current", result["text"])

    def test_threshold_is_configurable(self):
        rows = [row("b004", 70, 20), row("b001", 0, 0)]
        self.assertFalse(self.advise(rows)["warn"])
        self.assertTrue(self.advise(rows, env={"CCA_QUOTA_HINT_PERCENT": "60"})["warn"])

    def test_weekly_threshold_is_configurable_on_its_own(self):
        rows = [row("b004", 10, 96), row("b001", 0, 0)]
        self.assertFalse(self.advise(rows, env={"CCA_QUOTA_HINT_PERCENT": "60"})["warn"])
        self.assertTrue(self.advise(rows, env={"CCA_QUOTA_HINT_WEEKLY_PERCENT": "95"})["warn"])

    def test_reset_already_passed_is_ignored(self):
        stale = row("b004", 100, 20)
        stale["usage"]["windows"]["five_hour"]["reset_passed"] = True
        self.assertFalse(self.advise([stale, row("b001", 0, 0)])["warn"])


class PollingTest(unittest.TestCase):
    """离阈值远时少问、近时勤问；当前号的数据多旧才现取也跟着远近变。"""

    def interval(self, rows, account="b004"):
        return quota_hint.advise(account, "s", rows=rows, shares=everyone_shares, kind="bg")["next_check_seconds"]

    def test_intervals(self):
        self.assertEqual(self.interval([row("b004", 50, 20)]), quota_hint.POLL_FAR_SECONDS)
        self.assertEqual(self.interval([row("b004", 82, 20)]), quota_hint.POLL_NEAR_SECONDS)
        self.assertEqual(self.interval([row("b004", 97, 20), row("b001", 0, 0)]), quota_hint.POLL_NEAR_SECONDS)
        self.assertEqual(self.interval([row("ds", 0, 0, kind="api")], account="ds"), quota_hint.POLL_API_SECONDS)

    def rows(self, five: int, age: float, *, cache=None, tried_age=None):
        snapshot = [row("b004", five, 20), row("b001", 0, 0)]
        snapshot[0]["usage"]["fetched_at"] = time.time() - age
        calls = []

        def fetch(names, targets, *, refresh):
            calls.append((list(names), list(targets), refresh))
            return snapshot if not targets else [row("b004", five + 1, 20)]

        @contextlib.contextmanager
        def slot(_account):
            yield True

        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "b004.lock"
            if tried_age is not None:
                lock.with_suffix(".tried").touch()
                stamp = time.time() - tried_age
                os.utime(lock.with_suffix(".tried"), (stamp, stamp))
            with mock.patch("cca.report._fetch_list_rows", side_effect=fetch), \
                 mock.patch("cca.account.model.discover_profiles", return_value=["b004", "b001"]), \
                 mock.patch("cca.api.usage.read_usage_cache", return_value=cache or {}), \
                 mock.patch.object(quota_hint, "_slot_path", return_value=lock), \
                 mock.patch.object(quota_hint, "_refresh_slot", slot):
                out = quota_hint.account_rows("b004")
        return calls[1:], out

    def test_far_and_fresh_enough_reads_cache_only(self):
        refreshes, _ = self.rows(50, age=200)
        self.assertEqual(refreshes, [])

    def test_far_and_stale_refreshes_by_cache_rules(self):
        refreshes, out = self.rows(50, age=400)
        self.assertEqual(refreshes, [(["b004"], ["b004"], False)])
        self.assertEqual(out[0]["usage"]["windows"]["five_hour"]["percent"], 51)

    def test_near_forces_a_live_read_after_two_minutes(self):
        self.assertEqual(self.rows(85, age=60)[0], [])
        self.assertEqual(self.rows(85, age=150)[0], [(["b004"], ["b004"], True)])

    def test_someone_else_just_read_it_into_the_cache(self):
        """快照旧、但别的会话刚现取写了缓存：不再取，直接用缓存那份。"""
        cache = {"b004": {"fetched_at": time.time() - 30,
                          "windows": {"five_hour": {"percent": 93, "resets_at": time.time() + HOUR},
                                      "seven_day": {"percent": 20, "resets_at": time.time() + 72 * HOUR}}}}
        refreshes, out = self.rows(85, age=600, cache=cache)
        self.assertEqual(refreshes, [])
        self.assertEqual(out[0]["usage"]["windows"]["five_hour"]["percent"], 93)

    def test_a_failed_read_is_not_retried_every_poll(self):
        """现取失败什么都不写：靠「试过」的记录，2 分钟内别的会话不再去取。"""
        self.assertEqual(self.rows(85, age=600, tried_age=30)[0], [])


class EnsureWatchdogTest(unittest.TestCase):
    """cca quota-hint 被调用时顺手保证 watchdog 在跑：只在订阅号的后台会话里，终端里看不拉起。"""

    def call(self, args, *, kind="bg", profile_kind="oauth", session="sid-1"):
        with mock.patch.object(quota_hint, "current_account", return_value="b004"), \
             mock.patch.object(quota_hint, "account_rows", return_value=[]), \
             mock.patch.object(quota_hint, "_session_kind", return_value=kind), \
             mock.patch.object(quota_hint, "advise", return_value={"warn": False}), \
             mock.patch("cca.session.watchdog.ensure_running") as ensure, \
             mock.patch("cca.account.store.profile_kind", return_value=profile_kind), \
             mock.patch("cca.errors.say"), \
             mock.patch("cca.account.model.require_profile", side_effect=lambda name, **_: name), \
             mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session}), \
             contextlib.redirect_stdout(io.StringIO()):
            quota_hint.cmd_quota_hint(args)
        return ensure

    def test_background_session_pulls_up_the_watchdog_even_below_threshold(self):
        self.call(["--json"]).assert_called_once_with()

    def test_every_call_on_a_subscription_account_pulls_it_up(self):
        self.call(["b004"]).assert_called_once_with()
        self.call(["--json"], session="").assert_called_once_with()
        self.call(["--json"], kind="interactive").assert_called_once_with()

    def test_api_account_does_not(self):
        self.call(["--json"], profile_kind="api").assert_not_called()

    def test_a_failing_spawn_never_breaks_the_hint(self):
        with mock.patch("cca.session.watchdog.ensure_running", side_effect=OSError("fork")):
            quota_hint._ensure_watchdog("b004")  # noqa: SLF001

    def test_no_per_session_wake_arming_any_more(self):
        self.assertFalse(hasattr(quota_hint, "_arm_wake"))


class RequestSelfMoveTest(unittest.TestCase):
    """`cca handoff <目标> --current`：只登记意图，交给常驻的 watchdog，不再起帮手。"""

    def request(self, entry: dict, *, shares=True, source="b004", target="b002"):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abcdef12-0000"}), \
             mock.patch.object(lifecycle, "require_profile"), \
             mock.patch("cca.session.push.shares_sessions", return_value=shares), \
             mock.patch("cca.session.takeover.registry_entry", return_value=entry), \
             mock.patch.object(lifecycle.subprocess, "Popen") as popen, \
             mock.patch("cca.session.watchdog.request_move") as request, \
             mock.patch.object(lifecycle, "say"):
            code = lifecycle._request_self_move(source, target)  # noqa: SLF001
        return code, request, popen

    def test_background_session_hands_the_move_to_the_daemon(self):
        code, request, popen = self.request({"kind": "bg", "status": "busy"})
        self.assertEqual(code, 0)
        request.assert_called_once_with("b004", "b002", "abcdef12-0000")
        popen.assert_not_called()

    def test_repeated_requests_just_overwrite(self):
        self.request({"kind": "bg"})
        code, request, popen = self.request({"kind": "bg"})
        self.assertEqual((code, request.call_count, popen.call_count), (0, 1, 0))

    def test_interactive_session_is_refused(self):
        with self.assertRaises(SystemExit):
            self.request({"kind": "interactive"})

    def test_unregistered_session_is_refused(self):
        with self.assertRaises(SystemExit):
            self.request({})

    def test_same_account_and_isolated_target_are_refused(self):
        with self.assertRaises(SystemExit):
            self.request({"kind": "bg"}, target="b004")
        with self.assertRaises(SystemExit):
            self.request({"kind": "bg"}, shares=False)

    def test_cli_routes_current_and_drops_the_after_turn_helper(self):
        with mock.patch.object(lifecycle, "_current_profile", return_value="b004"), \
             mock.patch.object(lifecycle, "_current_session_prefix", return_value="abcdef12"), \
             mock.patch.object(lifecycle, "_request_self_move", return_value=0) as request:
            self.assertEqual(lifecycle.cmd_handoff(["b002", "--current"]), 0)
        request.assert_called_once_with("b004", "b002")
        with self.assertRaises(SystemExit):
            lifecycle.cmd_handoff(["b004", "b002", "--only=abcdef12", "--after-turn"])
        for gone in ("_helper_alive", "_wait_turn_end", "_settled_watch", "_source_still_limited",
                     "_finish_self_move", "_schedule_self_move", "_set_watchdog"):
            self.assertFalse(hasattr(lifecycle, gone), gone)


class RequestStayTest(unittest.TestCase):
    def stay(self, args, entry=None, windows=None):
        entry = {"kind": "bg"} if entry is None else entry
        said = []
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "abcdef12-0000", "CLAUDE_CONFIG_DIR": "/x/b004"}), \
             mock.patch("cca.session.takeover.registry_entry", return_value=entry), \
             mock.patch("cca.session.watchdog.request_stay",
                        return_value={"five_hour": 1.0} if windows is None else windows) as request, \
             mock.patch.object(lifecycle, "say", said.append):
            code = lifecycle.cmd_handoff(args)
        return code, request, said

    def test_stay_records_this_session_on_this_account(self):
        code, request, said = self.stay(["--stay"])
        self.assertEqual(code, 0)
        request.assert_called_once_with("b004", "abcdef12-0000")
        self.assertIn("留在 b004", said[0])
        self.assertIn("撞墙了仍会搬", said[0])

    def test_stay_takes_no_target_and_no_other_flags(self):
        for args in (["--stay", "b002"], ["--stay", "--current"], ["--stay", "--dry-run"]):
            with self.assertRaises(SystemExit):
                self.stay(args)

    def test_stay_on_an_interactive_session_is_a_no_op(self):
        code, request, said = self.stay(["--stay"], entry={"kind": "interactive"})
        self.assertEqual(code, 0)
        request.assert_not_called()
        self.assertIn("交互会话", said[0])

    def test_stay_without_quota_data_says_it_will_not_take_effect(self):
        _code, _request, said = self.stay(["--stay"], windows={})
        self.assertIn("读不到", said[0])


class MoveSessionTest(unittest.TestCase):
    """搬迁执行（watchdog 调）：任何一步不成，都不让会话就这么停着；也不复活被人停掉的、不造副本。"""

    FULL = "abcdef12-0000"
    ITEM = {"full": FULL,  # noqa: RUF012 - 只读夹具
            "id": "abcdef12", "profile": "b004", "cwd": "/work", "completed": True}

    def finish(self, *, problem="", moved=True, live_before=True, after_on=(),  # noqa: PLR0913
               failure="abcdef12: 起不来", prechecked=False):
        """after_on：搬失败以后它在哪些号上跑着（空 = 哪都没跑）。"""
        told, resumed, handed = [], [], []
        state = {"after": False}

        def run_handoff(target, items, selected, **kw):
            handed.append((target, kw.get("prompt", "")))
            state["after"] = True
            return ([self.ITEM], []) if moved else ([], [failure])

        def live(name):
            if not state["after"]:
                return {self.FULL} if (live_before and name == "b004") else set()
            return {self.FULL} if name in after_on else set()

        with mock.patch.object(lifecycle, "target_problem", return_value=problem) as check, \
             mock.patch.object(lifecycle, "handoff_candidates", return_value=[dict(self.ITEM)]), \
             mock.patch.object(lifecycle, "run_handoff", side_effect=run_handoff), \
             mock.patch.object(lifecycle, "_report_handoff", return_value=0), \
             mock.patch.object(lifecycle, "discover_profiles", return_value=["b004", "b002"]), \
             mock.patch.object(lifecycle, "live_session_ids", side_effect=live), \
             mock.patch.object(lifecycle, "_tell_session", side_effect=lambda *a: told.append(a)), \
             mock.patch.object(lifecycle, "_resume_back", side_effect=lambda *a, **kw: resumed.append((*a, kw.get("cwd")))), \
             mock.patch.object(lifecycle, "say"):
            code = lifecycle.move_session("b004", "b002", self.FULL, prechecked=prechecked)
        return code, handed, told, resumed, check

    def test_success_moves_with_the_nudge(self):
        code, handed, told, resumed, check = self.finish()
        self.assertEqual(code, 0)
        self.assertEqual(handed[0][0], "b002")
        self.assertIn("接着做", handed[0][1])
        self.assertEqual((told, resumed), ([], []))
        check.assert_called_once_with("b002")

    def test_prechecked_target_is_not_checked_twice(self):
        _code, _handed, _told, _resumed, check = self.finish(prechecked=True)
        check.assert_not_called()

    def test_stopped_during_preflight_is_not_revived(self):
        code, handed, told, resumed, _ = self.finish(live_before=False)
        self.assertEqual((code, handed, told, resumed), (1, [], [], []))

    def test_target_problem_tells_the_live_session_to_carry_on(self):
        code, handed, told, resumed, _ = self.finish(problem="目标号额度已经用满")
        self.assertEqual((code, handed, resumed), (1, [], []))
        self.assertIn("没换到 b002：目标号额度已经用满", told[0][2])

    def test_failed_resume_after_stop_comes_back_to_source(self):
        code, _, told, resumed, _ = self.finish(moved=False, after_on=())
        self.assertEqual((code, told), (1, []))
        self.assertEqual(resumed[0][0], "b004")
        self.assertIn("起不来", resumed[0][2])
        self.assertEqual(resumed[0][3], "/work")       # 用 handoff 已经认好的目录，不再去猜

    def test_another_handoff_holding_the_lock_is_left_to_finish(self):
        """同一条会话已经有一个 handoff 在搬：抢不到锁的什么都不做（回源号续就是副本）。"""
        code, _, told, resumed, _ = self.finish(moved=False, after_on=(),
                                                failure=f"abcdef12: {lifecycle.HANDOFF_BUSY}")
        self.assertEqual((code, told, resumed), (1, [], []))

    def test_already_running_on_target_is_a_duplicate_not_a_failed_move(self):
        """目标号上已经有一份：返回专用码（watchdog 不计失败），也不往源号发“没换成”。"""
        code, _, told, resumed, _ = self.finish(moved=False, after_on=("b004", "b002"),
                                                failure="abcdef12: 它已经在 b002 上跑着了")
        self.assertEqual((code, told, resumed), (lifecycle.MOVE_ALREADY_THERE, [], []))

    def test_failed_but_running_elsewhere_is_not_duplicated(self):
        """目标号其实起来了（或另有人在搬）：不再回源号续一份副本。"""
        code, _, told, resumed, _ = self.finish(moved=False, after_on=("b002",))
        self.assertEqual((code, told, resumed), (1, [], []))

    def test_resume_carries_the_nudge_and_never_a_name_flag(self):
        calls = []

        def fake_run(args, name, **_):
            calls.append((name, list(args)))
            return mock.Mock(returncode=0, stdout="", stderr="")

        item = {"full": "abcdef12-0000", "id": "abcdef12", "profile": "b004", "cwd": "/work", "completed": True}
        with mock.patch.object(lifecycle, "_run_claude", side_effect=fake_run), \
             mock.patch.object(lifecycle, "live_session_ids", return_value=set()), \
             mock.patch.object(lifecycle, "copy_trust", return_value=False), \
             mock.patch.object(lifecycle, "profile_dir", side_effect=lambda name: Path("/nonexistent") / name), \
             mock.patch.object(lifecycle, "say"), \
             mock.patch("cca.session.transcript.target_needs_strip", return_value=False):
            self.assertEqual(lifecycle._move_one(item, "b002", "接着做"), "")  # noqa: SLF001
        resume = next(call for call in calls if "--resume" in call[1])
        self.assertEqual(calls[-1], ("b004", ["rm", "abcdef12"]))       # 搬完清源号面板
        self.assertEqual(resume[0], "b002")
        self.assertEqual(resume[1][-4:], ["--bg", "--resume", "abcdef12-0000", "接着做"])
        self.assertNotIn("--name", resume[1])
        self.assertNotIn("-n", resume[1])


if __name__ == "__main__":
    unittest.main()


class CurrentWithoutTargetTest(unittest.TestCase):
    """会话里的 /cca handoff 不写号：登记一条目标为空的意图，交给 watchdog 按额度挑。"""

    def run_handoff(self, args, *, kind="bg"):
        entry = {"kind": kind, "sessionId": "sid-full"}
        env = {"CLAUDE_CONFIG_DIR": "/x/b005"}
        with mock.patch.dict(os.environ, env), \
             mock.patch("cca.session.takeover.registry_entry", return_value=entry), \
             mock.patch("cca.session.watchdog.request_move") as move, \
             mock.patch.object(lifecycle, "require_profile"), \
             mock.patch("cca.session.push.shares_sessions", return_value=True), \
             mock.patch.object(lifecycle, "say"):
            code = lifecycle.cmd_handoff(args)
        return code, move

    def test_no_target_registers_auto_pick(self):
        code, move = self.run_handoff(["--current", "--session=sid-full"])
        self.assertEqual(code, 0)
        move.assert_called_once_with("b005", "", "sid-full")

    def test_session_flag_is_used_as_the_current_session(self):
        _, move = self.run_handoff(["b004", "--current", "--session=sid-other"])
        self.assertEqual(move.call_args.args, ("b005", "b004", "sid-other"))

    def test_session_flag_needs_current_or_stay(self):
        with self.assertRaises(SystemExit):
            lifecycle.cmd_handoff(["b005", "b004", "--session=x"])


class HintKeyTest(unittest.TestCase):
    def test_key_is_stable_across_sub_second_jitter(self):
        """不同数据源算出的重置时刻差零点几秒（…399.9 / …400.1）：去重的 key 要一样，提醒只给一次。"""
        now = time.time()
        keys = set()
        for resets in (now + 3600 - 0.08, now + 3600 + 0.1):
            rows = [{"profile": "b004", "kind": "oauth", "logged_in": True, "usage": {"windows": {
                "five_hour": {"percent": 97, "resets_at": resets, "reset_passed": False},
                "seven_day": {"percent": 20, "resets_at": now + 86400, "reset_passed": False}}}}]
            keys.add(quota_hint.advise("b004", "sid", rows=rows, shares=lambda _n: True, kind="bg")["key"])
        self.assertEqual(len(keys), 1)
