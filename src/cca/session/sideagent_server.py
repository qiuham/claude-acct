"""sideagent 常驻进程（cca sideagent serve）：包着 claude 的 stream-json 跑，收发消息、写进度、回报主会话。

从 sideagent.py 拆出来；引用 sideagent 里的公用函数一律写 core.X，测试照旧 patch sideagent 模块。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from pathlib import Path

from cca.errors import say
from cca.session import inbox, summary
from cca.session import sideagent as core


class _SideagentServer:
    """sideagent进程：持有 claude 子进程，一次只服务一个请求。"""

    def __init__(self, name: str, cwd: str, idle_seconds: int, claude_args: list) -> None:  # noqa: PLR0915 - 一个个状态字段
        self.name = name
        self.account = core._account_of(name)
        self.cwd = cwd
        self.idle_seconds = idle_seconds
        self.claude_args = claude_args
        # 登记表镜像：sideagent的条目发给调用方，调用方的条目发给sideagent。
        self.caller_sessions = core._caller_sessions()
        self.agent_sessions = core.profile_dir(self.account) / "sessions"
        self.mirrored: set[Path] = set()
        self.progress = core._progress_path(name)
        self.socket_path = core._socket_path(name)
        self.lock = threading.Lock()
        self.busy = False
        self.last_output = time.time()
        self.current: socket.socket | None = None
        self.queue: deque = deque()
        self.turn_tools: set = set()
        # 这一轮本体调了几次工具（子代理里的不算），回报里的 tool_uses 用它
        self.turn_tool_uses = 0
        self.turn_task = ""
        # 这一轮的词元：↑ 输入（含缓存）、↓ 输出，按每次调用累计
        self.turn_in = 0
        self.turn_out = 0
        self.msg_out = 0
        # 横条那个数：最近一次输入、正在输出的字数（估词元用）、这一轮有没有动作
        self.latest_in = 0
        self.stream_chars = 0
        self.stream_saved = 0
        self.acted = False
        # 这一轮是不是被打断的、是谁的任务
        self.turn_interrupted = False
        self.current_job = ""
        # 插话：uuid -> {text, notify, job, started}；claude 用 command_lifecycle 报它进没进这一轮
        self.injected: dict = {}
        self.followup_turn = False
        # 这一轮发出去的那句话的编号；claude 说它还在排队（command_lifecycle queued）时为真。
        # 排队期间冒出来的结果不是这一轮的（例如续上旧会话时，先为旧后台任务的“已停止”通知出一个空结果）
        self.turn_uuid = ""
        self.turn_queued = False
        # 这一轮 sideagent 自己（不算子代理）说的最后一段话：结果为空时拿它当结论
        self.last_say = ""
        # 还在跑的后台任务（task_id）：照 claude 的 background_tasks_changed 整份替换，不自己配对开始/结束。
        # 环境任务（ambient）、被等着的（awaited）、Monitor 起的不算：前两个不是这一轮的活，Monitor 本来就一直开着
        self.background: set = set()
        self.background_live: set = set()
        self.monitor_tools: set = set()
        self.monitor_tasks: set = set()
        # 一轮结束时后台还有任务（工作流、后台子代理、后台命令）：先压下的那个结果事件。
        # 后台做完时 claude 会自己再开一轮，那一轮的结果才是最终的
        self.held: dict | None = None
        # 这一轮有没有调用方在 socket 上等结果；调用方说了 keep=false 就干完自己退出
        self.turn_waited = False
        self.turn_keep = True
        # 干完自己退出的时刻（0 表示不退）：到点时闲着、没排队才退
        self.exit_at = 0.0
        self.state_lock = threading.Lock()
        # 进度短语（照 Claude Code 的 AgentSummary）：上一句、上次用的记录、调了几次
        self.summary_text = ""
        self.summary_notes = ""
        self.summary_calls = 0
        self.summary_runner = summary.run_model
        self.child_env: dict = {}
        self.live_text = ""
        self.live_at = 0.0
        # 这一轮最近一次 api_retry：{"summary", "attempt", "max", "at"}；正常出了内容就清掉
        self.api_retry: dict | None = None
        self.last_push = 0.0
        self.turn_started = time.time()
        try:
            owner = json.loads(os.environ.get("CCA_OWNER") or "{}")
        except ValueError:
            owner = {}
        self.owner = core.owner_of({"pid": owner.get("pid"), "procStart": owner.get("proc_start"),
                               "sessionId": owner.get("session")})
        self.owner_account = core._owner_account()
        self.notify_turn = False
        self.caller_socket = ""
        self.caller_name = ""
        self.instance_name = ""
        self.instance_socket = ""
        self.permission_mode = "bypass"
        self.last_activity = time.time()
        self.stop_flag = threading.Event()
        # 收到 SIGTERM（被停掉）：退出时手上的活记成 stopped，不是失败
        self.stop_requested = False
        # 串起“取走要不要回报 → 通知存好”：轮次结束、卡住收尾、退出收尾三处互斥，同一轮只回报一次
        self.finish_lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.server: socket.socket | None = None

    def run(self) -> None:
        # run/ 只在跑的时候有：上次没收拾干净的先清掉；上次留下的“主会话在退出”标记也删掉
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            inbox.owner_ending_path(self.name).unlink()
        with suppress(OSError):
            self.socket_path.unlink()
        from cca.api.client import api_launch_args  # noqa: PLC0415 - 只有守护进程要
        from cca.env import launch_env  # noqa: PLC0415 - 只有守护进程要

        exe = shutil.which("claude")
        if not exe:
            say("找不到 claude 可执行文件")
            return
        args = api_launch_args(self.account, list(self.claude_args))
        self._refresh_caller()
        note = self._report_prompt()
        if note:
            args = [*args, "--append-system-prompt", note]
        env = launch_env(self.account, args)
        # 让sideagent里的 Mod 知道自己是sideagent，别启动面板和通知
        env["CCA_SIDEAGENT"] = "1"
        # 写进度短语时用同一个号、同一套环境（出口探测只做这一次）
        self.child_env = dict(env)
        self.process = subprocess.Popen(  # noqa: S603 - 参数来自本模块
            [exe, *core.STREAM_ARGS, *args],
            env=env, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        with self.state_lock:
            state = core._read_state(self.name)
            state.pop("session_id", None)
            state["child_pid"] = self.process.pid
            core._write_state(self.name, state)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self.server.listen(4)
        self.server.settimeout(1.0)
        # 起进程前写过的几项（例如 context_link）留着，其余按这次的写
        keep = {key: value for key, value in core._read_state(self.name).items() if key == "context_link"}
        core._write_state(self.name, {**keep, "kind": "spawned", "pid": os.getpid(),
                                 "child_pid": self.process.pid if self.process else 0,
                                 "started": time.time(), "last_activity": time.time(),
                                 "cwd": self.cwd, "session_id": "",
                                 "label": os.environ.get("CCA_LABEL") or "",
                                 "description": os.environ.get("CCA_DESCRIPTION") or "",
                                 "caller_agent": os.environ.get("CCA_CALLER_AGENT") or "",
                                 "model": core._model_arg(self.claude_args),
                                 "owner_session": self.owner["session"],
                                 "owner_pid": self.owner["pid"],
                                 "owner_proc_start": self.owner["proc_start"],
                                 "owner_account": self.owner_account,
                                 "code_version": core.CODE_VERSION,
                                 "caller_socket": self.caller_socket})
        threading.Thread(target=self._read_claude, daemon=True).start()
        if self.process is not None:
            threading.Thread(target=self._mirror_loop, daemon=True).start()
            threading.Thread(target=self._summary_loop, daemon=True).start()
        try:
            self._accept_loop()
        finally:
            self._shutdown()

    def _report_prompt(self) -> str:
        """告诉 sideagent 怎么中途给主会话发消息：用 claude 自带的 SendMessage。"""
        if not self.caller_socket:
            return ""
        address = f"uds:{self.caller_socket}"
        target = (f'主会话的名字 "{self.caller_name}"（不认名字时填 "{address}"）'
                  if self.caller_name else f'"{address}"')
        return ("你是主会话派出来干活的 sideagent。干完这一轮，结果会自动交回主会话，"
                "不用你另外发。要中途汇报或提问（例如卡住了要它拿主意、"
                "发现了会影响它计划的问题、长任务的阶段进展），就用 SendMessage 工具，"
                f"to 填{target}。第一行写清楚是什么事，少发，别刷屏。")

    def _summary_loop(self) -> None:
        """每 30 秒看一次：在忙、最近几步有变化，就让这个号的小模型写一句进度短语。"""
        while not self.stop_flag.wait(core.SUMMARY_INTERVAL):
            try:
                self.summarize_once()
            except Exception as exc:  # noqa: BLE001 - 写短语出错不能拖垮 sideagent
                self._summary_log(f"error {type(exc).__name__}")

    def summarize_once(self) -> str:  # noqa: PLR0911 - 每种跳过都说清原因
        """写一次进度短语；返回这次的结果（ok/skip 原因），测试和日志用。"""
        if not self.busy:
            return "skip idle"
        model = summary.configured_model(core.profile_dir(self.account))
        if not model:
            return "skip off"
        turn = self.turn_started
        events = core._tail_events(self.name, limit=200, tail_bytes=400_000)
        notes = summary.build_notes(events, self.cwd)
        if not notes:
            return "skip empty"
        if notes == self.summary_notes:
            return "skip unchanged"
        self.summary_notes = notes
        prompt = summary.user_prompt(notes, self.summary_text)
        self.summary_calls += 1
        started = time.time()
        reply = self.summary_runner(model, prompt, self.child_env or dict(os.environ), self.cwd)
        seconds = time.time() - started
        if turn != self.turn_started or not self.busy:
            self._summary_log(f"drop stale {seconds:.1f}s")
            return "drop stale"
        if not reply.get("ok"):
            # 失败就沿用上一句；面板没有短语时退回工具的活动描述
            self._update_state(summary_calls=self.summary_calls,
                               summary_error=str(reply.get("error") or "")[:200])
            self._summary_log(f"fail {seconds:.1f}s {reply.get('error') or ''}")
            return "fail"
        label = summary.accept(str(reply.get("text") or ""))
        if label is None:
            self._update_state(summary_calls=self.summary_calls)
            self._summary_log(f"unusable {seconds:.1f}s {str(reply.get('text') or '')[:80]!r}")
            return "unusable"
        self.summary_text = label
        # 进度文件里也记一行：实例退出、状态文件没了以后，横条“刚做完”那一行还能显示它
        self._note_text(f"SUMMARY {label}")
        self._update_state(summary=label, summary_at=time.time(), summary_calls=self.summary_calls,
                           summary_model=model, summary_error="")
        self._summary_log(f"ok {seconds:.1f}s ↑{reply.get('tokens_in') or 0} "
                          f"↓{reply.get('tokens_out') or 0} {label}")
        return "ok"

    def _summary_log(self, text: str) -> None:
        """每次写短语记一行（时间、结果、耗时、词元、短语），查“一小时调了几次”用。"""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with suppress(OSError), core._summary_log_path(self.name).open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {text}\n")

    def _update_state(self, **fields) -> None:
        """改状态文件里的几项；几个线程都会写，加锁免得互相盖掉。"""
        with self.state_lock:
            state = core._read_state(self.name)
            state.update(fields)
            core._write_state(self.name, state)

    def _mirror_loop(self) -> None:
        """每 2 秒同步一次登记表：sideagent条目 → 调用方目录，调用方条目 → sideagent目录。

        两份都是拷贝，不是软链：claude 更新状态时是原子覆盖写，软链会指向旧节点。
        """
        if self.process is None:
            return
        pid = self.process.pid
        while not self.stop_flag.is_set():
            # 每一圈都现查：主会话被继续、恢复后编号和 socket 都可能变
            caller_entry = self._refresh_caller()
            self._publish(self.agent_sessions, self.caller_sessions, pid)
            caller_pid = int(caller_entry.get("pid") or 0)
            if caller_pid:
                self._publish(self.caller_sessions, self.agent_sessions, caller_pid)
            self.stop_flag.wait(2.0)

    @property
    def owner_session(self) -> str:
        """主会话最近一次看到的会话编号（面板、会话结束收工按它认）。"""
        return self.owner["session"]

    @property
    def caller_session(self) -> str:
        return self.owner["session"]

    def _refresh_caller(self) -> dict:
        """到登记表现查主会话（见 resolve_owner），更新名字、socket，并把归属跟到现在的进程和编号。"""
        entry = core.resolve_owner(self.caller_sessions, self.owner)
        if not entry:
            return {}
        self.caller_name = str(entry.get("name") or self.caller_name)
        self.caller_socket = str(entry.get("messagingSocketPath") or "")
        now = core.owner_of(entry)
        if now != self.owner:
            # 主会话换了编号（继续、恢复）或重开成了新进程：归属跟过去，面板和 MCP 也认得它
            self._note_text(f"OWNER {self.owner['session'] or '?'} -> {now['session'] or '?'}"
                            f"（进程 {self.owner['pid'] or '?'} -> {now['pid'] or '?'}）")
            self.owner = now
            # 名单（sessions/<编号>.json）由主会话自己并到新编号，这里只改自己的 state.json
            self._update_state(owner_session=now["session"], owner_pid=now["pid"],
                               owner_proc_start=now["proc_start"])
        return entry

    def _find_caller_entry(self) -> dict:
        return core.resolve_owner(self.caller_sessions, self.owner)

    def _publish(self, source: Path, dest: Path, pid: int) -> None:
        """把 <source>/<pid>.json 和对应的 .key 拷到 dest。"""
        if not source.is_dir() or not dest.is_dir():
            return
        for path in source.glob(f"{pid}.*"):
            target = dest / path.name
            if target.exists():
                try:
                    if target.stat().st_mtime >= path.stat().st_mtime:
                        continue
                except OSError:
                    pass
            with suppress(OSError):
                target.write_bytes(path.read_bytes())
                target.chmod(path.stat().st_mode & 0o777)
                self.mirrored.add(target)

    def _unmirror(self) -> None:
        for path in self.mirrored:
            with suppress(OSError):
                path.unlink()

    def _note_text(self, text: str) -> None:
        with suppress(OSError), self.progress.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def _read_own_identity(self) -> None:
        """从自己的登记条目里取名字和套接字，发消息时要用。"""
        if self.process is None:
            return
        path = self.agent_sessions / f"{self.process.pid}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.instance_name = str(data.get("name") or "")
        socket_path = str(data.get("messagingSocketPath") or "")
        if socket_path and socket_path != self.instance_socket:
            # 记下自己的消息 socket：它中途用 SendMessage 发的话，插件按发件地址认出是哪个实例
            self._update_state(instance_socket=socket_path)
        self.instance_socket = socket_path

    def _usage_of(self, event: dict) -> dict:
        return {"tokens": int(event.get("cca_token_count") or 0),
                "tool_uses": int(event.get("cca_tool_uses") or 0),
                "duration_ms": int(event.get("duration_ms") or 0)}

    def _notification(self, text: str, *, status: str, usage: dict, error: str = "") -> str:
        """照 Claude Code 子代理的完成通知（task-notification）写：编号、状态、一句摘要、结果、用量。"""
        title = (os.environ.get("CCA_DESCRIPTION") or os.environ.get("CCA_LABEL")
                 or self.turn_task[:60] or self.name)
        return inbox.notification_text(self.name, title, status=status, text=text, usage=usage, error=error)

    def _notify_main(self, text: str, *, status: str = "completed", usage: dict | None = None,  # noqa: PLR0913
                     error: str = "", job: str = "", settle: bool = False, push: bool = True) -> None:
        """一件活结束：经唯一出口（inbox.finalize）存一条通知，再马上推到主会话的消息通道。

        推不到（主会话关了、socket 没了）通知就留在收件箱里，主会话的面板来取；同一件活只存一条。
        """
        if not self.instance_socket:
            self._read_own_identity()
        if not text and status == "completed":
            # 做完了也要说一声（以前跳过，主会话就一直等）。走到这里说明这一轮一句话都没说过
            text = (f"这一轮结束了，但它没有写任何文字结论（调了 {self.turn_tool_uses} 次工具）。"
                    "要结论可以再问它一句。")
        # 每次投递前都现查：主会话可能换了编号、换了 socket，不能用起进程时记下的
        entry = self._refresh_caller()
        if not entry:
            self.caller_socket = ""
        session = str(entry.get("sessionId") or "") or self.owner["session"]
        notice = {"text": self._notification(text, status=status, usage=usage or {}, error=error),
                  "sender": f"uds:{self.instance_socket}", "instance_name": self.instance_name or self.name,
                  "permission_mode": self.permission_mode,
                  "caller_agent": os.environ.get("CCA_CALLER_AGENT") or ""}
        posted = inbox.finalize(self.name, job, session=session, status=status, notice=notice, settle=settle)
        if posted is None:
            self._note_text(f"NOTIFY skip 这件活已经通知过（{job or '这一轮'}）" if session
                            else "NOTIFY skip 不知道主会话是谁，没处存")
            return
        # 主会话自己在退出（会话结束时 stop-owned 停它）：别往正在关的会话推，存着等它续上时面板来取
        if push and inbox.owner_ending_path(self.name).exists():
            push = False
        if push and inbox.push(posted, self.caller_socket):
            self._note_text("NOTIFY ok")
            return
        # 推不到：通知留在收件箱，面板看到收件箱里有文件就来取
        self._note_text(f"NOTIFY queued 主会话现在收不到，通知存在 {posted.parent}，等它来取")

    def _read_claude(self) -> None:
        if self.process is None or self.process.stdout is None:
            self.stop_flag.set()
            return
        for line in self.process.stdout:
            last = time.time()
            self.last_activity = last
            self.last_output = last
            self._note_progress(line)
            self._remember_session(line)
            self._note_control(line)
            with self.lock:
                target = self.current
            self._forward(target, line)
        self.stop_flag.set()

    def _note_control(self, line: str) -> None:
        """插话的去向：claude 用 command_lifecycle 报每条带 uuid 的消息排队/开始/做完。"""
        event = core._parse(line.encode("utf-8"))
        if event is None:
            return
        if event.get("type") == "control_response":
            self._after_interrupt(event.get("response") or {})
            return
        if event.get("type") != "command_lifecycle":
            return
        key = str(event.get("command_uuid") or "")
        stage = str(event.get("state") or "")
        with self.lock:
            if key and key == self.turn_uuid:
                self.turn_queued = stage == "queued"
            entry = self.injected.get(key)
            if entry is None:
                return
            if stage == "started" and not entry.get("started"):
                entry["started"] = True
                if entry.get("notify") and not self.notify_turn:
                    # 要回报的插话并进了一轮本来不用回报的（例如面板里打的话）：这一轮结束时替它回报
                    self.notify_turn = True
                    self.current_job = self.current_job or str(entry.get("job") or "")
                self._note_event({"kind": "task", "text": entry["text"], "interject": True})
                self._note_text(f"TASK （插话）{entry['text']}")
            elif stage in ("completed", "cancelled", "failed"):
                self.injected.pop(key, None)

    def _after_interrupt(self, response: dict) -> None:
        """打断后 claude 会说哪些排着的消息还留着；没留下的插话就不再等它开轮。"""
        if not str(response.get("request_id") or "").startswith("interrupt-"):
            return
        kept = (response.get("response") or {}).get("still_queued")
        if not isinstance(kept, list):
            return
        keep = {str(item.get("uuid") if isinstance(item, dict) else item) for item in kept}
        with self.lock:
            dropped = [self.injected.pop(key) for key, entry in list(self.injected.items())
                       if not entry.get("started") and key not in keep]
        self._report_dropped(dropped, status="interrupted")

    def _note_system(self, event: dict) -> None:
        """system 事件：API 重试、后台任务名单、后台任务的开始/进度/结束。"""
        subtype = event.get("subtype")
        if subtype == "api_retry":
            self._note_api_retry(event)
        elif subtype == "background_tasks_changed":
            self._set_background(event.get("tasks"))
        else:
            entry = core.task_event(event)
            if entry:
                self._note_monitor(entry)
                self._note_event(entry)

    def _note_progress(self, line: str) -> None:
        """把这一轮的关键事件写成几行人能读的进度，同时记一份详细经过。"""
        event = core._parse(line.encode("utf-8"))
        if event is None:
            return
        if event.get("type") == "stream_event":
            # 子代理的流不算：横条那个数和正在说的话都只看 sideagent 自己
            if not event.get("parent_tool_use_id"):
                self._clear_api_retry()
                self._note_stream_event(event.get("event") or {})
            return
        if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
            self._clear_api_retry()
        if event.get("type") == "error":
            text = str(event.get("message") or "")
            self._note_event({"kind": "error", "text": text[:4000]})
            with suppress(OSError), self.progress.open("a", encoding="utf-8") as handle:
                handle.write("ERROR " + text[:200] + "\n")
            return
        if event.get("type") == "system":
            self._note_system(event)
            return
        if self._foreign_result(event):
            return
        handlers = {"assistant": self._note_assistant,
                    "user": self._note_tool_result,
                    "result": self._note_result}
        handler = handlers.get(str(event.get("type")))
        note_text = handler(event) if handler else ""
        if note_text:
            with suppress(OSError), self.progress.open("a", encoding="utf-8") as handle:
                handle.write(note_text + "\n")

    def _note_api_retry(self, event: dict) -> None:
        """Claude Code 重试 API 错误时的 system/api_retry：记下最近一次，进进度、状态和事件。

        面板和 sideagent_status 的“在干什么”读状态里的 api_retry，所以人能看到
        “API 没回应：504 网关超时，第 2/10 次重试”，而不是干等。
        """
        if not self.busy:
            return   # 这一轮已经收了（放弃、出结果）：迟到的重试不再写回状态
        summary = api_error_summary(event)
        attempt = _safe_int(event.get("attempt"))
        limit = _safe_int(event.get("max_retries"))
        text = f"API 没回应：{summary}，第 {_retry_ordinal(attempt, limit)} 次重试"
        self.api_retry = {"summary": summary, "attempt": attempt, "max": limit, "at": time.time()}
        self._note_event({"kind": "error", "text": text, "api_retry": True})
        self._note_text("RETRY " + text)
        self._update_state(api_retry=text)

    def _clear_api_retry(self) -> None:
        """这一轮正常出了内容：之前的 API 错误已经过去，清掉。"""
        if self.api_retry is None:
            return
        self.api_retry = None
        self._update_state(api_retry="")

    def _note_stream_event(self, inner: dict) -> None:
        """文字增量：先写进临时的 live 文件，面板能边生成边看；顺手记词元。

        词元只在这里记：assistant 事件里的 usage 是调用开头的占位值（输出常常只有个位数），
        真的输出数在 message_delta 里（同一次调用内是累计值）。
        """
        kind = inner.get("type")
        if kind == "message_start":
            used = core.usage_in((inner.get("message") or {}).get("usage"))
            self.turn_in += used
            # 横条那个数用的是“最近一次调用的输入”：覆盖，不累加，等于当前上下文大小
            self.latest_in = used
            self.msg_out = 0
            self.stream_chars = 0
            self._save_tokens()
            return
        if kind == "message_delta":
            out = int((inner.get("usage") or {}).get("output_tokens") or 0)
            # 真的输出数到了，这条消息的流式估计就不要了
            self.stream_chars = 0
            if out > self.msg_out:
                self.turn_out += out - self.msg_out
                self.msg_out = out
            self._save_tokens()
            return
        if kind == "content_block_start":
            block_type = (inner.get("content_block") or {}).get("type")
            if block_type == "text":
                self.live_text = ""
            if block_type in ("text", "tool_use") and not self.acted:
                # 有了第一个动作（说话或调工具），横条箭头从 ↑ 变 ↓
                self.acted = True
                self._save_tokens()
            return
        if kind != "content_block_delta":
            return
        delta = inner.get("delta") or {}
        piece = str(delta.get("text") or delta.get("thinking") or delta.get("partial_json") or "")
        self._add_stream_estimate(len(piece))
        if delta.get("type") != "text_delta":
            return
        self.live_text += str(delta.get("text") or "")
        now = time.time()
        if now - self.live_at < core.LIVE_WRITE_INTERVAL:
            return
        self.live_at = now
        with suppress(OSError):
            core._live_path(self.name).write_text(self.live_text[-4000:], encoding="utf-8")

    def _add_stream_estimate(self, chars: int) -> None:
        """正在输出时按字数估词元（4 个字符算 1 个）；每多出约 50 个才写一次状态。"""
        if chars <= 0:
            return
        self.stream_chars += chars
        if self.stream_chars // core.STREAM_CHARS_PER_TOKEN - self.stream_saved >= core.STREAM_SAVE_EVERY:
            self._save_tokens()

    def token_count(self) -> int:
        """和 Claude Code 子代理那一行同一个数：最近一次输入 + 累计输出 + 正在输出的估计。"""
        return self.latest_in + self.turn_out + self.stream_chars // core.STREAM_CHARS_PER_TOKEN

    def _clear_live(self) -> None:
        self.live_text = ""
        with suppress(OSError):
            core._live_path(self.name).write_text("", encoding="utf-8")

    def _note_assistant(self, event: dict) -> str:
        message = event.get("message") or {}
        parent = event.get("parent_tool_use_id") or ""
        if not parent:
            self._note_usage_fallback(message)
        for block in message.get("content") or []:
            if isinstance(block, dict):
                note = self._note_block(block, message, parent)
                if note is not None:
                    return note
        return ""

    def _note_usage_fallback(self, message: dict) -> None:
        """兜底：流里没见到 message_start 时，用这条消息的输入当“最近一次输入”（覆盖）。"""
        used = core.usage_in(message.get("usage"))
        acted = any(isinstance(block, dict) and block.get("type") in ("text", "tool_use")
                    for block in message.get("content") or [])
        if (used and not self.latest_in) or (acted and not self.acted):
            self.latest_in = self.latest_in or used
            self.acted = self.acted or acted
            self._save_tokens()

    def _note_block(self, block: dict, message: dict, parent: str) -> str | None:
        """一条消息里的一个块；记下来并返回进度行。没内容返回 None（接着看下一块）。"""
        kind = block.get("type")
        if kind == "thinking":
            thought = str(block.get("thinking") or "").strip()
            if not thought:
                return None
            self._note_event({"kind": "think", "text": thought[:4000], "parent": parent})
            return "THINK"
        if kind == "tool_use":
            name = str(block.get("name") or "?")
            summary = core._tool_summary(block.get("input"))
            self._note_event({"kind": "tool", "name": name, "id": block.get("id") or "",
                              "input": core._clip_input(block.get("input")), "summary": summary,
                              "parent": parent})
            return f"TOOL {name}" + (f" · {summary}" if summary else "")
        text = str(block.get("text") or "").strip()
        if not text:
            return None
        self._clear_live()
        if not parent:
            self.last_say = text
        usage = message.get("usage") or {}
        used = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        self._note_event({"kind": "say", "text": text[:4000], "tokens": used, "parent": parent})
        return "SAY " + " ".join(text.split())[:160]

    def _note_tool_result(self, event: dict) -> str:
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                self._note_event({"kind": "tool_result", "text": core._tool_output(block)[:4000],
                                  "id": block.get("tool_use_id") or "",
                                  "error": bool(block.get("is_error")),
                                  "parent": event.get("parent_tool_use_id") or ""})
        return ""

    def _note_result(self, event: dict) -> str:
        text = str(event.get("result") or "")
        usage = event.get("usage") or {}
        # 结果里的 usage 是整轮累计；没有就用流里自己数的
        tokens_in = core.usage_in(usage) or self.turn_in
        tokens_out = int(usage.get("output_tokens") or 0) or self.turn_out
        self.turn_in, self.turn_out = tokens_in, tokens_out
        # 这一轮结束：流式估计并进累计输出（上面已经用了真的输出数），清零
        self.stream_chars = 0
        self._save_tokens()
        count = self.token_count()
        seconds = int(event.get("duration_ms") or 0) / 1000
        self._clear_live()
        self._note_event({"kind": "result", "text": text[:4000],
                          "tokens_in": tokens_in, "tokens_out": tokens_out,
                          "token_count": count, "seconds": seconds, "at": round(time.time(), 1)})
        note_text = "RESULT " + text[:200]
        if tokens_in or tokens_out:
            note_text += f" · ↑{core._kilo(tokens_in)} ↓{core._kilo(tokens_out)} tok · {core._kilo(count)} total"
        if seconds:
            note_text += f" · {seconds:.1f}s"
        return note_text

    def _note_event(self, entry: dict) -> None:
        """把一步详细经过写成一行 JSON，界面按主界面的样子画。

        工作流的完整进度每次都是一整份清单，不追加进事件文件，单独覆盖写一个文件。
        """
        progress = entry.get("workflow_progress")
        if progress is not None:
            entry = {key: value for key, value in entry.items() if key != "workflow_progress"}
            self._save_workflow(entry.get("task_id") or "", progress)
        entry = {**entry, "t": round(time.time() - self.turn_started, 1)}
        with suppress(OSError), core._events_path(self.name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _save_workflow(self, task_id: str, progress: list) -> None:
        """工作流最新的完整进度：先写临时文件再改名，界面不会读到写了一半的。"""
        if not task_id:
            return
        path = core.workflow_path(self.name, task_id)
        with suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"task_id": task_id, "items": progress}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)

    def _trim_events(self) -> None:
        """事件文件太长就砍掉前面的，只留最近 2000 行，别让界面越读越慢。"""
        path = core._events_path(self.name)
        try:
            if not path.exists() or path.stat().st_size <= core.EVENTS_MAX_BYTES:
                return
            kept = path.read_text(encoding="utf-8").splitlines()[-2000:]
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        except OSError:
            return

    def _save_tokens(self) -> None:
        """这一轮到现在的 ↑输入 ↓输出、横条那个数，写进状态文件给界面看。"""
        self.stream_saved = self.stream_chars // core.STREAM_CHARS_PER_TOKEN
        self._update_state(tokens_in=self.turn_in, tokens_out=self.turn_out,
                           tokens=self.turn_in + self.turn_out,
                           token_count=self.token_count(), acted=self.acted)

    def _remember_session(self, line: str) -> None:
        event = core._parse(line.encode("utf-8"))
        if event and event.get("type") == "system" and event.get("subtype") == "init":
            session = str(event.get("session_id") or "")
            # 实际在用的模型以 init 里报的为准（派活时没指定模型也有）
            model = str(event.get("model") or "") or core._model_arg(self.claude_args)
            self._update_state(session_id=session, model=model)
            # 一拿到会话编号就记进留存的记录：实例怎么退出（干完、闲置、被停）都接得上
            core.remember_instance(self.name, {"session_id": session, "code_version": core.CODE_VERSION,
                                          "cwd": self.cwd, "label": os.environ.get("CCA_LABEL") or "",
                                          "description": os.environ.get("CCA_DESCRIPTION") or "",
                                          "caller_agent": os.environ.get("CCA_CALLER_AGENT") or "",
                                          "model": model})
            self.permission_mode = str(event.get("permissionMode") or "bypass")
            self._read_own_identity()

    def _annotate(self, line: str) -> tuple:
        """数这一轮本体调了几次工具；结果那一行补上词元数、工具次数、是不是被打断。"""
        event = core._parse(line.encode("utf-8"))
        if event and event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
            with self.lock:
                self.turn_tool_uses += sum(1 for block in (event.get("message") or {}).get("content") or []
                                           if isinstance(block, dict) and block.get("type") == "tool_use")
        if event and event.get("type") == "result":
            # 照子代理的回报：带上这一轮的词元数、工具调用次数；被打断的标出来，调用方记成 interrupted
            extra = {"cca_tool_uses": self.turn_tool_uses, "cca_token_count": self.token_count()}
            if not str(event.get("result") or "").strip() and self.last_say and not event.get("is_error"):
                # 最后一步只调了工具、没写结论：用它这一轮最后说的那段话当结论
                extra["result"] = self.last_say
            if self.turn_interrupted:
                extra["cca_interrupted"] = True
            event = {**event, **extra}
            line = json.dumps(event, ensure_ascii=False) + "\n"
        return event, line

    def _set_background(self, tasks) -> None:
        """claude 发来后台任务的整份名单：照它换掉自己记的。打断后等着的那一轮，名单空了就交结果。"""
        live = {str(task.get("task_id") or "") for task in tasks or []
                if isinstance(task, dict) and not task.get("ambient") and not task.get("awaited")}
        with self.lock:
            self.background_live = live - {""}
            self.background = self.background_live - self.monitor_tasks
            release = self.held is not None and self.turn_interrupted and not self.background
        if release:
            self._release_held()

    def _note_monitor(self, entry: dict) -> None:
        """Monitor 起的任务在名单里也是 local_bash，靠它的 task_started 带的工具编号认出来，不算后台的活。"""
        if entry.get("subtype") != "task_started" or entry.get("tool_use_id") not in self.monitor_tools:
            return
        with self.lock:
            self.monitor_tasks.add(entry.get("task_id") or "")
            self.background = self.background_live - self.monitor_tasks

    def _foreign_result(self, event: dict | None) -> bool:
        """这一轮的话还在 claude 那里排队时出的结果：不是这一轮的，不记、不交。"""
        if not event or event.get("type") != "result":
            return False
        with self.lock:
            return self.busy and self.turn_queued

    def _should_hold(self, event: dict | None) -> bool:
        """这一轮的结果先压下：后台还有任务在跑，又不是被打断、不是出错。"""
        if not event or event.get("type") != "result":
            return False
        with self.lock:
            return bool(self.background) and self.busy and not self.turn_interrupted \
                and not event.get("is_error")

    def _hold(self, event: dict) -> None:
        """压下这一轮的结果：不交给调用方、不回报、不退出，等后台做完 claude 自己开的那一轮。"""
        with self.lock:
            self.held = event
            count = len(self.background)
            target = self.current
            # 压下那一轮说的（例如“已启动”）不是结论，后面那一轮的话才是
            self.last_say = ""
        # 告诉等结果的调用方：前面这轮说的话不是最终回复（它会清掉已收的正文）
        if target is not None:
            with suppress(OSError):
                target.sendall((json.dumps({"type": "cca_held", "background": count}) + "\n").encode("utf-8"))
        self._note_text(f"WAIT 后台还有 {count} 个任务在跑，做完再交结果")
        self._update_state(waiting_background=count)

    def _release_held(self) -> None:
        """主会话打断了等后台的这一轮，后台任务也停完了：claude 不会再开一轮，把压下的交出去（标成被打断）。"""
        with self.lock:
            event = self.held
            target = self.current
        if event is None:
            return
        line = json.dumps(event, ensure_ascii=False) + "\n"
        if target is not None:
            try:
                target.sendall(line.encode("utf-8"))
            except OSError:
                target = None
        self._end_turn(target, event)

    def _forward(self, target: socket.socket | None, line: str) -> None:
        event, line = self._annotate(line)
        if self._foreign_result(event):
            self._note_text("SKIP 这一轮的话还在排队，先出的这个结果不是它的")
            return
        if self._should_hold(event):
            self._hold(event)
            return
        if target is not None:
            try:
                target.sendall(line.encode("utf-8"))
            except OSError:
                target = None
        if event and event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    with self.lock:
                        self.turn_tools.add(str(block.get("name") or ""))
                        if block.get("name") == "Monitor":
                            self.monitor_tools.add(str(block.get("id") or ""))
        if event and event.get("type") == "result":
            self._end_turn(target, event)

    def _end_turn(self, target: socket.socket | None, event: dict) -> None:
        """这一轮出了结果：放开忙碌，记账、按需回报，接着发下一条。"""
        # 被主会话打断的那一轮 CC 也可能带 is_error：那不是故障，照原来的回报
        failed = bool(event.get("is_error")) and not self.turn_interrupted
        failure_text = self._failure_text(event) if failed else ""
        self._clear_api_retry()
        # 从“取走要不要回报”到“通知存好”一口气做完：退出收尾（_finish_open）同时来，也只有一边回报
        with self.finish_lock:
            self._settle_turn(target, event, failed=failed, failure_text=failure_text)
        if target is not None:
            with suppress(OSError):
                target.close()
        if not self._begin_followup():
            self._dispatch()

    def _settle_turn(self, target: socket.socket | None, event: dict, *, failed: bool, failure_text: str) -> None:
        """这一轮结束：放开忙碌、按 keep 安排退出、记任务、经唯一出口回报。调用方拿着 finish_lock。"""
        with self.lock:
            self.current = None
            self.busy = False
            self.held = None
            want_notify = self.notify_turn
            self.notify_turn = False
            followup = self.followup_turn
            self.followup_turn = False
            # 有人在等、结果却没送到（例如 MCP 服务重启，等结果的线程跟着没了）：
            # 没人会写任务记录、回报、安排退出，这里自己做
            lost = self.turn_waited and target is None and not followup
            if lost:
                # 等结果的调用方断了：没人拿得到结果，就算它没要回报也通知一次
                want_notify = True
            self.turn_waited = False
            job = self.current_job
            if not self.turn_keep:
                self.exit_at = time.time() + core.LINGER_SECONDS
        if lost:
            self._finish_job(job, event)
        self._update_state(notify=want_notify, busy=False, turn_started=0, waiting_background=0)
        if want_notify:
            # 不管谁在等、等的人还在不在，要回报的活结束了就经唯一出口通知一次
            if self.turn_interrupted:
                self._notify_main(str(event.get("result") or ""), status="interrupted",
                                  usage=self._usage_of(event), job=job)
            elif failed:
                # CC 自己放弃（重试用完、鉴权失败等）：以 failed 回报，带真实错误
                self._notify_main("", status="failed", error=failure_text, usage=self._usage_of(event), job=job)
            else:
                self._notify_main(str(event.get("result") or ""), usage=self._usage_of(event), job=job)

    def _failure_text(self, event: dict) -> str:
        """result 带 is_error：CC 自己放弃了这一轮。取它给的真实错误，附上最后一次 api_retry。"""
        text = " ".join(str(event.get("result") or event.get("subtype") or "未知错误").split())[:300]
        retry = self.api_retry
        if retry is not None:
            text += f"（最后一次 API 错误：{retry['summary']}，第 {_retry_ordinal(retry['attempt'], retry['max'])} 次重试）"
        return text

    def _finish_job(self, job: str, event: dict) -> None:
        """调用方断了：照它本来要写的样子，把这一轮的结果记进任务记录。"""
        self._note_text("CALLER lost 等结果的调用方断了，结果由 sideagent 自己记下")
        if not job:
            return
        with suppress(OSError):
            core.write_job(self.name, {"id": job, "account": self.name,
                                  "state": "interrupted" if event.get("cca_interrupted") else "finished",
                                  "text": str(event.get("result") or ""),
                                  "turns": event.get("num_turns"),
                                  "duration_ms": event.get("duration_ms"),
                                  "tool_uses": event.get("cca_tool_uses"),
                                  "token_count": event.get("cca_token_count")})

    def _idle_too_long(self) -> bool:
        """闲太久：没在干活、后台也没任务，又很久没有输出。压着结果等后台时不算闲。"""
        with self.lock:
            working = self.busy or bool(self.background)
        return not working and time.time() - self.last_activity > self.idle_seconds

    def _done_lingering(self) -> bool:
        """keep=false 的活干完、留够了时间、又没接新活：该退出了。"""
        with self.lock:
            return (bool(self.exit_at) and time.time() >= self.exit_at
                    and not self.busy and not self.queue and not self.background)

    def _begin_followup(self) -> bool:
        """插话没赶上这一轮（最后一步没有工具调用）：claude 会马上另开一轮跑它。

        这一轮没人在 socket 上等，结果照常写进度、按需回报；跑完之前不发排队的下一条，
        否则两轮的输出会混在一起。
        """
        with self.lock:
            waiting = [entry for entry in self.injected.values() if not entry.get("started")]
            if not waiting:
                return False
            self.busy = True
            self.current = None
            notifying = [entry for entry in waiting if entry.get("notify")]
            self.notify_turn = bool(notifying)
            # 用插话自己的任务编号：同一件活只通知一次靠它
            self.current_job = str((notifying or waiting)[0].get("job") or "")
            self.followup_turn = True
        self._start_turn_books()
        self._update_state(busy=True, turn_started=self.turn_started,
                           tokens=0, tokens_in=0, tokens_out=0, token_count=0, acted=False)
        return True

    def _drop_empty_followup(self) -> None:
        """当成插话另开的一轮，FOLLOWUP_GRACE 秒都没输出：多半插话其实已并进上一轮。

        就当没有这一轮，放开忙碌，接着发排队的下一条，不让它一直挂着。
        """
        with self.lock:
            if not (self.followup_turn and self.busy) or self.held is not None:
                # 压着结果等后台的那一轮是真有的一轮，不是“没开成的插话轮”
                return
            if any(entry.get("started") for entry in self.injected.values()):
                # claude 已经报了插话开始：这一轮真开了，只是第一个字慢；真卡住由 _stalled/_give_up 收尾
                return
            if time.time() - self.last_output <= core.FOLLOWUP_GRACE:
                return
            self.busy = False
            self.followup_turn = False
            self.notify_turn = False
            dropped = list(self.injected.values())
            self.injected.clear()
        self._update_state(busy=False, turn_started=0)
        self._report_dropped(dropped, status="failed", error="claude 没有处理这条插话")
        self._dispatch()

    def _report_dropped(self, entries: list, *, status: str, error: str = "") -> None:
        """没被处理就丢掉的插话：要回报的各通知一次（被打断、没处理、sideagent 退出）。"""
        for entry in entries:
            if entry.get("notify"):
                self._notify_main("", status=status, job=str(entry.get("job") or ""), settle=True, error=error)

    def _start_turn_books(self) -> None:
        """新一轮开始：清掉上一轮的账。"""
        self.turn_tools = set()
        self.turn_tool_uses = 0
        self.turn_in = 0
        self.turn_out = 0
        self.msg_out = 0
        self.latest_in = 0
        self.stream_chars = 0
        self.acted = False
        self.turn_interrupted = False
        self.turn_started = time.time()
        self.last_say = ""
        # 卡住的计时从这一轮开始算：空闲了很久再接活，不能拿上一次输出的时间当起点
        self.last_output = self.turn_started
        # 新的一轮：记录重新数；短语留着（照 Claude Code：做完、闲着、再接活时都显示上一句，
        # 下一次写短语时它就是 Previous）
        self.summary_notes = ""
        self.api_retry = None

    def _accept_loop(self) -> None:
        while not self.stop_flag.is_set():
            if not self._accept_once():
                break

    def _accept_once(self) -> bool:
        """接一条请求。返回 False 表示该退出了。"""
        if self.server is None:
            return False
        try:
            conn, _ = self.server.accept()
        except TimeoutError:
            if self.process is not None and self.process.poll() is not None:
                return False
            self._drop_empty_followup()
            if self._stalled():
                self._give_up()
                return False
            if self._idle_too_long() or self._done_lingering():
                return False
            self._touch()
            return True
        self._handle(conn)
        return True

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(core.SOCKET_TIMEOUT)
        try:
            request = core._parse(_read_line(conn)) or {}
        except OSError:
            conn.close()
            return
        if request.get("interrupt"):
            busy = self._interrupt()
            _reply(conn, {"type": "cca_ack", "busy": busy})
            conn.close()
            return
        text = str(request.get("text") or "")
        if text:
            # 干完后安排的“过一会儿退出”看到这一条，就知道又接了新活，不退
            self.exit_at = 0.0
            self._update_state(last_request=time.time())
        if not text:
            # 健康检查（running()）连一下就关，不带消息：不能当成一次请求。
            conn.close()
            return
        notify = bool(request.get("notify"))
        detach = bool(request.get("detach"))
        job = str(request.get("job") or "")
        # 老调用方不带 keep：当成要留着，退不退由它自己管（stop_later）
        keep = request.get("keep") is not False
        if request.get("interject") and self._inject(text, notify=notify, job=job, conn=conn):
            return
        with self.lock:
            if len(self.queue) >= core.QUEUE_LIMIT:
                _reply_error(conn, "排队太多了，等前面的跑完再发")
                return
            # 只发不等的：不留连接，结果照样写进进度文件，界面自己看
            self.queue.append((None if detach else conn, text, notify, job, keep))
            queued = len(self.queue)
        if detach:
            if request.get("interject"):
                _reply(conn, {"type": "cca_ack", "mode": "queue"})
            conn.close()
        self._update_state(queued=queued)
        self._dispatch()

    def _inject(self, text: str, *, notify: bool, job: str, conn: socket.socket) -> bool:
        """插话：它正忙就直接写进 claude 的输入，claude 在下一个工具调用结束时并进这一轮。

        它闲着就返回 False，走普通的开一轮。消息带 uuid，claude 会用 command_lifecycle
        报它什么时候开始处理，见 _note_control。
        """
        with self.lock:
            if not self.busy or self.process is None or self.process.stdin is None:
                return False
            key = str(uuid.uuid4())
            self.injected[key] = {"text": " ".join(text.split())[:120], "notify": notify,
                                  "job": job, "started": False}
            merged_into = self.current_job
        message = {"type": "user", "uuid": key,
                   "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except OSError:
            with self.lock:
                self.injected.pop(key, None)
            _reply_error(conn, "写不进 sideagent 的输入，插话没送到")
            return True
        _reply(conn, {"type": "cca_ack", "mode": "interject", "merged_into": merged_into,
                      "uuid": key})
        conn.close()
        return True

    def _stalled(self) -> bool:
        """在一轮里，超过 STALL_SECONDS 一条输出都没有。api_retry 也算输出，所以 CC 在重试时不会触发。"""
        if not self.busy or self.background:
            # 后台任务在跑时没有输出是正常的（例如 sleep 的后台命令）
            return False
        return time.time() - self.last_output > core.STALL_SECONDS

    def _stall_text(self, seconds: int) -> str:
        """按实际情况写卡住的原因：这一轮出现过 api_retry 就报最后一次错误，否则只陈述事实。"""
        retry = self.api_retry
        if retry is None:
            return f"卡住了：{seconds} 秒没有任何输出，claude 进程没有动静"
        ago = int(time.time() - retry["at"])
        return (f"卡住了：{seconds} 秒没有任何输出；最后一次 API 错误：{retry['summary']}"
                f"（第 {_retry_ordinal(retry['attempt'], retry['max'])} 次重试，{ago} 秒前）")

    def _give_up(self) -> None:
        """卡住就收摊：写一行说明，叫醒等着的调用方，让界面看到原因，别一直挂着。"""
        seconds = int(time.time() - self.last_output)
        text = self._stall_text(seconds)
        with suppress(OSError), core._events_path(self.name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "error", "text": text,
                                     "t": round(time.time() - self.turn_started, 1)},
                                    ensure_ascii=False) + "\n")
        with suppress(OSError), self.progress.open("a", encoding="utf-8") as handle:
            handle.write(f"STUCK {text}\n")
        self._update_state(busy=False, turn_started=0, stalled=text, api_retry="")
        # 照子代理：失败也回报一次（status failed），主会话不用干等。
        # 先存好通知再回调用方：调用方收到错误会补一条，看到已通知过就不补，同一件活只一条
        with self.finish_lock:
            with self.lock:
                # 等结果的调用方已经断了：没人收得到错误，就算它没要回报也通知一次
                notify = self.notify_turn or (self.turn_waited and self.current is None)
                self.notify_turn = False
            if notify:
                self._notify_main(self.last_say, status="failed", error=text, job=self.current_job, settle=True,
                                  usage={"tokens": self.token_count(), "tool_uses": self.turn_tool_uses,
                                         "duration_ms": int((time.time() - self.turn_started) * 1000)})
        target = self.current
        if target is not None:
            _reply_error(target, text)

    def _interrupt(self) -> bool:
        """打断当前这一轮，进程不退出。返回打断时是不是在干活。"""
        if self.process is None or self.process.stdin is None:
            return False
        with self.lock:
            busy = self.busy
            if busy:
                self.turn_interrupted = True
            waiting = sorted(self.background) if self.held is not None else []
            if waiting:
                self.held = {**self.held, "cca_interrupted": True}
        if busy:
            self._note_text("INTERRUPT 主会话打断了这一轮")
        if waiting:
            # 结果压着、claude 闲着等后台：打断 = 把这一轮起的后台任务停掉。
            # 停完 claude 发空名单，那时把压下的结果交出去（_set_background），claude 不会再开一轮
            for task in waiting:
                self._send_control({"subtype": "stop_task", "task_id": task})
            return busy
        self._send_control({"subtype": "interrupt"})
        return busy

    def _send_control(self, request: dict) -> None:
        """给 claude 发一条控制请求（打断、停后台任务）。"""
        if self.process is None or self.process.stdin is None:
            return
        line = {"type": "control_request", "request_id": f"{request['subtype']}-{time.time_ns()}",
                "request": request}
        with suppress(OSError):
            self.process.stdin.write(json.dumps(line, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _dispatch(self) -> None:
        """取队首开跑。已经有一轮在跑就什么都不做。"""
        with self.lock:
            if self.busy or not self.queue:
                return
            target, text, notify, job, keep = self.queue.popleft()
            self.busy = True
            self.current = target
            self.notify_turn = notify
            self.current_job = job
            self.turn_waited = target is not None
            self.turn_keep = keep
        if self.process is None or self.process.stdin is None:
            with self.lock:
                self.busy = False
                self.current = None
            self._update_state(busy=False)
            if target is not None:
                _reply_error(target, "sideagent进程不在")
            return
        # 这一轮的账，谁发来的都记（tell 只发不等，也得算）
        self._start_turn_books()
        # 开头先写这一轮交办的事，界面上先显示“在干什么”
        task = " ".join(text.split())[:120]
        self.turn_task = task
        # 进度文件每轮重写；上一句进度短语接着写上（照子代理：一直显示到下一句替换它）
        kept = f"SUMMARY {self.summary_text}\n" if self.summary_text else ""
        with suppress(OSError):
            self.progress.write_text(f"TASK {task}\n{kept}", encoding="utf-8")
        # 追加，不重写：界面靠这个文件画整段对话，翻回去还能看到前面几轮
        self._trim_events()
        with suppress(OSError), core._events_path(self.name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "task", "text": task}, ensure_ascii=False) + "\n")
        self._clear_live()
        self._update_state(tokens=0, tokens_in=0, tokens_out=0, token_count=0, acted=False,
                           busy=True,
                           turn_started=self.turn_started, queued=len(self.queue))
        key = str(uuid.uuid4())
        with self.lock:
            self.turn_uuid = key
            self.turn_queued = False
        message = {"type": "user", "uuid": key,
                   "message": {"role": "user",
                               "content": [{"type": "text", "text": text}]}}
        with suppress(OSError):
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _touch(self) -> None:
        self._update_state(last_activity=self.last_activity)

    def _finish_open(self) -> None:
        """要退出了：手上这一轮（含压着结果等后台的）和排着队的，凡是要回报的都通知一次。

        被停掉（收到 SIGTERM）算 stopped；没人停、claude 自己退了算失败。
        """
        killed = self.stop_requested
        status = "killed" if killed else "failed"
        with self.finish_lock:
            with self.lock:
                busy, job = self.busy, self.current_job
                # 等结果的调用方已经断了：没人收得到“停了”，就算它没要回报也通知一次
                notify = self.notify_turn or (self.turn_waited and self.current is None)
                held = self.held
                queued = list(self.queue)
                waiting = [entry for entry in self.injected.values() if not entry.get("started")]
                self.queue.clear()
                self.injected.clear()
                self.notify_turn = False
                self.busy = False
            if busy and notify:
                said = str((held or {}).get("result") or "") or self.last_say
                self._notify_main(said, status=status, job=job, settle=True,
                                  error="" if killed else "sideagent 的 claude 进程退出了，这一轮没做完")
            for conn, _text, wants, queued_job, _keep in queued:
                if wants:
                    self._notify_main("", status=status, job=queued_job, settle=True,
                                      error="" if killed else "sideagent 退出了，这条排着队还没开始")
                if conn is not None:
                    _reply_error(conn, "sideagent 停了，这条没开始")
            for entry in waiting:
                if entry.get("notify") and str(entry.get("job") or "") != job:
                    self._notify_main("", status=status, job=str(entry.get("job") or ""), settle=True,
                                      error="" if killed else "sideagent 退出了，这条插话还没开始")

    def _shutdown(self) -> None:
        self.stop_flag.set()
        self._finish_open()
        self._unmirror()
        # 会话编号、代码版本记进留存的记录（追问接着开靠它）
        state = core._read_state(self.name)
        core.remember_instance(self.name, core._record_of(state))
        link = str(state.get("context_link") or "")
        if link:
            with suppress(OSError):
                Path(link).unlink()
        # 停了：删 run/，state.json 写 finished_at，其余留着（规则 8）
        for closer in (self.server.close, lambda: self.socket_path.unlink(missing_ok=True),
                       lambda: self.process.terminate() if self.process else None,
                       lambda: core.mark_finished(self.name)):
            with suppress(OSError):
                closer()


def _safe_int(value: object) -> int:
    """api_retry 的 attempt / max_retries：CC 版本不同可能给非整数，解析不了就当没有（0）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _retry_ordinal(attempt: int, limit: int) -> str:
    """“2/10”；上限不明就只写第几次。"""
    return f"{attempt}/{limit}" if limit > 0 else str(attempt)


