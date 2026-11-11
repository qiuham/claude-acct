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
from cca.session import sideagent as core
from cca.session import summary


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
        self.process: subprocess.Popen | None = None
        self.server: socket.socket | None = None

    def run(self) -> None:
        # run/ 只在跑的时候有：上次没收拾干净的先清掉
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
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
        words = {"completed": "finished", "failed": f"failed: {error or 'Unknown error'}",
                 "killed": "was stopped"}
        note = (f"每次它停下来都会回报一次。要接着问它，用 sideagent_send 发给 {self.name}"
                "（它退出了也能接着，上下文还在）。")
        parts = ["<task-notification>", f"<task-id>{self.name}</task-id>", f"<status>{status}</status>",
                 f"<summary>{_escape_xml(f'Agent {chr(34)}{title}{chr(34)} {words.get(status, status)}')}</summary>",
                 f"<note>{_escape_xml(note)}</note>"]
        if text:
            parts.append(f"<result>{_escape_xml(text[:core.NOTIFY_RESULT_CHARS])}</result>")
        if usage:
            parts.append(f"<usage><subagent_tokens>{usage.get('tokens', 0)}</subagent_tokens>"
                         f"<tool_uses>{usage.get('tool_uses', 0)}</tool_uses>"
                         f"<duration_ms>{usage.get('duration_ms', 0)}</duration_ms></usage>")
        parts.append("</task-notification>")
        return "\n".join(parts)

    def _notify_main(self, text: str, *, status: str = "completed", usage: dict | None = None,
                     error: str = "") -> None:
        """把这一轮的结果直接投到主会话的消息通道。不经过模型，不靠提示词。"""
        if not self.instance_socket:
            self._read_own_identity()
        # 每次投递前都现查：主会话可能换了编号、换了 socket，不能用起进程时记下的
        if not self._refresh_caller():
            self.caller_socket = ""
        if not self.caller_socket:
            # 别悄悄跳过：写一行原因，查“为什么没回报”时一看就知道
            self._note_text(f"NOTIFY skip 找不到主会话的消息 socket（登记表 {self.caller_sessions}，"
                            f"进程 {self.owner['pid'] or '?'} 和会话 {self.owner['session'] or '?'} "
                            "都没有登记条目：主会话已经关了，或者重开时换成了另一段对话）")
            return
        if not text and status == "completed":
            self._note_text("NOTIFY skip 结果是空的")
            return
        sender = f"uds:{self.instance_socket}"
        notice = self._notification(text, status=status, usage=usage or {}, error=error)
        body = (
            f'<cross-session-message from="{sender}" from-name="{self.instance_name or self.name}"'
            f' from-mode="{self.permission_mode}" cca-instance="{self.name}">\n{notice}\n'
            "</cross-session-message>"
        )
        frame = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user",
                 "message": {"role": "user", "content": body},
                 "priority": "next", "from": sender}
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            sock.connect(self.caller_socket)
            sock.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
            self._note_text("NOTIFY ok")
        except OSError as exc:
            self._note_text(f"NOTIFY fail {type(exc).__name__}")
        finally:
            sock.close()

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
            entry = self.injected.get(key)
            if entry is None:
                return
            if stage == "started" and not entry.get("started"):
                entry["started"] = True
                self._note_event({"kind": "task", "text": entry["text"], "interject": True})
                self._note_text(f"TASK （插话）{entry['text']}")
            elif stage in ("completed", "cancelled", "failed"):
                self.injected.pop(key, None)

    def _after_interrupt(self, response: dict) -> None:
        """打断后 claude 会说哪些排着的消息还留着；没留下的插话就不再等它开轮。"""
        if not str(response.get("request_id") or "").startswith("int-"):
            return
        kept = (response.get("response") or {}).get("still_queued")
        if not isinstance(kept, list):
            return
        keep = {str(item.get("uuid") if isinstance(item, dict) else item) for item in kept}
        with self.lock:
            for key in [key for key, entry in self.injected.items()
                        if not entry.get("started") and key not in keep]:
                self.injected.pop(key, None)

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
            if event.get("subtype") == "api_retry":
                self._note_api_retry(event)
                return
            entry = core.task_event(event)
            if entry:
                self._note_event(entry)
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
        """把一步详细经过写成一行 JSON，界面按主界面的样子画。"""
        entry = {**entry, "t": round(time.time() - self.turn_started, 1)}
        with suppress(OSError), core._events_path(self.name).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

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
            if self.turn_interrupted:
                extra["cca_interrupted"] = True
            event = {**event, **extra}
            line = json.dumps(event, ensure_ascii=False) + "\n"
        return event, line

    def _forward(self, target: socket.socket | None, line: str) -> None:
        event, line = self._annotate(line)
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
        if event and event.get("type") == "result":
            self._end_turn(target, event)

    def _end_turn(self, target: socket.socket | None, event: dict) -> None:
        """这一轮出了结果：放开忙碌，记账、按需回报，接着发下一条。"""
        # 被主会话打断的那一轮 CC 也可能带 is_error：那不是故障，照原来的回报
        failed = bool(event.get("is_error")) and not self.turn_interrupted
        failure_text = self._failure_text(event) if failed else ""
        self._clear_api_retry()
        with self.lock:
            self.current = None
            self.busy = False
            want_notify = self.notify_turn
            self.notify_turn = False
            followup = self.followup_turn
            self.followup_turn = False
            # 有人在等、结果却没送到（例如 MCP 服务重启，等结果的线程跟着没了）：
            # 没人会写任务记录、回报、安排退出，这里自己做
            lost = self.turn_waited and target is None and not followup
            self.turn_waited = False
            job = self.current_job
            if not self.turn_keep:
                self.exit_at = time.time() + core.LINGER_SECONDS
        if lost:
            self._finish_job(job, event)
        # 记下这一轮的结果是不是已经交给等着的调用方；
        # “有人在等”的由界面叫醒主代理
        # 插话自己的那一轮、调用方断了由这里直接回报的，也算“已交回”，界面就不再叫一遍
        self._update_state(delivered_to_caller=target is not None or followup or (lost and want_notify),
                           notify=want_notify, busy=False, turn_started=0)
        # 把“要叫醒”也写进进度：老面板只看进度文件
        if want_notify:
            with suppress(OSError), self.progress.open("a", encoding="utf-8") as handle:
                handle.write("NOTIFY 1\n")
        if want_notify and (target is not None or followup or lost):
            if failed:
                # CC 自己放弃（重试用完、鉴权失败等）：以 failed 回报，带真实错误
                self._notify_main("", status="failed", error=failure_text, usage=self._usage_of(event))
            else:
                self._notify_main(str(event.get("result") or ""), usage=self._usage_of(event))
        if target is not None:
            with suppress(OSError):
                target.close()
        if not self._begin_followup():
            self._dispatch()

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

    def _done_lingering(self) -> bool:
        """keep=false 的活干完、留够了时间、又没接新活：该退出了。"""
        with self.lock:
            return (bool(self.exit_at) and time.time() >= self.exit_at
                    and not self.busy and not self.queue)

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
            self.notify_turn = any(entry.get("notify") for entry in waiting)
            self.current_job = ""
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
            if not (self.followup_turn and self.busy):
                return
            if time.time() - self.last_output <= core.FOLLOWUP_GRACE:
                return
            self.busy = False
            self.followup_turn = False
            self.notify_turn = False
            self.injected.clear()
        self._update_state(busy=False, turn_started=0)
        self._dispatch()

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
            if time.time() - self.last_activity > self.idle_seconds or self._done_lingering():
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
        if not self.busy:
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
        target = self.current
        if target is not None:
            _reply_error(target, text)
        self._update_state(busy=False, turn_started=0, stalled=text, api_retry="")
        # 照子代理：失败也回报一次（status failed），主会话不用干等
        if self.notify_turn:
            self.notify_turn = False
            self._notify_main("", status="failed", error=text,
                              usage={"tokens": self.token_count(), "tool_uses": self.turn_tool_uses,
                                     "duration_ms": int((time.time() - self.turn_started) * 1000)})

    def _interrupt(self) -> bool:
        """打断当前这一轮，进程不退出。返回打断时是不是在干活。"""
        if self.process is None or self.process.stdin is None:
            return False
        with self.lock:
            busy = self.busy
            if busy:
                self.turn_interrupted = True
        if busy:
            self._note_text("INTERRUPT 主会话打断了这一轮")
        request = {"type": "control_request", "request_id": f"int-{int(time.time() * 1000)}",
                   "request": {"subtype": "interrupt"}}
        with suppress(OSError):
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        return busy

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
        message = {"type": "user",
                   "message": {"role": "user",
                               "content": [{"type": "text", "text": text}]}}
        with suppress(OSError):
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _touch(self) -> None:
        self._update_state(last_activity=self.last_activity)

    def _shutdown(self) -> None:
        self.stop_flag.set()
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

    signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
    name, cwd, idle_seconds_text, *claude_args = sys.argv[1:]
    _SideagentServer(name, cwd, int(idle_seconds_text), claude_args).run()
