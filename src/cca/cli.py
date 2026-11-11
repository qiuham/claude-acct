"""命令行入口：参数解析、帮助、分发。业务逻辑都在别的模块。"""

from __future__ import annotations

import contextlib
import difflib
import os
import shutil
import sys
import time
import unicodedata
from pathlib import Path

from cca.account.model import require_profile
from cca.account.store import ensure_onboarding_flag, profile_kind
from cca.api.client import api_launch_args, check_api_profile, identity_banner, models_banner
from cca.config import COMMAND_USAGE, REMOVED_COMMANDS
from cca.env import launch_env
from cca.errors import die, note, say
from cca.paths import repo_root


def did_you_mean(word: str, options) -> str:
    """给拼错的 flag 猜一个最接近的候选（stdlib difflib），猜不出返回空串。"""
    hits = difflib.get_close_matches(word, list(options), n=1, cutoff=0.6)
    return hits[0] if hits else ""

def parse_flags(args: list, bool_flags: tuple, value_flags: tuple = (), usage: str = "",
                multi_flags: tuple = ()) -> tuple:
    """拆 `--flag` / `--flag=值` 两种写法，返回 (位置参数, {flag: True 或 值})。

    **未知 flag 一律报错**，不静默忽略 —— 静默忽略是最坏的失败方式：`cca switch 002 --cary`
    看着像成功了，其实什么都没接管。只有 `=` 形式带值，所以 `--only` 裸写仍是它自己的含义
    （进交互选择），不会把后面的位置参数吃掉。

    `multi_flags` 里的参数可重复，值按出现顺序收进列表（`--only=a --only=b` → `["a", "b"]`）。
    """
    rest, flags = [], {}
    for arg in args:
        head, _, value = arg.partition("=")
        if head in multi_flags:
            flags.setdefault(head, []).append(value)
        elif head in value_flags:
            flags[head] = value
        elif arg in bool_flags:
            flags[arg] = True
        elif arg.startswith("-") and arg != "-":
            hint = did_you_mean(arg, tuple(bool_flags) + tuple(value_flags) + tuple(multi_flags))
            die(
                f"未知参数 {arg}"
                + (f"；是想用 {hint} 吗？" if hint else "")
                + (f"\n用法: {usage}" if usage else "")
            )
        else:
            rest.append(arg)
    return rest, flags

def merge_space_value_flags(args: list, value_flags: tuple) -> list:
    """把 `--flag value` 合成 `--flag=value`（`parse_flags` 只认 `=` 形式）。

    只给 `cca model` 用：它要收 slug，写成 `--main openrouter/free` 最自然。
    """
    out, index = [], 0
    while index < len(args):
        arg = str(args[index])
        if arg in value_flags and index + 1 < len(args) and not str(args[index + 1]).startswith("-"):
            out.append(f"{arg}={args[index + 1]}")
            index += 2
            continue
        out.append(arg)
        index += 1
    return out

# 总帮助：按"要干什么"分组，一行一条。加命令只改这张表，对齐交给 usage()。
HELP_SECTIONS = (
    ("启动（号写在命令里，后面的参数原样交给 claude）", (
        ("cca <号> [claude 参数…]", "例：cca ds · cca 002 -c · cca 002 -r <完整id>"),
        ("cca <号> --bg '<任务>'", "在这个号上起一个后台会话"),
    )),
    ("看", (
        ("cca ls [--wide]", "号面板：每个号的额度（已用 % + 重置时刻）"),
        ("cca status [<号>]", "每个号一段摘要；带号 = 单号详情和后台会话"),
        ("cca agents [--all]", "跨号会话总表：现在每条会话在哪个号、什么状态（只看）"),
        ("cca sessions [--all]", "能续的会话（会话不绑号，任何号都能续）"),
        ("cca which <号>", "这个号是谁：目录 / 类型 / 端点"),
    )),
    ("会话与额度", (
        ("cca handoff <源号> <目标号>", "把后台会话搬到另一个号接着跑"),
        ("cca wake <号>", "额度重置后在原号叫醒选中的会话（睡前排上）"),
        ("cca watchdog [status]", "常驻的额度看门狗：到提醒点的后台会话换号、撞墙的叫醒（自动起）"),
        ("cca quota-hint [<号>]", "额度快满时换不换、换到哪；不写号 = 现在先用哪个号"),
        ("cca stop-bg <号>|--all", "停掉后台会话（只停不删）；--all 换 workspace 前用，会记下清单"),
        ("cca resume-bg", "换 workspace 后照 stop-bg --all 的清单在原号续上"),
        ("cca trim <会话id>", "把超限会话的历史裁到窗口内（先备份）"),
    )),
    ("号", (
        ("cca add <号>", "建订阅号并登录"),
        ("cca add <号> --provider deepseek --key-stdin", "建 API 号；自备端点用 --url"),
        ("cca login <号> · cca logout <号>", "登录 / 登出；API 号用 login 换 key"),
        ("cca model [<号>] · cca models <号>", "看或改模型映射 · 看端点上有哪些模型"),
        ("cca mv <旧号> <新号> · cca rm <号>", "改名 · 删本地配置（对话留在共享池）"),
        ("cca new <号> [--isolated]", "只建目录；--isolated 不共享会话，用 cca import 导入"),
    )),
    ("sideagent（另一个号上的会话帮你干活；派活在对话里用 sideagent_* 工具）", (
        ("cca mcp install --all", "注册 MCP 工具（新建的号会自动注册）"),
        ("cca plugin install --all", "装面板插件"),
        ("cca sideagent hook install --all", "接管会话的「请确认」转到面板上点"),
    )),
    ("设置与维护", (
        ("cca set proxy <号>|--global <url|none>", "代理，单号优先；其余设置见 cca set --help"),
        ("cca sync <号>", "按默认 settings 重新播种（~/.cca/defaults/settings.json）"),
        ("cca doctor [--online]", "体检"),
        ("cca version", "版本"),
    )),
)


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


