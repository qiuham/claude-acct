"""sideagent 命令行入口（cca sideagent ...）：tell、version、bg-sync、stop-owned、release，以及停进程、清理残留文件。

从 sideagent.py 拆出来；引用一律写 core.X（包括本文件自己的函数），测试照旧 patch sideagent 模块就能生效。"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import suppress
from pathlib import Path

from cca.errors import die, say
from cca.session import sideagent as core


def cmd_sideagent(args: list) -> int:
    """面板和调试用的几个子命令；派活走对话里的工具（sideagent_send 等），不从命令行用。"""
    if not args or args[0] in ("-h", "--help"):
        say(core._usage())
        return 0
    handler = SUBCOMMANDS.get(args[0])
    if handler is None:
        return die("命令行只支持 `cca sideagent tell <号> 「要说的话」`；"
                   "派活请在对话里用 sideagent_send")
    return handler(args[1:])


def _version_command(_args: list) -> int:
    # 面板拿它和状态里的 code_version 比，对不上就标“旧版”
    say(core.CODE_VERSION)
    return 0


def _bg_sync_command(args: list) -> int:
    # 平时由 MCP 服务的后台线程同步；这里留给 MCP 没起来时面板兜底和手动调试
    from cca.session import backends  # noqa: PLC0415

    backends.sync_owned(args[0].strip() if args else "")
    return 0


def _takeover_candidates_command(args: list) -> int:
    # 面板接管挑选框用：本机各号能接管的会话（JSON）；--all 连在跑的一起列
    from cca.session import takeover  # noqa: PLC0415

    owner = str(core.caller_now().get("sessionId") or "")
    print(json.dumps(takeover.picker_rows(include_running="--all" in args, owner_session=owner),  # noqa: T201
                     ensure_ascii=False))
    return 0


def _latest_command(args: list) -> int:
    # 面板用：这些会话编号各自最新的那一份（/bg 等会把对话复制成新编号接着跑）。JSON：{编号: 最新编号}
    print(json.dumps({session: core.latest_session(session) for session in args if session.strip()},  # noqa: T201
                     ensure_ascii=False))
    return 0


def _release_command(args: list) -> int:
    """面板的“拿掉”按钮：把一个接管的会话从这个主会话的名单里拿掉，会话本身不停。"""
    from cca.session import takeover  # noqa: PLC0415 - 只有这个命令用

    name, owner = ([item.strip() for item in args] + ["", ""])[:2]
    if not name or not owner:
        die("用法：cca sideagent release <名字> <主会话编号>")
    if takeover.release(name, owner) < 0:
        die(f"{name} 不是接管的会话")
    return 0


def _stop_owned_command(args: list) -> int:
    if not args or not args[0].strip():
        die("用法：cca sideagent stop-owned <会话编号>")
    core.stop_owned(args[0].strip())
    return 0


def _usage() -> str:
    from cca.config import COMMAND_USAGE  # noqa: PLC0415 - 和 cca sideagent --help 同一份

    return COMMAND_USAGE["sideagent"]


def _start_command(args: list) -> int:
    name, rest, flags = core._split(args)
    if not name:
        die("要给号：cca sideagent start <号>")
    core._ensure_started(name, str(flags.get("--cwd") or ""),
                    int(flags.get("--idle") or core.DEFAULT_IDLE_MINUTES) * 60,
                    claude_args=rest, context=str(flags.get("--context") or ""))
    return 0


def _send_command(args: list) -> int:
    name, rest, flags = core._split(args)
    if not name or not rest:
        die("用法：cca sideagent send <号> 「要做的事」")
    timeout = float(flags.get("--timeout") or core.SEND_TIMEOUT)
    return core._run_once(name, " ".join(rest), timeout,
                     keep=bool(flags.get("--keep")), raw=bool(flags.get("--json")))


def _tell_command(args: list) -> int:
    name, rest, flags = core._split(args)
    if not name or not rest:
        die("用法：cca sideagent tell [--interject] <号> 「要说的话」")
    from cca.session import backends  # noqa: PLC0415 - backends 导入本模块，延迟破环

    error = backends.backend_for(name).tell(name, " ".join(rest), interject=bool(flags.get("--interject")))
    if error:
        die(error)
    return 0


def _tell(name: str, text: str) -> int:
    """只发不等：sideagent在忙就排队，忙完接着做。

    面板回车用的是 `--interject`（插话，和主界面忙时打字一样，下一个工具调用结束就送到）。
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(str(core._socket_path(name)))
    except OSError as exc:
        die(f"{name}: 连不上sideagent（{exc}）")
    try:
        sock.sendall((json.dumps({"text": text, "detach": True}, ensure_ascii=False)
                      + "\n").encode("utf-8"))
    except OSError as exc:
        die(f"{name}: 发不出去（{exc}）")
    finally:
        sock.close()
    return 0


