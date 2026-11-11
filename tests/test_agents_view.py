"""cca agents：跨号合并、排序、默认范围、表格与 --json、取不到的号跳过。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca import report
from cca.config import AGENTS_TIMEOUT
from cca.session import agents_view, takeover

NOW = 1_800_000_000.0
DAY = 86400
NARROW = 60


def row(account, sid, state, **extra):
    pid = extra.get("pid", 0)
    return {"account": account, "id": sid[:8], "session_id": sid, "name": extra.get("name", ""),
            "kind": "background", "cwd": "/w", "state": state, "running": bool(pid),
            "started_at": extra.get("started", NOW - 100), "pid": pid,
            "status_text": extra.get("status", "")}


def transcript_lines(session, text, *, error=False):
    """一份假对话记录：一句人话、一句它回的话（error=True 时是额度错误）。"""
    ask = {"type": "user", "message": {"content": "做这个"}}
    reply = {"type": "assistant", "isApiErrorMessage": error,
             "message": {"content": [{"type": "text", "text": text}]}}
    return "\n".join(json.dumps(one, ensure_ascii=False) for one in (ask, reply)) + "\n"


class ViewCase(unittest.TestCase):
    """只替换两个边界：各号取回的行（candidates_checked）和对话记录目录（PROJECTS_DIR）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.projects = Path(self.tmp.name)
        (self.projects / "p").mkdir()

    def seed(self, session, text="最后一句", *, error=False, mtime=None):
        path = self.projects / "p" / f"{session}.jsonl"
        path.write_text(transcript_lines(session, text, error=error), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def build(self, rows, *, failed=(), show_all=False):
        with mock.patch.object(takeover, "candidates_checked", return_value=(rows, list(failed))), \
                mock.patch.object(takeover, "PROJECTS_DIR", self.projects):
            return agents_view.build(show_all=show_all, now=NOW)


def build(rows, *, failed=(), show_all=False, active=None):
    """渲染测试用：没有对话记录，最后活动用 started_at（active 可按会话覆盖）。"""
    case = ViewCase()
    case.setUp()
    try:
        for sid, when in (active or {}).items():
            case.seed(sid, mtime=when)
        return case.build(rows, failed=failed, show_all=show_all)
    finally:
        case.tmp.cleanup()


class MergeTest(ViewCase):
    def test_alive_copy_wins_over_old_stopped(self):
        view = build([row("001", "aaaa1111", "stopped", name="job", started=NOW - 5000),
                      row("002", "aaaa1111", "working", name="job", pid=7, started=NOW - 100)])
        self.assertEqual(len(view["rows"]), 1)
        self.assertEqual(view["rows"][0]["account"], "002")
        self.assertEqual(view["rows"][0]["also_on"], ["001"])
        self.assertTrue(view["rows"][0]["alive"])

    def test_alive_wins_even_if_older_record(self):
        view = build([row("001", "aaaa1111", "blocked", pid=9, started=NOW - 5000),
                      row("002", "aaaa1111", "stopped", started=NOW - 100)])
        self.assertEqual(view["rows"][0]["account"], "001")

    def test_none_alive_takes_most_recent(self):
        view = build([row("001", "aaaa1111", "stopped", started=NOW - 5000),
                      row("002", "aaaa1111", "stopped", started=NOW - 100)])
        self.assertEqual(len(view["rows"]), 1)
        self.assertEqual(view["rows"][0]["account"], "002")
        self.assertFalse(view["rows"][0]["alive"])


class OrderAndScopeTest(ViewCase):
    def test_attention_then_running_then_recent(self):
        rows = [row("001", "d1", "done", started=NOW - 50),
                row("001", "w1", "working", pid=1, started=NOW - 500),
                row("001", "b1", "blocked", pid=2, started=NOW - 9000),
                row("001", "d2", "done", started=NOW - 10)]
        ids = [r["session_id"] for r in build(rows)["rows"]]
        self.assertEqual(ids, ["b1", "w1", "d2", "d1"])

    def test_quota_hit_ranks_first(self):
        self.seed("i1", "You've hit your usage limit", error=True)
        self.seed("w1", "在做")
        rows = [row("001", "w1", "working", pid=1), row("001", "i1", "idle", pid=2, started=NOW - 9000)]
        out = self.build(rows)["rows"]
        self.assertEqual([(r["session_id"], r["state"]) for r in out], [("i1", "quota"), ("w1", "working")])

    def test_idle_and_blocked_without_pid_count_as_alive(self):
        rows = [row("001", "i1", "idle", started=NOW - 30 * DAY),
                row("001", "b1", "blocked", started=NOW - 30 * DAY),
                row("001", "d1", "done", started=NOW - 30 * DAY)]
        view = self.build(rows)
        self.assertEqual([r["session_id"] for r in view["rows"]], ["b1", "i1"])
        self.assertTrue(all(r["alive"] for r in view["rows"]))
        self.assertEqual(view["hidden"], 1)

    def test_default_scope_vs_all(self):
        rows = [row("001", "live", "idle", pid=1, started=NOW - 30 * DAY),
                row("001", "new", "stopped", started=NOW - 1 * DAY),
                row("001", "old", "stopped", started=NOW - 10 * DAY)]
        default = build(rows)
        self.assertEqual({r["session_id"] for r in default["rows"]}, {"live", "new"})
        self.assertEqual(default["hidden"], 1)
        full = build(rows, show_all=True)
        self.assertEqual({r["session_id"] for r in full["rows"]}, {"live", "new", "old"})
        self.assertEqual(full["hidden"], 0)

    def test_title_falls_back_to_short_id(self):
        self.seed("abcdef12-0000", "最后一句")
        out = self.build([row("001", "abcdef12-0000", "idle", pid=1)])["rows"][0]
        self.assertEqual(out["title"], "abcdef12")
        self.assertEqual(out["doing"], "最后一句")


class RenderTest(unittest.TestCase):
    def run_cmd(self, args, view, width=100):
        out = io.StringIO()
        console = report.make_console(file=out, width=width, force_terminal=False)
        with mock.patch.object(agents_view, "build", return_value=view), \
                mock.patch.object(report, "make_console", return_value=console), \
                contextlib.redirect_stdout(out):
            code = report.cmd_agents(args)
        return code, out.getvalue()

    def view(self, failed=()):
        rows = build([row("002", "aaaa1111", "blocked", name="alpha", pid=3),
                      row("003", "bbbb2222", "working", name="beta", pid=4)])
        rows["failed"] = list(failed)
        return rows

    def test_table_has_columns_chinese_state_and_hint(self):
        code, text = self.run_cmd([], self.view())
        self.assertEqual(code, 0)
        for token in ("alpha", "aaaa1111", "002", "blocked", "working", "cca <号> attach <短编号>"):
            self.assertIn(token, text)
        self.assertLess(text.index("alpha"), text.index("beta"))

    def test_narrow_does_not_wrap(self):
        _code, text = self.run_cmd([], self.view(), width=NARROW)
        body = [ln for ln in text.splitlines() if "aaaa1111" in ln or "bbbb2222" in ln]
        self.assertEqual(len(body), 2)
        self.assertTrue(all(len(ln) <= NARROW for ln in text.splitlines()))

    def test_json(self):
        code, text = self.run_cmd(["--json"], self.view(failed=["009"]))
        data = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["failed_accounts"], ["009"])
        self.assertEqual(data["agents"][0]["account"], "002")
        self.assertEqual(data["agents"][0]["state"], "blocked")

    def test_failed_account_noted_in_table(self):
        _code, text = self.run_cmd([], self.view(failed=["009"]))
        self.assertIn("没取到：009", text)

    def test_unknown_flag_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            report.cmd_agents(["--bogus"])


