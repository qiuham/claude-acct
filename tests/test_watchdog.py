"""额度看门狗：只测判断逻辑，不真跑 claude，也不真搬会话。"""

from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import watchdog  # noqa: E402

_REAL_ENSURE = watchdog.ensure_running      # AccountsCase 会把模块里的换成假的；单例的测试要真的

_SIDE = None
_SIDE_PATCH = None


def setUpModule():
    """测试不碰真的 ~/.cca/sideagents：看门狗搬成功会往那里写通知，面板会把假通知弹出来（实测踩到）。"""
    global _SIDE, _SIDE_PATCH  # noqa: PLW0603
    _SIDE = tempfile.TemporaryDirectory()
    _SIDE_PATCH = mock.patch.object(watchdog, "sideagent_dir", return_value=Path(_SIDE.name))
    _SIDE_PATCH.start()


def tearDownModule():
    _SIDE_PATCH.stop()
    _SIDE.cleanup()


def windows(percent: int, resets_at: float = 0.0) -> dict:
    return {"five_hour": {"label": "5小时", "percent": percent, "resets_at": resets_at}}


class QuotaTest(unittest.TestCase):
    def test_room_until_full(self):
        self.assertTrue(watchdog.has_room(windows(90)))
        self.assertFalse(watchdog.has_room(windows(100)))
        self.assertFalse(watchdog.has_room(windows(120)))
        # 拿不到窗口不当“用满”
        self.assertTrue(watchdog.has_room({}))

    def test_reset_at_is_earliest_full_window(self):
        quota = {"five_hour": {"percent": 100, "resets_at": 300.0},
                 "seven_day": {"percent": 100, "resets_at": 100.0}}
        self.assertEqual(watchdog.reset_at(quota), 100.0)
        self.assertEqual(watchdog.reset_at(windows(90, 50.0)), 0.0)


def assistant(text: str, *, is_error: bool = False) -> dict:
    return {"type": "assistant", "isApiErrorMessage": is_error,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class LimitTest(unittest.TestCase):
    def test_hits_limit_needs_error_record(self):
        # 真机上长这样：type=assistant + isApiErrorMessage
        self.assertTrue(watchdog.hits_limit(
            assistant("You've hit your 5-hour limit · resets 12am", is_error=True)))
        # 正文里聊到 rate limit 不算
        self.assertFalse(watchdog.hits_limit(
            assistant("我看一下这个 rate limit 的实现")))
        # 没带字段的老记录：整条就是那句 API Error 也算
        self.assertTrue(watchdog.hits_limit(assistant("API Error: 429 usage limit reached")))
        # 别的类型不算
        self.assertFalse(watchdog.hits_limit({"type": "user", "isApiErrorMessage": True,
                                              "message": {"content": [
                                                  {"type": "text", "text": "usage limit"}]}}))


API_NAMES = {"003", "ds", "openai"}


def make_accounts(root: Path) -> None:
    """造几个号目录：001/002 是订阅号，003/ds/openai 带清单文件（API 号）。"""
    for name in ("001", "002", "003", "ds", "openai"):
        (root / name).mkdir(parents=True)
    for name in API_NAMES:
        (root / name / ".cca-profile.json").write_text(
            json.dumps({"base_url": "https://example.invalid"}), encoding="utf-8")
    # 缓存目录不是号：discover_profiles 会把它滤掉
    (root / ".cache").mkdir()


class SubscriptionProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        make_accounts(self.root)
        patcher = mock.patch.object(watchdog, "accounts_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        # discover_profiles 内部读的是 cca.account.model 的这两个函数
        model_root = mock.patch("cca.account.model.accounts_root", return_value=self.root)
        model_root.start()
        self.addCleanup(model_root.stop)
        # 所有号都在 accounts_root 下；001 也当作一个普通目录存在
        (self.root / "001").mkdir(parents=True, exist_ok=True)
        # profile_kind 走的是 cca.paths 的 real 目录；测试里直接按名字给类型
        kind = mock.patch.object(watchdog, "profile_kind",
                                 side_effect=lambda name: "api" if name in API_NAMES else "oauth")
        kind.start()
        self.addCleanup(kind.stop)
        # 默认没有本地快照；需要时在单个测试里覆盖
        snapshot = mock.patch.object(watchdog, "usage_snapshot", return_value={})
        snapshot.start()
        self.addCleanup(snapshot.stop)

    def test_subscription_profiles_skip_cache_and_api(self):
        self.assertEqual(watchdog.subscription_profiles(), ["001", "002"])


# ---------------------------------------------------------------- v2：决策、循环、意图、单例、展示

HOUR = 3600
LIMITS = {"five_hour": 95, "seven_day": 98}


def win(five: int = 10, seven: int = 10, *, five_in: float = 2 * HOUR, seven_in: float = 72 * HOUR) -> dict:
    now = time.time()
    return {"five_hour": {"percent": five, "resets_at": now + five_in, "reset_passed": False},
            "seven_day": {"percent": seven, "resets_at": now + seven_in, "reset_passed": False}}


def row(name: str, five: int = 10, seven: int = 10, **kw) -> dict:
    return {"profile": name, "kind": "oauth", "logged_in": True, "usage": {"windows": win(five, seven, **kw)}}


def obs(**kw) -> watchdog.Obs:
    now = time.time()
    base = {"session": "sid-1", "account": "b005", "name": "dagset", "turn": "running", "idle": False,
            "last_active": now - 60, "windows": win(five=96), "limits": LIMITS, "now": now}
    base.update(kw)
    return watchdog.Obs(**base)


class DecideTest(unittest.TestCase):
    def action(self, **kw) -> str:
        return watchdog.decide(obs(**kw)).action

    def test_busy_session_at_threshold_waits_for_the_turn_end(self):
        decision = watchdog.decide(obs())
        self.assertEqual(decision.action, watchdog.ARMED)
        self.assertIn("5h 96%", decision.reason)

    def test_weekly_threshold_is_98(self):
        self.assertEqual(self.action(windows=win(seven=98)), watchdog.ARMED)
        self.assertEqual(self.action(windows=win(seven=97)), watchdog.SKIP)

    def test_below_threshold_is_left_alone(self):
        decision = watchdog.decide(obs(windows=win(five=94)))
        self.assertEqual((decision.action, decision.notable), (watchdog.SKIP, False))

    def test_walled_session_moves_now_ignoring_every_exception(self):
        soon = win(five=100, five_in=300)
        stay = {"windows": {"five_hour": soon["five_hour"]["resets_at"]}}
        decision = watchdog.decide(obs(turn="walled", windows=soon, stay=stay, last_active=0))
        self.assertEqual(decision.action, watchdog.MOVE_NOW)
        self.assertAlmostEqual(decision.resets_at, soon["five_hour"]["resets_at"])

    def test_walled_but_source_recovered_goes_to_wake(self):
        self.assertEqual(self.action(turn="walled", windows=win(five=20, seven=30)), watchdog.WAKE_NOW)
        reset = {"five_hour": {"percent": 100, "resets_at": time.time() - 5, "reset_passed": True},
                 "seven_day": {"percent": 10, "resets_at": time.time() + HOUR, "reset_passed": False}}
        self.assertEqual(self.action(turn="walled", windows=reset), watchdog.WAKE_NOW)

    def test_walled_with_unknown_quota_still_moves(self):
        self.assertEqual(self.action(turn="walled", windows={}), watchdog.MOVE_NOW)

    def test_window_resetting_within_15_minutes_is_awaited(self):
        decision = watchdog.decide(obs(windows=win(five=97, five_in=12 * 60)))
        self.assertEqual((decision.action, decision.notable), (watchdog.SKIP, True))
        self.assertIn("分钟后重置", decision.reason)
        self.assertEqual(self.action(windows=win(five=97, five_in=16 * 60)), watchdog.ARMED)

    def test_one_far_window_still_moves(self):
        """两个窗口都到了提醒点，只有一个快重置：另一个还要等很久，照搬。"""
        windows = win(five=97, seven=99, five_in=300)
        self.assertEqual(self.action(windows=windows), watchdog.ARMED)

    def test_long_idle_session_is_not_moved(self):
        decision = watchdog.decide(obs(last_active=time.time() - 48 * 60))
        self.assertEqual((decision.action, decision.notable), (watchdog.SKIP, True))
        self.assertIn("闲置 48 分钟", decision.reason)
        self.assertEqual(self.action(last_active=time.time() - 29 * 60), watchdog.ARMED)

    def test_stay_covers_the_same_reset_only(self):
        windows = win(five=96)
        resets = windows["five_hour"]["resets_at"]
        same = {"windows": {"five_hour": resets + 30, "seven_day": time.time() + 72 * HOUR}}
        self.assertEqual(self.action(windows=windows, stay=same), watchdog.SKIP)
        later = {"windows": {"five_hour": resets - 5 * HOUR}}     # 上一段的记录：窗口已经重置过了
        self.assertEqual(self.action(windows=windows, stay=later), watchdog.ARMED)

    def test_stay_does_not_cover_another_window_that_got_hot(self):
        windows = win(five=96, seven=99)
        stay = {"windows": {"five_hour": windows["five_hour"]["resets_at"]}}
        self.assertEqual(self.action(windows=windows, stay=stay), watchdog.ARMED)

    def test_requested_move_skips_the_exceptions_and_does_not_need_the_hot_window(self):
        move = {"account": "b005", "target": "b004"}
        soon = win(five=97, five_in=120)
        self.assertEqual(self.action(windows=soon, move=move, last_active=0), watchdog.ARMED)
        self.assertEqual(self.action(windows=win(five=50), move=move), watchdog.ARMED)


LIMIT = {"type": "assistant", "isApiErrorMessage": True,
         "message": {"content": [{"type": "text", "text": "You've hit your 5-hour limit"}]}}
REPLY = {"type": "assistant", "message": {"content": [{"type": "text", "text": "好"}]}}
NOTICE = {"type": "user", "message": {"content": "<task-notification>…</task-notification>"}}
END = {"type": "system", "subtype": "turn_duration"}
NOISE = ({"type": "system", "subtype": "away_summary"}, {"type": "queue-operation"},
         {"type": "custom-title", "customTitle": "x"})


class TurnStateTest(unittest.TestCase):
    def state(self, records) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abcdef12-0000.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with mock.patch.object(watchdog, "session_transcript", return_value=path):
                return watchdog.turn_state("b005", "abcdef12-0000")

    def test_reads_turn_end_wall_and_ongoing_turns(self):
        cases = [([REPLY, END, *NOISE], "ended"),   # 后台任务还在跑也算这一轮结束
                 ([REPLY, END, NOTICE], "running"),  # 通知进来、新一轮开始
                 ([NOTICE, LIMIT], "walled"),
                 ([NOTICE, LIMIT, END], "walled"),   # 撞墙后跟一条 turn_duration 也是撞墙
                 ([NOTICE, REPLY], "running")]
        for records, expected in cases:
            self.assertEqual(self.state(records), expected, records)

    def test_rereads_only_when_the_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "abcdef12-0000.jsonl"
            path.write_text(json.dumps(REPLY) + "\n" + json.dumps(END) + "\n", encoding="utf-8")
            with mock.patch.object(watchdog, "session_transcript", return_value=path):
                self.assertEqual(watchdog.turn_state("b005", "abcdef12-0000"), "ended")
                with mock.patch.object(Path, "open", side_effect=AssertionError("不该重读")):
                    self.assertEqual(watchdog.turn_state("b005", "abcdef12-0000"), "ended")
                with path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps({"type": "user", "message": {"content": "新任务"}}) + "\n")
                os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1))
                self.assertEqual(watchdog.turn_state("b005", "abcdef12-0000"), "running")

    def test_without_a_transcript_the_turn_is_never_over(self):
        with mock.patch.object(watchdog, "session_transcript", return_value=None):
            self.assertEqual(watchdog.turn_state("b005", "abcdef12-0000"), "running")


