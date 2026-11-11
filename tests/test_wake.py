"""cca wake：唤醒时刻取这个号的额度重置时间，只叫排上的人；到点的条目由常驻的 watchdog 每轮调 run_due 处理。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.session import wake


def rows(five=0, five_reset=None, seven=0, seven_reset=None):
    return [{"profile": "001", "usage": {"windows": {
        "five_hour": {"label": "5 小时", "percent": five, "resets_at": five_reset, "reset_passed": False},
        "seven_day": {"label": "7 天", "percent": seven, "resets_at": seven_reset, "reset_passed": False},
    }}}]


class ResetTimeTest(unittest.TestCase):
    def reset(self, data):
        with mock.patch("cca.report.list_rows", return_value=data):
            return wake.reset_time("001")

    def test_no_full_window(self):
        self.assertEqual(self.reset(rows(five=50, seven=30)), (0.0, []))

    def test_full_window_time(self):
        self.assertEqual(self.reset(rows(five=100, five_reset=1000.0, seven=30)), (1000.0, ["5 小时"]))

    def test_both_full_takes_the_later(self):
        moment, labels = self.reset(rows(five=100, five_reset=1000.0, seven=100, seven_reset=2000.0))
        self.assertEqual(moment, 2000.0)
        self.assertEqual(sorted(labels), ["5 小时", "7 天"])

    def test_passed_window_is_not_full(self):
        data = rows(five=100, five_reset=1000.0)
        data[0]["usage"]["windows"]["five_hour"]["reset_passed"] = True
        self.assertEqual(self.reset(data), (0.0, []))


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "wake.json"

    def run_schedule(self, data, **kwargs):
        with mock.patch.object(wake, "_state_path", return_value=self.state), \
                mock.patch("cca.report.list_rows", return_value=data), \
                mock.patch("cca.session.watchdog.ensure_running"), \
                mock.patch.object(wake, "say"):
            return wake.schedule("001", [{"full": "abc-full", "id": "abc", "name": "x", "cwd": "/w"}], **kwargs)

    def test_schedules_at_reset(self):
        self.assertEqual(self.run_schedule(rows(five=100, five_reset=time.time() + 3600)), 1)
        entry = json.loads(self.state.read_text(encoding="utf-8"))["abc-full"]
        self.assertEqual(entry["account"], "001")
        self.assertGreater(entry["wake_at"], time.time())

    def test_refuses_when_nothing_full(self):
        self.assertEqual(self.run_schedule(rows(five=10, seven=20)), 0)
        self.assertFalse(self.state.exists())

    def test_at_fallback(self):
        self.assertEqual(self.run_schedule(rows(five=10), at="08:00"), 1)


class RunDueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "wake.json"
        patch = mock.patch.object(wake, "_state_path", return_value=self.state)
        patch.start()
        self.addCleanup(patch.stop)

    def due(self, entry, *, live=(), limited=True, tell_error="", turn="running"):
        """live：[(号, 状态)]，它在哪些号上有活着的进程。返回 (动作记录, 剩下的预约)。"""
        self.state.write_text(json.dumps({"sid-9": entry}), encoding="utf-8")
        done = []
        with mock.patch.object(wake, "_live_entries",
                               return_value=[(name, {"status": status}) for name, status in live]), \
             mock.patch("cca.session.watchdog.last_assistant_record", return_value={}), \
             mock.patch("cca.session.watchdog.hits_limit", return_value=limited), \
             mock.patch("cca.session.watchdog.turn_state", return_value=turn), \
             mock.patch.object(wake, "_wake", side_effect=lambda e, sid, **_: done.append("resume") or ""), \
             mock.patch.object(wake, "_tell", side_effect=lambda reg: done.append("tell") or tell_error), \
             mock.patch.object(wake, "say"), mock.patch.object(wake, "note"):
            wake.run_due()
        return done, json.loads(self.state.read_text(encoding="utf-8"))

    @staticmethod
    def conditional():
        return {"account": "b004", "wake_at": 1.0, "only_if_limited": True}

    def test_walled_session_with_background_tasks_is_told(self):
        """撞墙停下但挂着后台任务（登记表一直是 shell）：不算在忙，额度恢复后照样发话叫醒。"""
        entry = {**self.conditional(), "wake_at": time.time() - 10}
        self.assertEqual(self.due(entry, live=[("b004", "shell")], turn="walled")[0], ["tell"])
        self.assertEqual(self.due(entry, live=[("b004", "shell")], turn="running")[0], [])

    def test_wakes_due_and_drops_entry(self):
        entry = {"account": "001", "wake_at": time.time() - 10, "wake_time": "x", "name": "x"}
        self.assertEqual(self.due(entry), (["resume"], {}))

    def test_future_entry_is_left(self):
        entry = {"account": "001", "wake_at": time.time() + 600}
        self.assertEqual(self.due(entry), ([], {"sid-9": entry}))

    def test_idle_live_session_gets_a_message_not_a_copy(self):
        """撞墙后进程还在、闲着：发一句话让它接着做；--bg --resume 会复制一份。"""
        entry = {"account": "001", "wake_at": time.time() - 10}
        self.assertEqual(self.due(entry, live=[("001", "idle")]), (["tell"], {}))

    def test_a_failed_message_keeps_the_entry(self):
        entry = {"account": "001", "wake_at": time.time() - 10}
        done, left = self.due(entry, live=[("001", "idle")], tell_error="发不出去")
        self.assertEqual(done, ["tell"])
        self.assertEqual(left["sid-9"]["tries"], 1)                      # 记一次失败，退避后再试
        self.assertGreater(left["sid-9"]["retry_at"], time.time())

    def test_an_entry_in_backoff_is_not_retried_and_is_dropped_after_max_tries(self):
        waiting = {"account": "001", "wake_at": time.time() - 10, "retry_at": time.time() + 600}
        self.assertEqual(self.due(waiting, live=[("001", "idle")]), ([], {"sid-9": waiting}))
        last = {"account": "001", "wake_at": time.time() - 10, "tries": wake.MAX_TRIES - 1}
        self.assertEqual(self.due(last, live=[("001", "idle")], tell_error="x"), (["tell"], {}))

    def test_running_session_is_kept_for_a_person_scheduled_wake(self):
        entry = {"account": "001", "wake_at": time.time() - 10}
        self.assertEqual(self.due(entry, live=[("001", "busy")]), ([], {"sid-9": entry}))

    def test_stuck_and_gone_is_resumed(self):
        self.assertEqual(self.due(self.conditional()), (["resume"], {}))

    def test_stuck_but_process_still_there_gets_a_message(self):
        self.assertEqual(self.due(self.conditional(), live=[("b004", "idle")]), (["tell"], {}))

    def test_finished_normally_is_dropped_without_a_call(self):
        self.assertEqual(self.due(self.conditional(), live=[("b004", "idle")], limited=False), ([], {}))

    def test_still_busy_conditional_entry_is_dropped(self):
        self.assertEqual(self.due(self.conditional(), live=[("b004", "busy")]), ([], {}))

    def test_moved_to_another_account_is_dropped(self):
        self.assertEqual(self.due(self.conditional(), live=[("b001", "idle")]), ([], {}))

    def test_entry_rearmed_during_delivery_is_not_deleted(self):
        entry = {"account": "001", "wake_at": time.time() - 10}
        self.state.write_text(json.dumps({"sid-9": entry}), encoding="utf-8")

        def rearm(_registry):
            self.state.write_text(json.dumps({"sid-9": {**entry, "wake_at": time.time() + 3600}}), encoding="utf-8")
            return ""

        with mock.patch.object(wake, "_live_entries", return_value=[("001", {"status": "idle"})]), \
             mock.patch.object(wake, "_tell", side_effect=rearm), \
             mock.patch.object(wake, "say"), mock.patch.object(wake, "note"):
            wake.run_due()
        self.assertIn("sid-9", json.loads(self.state.read_text(encoding="utf-8")))

    def test_delivery_happens_outside_the_state_lock(self):
        entry = {"account": "001", "wake_at": time.time() - 10}
        self.state.write_text(json.dumps({"sid-9": entry}), encoding="utf-8")
        seen = []

        def deliver(_registry):
            with wake._state_lock():          # noqa: SLF001 - 锁外投递：这里拿得到锁，不会死等
                seen.append(True)
            return ""

        with mock.patch.object(wake, "_live_entries", return_value=[("001", {"status": "idle"})]), \
             mock.patch.object(wake, "_tell", side_effect=deliver), \
             mock.patch.object(wake, "say"), mock.patch.object(wake, "note"):
            wake.run_due()
        self.assertEqual(seen, [True])

    def test_dry_run_lists_only_and_changes_nothing(self):
        entry = {"account": "001", "wake_at": time.time() - 10, "only_if_limited": True, "name": "n"}
        self.state.write_text(json.dumps({"sid-9": entry}), encoding="utf-8")
        said = []
        with mock.patch.object(wake, "_wake") as woke, mock.patch.object(wake, "_tell") as told, \
             mock.patch.object(wake, "say", said.append):
            wake.run_due(dry_run=True)
        woke.assert_not_called()
        told.assert_not_called()
        self.assertTrue(any("已到期" in line and "撞墙才叫" in line for line in said))
        self.assertIn("sid-9", json.loads(self.state.read_text(encoding="utf-8")))


class ArmTest(unittest.TestCase):
    """watchdog 替撞墙又没号可去的会话预约有条件的叫醒；人手排的不被覆盖，同一时刻不重排。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(wake, "accounts_root", return_value=Path(self.tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_arm_once_and_keep_a_person_scheduled_wake(self):
        resets = time.time() + 3600
        self.assertTrue(wake.arm("b004", "sid-1", resets_at=resets, name="n", cwd="/work"))
        self.assertFalse(wake.arm("b004", "sid-1", resets_at=resets))      # 同一时刻不重排
        entry = wake._read_state()["sid-1"]  # noqa: SLF001
        self.assertTrue(entry["only_if_limited"])
        self.assertEqual(int(entry["wake_at"]), int(resets + wake.RESET_MARGIN_SECONDS))
        wake._write_state({"sid-2": {"account": "b004", "wake_at": 1.0}})  # noqa: SLF001 - 人排的无条件叫醒
        self.assertFalse(wake.arm("b004", "sid-2", resets_at=resets))
        self.assertNotIn("only_if_limited", wake._read_state()["sid-2"])  # noqa: SLF001

    def test_old_entries_without_new_fields_still_read(self):
        """wake.json 格式没变：旧条目原样兼容。"""
        old = {"sid-old": {"account": "001", "wake_at": 5.0, "wake_time": "10-12 08:00", "name": "x", "cwd": "/w"}}
        wake._write_state(old)  # noqa: SLF001
        self.assertEqual(wake.pending(), [("sid-old", old["sid-old"])])

    def test_pending_is_sorted_and_forget_removes(self):
        wake._write_state({"b": {"wake_at": 20.0}, "a": {"wake_at": 10.0}})  # noqa: SLF001
        self.assertEqual([sid for sid, _ in wake.pending()], ["a", "b"])
        self.assertTrue(wake.forget("a"))
        self.assertFalse(wake.forget("a"))
        self.assertEqual([sid for sid, _ in wake.pending()], ["b"])


class NoOwnDaemonTest(unittest.TestCase):
    def test_wake_has_no_process_of_its_own(self):
        for name in ("daemon", "running", "ensure_running", "stop", "run_once"):
            self.assertFalse(hasattr(wake, name), name)

    def test_schedule_pulls_up_the_watchdog(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(wake, "_state_path", return_value=Path(tmp) / "wake.json"), \
                mock.patch("cca.report.list_rows", return_value=rows(five=100, five_reset=time.time() + 3600)), \
                mock.patch("cca.session.watchdog.ensure_running") as ensure, \
                mock.patch.object(wake, "say"):
            wake.schedule("001", [{"full": "abc-full", "id": "abc", "name": "x", "cwd": "/w"}])
        ensure.assert_called_once_with()

    def test_stop_is_no_longer_a_wake_subcommand(self):
        with mock.patch("cca.account.model.discover_profiles", return_value=["001"]), \
                self.assertRaises(SystemExit):
            wake.cmd_wake(["stop"])


if __name__ == "__main__":
    unittest.main()
