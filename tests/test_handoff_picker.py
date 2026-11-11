"""cca handoff 的交互选择框：questionary 一律用 mock 替掉，不真起交互。"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.account import handoff_picker, lifecycle


def session(index: int, *, running: bool = False, movable: bool = True, reason: str = "",
            state: str = "blocked") -> dict:
    return {
        "profile": "001", "id": f"id{index}", "full": f"id{index}-full", "state": state,
        "name": f"会话{index}", "cwd": "/work", "running": running, "completed": False,
        "movable": movable, "reason": reason, "active_at": "2026-01-01T00:00:00+00:00",
    }


def row(name: str, *, five: int = 10, seven: int = 20, kind: str = "oauth", logged_in: bool = True) -> dict:
    windows = {} if kind == "api" else {
        "five_hour": {"percent": five}, "seven_day": {"percent": seven}}
    return {"profile": name, "kind": kind, "logged_in": logged_in,
            "usage": {"windows": windows}, "background": {"running": 1}}


class TtyOut(io.StringIO):
    """假装是终端的 stdout。"""

    def isatty(self):
        return True


def asking(value):
    """questionary.xxx(...) 的替身：.ask() 返回 value。"""
    return mock.Mock(return_value=mock.Mock(ask=mock.Mock(return_value=value)))


class ClipTest(unittest.TestCase):
    def test_pads_and_truncates_by_display_width(self):
        self.assertEqual(handoff_picker.clip("ab", 5), "ab   ")
        clipped = handoff_picker.clip("中文标题很长很长", 9)
        self.assertEqual(handoff_picker.display_width(clipped), 9)
        self.assertIn("…", clipped)


class SessionChoicesTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(handoff_picker, "_last_ask", return_value="帮我看下 foo")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_checked_and_unmovable_disabled_with_reason(self):
        items = [session(1), session(2, running=True), session(3, movable=False, reason="终端正开着")]
        choices = handoff_picker.session_choices(items, default=[1])
        # 第一行是“全选等输入/挂起的”，后面才是会话
        self.assertEqual([c.value for c in choices], [0, 1, 2, 3])
        self.assertEqual([bool(c.checked) for c in choices], [False, True, False, False])
        self.assertIsNone(choices[1].disabled)
        self.assertEqual(choices[3].disabled, "终端正开着")

    def test_line_has_title_state_ago_and_last_ask(self):
        line = handoff_picker.session_line(session(1))
        self.assertIn("会话1", line)
        self.assertIn("blocked", line)
        self.assertIn("天前", line)
        self.assertIn("帮我看下 foo", line)

    def test_unmovable_in_default_is_not_checked(self):
        choices = handoff_picker.session_choices([session(1, movable=False)], default=[1])
        self.assertFalse(choices[0].checked)


class PickSessionsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(handoff_picker, "_last_ask", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_checked_numbers(self):
        with mock.patch.object(handoff_picker.questionary, "checkbox", asking([1, 3])) as box:
            self.assertEqual(handoff_picker.pick_sessions([session(1), session(2), session(3)], [1]), [1, 3])
        self.assertEqual(len(box.call_args.kwargs["choices"]), 4)   # 3 条会话 + 一行“全选等输入的”

    def test_select_waiting_row_expands_to_blocked_and_idle(self):
        items = [session(1), session(2, state="done"), session(3, state="idle"),
                 session(4, state="blocked", movable=False)]
        with mock.patch.object(handoff_picker.questionary, "checkbox", asking([0])):
            self.assertEqual(handoff_picker.pick_sessions(items, []), [1, 3])

    def test_select_waiting_row_keeps_manually_checked_ones(self):
        items = [session(1), session(2, state="done")]
        with mock.patch.object(handoff_picker.questionary, "checkbox", asking([0, 2])):
            self.assertEqual(handoff_picker.pick_sessions(items, []), [1, 2])

    def test_nothing_checked_returns_empty(self):
        with mock.patch.object(handoff_picker.questionary, "checkbox", asking([])):
            self.assertEqual(handoff_picker.pick_sessions([session(1)], []), [])

    def test_ctrl_c_returns_none(self):
        with mock.patch.object(handoff_picker.questionary, "checkbox", asking(None)):
            self.assertIsNone(handoff_picker.pick_sessions([session(1)], [1]))

    def test_nothing_selectable_does_not_open_box(self):
        with mock.patch.object(handoff_picker.questionary, "checkbox") as box, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(handoff_picker.pick_sessions([session(1, movable=False)], []), [])
        box.assert_not_called()


ROWS = [row("001"), row("002", five=100), row("003", seven=40), row("iso"),
        row("ds", kind="api", logged_in=False), row("out", logged_in=False)]


class AccountChoicesTest(unittest.TestCase):
    rows = tuple(ROWS)

    @staticmethod
    def shares(name: str) -> bool:
        return name != "iso"

    def test_target_reasons_and_percent(self):
        choices = {c.value: c for c in handoff_picker.target_choices(self.rows, "001", self.shares)}
        self.assertEqual(choices["001"].disabled, "这是源号")
        self.assertEqual(choices["002"].disabled, "额度用满")
        self.assertIn("隔离号", choices["iso"].disabled)
        self.assertEqual(choices["out"].disabled, "没登录")
        self.assertIsNone(choices["003"].disabled)
        self.assertIn("5 小时 10%", choices["003"].title)
        self.assertIn("7 天 40%", choices["003"].title)
        self.assertIsNone(choices["ds"].disabled)

    def test_targets_ordered_by_weekly_reset(self):
        """订阅号按周额度最早重置排在前面，api 号在订阅号后面。"""
        import time  # noqa: PLC0415

        def at(name: str, hours: float) -> dict:
            out = row(name)
            out["usage"]["windows"]["seven_day"]["resets_at"] = time.time() + hours * 3600
            return out

        rows = [at("b001", 48), row("ds", kind="api"), at("b004", 3), row("b005")]
        order = [c.value for c in handoff_picker.target_choices(rows, "001", self.shares)]
        self.assertEqual(order, ["b004", "b001", "b005", "ds"])

    def test_source_disables_isolated(self):
        choices = {c.value: c for c in handoff_picker.source_choices(self.rows, self.shares)}
        self.assertIn("隔离号", choices["iso"].disabled)
        self.assertIsNone(choices["001"].disabled)

    def test_pick_pair_asks_source_then_target(self):
        with mock.patch.object(handoff_picker.questionary, "select",
                               side_effect=[mock.Mock(ask=lambda: "001"), mock.Mock(ask=lambda: "003")]) as sel:
            pair = handoff_picker.pick_pair(self.rows, self.shares)
        self.assertEqual(pair, ("001", "003"))
        self.assertEqual(sel.call_count, 2)

    def test_pick_pair_cancel_on_target(self):
        with mock.patch.object(handoff_picker.questionary, "select",
                               side_effect=[mock.Mock(ask=lambda: "001"), mock.Mock(ask=lambda: None)]):
            self.assertIsNone(handoff_picker.pick_pair(self.rows, self.shares))


class ConfirmTest(unittest.TestCase):
    def test_yes_no_and_ctrl_c(self):
        for answer, expected in ((True, True), (False, False), (None, False)):
            with mock.patch.object(handoff_picker.questionary, "confirm", asking(answer)):
                self.assertEqual(handoff_picker.confirm_move(2, "001", "002"), expected)


class HandoffSelectionTest(unittest.TestCase):
    """lifecycle._handoff_selection：交互路径走选择框，其余路径不碰它。"""

    def setUp(self):
        self.items = [session(1), session(2, running=True), session(3)]
        self.console = mock.Mock()
        plan = mock.patch.object(lifecycle, "handoff_plan")
        self.plan = plan.start()
        self.addCleanup(plan.stop)
        out = mock.patch.object(lifecycle, "say")
        self.say = out.start()
        self.addCleanup(out.stop)

    def run_sel(self, opts, *, picked=None, confirmed=True):
        with mock.patch.object(handoff_picker, "pick_sessions", return_value=picked) as pick, \
                mock.patch.object(handoff_picker, "confirm_move", return_value=confirmed) as confirm:
            result = lifecycle._handoff_selection(self.console, self.items, "002", opts)  # noqa: SLF001
        return result, pick, confirm

    def test_all_only_and_dry_run_never_ask(self):
        for opts, expected in (((None, True, False, "001"), {1, 2, 3}),
                               (([], False, True, "001"), set()),
                               ((["id2"], False, False, "001"), {2})):
            result, pick, confirm = self.run_sel(opts)
            self.assertEqual(result, expected)
            pick.assert_not_called()
            confirm.assert_not_called()

    def test_default_is_empty(self):
        """默认一个都不勾：回车不会顺手搬走一堆早就做完的会话。"""
        _result, pick, _confirm = self.run_sel(([], False, False, "001"), picked=[1])
        self.assertEqual(pick.call_args.args, (self.items, []))

    def test_picked_then_plan_then_confirm(self):
        result, _pick, confirm = self.run_sel(([], False, False, "001"), picked=[1, 3])
        self.assertEqual(result, {1, 3})
        self.plan.assert_called_once_with(self.console, self.items, "002", {1, 3})
        confirm.assert_called_once_with(2, "001", "002")

    def test_ctrl_c_in_list_moves_nothing(self):
        result, _pick, confirm = self.run_sel(([], False, False, "001"), picked=None)
        self.assertEqual(result, set())
        confirm.assert_not_called()
        self.plan.assert_not_called()

    def test_empty_pick_moves_nothing(self):
        result, _pick, confirm = self.run_sel(([], False, False, "001"), picked=[])
        self.assertEqual(result, set())
        confirm.assert_not_called()

    def test_declined_confirm_moves_nothing(self):
        result, _pick, _confirm = self.run_sel(([], False, False, "001"), picked=[1], confirmed=False)
        self.assertEqual(result, set())


class MissingQuestionaryTest(unittest.TestCase):
    """没装 questionary 时给一句能照做的话，而不是抛 traceback。"""

    def test_missing_questionary_dies_with_install_hint(self):
        err = io.StringIO()
        missing = ModuleNotFoundError("No module named 'questionary'", name="questionary")
        with mock.patch("importlib.import_module", side_effect=missing), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            lifecycle._load_handoff_picker()  # noqa: SLF001
        self.assertIn("questionary", err.getvalue())
        self.assertIn("pip install", err.getvalue())

    def test_other_missing_module_is_not_swallowed(self):
        missing = ModuleNotFoundError("No module named 'rich'", name="rich")
        with mock.patch("importlib.import_module", side_effect=missing), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ModuleNotFoundError):
            lifecycle._load_handoff_picker()  # noqa: SLF001


class CmdHandoffTest(unittest.TestCase):
    def test_stopped_sessions_are_hidden_unless_named(self):
        """停掉的不列（面板也不列），但显式 --only 点名的那条照旧能搬。"""
        items = [session(1, state="stopped"), session(2, state="done")]
        seen = {}

        def selection(_console, rows, _target, _opts):
            seen["ids"] = [item["id"] for item in rows]
            return set()

        for argv, expect in ((["001", "002", "--dry-run"], ["id2"]),
                             (["001", "002", "--dry-run", "--only=id1"], ["id1", "id2"])):
            with mock.patch.object(lifecycle, "handoff_candidates", return_value=items), \
                    mock.patch.object(lifecycle, "require_profile"), \
                    mock.patch.object(lifecycle, "make_console"), \
                    mock.patch.object(lifecycle, "_handoff_selection", side_effect=selection), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(lifecycle.cmd_handoff(argv), 0)
            self.assertEqual(seen["ids"], expect)

    def test_only_stopped_says_so_and_moves_nothing(self):
        out = io.StringIO()
        with mock.patch.object(lifecycle, "handoff_candidates",
                               return_value=[session(1, state="stopped")]), \
                mock.patch.object(lifecycle, "require_profile"), \
                mock.patch.object(lifecycle, "make_console"), \
                mock.patch.object(lifecycle, "_handoff_selection") as selection, \
                contextlib.redirect_stdout(out):
            self.assertEqual(lifecycle.cmd_handoff(["001", "002", "--dry-run"]), 0)
        selection.assert_not_called()
        self.assertIn("只剩停掉的会话", out.getvalue())

    def test_cancel_exits_zero_and_moves_nothing(self):
        with mock.patch.object(lifecycle, "handoff_candidates", return_value=[session(1)]), \
                mock.patch.object(lifecycle, "require_profile"), \
                mock.patch.object(lifecycle, "make_console"), \
                mock.patch.object(lifecycle, "handoff_plan"), \
                mock.patch.object(lifecycle, "run_handoff") as moved, \
                mock.patch.object(lifecycle.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(handoff_picker, "pick_sessions", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lifecycle.cmd_handoff(["001", "002"]), 0)
        moved.assert_not_called()

    def test_bare_handoff_in_tty_picks_pair_then_continues(self):
        with mock.patch.object(lifecycle.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(lifecycle, "_pick_handoff_pair", return_value=["001", "002"]), \
                mock.patch.object(lifecycle, "require_profile"), \
                mock.patch.object(lifecycle, "make_console"), \
                mock.patch.object(lifecycle, "handoff_candidates", return_value=[]) as cands, \
                contextlib.redirect_stdout(TtyOut()):
            self.assertEqual(lifecycle.cmd_handoff([]), 0)
        cands.assert_called_once_with("001")

    def test_bare_handoff_cancel_exits_zero(self):
        with mock.patch.object(lifecycle.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(lifecycle, "_pick_handoff_pair", return_value=[]), \
                mock.patch.object(lifecycle, "handoff_candidates") as cands, \
                contextlib.redirect_stdout(TtyOut()):
            self.assertEqual(lifecycle.cmd_handoff([]), 0)
        cands.assert_not_called()

    def test_bare_handoff_without_tty_is_usage_error(self):
        with mock.patch.object(lifecycle.sys.stdin, "isatty", return_value=False), \
                mock.patch.object(lifecycle, "_pick_handoff_pair") as pair, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            lifecycle.cmd_handoff([])
        pair.assert_not_called()

    def test_pair_picker_uses_ls_rows_and_shares_sessions(self):
        rows = [row("001"), row("002")]
        with mock.patch.object(lifecycle, "discover_profiles", return_value=["001", "002"]), \
                mock.patch("cca.report.list_rows", return_value=rows) as listed, \
                mock.patch.object(handoff_picker, "pick_pair", return_value=("001", "002")) as pair, \
                mock.patch.object(lifecycle, "_refresh_usage_in_background") as refresh, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lifecycle._pick_handoff_pair(), ["001", "002"])  # noqa: SLF001
        # 不等实时额度：只读本地快照，后台再刷
        listed.assert_called_once_with(["001", "002"], cached_only=True)
        refresh.assert_called_once_with(rows)
        pair.assert_called_once_with(rows, lifecycle.shares_sessions)

    def test_background_refresh_only_when_stale(self):
        now = 10_000.0
        fresh = {"kind": "oauth", "usage": {"fetched_at": now - 10}}
        stale = {"kind": "oauth", "usage": {"fetched_at": now - lifecycle.USAGE_LIVE_TTL - 1}}
        api = {"kind": "api", "usage": {}}
        with mock.patch.object(lifecycle.time, "time", return_value=now), \
                mock.patch.object(lifecycle.subprocess, "Popen") as popen:
            lifecycle._refresh_usage_in_background([fresh, api])  # noqa: SLF001
            popen.assert_not_called()
            lifecycle._refresh_usage_in_background([fresh, stale])  # noqa: SLF001
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0][-2:], ["ls", "--json"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