HELP_COLUMN_MAX = 40   # 命令列最宽多少；更长的命令说明另起一行


def usage() -> str:
    width = max(w for _, rows in HELP_SECTIONS for cmd, _ in rows
                if (w := _display_width(cmd)) <= HELP_COLUMN_MAX)
    lines = ["cca — 一台机器上管多个 Claude Code 号（订阅号 / API 号），会话在号之间互通"]
    for title, rows in HELP_SECTIONS:
        lines += ["", title]
        for cmd, desc in rows:
            pad = width - _display_width(cmd)
            # 太长的命令（建 API 号那条）单独一行，说明另起一行缩进对齐
            lines.append(f"  {cmd}{' ' * (pad + 2)}{desc}" if pad >= 0
                         else f"  {cmd}\n  {' ' * (width + 2)}{desc}")
    lines += ["", "每个子命令都有 cca <命令> --help。没有默认号：写谁就是谁；"
                  "不经 cca 直接跑 claude 用的是 ~/.claude。"]
    return "\n".join(lines) + "\n"

def cmd_run(name_or_none, claude_args: list) -> int:
    from cca.account.lifecycle import (  # noqa: PLC0415 - lifecycle 顶部导入本模块，延迟破环
        no_browser_env,
    )
    from cca.session.push import (  # noqa: PLC0415 - push 顶部导入本模块，延迟破环
        background_guard,
        clear_stale_resume_job,
    )

    name = require_profile(name_or_none, usage="cca <profile> [claude 参数…]")
    check_api_profile(name)
    if profile_kind(name) != "api":
        # 订阅号起会话：顺手保证额度看门狗在（全机一个，没事可做自己退出）；它出什么事都不该拦住启动
        from cca.session.watchdog import ensure_running  # noqa: PLC0415 - 用到才导入

        with contextlib.suppress(Exception):
            ensure_running()
    background_guard(name)
    if sys.stderr.isatty():
        print(f"→ 账号 {name} [{identity_banner(name)}]{models_banner(name)}", file=sys.stderr)  # noqa: T201 - CLI 输出边界
    if ensure_onboarding_flag(name):
        note(f"{name}: 已补上 hasCompletedOnboarding —— 该号首次交互启动不会再走引导")
    exe = shutil.which("claude")
    if not exe:
        die("找不到 claude 可执行文件，请确认已安装且在 PATH 中")
    claude_args = api_launch_args(name, claude_args)
    clear_stale_resume_job(name, claude_args)
    _sanitize_resume(name, claude_args)
    env = launch_env(name, claude_args)
    env.update(no_browser_env(name))          # 界面里用 /login 时也只打链接，不去 open 浏览器
    os.execvpe(exe, [exe, *claude_args], env)  # noqa: S606 - 这里就是要用 claude 可执行文件替换当前进程
    return 0  # 不会到达

def _sanitize_resume(name: str, claude_args: list) -> None:
    """续会话前清掉 Anthropic 不认的字段（Gemini 历史里的 tool_use.signature）。

    会话还活着、或者目标本身是 Gemini 号时不清：前者会丢回合，后者删了签名
    Gemini 下一轮反而会 400。
    """
    from cca.paths import profile_dir  # noqa: PLC0415
    from cca.session.transcript import (  # noqa: PLC0415 - 只在续会话时需要
        resume_id,
        session_live,
        strip_session,
        target_needs_strip,
    )

    session = resume_id(claude_args)
    if not session or not target_needs_strip(name):
        return
    if session_live(session):
        note("这条会话还有进程在跑，这次不改写记录（停了再续就会清）")
        return
    stripped = strip_session(session, profile_dir(name) / "projects")
    if stripped:
        note(f"清掉了 {stripped} 处 tool_use.signature（Gemini 历史），接着跑")


