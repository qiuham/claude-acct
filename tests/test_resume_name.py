"""续会话保留原名：cca 自己发起的每次 `--bg --resume` 都用 `/rename <原名>` 当启动提示，原提示续上后以消息送到。

不带 --name / -n：2.1.321+ 带 --name 续会话会分叉出新编号；不改名则 CC 按提示词自动起名、覆盖原名。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolate  # noqa: E402,F401 - 先于 cca 导入：HOME 指到临时目录，测试不碰真实 ~/.claude-accounts

from cca.account import lifecycle  # noqa: E402
from cca.session import push, takeover, wake, watchdog  # noqa: E402

SID = "12e1c36e-24a8-4bea-a9be-6a1ff93025e5"
NAME = "我的 会话"


LATE_AFTER = 4          # 第几次等待之后套接字才出现


def rec(kind, **fields):
    return {"type": kind, **fields}


class NameTestCase(unittest.TestCase):
    """临时号目录 + 一条对话记录；takeover 的登记表和投递都换成假的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "cfg"
        self.path = self.cfg / "projects" / "proj" / f"{SID}.jsonl"
        self.path.parent.mkdir(parents=True)
        self.now = 1000.0
        self.clock = mock.Mock(time=self.tick, sleep=self.advance)   # takeover 里的 time：假时钟，sleep 只拨钟
        self.delivered = []
        self.deliver_errors = []          # 依次作为每次 deliver 的返回；用完返回成功
        self.registry = {"messagingSocketPath": "/run/x.sock", "status": "idle"}
        self.warnings = []
        for patch in (mock.patch.object(push, "transcript_cache_file", return_value=self.root / "c.json"),
                      mock.patch.object(push, "_transcript_cache", None),
                      mock.patch.object(push, "_transcript_cache_dirty", new=False),
                      mock.patch.object(push.atexit, "register"),
                      mock.patch.object(push, "profile_dir", return_value=self.cfg),
                      mock.patch.object(watchdog, "profile_dir", return_value=self.cfg),
                      mock.patch.object(takeover, "profile_dir", return_value=self.cfg),
                      mock.patch.object(takeover, "registry_entry", side_effect=lambda *_: dict(self.registry)),
                      mock.patch.object(takeover, "deliver", side_effect=self.fake_deliver),
                      mock.patch.object(takeover, "time", new=self.clock),
                      mock.patch.object(lifecycle, "say"),
                      mock.patch.object(lifecycle, "warn", side_effect=self.warnings.append)):
            patch.start()
            self.addCleanup(patch.stop)

    def tick(self):
        self.now += 0.01
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def fake_deliver(self, entry, text, sender):
        self.delivered.append((entry, text, sender))
        return self.deliver_errors.pop(0) if self.deliver_errors else ""

    def write(self, *records):
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    def named(self):
        self.write(rec("custom-title", customTitle=NAME), rec("ai-title", aiTitle="auto"))

    def unnamed(self):
        self.write(rec("user", message={"content": "hi"}))

    def assert_renamed(self, cmd, prompt):
        """有名字：最后一个参数是 /rename，原提示不在命令行，没有 --name / -n。"""
        self.assertEqual(cmd[-1], f"/rename {NAME}")
        self.assertNotIn(prompt, cmd)
        self.assert_no_name_flag(cmd)

    def assert_no_name_flag(self, cmd):
        self.assertFalse([a for a in cmd if a in ("-n", "--name") or str(a).startswith(("--name=", "-n="))], cmd)

    def assert_delivered(self, prompt, sender_name="cca"):
        self.assertEqual([(text, sender["name"]) for _e, text, sender in self.delivered], [(prompt, sender_name)])