def _shorthand(args: list) -> int:
    name, rest = args[0], args[1:]
    if not rest:
        die(core._usage())
    return core._run_once(name, " ".join(rest), core.SEND_TIMEOUT, keep=False, raw=False)


def _run_once(name: str, text: str, timeout: float, *, keep: bool, raw: bool) -> int:
    """发一次活：这个号上已经有同名sideagent就接上，没有就临时起一个，干完销毁。"""
    agent_name, started = core.prepare(name)
    try:
        return core._send(agent_name, text, timeout, raw=raw)
    finally:
        if started and not keep:
            core.stop_later(agent_name)


def prepare(account: str, *, cwd: str = "", context: str = "", label: str = "",  # noqa: PLR0913
            description: str = "", model: str = "", caller_agent: str = "") -> tuple[str, bool]:
    """派新活：一律新开一个实例（<号>#哈希），上下文从零开始，像子代理。返回（名字，是不是新起的）。"""
    name = core._oneshot_name(core._account_of(account))
    started = core._ensure_started(name, cwd or str(Path.cwd()), core.DEFAULT_IDLE_MINUTES * 60, [],
                              context=context, label=label, description=description, model=model,
                              caller_agent=caller_agent)
    return name, started


def _state_files():
    """所有 sideagent 的 state.json（停了的也在，看 finished_at）。"""
    return [core._state_path(name) for name in core.agent_names()]


def _list() -> int:
    from cca.session import roster  # noqa: PLC0415 - roster 反过来要用本模块

    # 顺手清理过期的文件夹和没人用的名单（规则 9）
    roster.cleanup()
    names = core.agent_names(include_finished=False)
    if not names:
        say("还没有sideagent在跑")
        return 0
    for name in names:
        state = core._read_state(name)
        state_text = "在跑" if core.running(name) else "已停"
        idle_min = int(time.time() - float(state.get("last_activity") or 0)) // 60
        say(f"{name:10} {state_text}  会话 {state.get('session_id') or '?'}  闲置 {idle_min} 分钟")
    return 0


def _interrupt_command(args: list) -> int:
    """打断sideagent当前这一轮，进程继续活着。"""
    name = args[0] if args else ""
    if not name:
        die("要给号：cca sideagent interrupt <号>")
    if not core.running(name):
        die(f"{name}: 没在跑")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect(str(core._socket_path(name)))
        sock.sendall(b'{"interrupt": true}\n')
    except OSError as exc:
        die(f"{name}: 连不上sideagent（{exc}）")
    finally:
        sock.close()
    say(f"{name}: 已发送中断")
    return 0


def stop_owned(session_id: str) -> int:
    """停掉某个会话派出去的所有sideagent（会话结束、或在面板里收工时用）。

    只看这个会话自己的名单（一串编号合起来）；接管的不停（只当作解开，规则 6），会话照跑。
    """
    from cca.session import backends, roster  # noqa: PLC0415 - backends 导入本模块，延迟破环

    stopped = 0
    if not session_id:
        return 0
    for name, entry in roster.mine(session_id).items():
        if entry.get("kind") == roster.ATTACHED or backends.backend_for(name).outlives_owner:
            continue
        if core._read_state(name).get("finished_at"):
            continue
        core._stop([name])
        stopped += 1
    return stopped


def _serve_pids(name: str) -> list:
    """命令行正好是 `python -c "...serve()" <名字> ...` 的进程：只认这个名字，不会误伤别的实例。"""
    found = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        args = [part.decode("utf-8", "replace") for part in argv]
        if core.SERVE_CODE in args and args.index(core.SERVE_CODE) + 1 < len(args) \
                and args[args.index(core.SERVE_CODE) + 1] == name and int(entry.name) != os.getpid():
            found.append(int(entry.name))
    return found


def _stop(args: list) -> int:
    name = args[0] if args else ""
    if not name:
        die("要给号：cca sideagent stop <号>")
    state = core._read_state(name)
    for pid in (int(state.get("pid") or 0), int(state.get("child_pid") or 0)):
        if pid > 0:
            with suppress(OSError):
                os.kill(pid, 15)
    # 状态里的进程号可能过期（状态文件被盖过）：按命令行精确找这个实例的守护进程
    for pid in core._serve_pids(name):
        with suppress(OSError):
            os.kill(pid, 15)
    # 状态文件要删了：会话编号、代码版本先记进留存的记录（退出后查状态、追问都靠它）
    core.remember_instance(name, core._record_of(state))
    link = str(state.get("context_link") or "")
    if link:
        with suppress(OSError):
            Path(link).unlink()
    # 停了：删 run/，state.json 写 finished_at，其余留着（规则 8）
    core.mark_finished(name)
    say(f"{name}: 已停")
    return 0