def cmd_version() -> int:
    vf = repo_root() / "VERSION"
    ver = vf.read_text(encoding="utf-8").strip() if vf.is_file() else "unknown"
    say(f"claude-acct {ver}")
    return 0

def _run_command(head: str, rest: list, dispatch: dict):
    """按子命令分发；不是已知命令时返回 None（留给调用方当号名处理）。"""
    if head in REMOVED_COMMANDS:
        die(f"{head} 已移除 —— {REMOVED_COMMANDS[head]}")
    if head in dispatch:
        if any(a in ("-h", "--help") for a in rest):
            say(COMMAND_USAGE.get(head, usage()))
            return 0
        return dispatch[head](rest)
    if head == "version":
        return cmd_version()
    if head.startswith("-"):
        # 参数写在号名前面是最常见的手误（`cca -c`）：说清正确写法，别让它去找名为 "-c" 的号。
        tail = " ".join([head, *rest]).strip()
        die(f"参数 {head} 要跟在号名后面：cca <profile> {tail}\n  例: cca 002 {tail}    看帮助: cca --help")
    return None

def main(argv=None) -> int:
    # 终端显示固定上海，但只在格式化时换算（report.format_clock），不改 os.environ：
    # 改了会把上海时区传给子进程，claude 的后台守护进程会把它固化下来。
    # 传给 claude 的时区跟出口 IP 走（见 env.launch_env）。
    from cca.account.lifecycle import (  # noqa: PLC0415 - lifecycle 顶部导入本模块，延迟破环
        cmd_add,
        cmd_handoff,
        cmd_import,
        cmd_login,
        cmd_logout,
        cmd_model,
        cmd_models,
        cmd_new,
        cmd_rename,
        cmd_rm,
        cmd_set,
        cmd_sync,
        cmd_which,
    )
    from cca.doctor import cmd_doctor  # noqa: PLC0415 - doctor 顶部导入本模块，延迟破环
    from cca.mcp_sideagent import cmd_mcp  # noqa: PLC0415 - 只在用 mcp 时才需要
    from cca.plugin import cmd_plugin  # noqa: PLC0415 - 只在装插件时才需要
    from cca.report import (  # noqa: PLC0415 - report 顶部导入本模块，延迟破环
        cmd_agents,
        cmd_list,
        cmd_sessions,
        cmd_status,
    )
    from cca.session.push import cmd_resume_bg, cmd_stop_bg  # noqa: PLC0415 - push 顶部导入本模块，延迟破环
    from cca.session.quota_hint import cmd_quota_hint  # noqa: PLC0415 - 用到才导入
    from cca.session.sideagent import cmd_sideagent  # noqa: PLC0415 - 只在用 sideagent 时才需要
    from cca.session.trim import cmd_trim  # noqa: PLC0415 - trim 顶部导入本模块，延迟破环
    from cca.session.wake import cmd_wake  # noqa: PLC0415 - 用到才导入
    from cca.session.watchdog import cmd_watchdog  # noqa: PLC0415 - 用到才导入
    argv = list(sys.argv[1:] if argv is None else argv)
    invoked = Path(sys.argv[0]).name

    if invoked == "ccl":  # ccl 是 list 的别名
        return cmd_list(argv)

    if not argv:
        # 裸跑 = 没想好要干什么，不是错误：直接给帮助，退出码 0（与 kubectl 一致）。
        say(usage())
        return 0
    if argv[0] in ("-h", "--help", "help"):
        say(usage())
        return 0

    head, rest = argv[0], argv[1:]
    dispatch = {
        "new": cmd_new,
        "import": cmd_import,
        "add": cmd_add,
        "login": cmd_login,
        "logout": cmd_logout,
        "status": cmd_status,
        "list": cmd_list,
        "ls": cmd_list,
        "rm": cmd_rm,
        "mv": cmd_rename,
        "rename": cmd_rename,   # 别名：文档与旧脚本还用 rename
        "handoff": cmd_handoff,
        "watchdog": cmd_watchdog,
        "wake": cmd_wake,
        "quota-hint": cmd_quota_hint,
        "model": cmd_model,
        "models": cmd_models,
        "sideagent": cmd_sideagent,
        "mcp": cmd_mcp,
        "plugin": cmd_plugin,
        "trim": cmd_trim,
        "stop-bg": cmd_stop_bg,
        "resume-bg": cmd_resume_bg,
        "sessions": cmd_sessions,
        "agents": cmd_agents,
        "which": cmd_which,
        "set": cmd_set,
        "sync": cmd_sync,
        "doctor": cmd_doctor,
    }
    handled = _run_command(head, rest, dispatch)
    if handled is not None:
        return handled

    # 其余情况：第一个参数是 profile，后面全部透传给 claude
    return cmd_run(head, rest)