class SessionNameTest(NameTestCase):
    def test_custom_title_wins_over_later_auto_names(self):
        self.write(rec("custom-title", customTitle="我的会话"),
                   rec("ai-title", aiTitle="auto"), rec("agent-name", agentName="auto"))
        self.assertEqual(push.session_name(self.path), "我的会话")

    def test_last_custom_title_wins(self):
        self.write(rec("custom-title", customTitle="旧"), rec("custom-title", customTitle="新"))
        self.assertEqual(push.session_name(self.path), "新")

    def test_only_auto_name_uses_the_last_one(self):
        self.write(rec("ai-title", aiTitle="first"), rec("agent-name", agentName="second"))
        self.assertEqual(push.session_name(self.path), "second")

    def test_nothing_gives_empty(self):
        self.unnamed()
        self.assertEqual(push.session_name(self.path), "")
        self.assertEqual(push.session_name(None), "")

    def test_whitespace_collapsed(self):
        self.write(rec("custom-title", customTitle="-x  a\n b\t c"))
        self.assertEqual(push.session_name(self.path), "-x a b c")

    def test_title_beyond_the_tail_is_found_by_full_scan(self):
        self.write(rec("custom-title", customTitle="很早起的名"), rec("user", message={"content": "x" * 200}))
        with mock.patch.object(push, "NAME_TAIL_BYTES", 50):
            self.assertEqual(push.session_name(self.path), "很早起的名")

    def test_watchdog_title_uses_it_with_short_id_fallback(self):
        self.named()
        self.assertEqual(watchdog.session_title("001", SID), NAME)
        self.unnamed()
        self.assertEqual(push.session_name(self.path), "")
        self.assertEqual(watchdog.session_title("001", SID), SID[:8])


class ResumeLaunchTest(NameTestCase):
    def test_named_starts_with_rename_and_defers_prompt(self):
        self.named()
        self.assertEqual(takeover.resume_launch("001", SID, "接着做"), ([f"/rename {NAME}"], "接着做"))
        self.assertEqual(takeover.resume_launch("001", SID), ([f"/rename {NAME}"], ""))

    def test_unnamed_is_unchanged(self):
        self.unnamed()
        self.assertEqual(takeover.resume_launch("001", SID, "接着做"), (["接着做"], ""))
        self.assertEqual(takeover.resume_launch("001", SID), ([], ""))


class DeliverAfterResumeTest(NameTestCase):
    def test_delivers_once_registered(self):
        self.assertEqual(takeover.deliver_after_resume("001", SID, "话"), "")
        self.assert_delivered("话")

    def test_empty_text_does_nothing(self):
        self.registry = {}
        self.assertEqual(takeover.deliver_after_resume("001", SID, ""), "")
        self.assertEqual(self.delivered, [])

    def test_waits_for_socket_then_gives_up_with_reason(self):
        self.registry = {"status": "idle"}
        error = takeover.deliver_after_resume("001", SID, "话", wait=3)
        self.assertIn("还没出现", error)
        self.assertEqual(self.delivered, [])

    def test_socket_appearing_late_still_delivers(self):
        self.registry = {"status": "idle"}
        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            self.now += seconds
            if len(sleeps) == LATE_AFTER:
                self.registry = {"messagingSocketPath": "/run/x.sock", "status": "idle"}

        self.clock.sleep = advance
        self.assertEqual(takeover.deliver_after_resume("001", SID, "话", wait=30), "")
        self.assert_delivered("话")

    def test_retries_until_deadline_not_a_fixed_count(self):
        # 套接字登记出来了但还连不上（刚起来、在处理 /rename）：不止试 3 次，直到期限
        self.deliver_errors = [f"发不出去：{n}" for n in range(10)]
        self.assertEqual(takeover.deliver_after_resume("001", SID, "话"), "")
        self.assertEqual(len(self.delivered), 11)

    def test_gives_up_at_deadline_and_reports_last_error(self):
        self.deliver_errors = [f"发不出去：{n}" for n in range(1000)]
        error = takeover.deliver_after_resume("001", SID, "话", wait=5)
        self.assertTrue(error.startswith("发不出去："))
        self.assertGreater(len(self.delivered), 3)
        self.assertLess(self.now, 1000 + 5 + 2)

    def test_zero_wait_tries_once(self):
        self.deliver_errors = ["发不出去：1"]
        self.assertEqual(takeover.deliver_after_resume("001", SID, "话", wait=0), "发不出去：1")
        self.assertEqual(len(self.delivered), 1)