class AccountsCase(unittest.TestCase):
    """临时 accounts_root：意图、锁文件、pid 都写在里面，不碰真实的 ~/.claude-accounts。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lines = []
        for patch in (mock.patch.object(watchdog, "accounts_root", return_value=self.root),
                      mock.patch.object(watchdog, "log", self.lines.append),
                      mock.patch.object(watchdog, "ensure_running", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.ensure = watchdog.ensure_running


class IntentTest(AccountsCase):
    def test_move_then_stay_cancels_the_move_and_back(self):
        watchdog.request_move("b005", "b004", "sid-1")
        self.assertEqual(watchdog.read_intents()["moves"]["sid-1"]["target"], "b004")
        with mock.patch("cca.session.quota_hint.account_rows", return_value=[row("b005", 96)]):
            windows = watchdog.request_stay("b005", "sid-1")
        intents = watchdog.read_intents()
        self.assertEqual((intents["moves"], sorted(windows)), ({}, ["five_hour", "seven_day"]))
        self.assertEqual(intents["stays"]["sid-1"]["windows"], windows)
        watchdog.request_move("b005", "b003", "sid-1")
        intents = watchdog.read_intents()
        self.assertEqual((intents["stays"], intents["moves"]["sid-1"]["target"]), ({}, "b003"))
        self.assertEqual(self.ensure.call_count, 3)       # 先写再 ensure_running

    def test_repeated_move_overwrites_one_record(self):
        watchdog.request_move("b005", "b004", "sid-1")
        watchdog.request_move("b005", "b003", "sid-1")
        self.assertEqual(list(watchdog.read_intents()["moves"]), ["sid-1"])

    def test_stay_with_unreadable_quota_records_nothing_useful(self):
        with mock.patch("cca.session.quota_hint.account_rows", side_effect=OSError("boom")):
            self.assertEqual(watchdog.request_stay("b005", "sid-1"), {})

    def test_gc_drops_stale_intents(self):
        now = time.time()
        data = {"version": 1,
                "moves": {"gone": {"account": "b005", "at": now}, "old": {"account": "b005", "at": now - 13 * HOUR},
                          "ok": {"account": "b005", "target": "b004", "at": now}},
                "stays": {"over": {"account": "b005", "windows": {"five_hour": now - 5}},
                          "moved": {"account": "b005", "windows": {"five_hour": now + HOUR}},
                          "keep": {"account": "b005", "windows": {"five_hour": now + HOUR}}}}
        watchdog._write_intents(data)  # noqa: SLF001
        live = {("b005", "ok"), ("b005", "old"), ("b005", "over"), ("b005", "keep")}
        self.assertTrue(watchdog.gc_intents(live, now))
        intents = watchdog.read_intents()
        self.assertEqual((sorted(intents["moves"]), sorted(intents["stays"])), (["ok"], ["keep"]))
        self.assertFalse(watchdog.gc_intents(live, now))

    def test_gc_needs_several_missing_rounds_before_dropping_a_stay(self):
        now = time.time()
        watchdog._write_intents({"version": 1, "moves": {}, "fails": {},  # noqa: SLF001
                                 "stays": {"s": {"account": "b005", "windows": {"five_hour": now + HOUR}}}})
        absent = {}
        for _ in range(watchdog.ABSENT_ROUNDS - 1):
            watchdog.gc_intents(set(), now, absent)
        self.assertIn("s", watchdog.read_intents()["stays"])
        watchdog.gc_intents({("b005", "s")}, now, absent)         # 中间读到一次：计数清零
        for _ in range(watchdog.ABSENT_ROUNDS - 1):
            watchdog.gc_intents(set(), now, absent)
        self.assertIn("s", watchdog.read_intents()["stays"])
        watchdog.gc_intents(set(), now, absent)
        self.assertEqual(watchdog.read_intents()["stays"], {})

    def test_failures_back_off_and_stop_the_automatic_move(self):
        now = 1000.0
        untils = [watchdog.record_failure("b005", "sid-1", now) for _ in range(watchdog.MAX_FAILS)]
        self.assertEqual(untils[:3], [now + 300, now + 600, now + 1200])
        self.assertGreater(untils[-1], now + 3600 * 6)
        self.assertEqual(watchdog.read_intents()["fails"]["sid-1"]["n"], watchdog.MAX_FAILS)
        watchdog._forget_intents("sid-1")  # noqa: SLF001
        self.assertEqual(watchdog.read_intents()["fails"], {})

    def test_garbage_file_reads_as_empty(self):
        (self.root / "watchdog.json").write_text("{not json", encoding="utf-8")
        self.assertEqual((watchdog.read_intents()["moves"], watchdog.read_intents()["stays"]), ({}, {}))


class SnapshotTest(AccountsCase):
    """真读登记表和对话记录（都在临时目录里），额度行用假的。"""

    def setUp(self):
        super().setUp()
        self.transcripts = {}
        self.procs = {}            # pid → 进程环境里的 CLAUDE_CONFIG_DIR（没写的读不到，按登记的号算）
        for name in ("b005", "b004"):
            (self.root / name / "sessions").mkdir(parents=True)
        for patch in (mock.patch.object(watchdog, "profile_dir", side_effect=lambda name: self.root / name),
                      mock.patch.object(watchdog, "process_config_dir", side_effect=lambda pid: self.procs.get(pid, "")),
                      mock.patch.object(watchdog, "subscription_profiles", return_value=["b004", "b005"]),
                      mock.patch.object(watchdog, "shares_sessions", return_value=True),
                      mock.patch.object(watchdog, "session_transcript",
                                        side_effect=lambda _p, short: self.transcripts.get(short))):
            patch.start()
            self.addCleanup(patch.stop)

    def session(self, full, *, status="busy", kind="bg", account="b005", pid=None, active=60, records=(), started=0):  # noqa: PLR0913 - 夹具造数据的开关
        now = time.time()
        entry = {"sessionId": full, "kind": kind, "status": status, "name": f"n-{full[:4]}", "cwd": "/w",
                 "pid": pid or os.getpid(), "statusUpdatedAt": int((now - active) * 1000),
                 "startedAt": started, "jobId": full[:8]}
        (self.root / account / "sessions" / f"{full[:6]}.json").write_text(json.dumps(entry), encoding="utf-8")
        path = self.root / f"{full}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        os.utime(path, (now - active, now - active))
        self.transcripts[full[:8]] = path

    def snapshot(self, rows):
        with mock.patch("cca.session.quota_hint.cached_rows", return_value=rows):
            return watchdog.snapshot()

    def test_only_live_background_sessions_are_planned(self):
        self.session("aaaaaaaa-1", kind="interactive")
        self.session("bbbbbbbb-2", pid=2 ** 22 + 12345)        # 进程不在了
        self.session("cccccccc-3")
        plans, _rows = self.snapshot([row("b005", 96), row("b004", 10)])
        self.assertEqual([d.session for d in plans], ["cccccccc-3"])
        self.assertEqual((plans[0].action, plans[0].target), (watchdog.ARMED, "b004"))

    def test_session_ending_its_turn_is_marked_settled(self):
        self.session("cccccccc-3", status="shell", records=[REPLY, END])
        plans, _rows = self.snapshot([row("b005", 96), row("b004", 10)])
        self.assertEqual((plans[0].action, plans[0].settled, plans[0].idle), (watchdog.ARMED, True, False))

    def test_walled_session_moves_now_to_the_roomy_account(self):
        self.session("cccccccc-3", status="idle", records=[NOTICE, LIMIT], active=3 * HOUR)
        plans, _rows = self.snapshot([row("b005", 100), row("b004", 10)])
        self.assertEqual((plans[0].action, plans[0].target), (watchdog.MOVE_NOW, "b004"))

    def test_everyone_full_walled_waits_for_wake_and_hot_one_is_noted(self):
        self.session("cccccccc-3", records=[NOTICE, LIMIT])
        self.session("dddddddd-4", account="b005")
        plans, _rows = self.snapshot([row("b005", 100), row("b004", 97)])
        self.assertEqual({d.session: d.action for d in plans},
                         {"cccccccc-3": watchdog.WAIT_WAKE, "dddddddd-4": watchdog.SKIP})
        # 没撞墙、到提醒点、没有可去的号：只记一笔，不排叫醒
        self.session("cccccccc-3")
        plans, _rows = self.snapshot([row("b005", 96), row("b004", 97)])
        self.assertEqual({d.action for d in plans}, {watchdog.SKIP})
        self.assertTrue(all(d.notable and "全满" in d.reason for d in plans))

    def test_requested_target_is_preferred(self):
        self.session("cccccccc-3")
        (self.root / "b003").mkdir()
        watchdog._write_intents({"version": 1, "stays": {},  # noqa: SLF001
                                 "moves": {"cccccccc-3": {"account": "b005", "target": "b003", "at": time.time()}}})
        plans, _rows = self.snapshot([row("b005", 96), row("b004", 10), row("b003", 20)])
        self.assertEqual((plans[0].action, plans[0].target), (watchdog.ARMED, "b003"))


class DuplicateSessionTest(SnapshotTest):
    """同一条会话在几个号上同时跑：先按进程真正的号去重，同一轮只做一个决定。"""

    FULL = "cccccccc-3"
    P1, P2 = os.getpid(), os.getppid()           # 两个确实活着、pid 不同的进程

    def two_copies(self, *, started=(1000, 2000)):
        self.session(self.FULL, account="b005", pid=self.P1, started=started[0])
        self.session(self.FULL, account="b004", pid=self.P2, started=started[1])
        self.procs = {self.P1: str(self.root / "b005"), self.P2: str(self.root / "b004")}

    def only(self, plans):
        self.assertEqual([d.session for d in plans], [self.FULL], "同一轮一条会话只能有一个决定")
        return plans[0]

    def test_same_pid_registered_under_two_accounts_is_a_leftover_not_a_duplicate(self):
        self.session(self.FULL, account="b005", pid=self.P1)
        self.session(self.FULL, account="b004", pid=self.P1)
        self.procs = {self.P1: str(self.root / "b004")}                # 进程真正在 b004
        plans, _ = self.snapshot([row("b005", 96), row("b004", 10)])
        decision = self.only(plans)
        self.assertEqual((decision.action, decision.account), (watchdog.SKIP, "b004"))

    def test_same_pid_leftover_without_a_registry_at_its_real_account_is_attributed_there(self):
        self.session(self.FULL, account="b005", pid=self.P1)
        self.procs = {self.P1: str(self.root / "b004")}
        plans, _ = self.snapshot([row("b005", 96), row("b004", 10)])
        self.assertEqual(self.only(plans).account, "b004")

    def test_two_pids_on_two_accounts_keep_the_one_not_at_the_reminder_point(self):
        self.two_copies(started=(2000, 1000))          # 热的那份反而启动得更晚：规则一优先
        decision = self.only(self.snapshot([row("b005", 96), row("b004", 10)])[0])
        self.assertEqual((decision.action, decision.account), (watchdog.DUPLICATE, "b004"))
        self.assertEqual([(d["account"], d["job"]) for d in decision.drops], [("b005", "cccccccc")])
        self.assertIn("b004、b005", decision.reason)
        self.assertIn("保留 b004", decision.reason)

    def test_neither_at_the_reminder_point_keeps_the_most_recently_started(self):
        self.two_copies(started=(2000, 1000))
        decision = self.only(self.snapshot([row("b005", 10), row("b004", 20)])[0])
        self.assertEqual((decision.account, [d["account"] for d in decision.drops]), ("b005", ["b004"]))

    def test_both_at_the_reminder_point_keeps_the_most_recently_started(self):
        self.two_copies(started=(1000, 2000))
        decision = self.only(self.snapshot([row("b005", 96), row("b004", 97)])[0])
        self.assertEqual((decision.account, [d["account"] for d in decision.drops]), ("b004", ["b005"]))

    def test_two_pids_in_the_same_account_are_not_a_cross_account_duplicate(self):
        self.session(self.FULL, account="b005", pid=self.P1, started=1000)
        (self.root / "b005" / "sessions" / "other.json").write_text(json.dumps(
            {"sessionId": self.FULL, "kind": "bg", "status": "busy", "name": "n", "cwd": "/w",
             "pid": self.P2, "startedAt": 2000}), encoding="utf-8")
        self.procs = {self.P1: str(self.root / "b005"), self.P2: str(self.root / "b005")}
        decision = self.only(self.snapshot([row("b005", 10), row("b004", 10)])[0])
        self.assertNotEqual(decision.action, watchdog.DUPLICATE)

    def test_pid_running_under_a_non_subscription_dir_is_still_observed_at_its_registered_account(self):
        self.session(self.FULL, account="b005", pid=self.P1)
        self.procs = {self.P1: str(self.root / "gpt")}
        decision = self.only(self.snapshot([row("b005", 96), row("b004", 10)])[0])
        self.assertEqual((decision.action, decision.account), (watchdog.ARMED, "b005"))

    def test_same_pid_under_two_accounts_with_unreadable_environ_is_not_trusted_for_a_duplicate(self):
        self.session(self.FULL, account="b005", pid=self.P1, started=1000)
        self.session(self.FULL, account="b004", pid=self.P1, started=1000)
        self.session("dddddddd-4", account="b004", pid=self.P2)
        # 另一份真的重复：P2 在 b005 上也有一条同会话登记，但 P1 的环境读不到，认不出真实号
        self.procs = {}
        plans, _ = self.snapshot([row("b005", 10), row("b004", 10)])
        self.assertNotIn(watchdog.DUPLICATE, {d.action for d in plans})
        self.assertEqual([d.session for d in plans if d.session == self.FULL], [self.FULL])

    def test_unreadable_environ_with_a_second_real_copy_skips_the_duplicate_action(self):
        self.session(self.FULL, account="b005", pid=self.P1, started=1000)
        (self.root / "b004" / "sessions" / "x.json").write_text(json.dumps(
            {"sessionId": self.FULL, "kind": "bg", "status": "busy", "name": "n", "cwd": "/w",
             "pid": self.P1, "startedAt": 1000}), encoding="utf-8")
        (self.root / "b004" / "sessions" / "y.json").write_text(json.dumps(
            {"sessionId": self.FULL, "kind": "bg", "status": "busy", "name": "n", "cwd": "/w",
             "pid": self.P2, "startedAt": 2000}), encoding="utf-8")
        self.procs = {self.P2: str(self.root / "b004")}
        plans, _ = self.snapshot([row("b005", 10), row("b004", 10)])
        self.assertNotIn(watchdog.DUPLICATE, {d.action for d in plans})

    def test_busy_copy_is_kept_over_a_later_started_idle_one(self):
        self.session(self.FULL, account="b005", pid=self.P1, started=1000, status="idle")
        self.session(self.FULL, account="b004", pid=self.P2, started=500, status="busy")
        self.procs = {self.P1: str(self.root / "b005"), self.P2: str(self.root / "b004")}
        decision = self.only(self.snapshot([row("b005", 10), row("b004", 10)])[0])
        self.assertEqual((decision.account, [d["account"] for d in decision.drops]), ("b004", ["b005"]))

    def test_hot_still_beats_busy(self):
        self.session(self.FULL, account="b005", pid=self.P1, started=1000, status="busy")
        self.session(self.FULL, account="b004", pid=self.P2, started=500, status="idle")
        self.procs = {self.P1: str(self.root / "b005"), self.P2: str(self.root / "b004")}
        decision = self.only(self.snapshot([row("b005", 96), row("b004", 10)])[0])
        self.assertEqual(decision.account, "b004")


class DuplicateActionTest(AccountsCase):
    FULL = "cccccccc-3333"

    def setUp(self):
        super().setUp()
        watchdog._DUP_TRIED.clear()  # noqa: SLF001
        self.addCleanup(watchdog._DUP_TRIED.clear)  # noqa: SLF001

    def decision(self, **kw):
        base = {"cwd": "/w", "notable": True,
                "drops": ({"account": "b005", "job": "cccccccc", "pid": 4242},),
                "reason": "同时在 b004、b005 上跑；保留 b004，停掉 b005"}
        return watchdog.Decision(watchdog.DUPLICATE, self.FULL, "b004", "dagset", **{**base, **kw})

    def test_resolve_stops_logs_notifies_and_clears_old_failures(self):
        watchdog._write_intents({"version": 1, "moves": {}, "stays": {},  # noqa: SLF001
                                 "fails": {self.FULL: {"account": "b005", "n": 4, "until": time.time() + 999}}})
        side = Path(self.root / "side")
        with mock.patch.object(watchdog, "_stop_copy", return_value=(True, "已清 b005 面板里的 cccccccc（重复会话）")) as stop, \
             mock.patch.object(watchdog, "sideagent_dir", return_value=side), \
             mock.patch.object(watchdog, "session_title", return_value="dagset"):
            watchdog.resolve_duplicate(self.decision())
        stop.assert_called_once_with("b005", {"account": "b005", "job": "cccccccc", "pid": 4242}, self.FULL)
        self.assertTrue(any("重复会话" in line and "保留 b004" in line for line in self.lines))
        self.assertTrue(any("已清 b005" in line for line in self.lines))
        notice = json.loads((side / "notices.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertIn("dagset", notice["text"])
        self.assertIn("同时在 b004、b005 上跑", notice["text"])
        self.assertEqual(watchdog.read_intents()["fails"], {})

    def run_resolve(self, stop_result, *, now, intents=None):
        if intents:
            watchdog._write_intents(intents)  # noqa: SLF001
        side = Path(self.root / "side")
        with mock.patch.object(watchdog, "_stop_copy", return_value=stop_result) as stop, \
             mock.patch.object(watchdog, "sideagent_dir", return_value=side), \
             mock.patch.object(watchdog, "session_title", return_value="dagset"):
            done = watchdog.resolve_duplicate(self.decision(), now=now)
        notices = (side / "notices.jsonl").read_text(encoding="utf-8").splitlines() if (side / "notices.jsonl").exists() else []
        return done, stop.call_count, notices

    def test_resolve_keeps_move_and_stay_intents_and_only_clears_fails(self):
        at = time.time()
        intents = {"version": 1, "moves": {self.FULL: {"account": "b004", "target": "b003", "at": at}},
                   "stays": {self.FULL: {"at": at}},
                   "fails": {self.FULL: {"account": "b005", "n": 4, "until": at + 999}}}
        self.run_resolve((True, "ok"), now=at, intents=intents)
        data = watchdog.read_intents()
        self.assertEqual(data["fails"], {})
        self.assertIn(self.FULL, data["moves"])
        self.assertIn(self.FULL, data["stays"])

    def test_failed_stop_is_retried_only_after_the_interval_and_notified_once(self):
        t0 = 10_000.0
        done, calls, notices = self.run_resolve((False, "没停掉 b005 上的 cccccccc：boom"), now=t0)
        self.assertEqual((done, calls, len(notices)), (True, 1, 1))
        self.assertIn("没停掉", json.loads(notices[0])["text"])
        done, calls, _ = self.run_resolve((False, "boom"), now=t0 + watchdog.ROUND_SECONDS)   # 下一轮：限频，什么都不做
        self.assertEqual((done, calls), (False, 0))
        done, calls, notices = self.run_resolve((False, "boom"), now=t0 + watchdog.DUP_RETRY_SECONDS + 1)
        self.assertEqual((done, calls, len(notices)), (True, 1, 1))                               # 重试了，但不再发通知

    def test_success_after_failure_notifies_and_clears_the_retry_mark(self):
        t0 = 10_000.0
        self.run_resolve((False, "boom"), now=t0)
        _done, _calls, notices = self.run_resolve((True, "已清"), now=t0 + watchdog.DUP_RETRY_SECONDS + 1)
        self.assertEqual(len(notices), 2)
        self.assertNotIn((self.FULL, "b005"), watchdog._DUP_TRIED)  # noqa: SLF001

    def test_loop_resolves_a_duplicate_once_ignoring_the_cooldown_and_never_executes_a_move(self):
        loop = watchdog.Loop()
        loop.cooldown[self.FULL] = time.time() + 9999
        resolved = []
        with mock.patch.object(watchdog, "plan_round", return_value=[self.decision()]), \
             mock.patch.object(watchdog, "resolve_duplicate", side_effect=lambda d: resolved.append(d) or True), \
             mock.patch.object(watchdog, "execute") as execute, \
             mock.patch("cca.session.wake.run_due"), mock.patch("cca.session.wake.pending", return_value=[]):
            loop.round()
        self.assertEqual(len(resolved), 1)
        execute.assert_not_called()
        self.assertNotIn(self.FULL, loop.cooldown)

    def test_stop_copy_stops_then_removes_the_panel_entry(self):
        from cca.account import lifecycle  # noqa: PLC0415

        calls = []
        alive = {"b005": {self.FULL}}

        def fake_run(args, name, **_kw):
            calls.append((name, list(args)))
            if args[0] == "stop":
                alive[name].discard(self.FULL)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("cca.session.push.live_session_ids", side_effect=lambda n: set(alive.get(n, ()))), \
             mock.patch.object(lifecycle, "live_session_ids", side_effect=lambda n: set(alive.get(n, ()))), \
             mock.patch("cca.session.push._run_claude", side_effect=fake_run), \
             mock.patch.object(lifecycle, "_run_claude", side_effect=fake_run), \
             mock.patch.object(lifecycle, "profile_dir", side_effect=lambda n: self.root / n), \
             mock.patch.object(watchdog, "profile_dir", side_effect=lambda n: self.root / n), \
             mock.patch.object(watchdog, "process_config_dir", return_value=str(self.root / "b005")), \
             mock.patch.object(watchdog, "_alive", return_value=True):
            result = watchdog._stop_copy("b005", self.decision().drops[0], self.FULL)  # noqa: SLF001
        self.assertEqual(calls, [("b005", ["stop", "cccccccc"]), ("b005", ["rm", "cccccccc"])])
        self.assertTrue(result[0])
        self.assertIn("已清 b005", result[1])

    def test_stop_copy_respects_the_worktree_rule(self):
        from cca.account import lifecycle  # noqa: PLC0415

        job = self.root / "b005" / "jobs" / "cccccccc"
        job.mkdir(parents=True)
        (job / "state.json").write_text(json.dumps({"worktreePath": "/w/wt"}), encoding="utf-8")
        calls = []
        with mock.patch("cca.session.push.live_session_ids", return_value=set()), \
             mock.patch.object(lifecycle, "live_session_ids", return_value=set()), \
             mock.patch.object(lifecycle, "_run_claude", side_effect=lambda *a, **k: calls.append(a)), \
             mock.patch.object(lifecycle, "profile_dir", side_effect=lambda n: self.root / n), \
             mock.patch.object(watchdog, "profile_dir", side_effect=lambda n: self.root / n), \
             mock.patch.object(watchdog, "process_config_dir", return_value=""), \
             mock.patch.object(watchdog, "_alive", return_value=False):
            result = watchdog._stop_copy("b005", self.decision().drops[0], self.FULL)  # noqa: SLF001
        self.assertEqual(calls, [])
        self.assertIn("worktree", result[1])

    def wall(self, code):
        """撞墙的会话（MOVE_NOW 不走 recheck），move_session 返回 code；跑一遍 Loop._run。返回 (loop, outcome 里的 abandoned)。"""
        from cca.account import lifecycle  # noqa: PLC0415

        decision = watchdog.Decision(watchdog.MOVE_NOW, self.FULL, "b005", "dagset", "撞墙", target="b004", cwd="/w")
        entry = {"sessionId": self.FULL, "status": "idle"}
        loop = watchdog.Loop()
        with mock.patch.object(watchdog, "live_bg_sessions", return_value=[entry]), \
             mock.patch("cca.session.quota_hint.account_rows", return_value=[row("b005", 100), row("b004", 10)]), \
             mock.patch.object(watchdog, "shares_sessions", return_value=True), \
             mock.patch.object(watchdog, "notify_move"), mock.patch.object(watchdog, "session_title", return_value="t"), \
             mock.patch("cca.session.wake.forget"), \
             mock.patch.object(lifecycle, "target_problem", return_value=""), \
             mock.patch.object(lifecycle, "move_session", return_value=code):
            loop._run(decision, time.time())  # noqa: SLF001
        return loop

    def test_already_running_on_target_is_not_counted_as_a_failure(self):
        from cca.account import lifecycle  # noqa: PLC0415

        loop = self.wall(lifecycle.MOVE_ALREADY_THERE)
        self.assertEqual(watchdog.read_intents()["fails"], {})
        self.assertLess(loop.cooldown[self.FULL] - time.time(), 60)    # 只是短暂停一下，不是失败冷却
        self.assertTrue(any("重复会话" in line and "不计失败" in line for line in self.lines))

    def test_repeated_already_running_never_reaches_the_failure_cap(self):
        from cca.account import lifecycle  # noqa: PLC0415

        for _ in range(watchdog.MAX_FAILS + 2):
            self.wall(lifecycle.MOVE_ALREADY_THERE)
        self.assertEqual(watchdog.read_intents()["fails"], {})

    def test_a_failed_move_still_counts(self):
        self.wall(1)
        self.assertEqual(watchdog.read_intents()["fails"][self.FULL]["n"], 1)

    def test_old_failures_of_a_session_that_left_the_source_are_garbage_collected(self):
        """以前因为“已经在 X 上跑着了”记下的失败：会话不在源号上了（重复的那份被停掉）就清掉。"""
        watchdog._write_intents({"version": 1, "moves": {}, "stays": {},  # noqa: SLF001
                                 "fails": {self.FULL: {"account": "b005", "n": 4, "until": time.time() + 999}}})
        watchdog.gc_intents(set(), time.time())
        self.assertEqual(watchdog.read_intents()["fails"], {})


class LoopTest(AccountsCase):
    def decision(self, action=watchdog.ARMED, *, session="sid-1", idle=False, settled=False, **kw):
        return watchdog.Decision(action, session, "b005", "dagset", "5h 96%", target="b004",
                                 idle=idle, settled=settled, **kw)

    def run_rounds(self, rounds, *, moved=True):
        """rounds：每一轮 plan_round 的结果。返回 (循环, execute 收到的会话, wake.arm 的调用)。"""
        loop = watchdog.Loop()
        executed, armed = [], []
        plans = iter(rounds)
        with mock.patch.object(watchdog, "plan_round", side_effect=lambda *_a, **_k: next(plans)), \
             mock.patch.object(watchdog, "execute",
                               side_effect=lambda d, **_k: executed.append((d.session, d.action)) or moved), \
             mock.patch("cca.session.wake.run_due"), mock.patch("cca.session.wake.pending", return_value=[]), \
             mock.patch("cca.session.wake.arm", side_effect=lambda *a, **k: armed.append((a, k)) or True):
            for _ in rounds:
                loop.round()
        return loop, executed, armed

    def test_session_is_moved_on_the_second_consecutive_end(self):
        _loop, executed, _ = self.run_rounds([[self.decision(idle=True)], [self.decision(idle=True)]])
        self.assertEqual(executed, [("sid-1", watchdog.ARMED)])

    def test_one_end_is_not_enough_and_a_busy_reading_resets_the_count(self):
        _loop, executed, _ = self.run_rounds([[self.decision(idle=True)], [self.decision()],
                                              [self.decision(idle=True)]])
        self.assertEqual(executed, [])

    def test_transcript_end_counts_even_when_the_registry_says_busy(self):
        """后台 shell 还在跑：登记表是 shell / busy，对话记录显示这一轮结束了，也算。"""
        _loop, executed, _ = self.run_rounds([[self.decision(settled=True)], [self.decision(settled=True)]])
        self.assertEqual(len(executed), 1)

    def test_a_session_that_never_ends_its_turn_is_never_moved(self):
        _loop, executed, _ = self.run_rounds([[self.decision()]] * 5)
        self.assertEqual(executed, [])

    def test_walled_session_moves_at_once(self):
        _loop, executed, _ = self.run_rounds([[self.decision(watchdog.MOVE_NOW)]])
        self.assertEqual(executed, [("sid-1", watchdog.MOVE_NOW)])

    def test_failed_move_cools_down(self):
        loop, executed, _ = self.run_rounds([[self.decision(watchdog.MOVE_NOW)]] * 3, moved=False)
        self.assertEqual(len(executed), 1)
        self.assertGreater(loop.cooldown["sid-1"], time.time() + 200)

    def test_at_most_three_moves_per_round(self):
        batch = [self.decision(watchdog.MOVE_NOW, session=f"sid-{n}") for n in range(5)]
        _loop, executed, _ = self.run_rounds([batch])
        self.assertEqual(len(executed), watchdog.MAX_MOVES_PER_ROUND)

    def test_a_persisted_failure_cools_down_a_restarted_loop(self):
        watchdog.record_failure("b005", "sid-1", time.time())
        loop, executed, _ = self.run_rounds([[self.decision(watchdog.MOVE_NOW)]])
        self.assertEqual(executed, [])
        self.assertGreater(loop.cooldown["sid-1"], time.time())

    def test_abandoning_is_not_counted_as_a_failure(self):
        loop = watchdog.Loop()
        with mock.patch.object(watchdog, "execute",
                               side_effect=lambda d, outcome=None, **_k: outcome.update(abandoned=True) or False):
            loop._run(self.decision(), time.time())  # noqa: SLF001
        self.assertEqual(watchdog.read_intents()["fails"], {})

    def test_recovered_wall_is_told_to_wake_only_once_per_cooldown(self):
        _loop, _, armed = self.run_rounds([[self.decision(watchdog.WAKE_NOW)]] * 3)
        self.assertEqual(len(armed), 1)

    def test_session_that_dropped_out_is_forgotten(self):
        loop, _, _ = self.run_rounds([[self.decision(idle=True)], []])
        self.assertEqual(loop.armed, {})

    def test_everyone_full_arms_a_conditional_wake_at_the_reset(self):
        resets = time.time() + HOUR
        _loop, executed, armed = self.run_rounds([[self.decision(watchdog.WAIT_WAKE, resets_at=resets)]])
        self.assertEqual(executed, [])
        (args, kwargs), = armed
        self.assertEqual((args, kwargs["resets_at"]), (("b005", "sid-1"), resets))

    def test_recovered_wall_asks_wake_to_tell_it_now(self):
        _loop, executed, armed = self.run_rounds([[self.decision(watchdog.WAKE_NOW)]])
        self.assertEqual(executed, [])
        self.assertLess(armed[0][1]["resets_at"], time.time())     # 已经到点：同一轮 run_due 就叫

    def test_poll_armed_moves_after_two_quiet_readings_and_acts_on_a_wall(self):
        loop = watchdog.Loop()
        loop.armed["sid-1"] = {"decision": self.decision(), "idle": 0, "settled": 0}
        executed = []
        turns = iter(["ended", "ended"])
        with mock.patch("cca.session.takeover.registry_entry", return_value={"status": "shell"}), \
             mock.patch.object(watchdog, "turn_state", side_effect=lambda *_: next(turns)), \
             mock.patch.object(watchdog, "execute", side_effect=lambda d, **_k: executed.append(d.action) or True):
            loop.poll_armed()
            self.assertEqual(executed, [])
            loop.poll_armed()
        self.assertEqual(executed, [watchdog.ARMED])
        loop.armed["sid-1"] = {"decision": self.decision(), "idle": 0, "settled": 0}
        with mock.patch("cca.session.takeover.registry_entry", return_value={"status": "busy"}), \
             mock.patch.object(watchdog, "turn_state", return_value="walled"), \
             mock.patch.object(watchdog, "execute", side_effect=lambda d, **_k: executed.append(d.action) or True):
            loop.poll_armed()
        self.assertEqual(executed[-1], watchdog.MOVE_NOW)

    def test_poll_armed_drops_a_session_that_vanished(self):
        loop = watchdog.Loop()
        loop.armed["sid-1"] = {"decision": self.decision(), "idle": 1, "settled": 0}
        with mock.patch("cca.session.takeover.registry_entry", return_value={}):
            loop.poll_armed()
        self.assertEqual(loop.armed, {})


class ExecuteTest(AccountsCase):
    SID = "abcdef12-0000"

    def go(self, action=watchdog.ARMED, *, rows=None, turn="ended", live=True, problems=None, code=0, intents=None, outcome=None):  # noqa: PLR0913 - 夹具造数据的开关
        from cca.account import lifecycle  # noqa: PLC0415

        decision = watchdog.Decision(action, self.SID, "b005", "dagset", "5h 96%", target="b004", cwd="/w")
        rows = rows or [row("b005", 96), row("b004", 10), row("b003", 20)]
        problems = problems or {}
        moved, notices, armed, forgotten = [], [], [], []
        if intents:
            watchdog._write_intents(intents)  # noqa: SLF001
        entry = {"sessionId": self.SID, "status": "busy", "statusUpdatedAt": time.time() * 1000}
        with mock.patch.object(watchdog, "live_bg_sessions", return_value=[entry] if live else []), \
             mock.patch("cca.session.quota_hint.account_rows", return_value=rows), \
             mock.patch.object(watchdog, "turn_state", return_value=turn), \
             mock.patch.object(watchdog, "shares_sessions", return_value=True), \
             mock.patch.object(watchdog, "session_title", return_value="dagset"), \
             mock.patch.object(watchdog, "notify_move", side_effect=lambda *a: notices.append(a)), \
             mock.patch.object(lifecycle, "target_problem", side_effect=lambda t: problems.get(t, "")), \
             mock.patch.object(lifecycle, "move_session",
                               side_effect=lambda *a, **k: moved.append((a, k)) or code), \
             mock.patch("cca.session.wake.forget", side_effect=forgotten.append), \
             mock.patch("cca.session.wake.arm", side_effect=lambda *a, **k: armed.append((a, k)) or True):
            result = watchdog.execute(decision, outcome=outcome)
        return result, moved, notices, armed, forgotten

    def test_success_moves_notifies_and_forgets(self):
        intents = {"version": 1, "stays": {}, "moves": {self.SID: {"account": "b005", "target": "b004", "at": 1}}}
        result, moved, notices, _, forgotten = self.go(intents=intents)
        self.assertTrue(result)
        self.assertEqual(moved, [(("b005", "b004", self.SID), {"prechecked": True})])
        self.assertEqual(notices, [("b005", "b004", "dagset")])
        self.assertEqual(forgotten, [self.SID])
        self.assertEqual(watchdog.read_intents()["moves"], {})
        self.assertTrue(any("已搬：b005 → b004 dagset (abcdef12)" in line for line in self.lines))

    def test_source_no_longer_hot_means_no_move(self):
        outcome = {}
        result, moved, *_ = self.go(rows=[row("b005", 50), row("b004", 10)], outcome=outcome)
        self.assertEqual((result, moved, outcome), (False, [], {"abandoned": True}))

    def test_a_new_turn_that_started_since_the_round_means_no_move(self):
        result, moved, *_ = self.go(turn="running")
        self.assertEqual((result, moved), (False, []))

    def test_a_stay_written_after_the_round_is_honoured(self):
        """模型在提醒后最后一步运行 --stay：登记写进去时旧结论还在 armed 里，搬之前必须重读。"""
        resets = [w["resets_at"] for w in win(96).values()]
        stay = {"account": "b005", "at": 1, "windows": {"five_hour": resets[0]}}
        intents = {"version": 1, "moves": {}, "stays": {self.SID: stay}}
        outcome = {}
        result, moved, *_ = self.go(intents=intents, outcome=outcome)
        self.assertEqual((result, moved, outcome), (False, [], {"abandoned": True}))

    def test_requested_move_goes_even_when_the_source_is_not_at_the_threshold(self):
        intents = {"version": 1, "stays": {}, "moves": {self.SID: {"account": "b005", "target": "b003", "at": 1}}}
        result, moved, *_ = self.go(rows=[row("b005", 50), row("b004", 10), row("b003", 20)], intents=intents)
        self.assertEqual((result, moved[0][0][:2]), (True, ("b005", "b003")))

    def test_session_gone_means_no_move(self):
        result, moved, *_ = self.go(live=False)
        self.assertEqual((result, moved), (False, []))

    def test_target_that_fails_the_precheck_is_skipped_for_the_next(self):
        result, moved, *_ = self.go(problems={"b004": "目标号额度已经用满"})
        self.assertTrue(result)
        self.assertEqual(moved[0][0][1], "b003")

    def test_no_target_passes_the_precheck(self):
        result, moved, notices, *_ = self.go(problems={"b004": "x", "b003": "y"})
        self.assertEqual((result, moved, notices), (False, [], []))

    def test_failed_move_reports_false_without_a_notice(self):
        result, moved, notices, *_ = self.go(code=1)
        self.assertEqual((result, len(moved), notices), (False, 1, []))

    def test_walled_session_moves_even_when_the_source_is_not_hot(self):
        result, moved, *_ = self.go(watchdog.MOVE_NOW, rows=[row("b005", 100), row("b004", 10)], turn="running")
        self.assertEqual((result, moved[0][0][:2]), (True, ("b005", "b004")))

    def test_walled_but_source_recovered_is_handed_to_wake(self):
        result, moved, _, armed, _ = self.go(watchdog.MOVE_NOW, rows=[row("b005", 30, 30), row("b004", 10)])
        self.assertEqual((result, moved), (False, []))
        self.assertEqual(armed[0][0], ("b005", self.SID))


class SingletonTest(AccountsCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"CCA_WATCHDOG_AUTOSTART": "1"})   # 单例测试要真的走拉起逻辑（Popen 照样 mock）
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_autostart_is_off_under_unittest_unless_asked(self):
        with mock.patch.dict(os.environ, {"CCA_WATCHDOG_AUTOSTART": ""}), \
             mock.patch.object(watchdog.subprocess, "Popen") as popen:
            self.assertFalse(_REAL_ENSURE())
            popen.assert_not_called()

    def test_ensure_running_spawns_once_and_respects_the_stamp(self):
        fn = _REAL_ENSURE
        with mock.patch.object(watchdog.subprocess, "Popen") as popen:
            self.assertTrue(fn())
            self.assertFalse(fn())                       # 10 秒内的第二次：不再 fork
            self.assertEqual(popen.call_count, 1)
            argv = popen.call_args.args[0]
            self.assertIn("from cca.session.watchdog import daemon", argv[-1])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_ensure_running_does_nothing_while_the_lock_is_held(self):
        holder = watchdog._try_lock()  # noqa: SLF001
        self.addCleanup(lambda: holder and watchdog._release(holder))  # noqa: SLF001
        with mock.patch.object(watchdog.subprocess, "Popen") as popen:
            self.assertFalse(_REAL_ENSURE())
            popen.assert_not_called()
            self.assertTrue(watchdog.running())
        watchdog._release(holder)  # noqa: SLF001
        holder = None
        self.assertFalse(watchdog.running())

    def test_probing_never_blocks_the_daemon_or_other_probes(self):
        probe = watchdog._try_lock(shared=True)  # noqa: SLF001
        self.assertIsNotNone(watchdog._try_lock(shared=True))  # noqa: SLF001 - 两个探测互不挡
        threading.Timer(0.3, lambda: watchdog._release(probe)).start()  # noqa: SLF001
        daemon_lock = watchdog._try_lock(wait=3)  # noqa: SLF001 - 探测方瞬间占锁：常驻进程重试后抢到
        self.assertIsNotNone(daemon_lock)
        self.assertTrue(watchdog.running())
        watchdog._release(daemon_lock)  # noqa: SLF001

    def test_a_second_daemon_exits_quietly(self):
        holder = watchdog._try_lock()  # noqa: SLF001
        self.addCleanup(lambda: watchdog._release(holder))  # noqa: SLF001
        with mock.patch.object(watchdog, "LOCK_WAIT_SECONDS", 0), mock.patch.object(watchdog, "Loop") as loop:
            self.assertEqual(watchdog.daemon(), 0)
            loop.assert_not_called()

    def test_daemon_exits_when_nothing_is_left_and_frees_the_lock(self):
        with mock.patch.object(watchdog.Loop, "round", return_value=False), \
             mock.patch.object(watchdog.Loop, "should_exit", return_value=True), \
             mock.patch.object(watchdog, "has_work", return_value=False):
            self.assertEqual(watchdog.daemon(), 0)
        self.assertFalse(watchdog.running())
        self.assertFalse((self.root / "watchdog.pid").exists())

    def test_daemon_keeps_going_when_work_shows_up_before_it_leaves(self):
        with mock.patch.object(watchdog.Loop, "round", return_value=False) as rounds, \
             mock.patch.object(watchdog.Loop, "should_exit", return_value=True), \
             mock.patch.object(watchdog, "has_work", side_effect=[True, False]), \
             mock.patch.object(watchdog.time, "time", side_effect=itertools.count(1000.0, 1000.0).__next__), \
             mock.patch.object(watchdog.Loop, "sleep"):
            self.assertEqual(watchdog.daemon(), 0)
        self.assertGreaterEqual(rounds.call_count, 2)

    def test_exit_needs_ten_quiet_rounds_and_the_start_grace(self):
        loop = watchdog.Loop()
        loop.started = 1000.0
        for _ in range(watchdog.QUIET_ROUNDS - 1):
            self.assertFalse(loop.should_exit(busy=False, now=1000 + 10 * 3600))
        self.assertTrue(loop.should_exit(busy=False, now=1000 + 10 * 3600))
        loop.should_exit(busy=True, now=1000 + 10 * 3600)                   # 来活了：重新数
        self.assertFalse(loop.should_exit(busy=False, now=1000 + 10 * 3600))
        young = watchdog.Loop()
        young.quiet = 99
        self.assertFalse(young.should_exit(busy=False, now=young.started + 60))      # 刚起：5 分钟内不退

    def test_has_work_sees_sessions_wakes_and_intents(self):
        with mock.patch.object(watchdog, "subscription_profiles", return_value=["b005"]), \
             mock.patch.object(watchdog, "live_bg_sessions", return_value=[]), \
             mock.patch("cca.session.wake.pending", return_value=[]):
            self.assertFalse(watchdog.has_work())
            with mock.patch.object(watchdog, "live_bg_sessions", return_value=[{"sessionId": "x"}]):
                self.assertTrue(watchdog.has_work())
            with mock.patch("cca.session.wake.pending", return_value=[("x", {})]):
                self.assertTrue(watchdog.has_work())
            watchdog._write_intents({"version": 1, "moves": {"x": {}}, "stays": {}})  # noqa: SLF001
            self.assertTrue(watchdog.has_work())


class StatusTest(AccountsCase):
    def render(self, plans, rows, **kw):
        with mock.patch.object(watchdog, "subscription_profiles", return_value=["b005", "b004"]):
            return watchdog.render_status(plans, rows, **kw)

    def sample(self):
        make = watchdog.Decision
        plans = [make(watchdog.ARMED, "3da86403-x", "b005", "dagset", "5h 96%", target="b004", target_why="余量充足"),
                 make(watchdog.MOVE_NOW, "aaaa1111-x", "b005", "x", "撞墙", target="b003", target_why="余量充足"),
                 make(watchdog.SKIP, "bbbb2222-x", "b005", "y", "--stay 至 14:30", notable=True),
                 make(watchdog.SKIP, "cccc3333-x", "b005", "z", "卡住的窗口 12 分钟后重置", notable=True),
                 make(watchdog.SKIP, "dddd4444-x", "b005", "w", "闲置 48 分钟", notable=True),
                 make(watchdog.SKIP, "eeee5555-x", "b004", "q", "没到提醒点")]
        return plans, [row("b005", 96, 61), row("b004", 23, 40)]

    def test_status_sections(self):
        plans, rows = self.sample()
        with mock.patch("cca.session.wake.pending", return_value=[
                ("3da86403-y", {"account": "b001", "name": "name", "wake_time": "10-12 08:00",
                                "only_if_limited": True})]):
            text = self.render(plans, rows, process="watchdog: 没在跑")
        for needle in ("watchdog: 没在跑", "额度（阈值 5h 95 / 7d 98）", "b005  5h 96%  7d 61%  → 到提醒点",
                       "b004  5h 23%  7d 40%  余量充足", "下一步",
                       "dagset (3da86403)  b005 → b004   这一轮结束后搬；原因：5h 96%；目标：余量充足",
                       "x (aaaa1111)  b005 → b003   立即搬；原因：撞墙", "不搬", "--stay 至 14:30",
                       "卡住的窗口 12 分钟后重置", "闲置 48 分钟", "另有 1 条会话所在的号没到提醒点",
                       "待叫醒", "b001  3da86403  name  10-12 08:00  （撞墙才叫）"):
            self.assertIn(needle, text)
        self.assertNotIn("只看不做", text)

    def test_dry_run_marks_the_preview(self):
        plans, rows = self.sample()
        with mock.patch("cca.session.wake.pending", return_value=[]):
            text = self.render(plans, rows, dry_run=True)
        self.assertIn("下一步  [只看不做]", text)

    def test_cmd_dry_run_reads_the_cache_and_touches_nothing(self):
        plans, rows = self.sample()
        out = []
        with mock.patch.object(watchdog, "snapshot", return_value=(plans, rows)) as snap, \
             mock.patch.object(watchdog, "subscription_profiles", return_value=["b005", "b004"]), \
             mock.patch("cca.session.wake.pending", return_value=[]), \
             mock.patch.object(watchdog, "say", out.append), \
             mock.patch.object(watchdog, "execute") as execute, \
             mock.patch.object(watchdog.subprocess, "Popen") as popen:
            self.assertEqual(watchdog.cmd_watchdog(["--dry-run"]), 0)
        snap.assert_called_once_with(fresh=False)
        execute.assert_not_called()
        popen.assert_not_called()
        self.ensure.assert_not_called()
        self.assertIn("[只看不做]", out[0])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [])        # 不写任何文件

    def test_cmd_status_reports_the_process(self):
        out = []
        with mock.patch.object(watchdog, "snapshot", return_value=([], [])), \
             mock.patch.object(watchdog, "subscription_profiles", return_value=[]), \
             mock.patch("cca.session.wake.pending", return_value=[]), \
             mock.patch.object(watchdog, "say", out.append):
            watchdog.cmd_watchdog([])
            self.assertTrue(out[-1].startswith("watchdog: 没在跑"))
            holder = watchdog._try_lock()  # noqa: SLF001
            try:
                (self.root / "watchdog.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
                watchdog.cmd_watchdog(["status"])
            finally:
                watchdog._release(holder)  # noqa: SLF001
        self.assertIn(f"在跑 pid {os.getpid()}", out[-1])

    def test_old_flags_are_gone(self):
        for args in (["--current"], ["order", "002"], ["--order", "002"], ["bogus"]):
            with self.assertRaises(SystemExit):
                watchdog.cmd_watchdog(args)


class SourceRecoveredTest(unittest.TestCase):
    def test_recovered_needs_every_live_window_well_below_its_threshold(self):
        now = time.time()
        self.assertTrue(watchdog.source_recovered(win(10, 10), LIMITS, now))
        self.assertFalse(watchdog.source_recovered(win(five=90), LIMITS, now))      # 95 - 15 = 80
        self.assertFalse(watchdog.source_recovered({}, LIMITS, now))


class NotifyTest(unittest.TestCase):
    def test_notice_text_is_title_and_move(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(watchdog, "sideagent_dir", return_value=Path(tmp)):
            watchdog.notify_move("b005", "b004", "dagset")
            record = json.loads((Path(tmp) / "notices.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(record["text"], "dagset：b005 → b004")



class BadTargetTest(AccountsCase):
    """搬不进去的号记进 bad，下一轮换别的号（11-11 train-h004 四次都撞 001，然后被永久放弃）。"""

    def test_failed_target_is_remembered_and_skipped(self):
        watchdog.record_failure("006", "sid-9", 1000.0, target="001")
        watchdog.record_failure("006", "sid-9", 2000.0, target="b003")
        fails = watchdog.read_intents()["fails"]["sid-9"]
        self.assertEqual(fails["bad"], ["001", "b003"])
        self.assertEqual(fails["n"], 2)