def _split(args: list) -> tuple:
    """拆出号、位置参数、`--flag 值`。没认过的 flag 原样留给 claude。"""
    name = ""
    rest: list = []
    flags: dict = {}
    index = 0
    while index < len(args):
        item = str(args[index])
        head, sep, value = item.partition("=")
        if head in core.OWN_FLAGS:
            if sep:
                flags[head] = value
                index += 1
            else:
                flags[head] = args[index + 1] if index + 1 < len(args) else ""
                index += 2
            continue
        if head in core.BOOL_FLAGS:
            flags[head] = True
        elif not name and not item.startswith("-"):
            name = item
        else:
            rest.append(item)
        index += 1
    return name, rest, flags


def _takeover_command(args: list) -> int:
    """面板挑选框按回车后调：接管一个会话，挂到发起命令的主会话上；结果打印成 JSON。"""
    from cca.session import takeover  # noqa: PLC0415

    key = next((item for item in args if not item.startswith("--")), "")
    account = next((item.split("=", 1)[1] for item in args if item.startswith("--account=")), "")
    entry = core.caller_now()
    owner = ({**core.owner_of(entry), "account": core._owner_account(),
              "instance_socket": str(entry.get("messagingSocketPath") or "")} if entry else {})
    if not key:
        result = {"error": "要给会话编号"}
    elif not owner.get("session"):
        result = {"error": "找不到发起命令的主会话，接不上"}
    else:
        result = takeover.takeover(key, owner, account=account)
    print(json.dumps(result, ensure_ascii=False))  # noqa: T201
    return 1 if result.get("error") else 0


def _permission_hook_command(_args: list) -> int:
    # 各号 settings.json 里的 PermissionRequest 钩子调它；没被接管的会话立刻无输出退出
    from cca.session import permission  # noqa: PLC0415

    return permission.hook_main()


def _permission_answer_command(args: list) -> int:
    """面板按“允许/拒绝”时调：cca sideagent permission-answer <实例名> allow|deny [请求编号]。"""
    from cca.session import permission  # noqa: PLC0415

    if len(args) < 2:  # noqa: PLR2004 - 至少要实例名和 allow/deny
        die("用法：cca sideagent permission-answer <实例名> allow|deny [请求编号]")
    error = permission.answer(args[0], args[1], args[2] if len(args) > 2 else "")  # noqa: PLR2004
    if error:
        die(error)
    return 0


def _hook_command(args: list) -> int:
    """cca sideagent hook install|uninstall --profile <号> | --all：把确认转发钩子装进/拿出各号 settings.json。"""
    import shutil  # noqa: PLC0415

    from cca import plugin  # noqa: PLC0415
    from cca.paths import profile_dir  # noqa: PLC0415
    from cca.session import permission  # noqa: PLC0415

    action = args[0] if args else ""
    if action not in ("install", "uninstall"):
        die("用法：cca sideagent hook install|uninstall --profile <号> | --all")
    rest = args[1:]
    # 装钩子会影响这个号上的每个会话：必须写清装到哪，不默认装到当前号
    if not any(item == "--all" or item.startswith("--profile") for item in rest):
        die("要写清装到哪：--profile <号> 或 --all")
    command = f"{shutil.which('cca') or 'cca'} {permission.HOOK_MARK}"
    for name in plugin.profiles(rest):
        path = profile_dir(name) / "settings.json"
        changed = (permission.install_into(path, command) if action == "install"
                   else permission.uninstall_from(path))
        word = {"install": ("已装上", "已经装过，没改"), "uninstall": ("已去掉", "本来就没装")}[action]
        say(f"{name}: {word[0] if changed else word[1]}（{path}）")
    return 0


SUBCOMMANDS = {
    "permission-hook": _permission_hook_command,
    "permission-answer": _permission_answer_command,
    "hook": _hook_command,
    "tell": _tell_command,
    "version": _version_command,
    "bg-sync": _bg_sync_command,
    "takeover-candidates": _takeover_candidates_command,
    "takeover": _takeover_command,
    "latest": _latest_command,
    "stop-owned": _stop_owned_command,
    "release": _release_command,
}