class FailedAccountTest(unittest.TestCase):
    def test_failing_account_skipped_and_reported(self):
        def fake(account, timeout=0):
            self.assertEqual(timeout, 7)
            if account == "bad":
                return None
            return [{"id": "aaaa1111", "sessionId": "aaaa1111-x", "state": "working", "pid": 5,
                     "startedAt": int(time.time() * 1000)}]
        with mock.patch.object(takeover, "agents_or_none", side_effect=fake):
            rows, failed = takeover.candidates_checked(["ok", "bad"], timeout=7)
        self.assertEqual(failed, ["bad"])
        self.assertEqual([r["account"] for r in rows], ["ok"])

    def test_agents_view_uses_agents_timeout(self):
        with mock.patch.object(takeover, "candidates_checked", return_value=([], [])) as fetch:
            agents_view.build(now=NOW)
        self.assertEqual(fetch.call_args.kwargs["timeout"], AGENTS_TIMEOUT)

    def test_timeout_is_passed_to_subprocess(self):
        done = mock.Mock(returncode=0, stdout="[]")
        with mock.patch.object(takeover, "_claude", return_value="claude"), \
                mock.patch.object(takeover.subprocess, "run", return_value=done) as run:
            takeover.agents_or_none("001", timeout=3)
            takeover.agents_or_none("001")
        self.assertEqual([c.kwargs["timeout"] for c in run.call_args_list], [3, takeover.AGENTS_LIST_TIMEOUT])

    def test_hung_account_times_out_to_none(self):
        with mock.patch.object(takeover, "_claude", return_value="claude"), \
                mock.patch.object(takeover.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 1)):
            self.assertIsNone(takeover.agents_or_none("slow", timeout=1))

    def test_run_failure_returns_none(self):
        with mock.patch.object(takeover, "_claude", return_value="claude"), \
                mock.patch.object(takeover.subprocess, "run", side_effect=OSError("x")):
            self.assertIsNone(takeover.agents_or_none("001"))


if __name__ == "__main__":
    unittest.main()


class MovedFromTest(unittest.TestCase):
    def test_recent_live_copy_with_stopped_older_copy_is_marked(self):
        now_ms = time.time() * 1000
        old = {"account": "b005", "state": "stopped", "started_at": now_ms - 3600_000, "pid": 0}
        new = {"account": "b004", "state": "working", "started_at": now_ms - 600_000, "pid": 123}
        stray = {"account": "ds", "state": "done", "started_at": now_ms - 7200_000, "pid": 0}
        self.assertEqual(agents_view._moved_from(new, [new, old, stray]), "b005")  # noqa: SLF001
        self.assertEqual(agents_view._moved_from(new, [new, stray]), "")  # noqa: SLF001
        long_ago = {**new, "started_at": now_ms - 7 * 3600_000}
        self.assertEqual(agents_view._moved_from(long_ago, [long_ago, old]), "")  # noqa: SLF001