class MoveOneTest(NameTestCase):
    def move(self, prompt="接着做", calls=None):
        calls = [] if calls is None else calls
        self.removed = []

        def fake_run(args, name, **_):
            if args[0] == "rm":                  # 搬完清源号面板：不算续跑调用，单独记
                self.removed.append((name, args[1]))
            else:
                calls.append((name, list(args)))
            return mock.Mock(returncode=0, stdout="", stderr="")

        item = {"full": SID, "id": SID[:8], "profile": "b004", "cwd": "/work", "completed": True}
        with mock.patch.object(lifecycle, "_run_claude", side_effect=fake_run), \
             mock.patch.object(lifecycle, "api_launch_args", side_effect=lambda _n, a: a), \
             mock.patch.object(lifecycle, "HANDOFF_LOCK_DIR", self.root / "locks"), \
             mock.patch.object(lifecycle, "live_session_ids", return_value=set()), \
             mock.patch.object(lifecycle, "copy_trust", return_value=False), \
             mock.patch("cca.session.transcript.target_needs_strip", return_value=False):
            result = lifecycle._move_one(item, "b002", prompt)  # noqa: SLF001
        return result, calls[-1][1]

    def test_named(self):
        self.named()
        result, cmd = self.move()
        self.assertEqual(result, "")
        self.assertEqual(cmd[:3], ["--bg", "--resume", SID])
        self.assert_renamed(cmd, "接着做")
        self.assert_delivered("接着做")
        self.assertEqual(self.removed, [("b004", SID[:8])])      # 源号面板里的旧条目清掉

    def test_named_without_prompt_only_renames(self):
        self.named()
        result, cmd = self.move(prompt="")
        self.assertEqual((result, cmd), ("", ["--bg", "--resume", SID, f"/rename {NAME}"]))
        self.assertEqual(self.delivered, [])

    def test_unnamed_is_unchanged(self):
        self.unnamed()
        result, cmd = self.move()
        self.assertEqual((result, cmd), ("", ["--bg", "--resume", SID, "接着做"]))
        self.assertEqual(self.delivered, [])
        self.unnamed()
        self.assertEqual(self.move(prompt="")[1], ["--bg", "--resume", SID])

    def test_delivery_failure_is_reported(self):
        self.named()
        self.deliver_errors = ["发不出去：x"] * 1000
        result, _cmd = self.move()
        self.assertEqual(result, "")           # 搬已经成功，不能报成搬失败
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("发不出去：x", self.warnings[0])
        self.assertIn("接着做", self.warnings[0])

    def test_delivery_failure_with_process_gone_relaunches_by_prompt(self):
        # 续上就退出了（登记没了）：用原提示再续一次，不让会话停在等输入
        self.named()
        self.registry = {}
        calls = []
        result, cmd = self.move(calls=calls)
        self.assertEqual(result, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(cmd, ["--bg", "--resume", SID, "接着做"])
        self.assertIn("重新续上", self.warnings[-1])

    def test_delivery_failure_with_process_alive_does_not_relaunch(self):
        self.named()
        self.deliver_errors = ["发不出去：x"] * 1000
        calls = []
        self.move(calls=calls)
        self.assertEqual(len(calls), 1)            # 进程还在：再续就是副本


class ResumeBackTest(NameTestCase):
    def resume_back(self):
        calls = []
        with mock.patch.object(lifecycle, "_run_claude",
                               side_effect=lambda args, name, **_: calls.append(list(args)) or
                               mock.Mock(returncode=0, stdout="", stderr="")), \
             mock.patch.object(lifecycle, "api_launch_args", side_effect=lambda _n, a: a), \
             mock.patch.object(lifecycle, "launch_dir", side_effect=lambda d: d):
            lifecycle._resume_back("001", SID, "没换成", cwd="/work")  # noqa: SLF001
        return calls[-1]

    def test_named(self):
        self.named()
        cmd = self.resume_back()
        self.assertEqual(cmd[:3], ["--bg", "--resume", SID])
        self.assert_renamed(cmd, "没换成")
        self.assert_delivered("没换成")

    def test_unnamed_is_unchanged(self):
        self.unnamed()
        self.assertEqual(self.resume_back(), ["--bg", "--resume", SID, "没换成"])
        self.assertEqual(self.delivered, [])

    def test_delivery_failure_is_reported(self):
        self.named()
        self.deliver_errors = ["发不出去：x"] * 1000
        self.resume_back()
        self.assertTrue(any("发不出去：x" in w for w in self.warnings), self.warnings)


class WakeTest(NameTestCase):
    def wake(self):
        entry = {"account": "001", "cwd": ""}
        with mock.patch.object(wake.shutil, "which", return_value="/usr/bin/claude"), \
             mock.patch("cca.env.session_env", return_value={}), \
             mock.patch.object(wake, "_log_path", return_value=self.root / "wake.log"), \
             mock.patch.object(wake.subprocess, "Popen") as popen:
            error = wake._wake(entry, SID)  # noqa: SLF001
        return error, popen.call_args.args[0]

    def test_named(self):
        self.named()
        error, cmd = self.wake()
        self.assertEqual(error, "")
        self.assertEqual(cmd[1:4], ["--bg", "--resume", SID])
        self.assert_renamed(cmd, wake.NUDGE)
        self.assert_delivered(wake.NUDGE, "cca wake")

    def test_unnamed_is_unchanged(self):
        self.unnamed()
        error, cmd = self.wake()
        self.assertEqual((error, cmd[1:]), ("", ["--bg", "--resume", SID, wake.NUDGE]))
        self.assertEqual(self.delivered, [])

    def test_delivery_failure_is_returned(self):
        self.named()
        self.deliver_errors = ["发不出去：x"] * 1000
        error, _cmd = self.wake()
        self.assertIn("发不出去：x", error)

    def test_round_budget_caps_the_wait(self):
        from cca.session.takeover import REGISTER_WAIT  # noqa: PLC0415

        now = 5000.0
        with mock.patch.object(wake.time, "time", return_value=now):
            self.assertEqual(wake._budget_left(now + 1000), REGISTER_WAIT)  # noqa: SLF001
            self.assertEqual(wake._budget_left(now + 7), 7)  # noqa: SLF001
            self.assertEqual(wake._budget_left(now - 1), 0)  # noqa: SLF001
        self.assertFalse(hasattr(wake, "REGISTER_WAIT"))


class TakeoverWakeTest(NameTestCase):
    def wake(self):
        row = {"session_id": SID, "kind": "background", "account": "001", "cwd": ""}
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(takeover, "_claude", return_value="claude"), \
             mock.patch("cca.env.session_env", return_value={}), \
             mock.patch("cca.session.transcript.target_needs_strip", return_value=False), \
             mock.patch.object(takeover.subprocess, "run", return_value=done) as run:
            error = takeover._wake(row)  # noqa: SLF001
        return error, run.call_args.args[0]

    def test_named_only_swaps_the_start_prompt(self):
        self.named()
        error, cmd = self.wake()
        self.assertEqual((error, cmd), ("", ["claude", "--bg", "--resume", SID, f"/rename {NAME}"]))
        self.assertEqual(self.delivered, [])          # 用户的话由外层 deliver，这里不投

    def test_unnamed_is_unchanged(self):
        self.unnamed()
        self.assertEqual(self.wake(), ("", ["claude", "--bg", "--resume", SID]))


class PushSessionTest(NameTestCase):
    def push(self, stamps=None):
        cmds = []

        def run(cmd, **_):
            cmds.append(list(cmd))
            return mock.Mock(returncode=0, stdout="backgrounded · 12e1c36e", stderr="")

        with mock.patch.object(push, "active_workers", return_value=[]), \
             mock.patch.object(push, "wait_last_conversation_stamp", return_value=0.0) as stamp:
            result = push.push_session_once("001", SID, "继续", "", run=run,
                                            verify=lambda *_a, **_k: ("sent", "ok"))
        if stamps is not None:
            stamps.append(stamp)
        return result, cmds[-1]

    def test_named(self):
        self.named()
        result, cmd = self.push()
        self.assertEqual(result, ("sent", "ok"))
        self.assertEqual(cmd[:5], ["cca", "001", "--bg", "--resume", SID])
        self.assert_renamed(cmd, "继续")
        self.assert_delivered("继续")

    def test_baseline_is_taken_once_before_the_push(self):
        # 投递之后不再重取基线：/rename 是 system 记录本来就不算，重取会吞掉投递那句话触发的一轮
        self.named()
        stamps = []
        self.push(stamps)
        self.assertEqual(stamps[0].call_count, 1)

    def test_unnamed_is_unchanged(self):
        self.unnamed()
        result, cmd = self.push()
        self.assertEqual((result, cmd), (("sent", "ok"), ["cca", "001", "--bg", "--resume", SID, "继续"]))
        self.assertEqual(self.delivered, [])

    def test_delivery_failure_is_a_retry_with_reason(self):
        self.named()
        self.deliver_errors = ["发不出去：x"] * 1000
        state, why = self.push()[0]
        self.assertEqual(state, "retry")
        self.assertIn("发不出去：x", why)


if __name__ == "__main__":
    unittest.main()


class ResumeOnTargetTest(unittest.TestCase):
    """续到目标号：目标号上的旧 job 先清掉；CC 还是复制了就收掉副本、按失败算（11-11 train-improve 被复制两份）。"""

    ITEM = {"full": "75842125-0000-0000-0000-000000000000",  # noqa: RUF012 - 只读夹具
            "id": "75842125", "profile": "b002", "cwd": "/w"}

    def run_resume(self, *, stale_job: bool, output: str = "", code: int = 0):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Path(tmp) / "001" / "jobs"
            jobs.mkdir(parents=True)
            if stale_job:
                (jobs / "75842125").mkdir()
            def fake_run(args, name, **_):
                calls.append((name, list(args)))
                return mock.Mock(returncode=code if args[0] == "--bg" else 0, stdout=output if args[0] == "--bg" else "",
                                 stderr="")
            with mock.patch.object(lifecycle, "profile_dir", side_effect=lambda n: Path(tmp) / n), \
                 mock.patch.object(lifecycle, "live_session_ids", return_value=set()), \
                 mock.patch.object(lifecycle, "drop_entry", side_effect=lambda a, j, f, why: calls.append(("drop", a, j)) or "ok"), \
                 mock.patch.object(lifecycle, "_run_claude", side_effect=fake_run), \
                 mock.patch.object(lifecycle, "_wait_stopped", return_value=True), \
                 mock.patch.object(lifecycle, "say"):
                problem = lifecycle._resume_on(dict(self.ITEM), "001", ["/rename train-improve"])  # noqa: SLF001
        return problem, calls

    def test_stale_job_on_target_is_cleared_before_resume(self):
        problem, calls = self.run_resume(stale_job=True)
        self.assertEqual(problem, "")
        self.assertEqual(calls[0], ("drop", "001", "75842125"))
        self.assertEqual(calls[1][1][:3], ["--bg", "--resume", self.ITEM["full"]])

    def test_no_stale_job_resumes_directly(self):
        problem, calls = self.run_resume(stale_job=False)
        self.assertEqual(problem, "")
        self.assertEqual([c[0] for c in calls], ["001"])

    def test_copy_is_stopped_removed_and_reported(self):
        problem, calls = self.run_resume(stale_job=False, output="started a copy as d3c532fd-3c91-49b2-86a3-108cfafa6d19")
        self.assertIn("副本 d3c532fd", problem)
        self.assertIn(("001", ["stop", "d3c532fd"]), calls)
        self.assertIn(("drop", "001", "d3c532fd"), calls)


class StaleRegistrationTest(unittest.TestCase):
    def test_only_dead_registrations_of_this_session_are_listed(self):
        import os  # noqa: PLC0415
        full = "bb183830-0000"
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "001" / "sessions"
            folder.mkdir(parents=True)
            (folder / "1.json").write_text(json.dumps({"sessionId": full, "pid": 999999999}), encoding="utf-8")
            (folder / "2.json").write_text(json.dumps({"sessionId": full, "pid": os.getpid()}), encoding="utf-8")
            (folder / "3.json").write_text(json.dumps({"sessionId": "other", "pid": 999999999}), encoding="utf-8")
            (folder / "4.json").write_text("{bad", encoding="utf-8")
            with mock.patch.object(lifecycle, "profile_dir", side_effect=lambda n: Path(tmp) / n):
                stale = lifecycle._stale_registrations("001", full)  # noqa: SLF001
            self.assertEqual([p.name for p in stale], ["1.json"])