def api_error_summary(event: dict) -> str:
    """api_retry 行 → 一句错误摘要：状态码，或首字节超时等了多久。"""
    status = event.get("error_status")
    names = {504: "504 网关超时", 502: "502 网关错误", 503: "503 服务不可用", 429: "429 被限流",
             401: "401 鉴权失败", 529: "529 服务过载"}
    if isinstance(status, int):
        return names.get(status, f"HTTP {status}")
    waited = (event.get("no_response") or {}).get("waited_ms")
    if isinstance(waited, (int, float)):
        return f"首字节超时（等了 {int(waited / 1000)} 秒没有响应）"
    return " ".join(str(event.get("error") or "连接失败").split())[:80]


def _read_line(conn: socket.socket) -> bytes:
    buffer = b""
    while not buffer.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buffer += chunk
    return buffer


def _escape_xml(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _reply(conn: socket.socket, data: dict) -> None:
    with suppress(OSError):
        conn.sendall((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))


def _reply_error(conn: socket.socket, message: str) -> None:
    with suppress(OSError):
        conn.sendall((json.dumps({"type": "error", "message": message},
                                 ensure_ascii=False) + "\n").encode("utf-8"))
    with suppress(OSError):
        conn.close()


def serve() -> None:
    """sideagent进程本体。参数：号、目录、闲置秒数、claude 参数。"""
    import signal  # noqa: PLC0415 - 只有守护进程要

    name, cwd, idle_seconds_text, *claude_args = sys.argv[1:]
    server = _SideagentServer(name, cwd, int(idle_seconds_text), claude_args)

    def on_term(*_args) -> None:
        # 不在信号里直接退出（会打断写文件）：只让主循环停下，退出前照常收尾、通知
        server.stop_requested = True
        server.stop_flag.set()

    signal.signal(signal.SIGTERM, on_term)
    server.run()
