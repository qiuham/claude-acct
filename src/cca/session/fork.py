"""`cca fork`：从当前会话分出一条独立的后台会话，交给它一块活。

和 sideagent 的区别：sideagent 是派活的会话「名下」的帮手，干完回报、闲置就退；fork 出来的是一条
**独立的正常会话**——有自己的名字，出现在 cca agents，额度到点由 watchdog 换号，别人按名字就能
找到它（sideagent_send {session: 名字}），一跑几天也没关系。会话自己就能分，不用人去终端操作。

    cca fork <名字> [--to <号>] [--fresh] [--from <会话>] "<交办>"      # 交办写 - 从 stdin 读

- 默认带上来源会话到此为止的完整对话（`--resume <来源> --fork-session`）；`--fresh` 只带交办。
- 不写 `--to` 按额度挑订阅号（和自动换号同一套排序）；要用 API 号就写 `--to gpt` 这类。
- 交办前面自动加一段身份说明：它是谁分出来的、该向谁回报。
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from cca.errors import die, say

FORK_USAGE = 'cca fork <名字> [--to <号>] [--fresh] [--from <会话>] "<交办>"（交办写 - 从 stdin 读）'
NEW_ID_RE = re.compile(r"attach\s+([0-9a-f]{8})")


def header(*, parent_name: str, parent_id: str, name: str, target: str, forked: bool) -> str:
    """交办前面的身份说明：分出来的会话一开始看到的是来源会话的整段对话，得先知道自己现在是谁。"""
    who = f"「{parent_name}」（{parent_id[:8]}）" if parent_name else f"{parent_id[:8]}"
    above = "上面是分出来之前的完整对话，供你了解来龙去脉；" if forked else ""
    return (f"（cca fork）你是从会话{who}分出来的新会话「{name}」，在 {target} 上独立运行。{above}"
            f"从这条消息起你只负责下面这件事，别接着做原会话手上的其它事。做完、卡住或要它拍板时，"
            f'用 sideagent_send {{session: "{parent_name or parent_id}"}} 回报给它。\n\n')


def pick_account(rows: list) -> str:
    """不写 --to：订阅号里余量充足、周额度最早重置的那个（和 watchdog 换号同一套排序）。"""
    from cca.session.push import shares_sessions  # noqa: PLC0415
    from cca.session.quota_hint import ranked_targets, thresholds  # noqa: PLC0415

    ranked = ranked_targets(rows, "", limits=thresholds(), shares=shares_sessions, now=time.time())
    return ranked[0]["profile"] if ranked else ""


def _name_taken(name: str) -> str:
    """同名的会话还在跑就不分：名字是别人找它的地址，撞了按名字发话会分不清。"""
    from cca.session.attach import candidates  # noqa: PLC0415

    live = [row for row in candidates() if row.get("name") == name and row.get("running")]
    return f"{live[0]['account']}/{live[0]['id']}" if live else ""


def session_id_for(key: str) -> str:
    """--from 给的名字 / 编号前缀 → 完整编号；按名字找的规则和 sideagent_send {session} 一样。"""
    from cca.session.attach import candidates, find, narrow_by_name  # noqa: PLC0415

    hits, ambiguous = narrow_by_name(find(key, candidates()))
    if ambiguous:
        die(f"--from {key}：{ambiguous}")
    return str(hits[0]["session_id"]) if hits else key   # 找不到就按编号去对话记录里找


def resolve_source(key: str) -> tuple[str, str, str]:
    """来源会话：(完整编号, 名字, 工作目录)。key 空就是执行这条命令的会话。

    工作目录取会话自己的（Claude Code 按工作目录对应的项目目录找对话记录；Bash 当前目录可能是子目录）。
    """
    from cca.session.push import _scan_transcript, session_name  # noqa: PLC0415
    from cca.session.transcript import find_transcript  # noqa: PLC0415

    if not key:
        key = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if not key:
            die(f"终端里没有「当前会话」：用 --from <会话名字或编号> 指定从哪条分。用法: {FORK_USAGE}")
    else:
        key = session_id_for(key)
    path = find_transcript(key)
    if path is None:
        die(f"找不到会话 {key} 的对话记录")
    cwd = str(_scan_transcript(path).get("cwd") or "")
    return path.stem, session_name(path), cwd if cwd and Path(cwd).is_dir() else str(Path.cwd())


def parse_args(args: list) -> tuple[str, str, dict]:
    """(名字, 交办, 选项)。交办可能以 - 开头（列表符号），所以不用通用的 parse_flags：
    只认 --to / --from / --fresh，名字之后的其它内容都是交办。"""
    name, message, flags = "", None, {}
    words = iter(args)
    for arg in words:
        head, sep, value = str(arg).partition("=")
        if head in ("--to", "--from") and message is None:
            flags[head] = value if sep else next(words, "")
        elif arg == "--fresh" and message is None:
            flags["--fresh"] = True
        elif not name:
            name = str(arg)
        elif message is None:
            message = str(arg)
        else:
            die(f"多余参数 {arg}（交办要用引号包成一个参数，或写 - 从 stdin 读）。用法: {FORK_USAGE}")
    return name.strip(), message or "", flags


def cmd_fork(args: list) -> int:
    from cca.account.model import discover_profiles, require_profile  # noqa: PLC0415
    from cca.api.client import api_launch_args  # noqa: PLC0415
    from cca.env import LaunchBlocked  # noqa: PLC0415
    from cca.report import _fetch_list_rows  # noqa: PLC0415
    from cca.session.push import _run_claude, shares_sessions  # noqa: PLC0415

    name, message, flags = parse_args(args)
    if message == "-":
        message = sys.stdin.read()
    if not name or not message.strip():
        die(f"名字和交办都要写。用法: {FORK_USAGE}")
    taken = _name_taken(name)
    if taken:
        die(f"已经有一条叫「{name}」的会话在跑（{taken}）；换个名字，或者直接给它发话")
    forked = not flags.get("--fresh")
    parent, parent_name, cwd = resolve_source(str(flags.get("--from") or ""))
    target = str(flags.get("--to") or "")
    if target:
        require_profile(target, usage=FORK_USAGE)
    else:
        target = pick_account(_fetch_list_rows(discover_profiles(), [], refresh=False))
        if not target:
            die("现在没有两个窗口都还有余量的订阅号；用 --to 指定一个号（API 号也行）")
    if forked and not shares_sessions(target):
        die(f"{target} 是隔离号（对话目录不共用），带不过去完整对话；加 --fresh 只带交办，或换个号")
    prompt = header(parent_name=parent_name, parent_id=parent, name=name, target=target, forked=forked) + message
    launch = ["--bg", *(["--resume", parent, "--fork-session"] if forked else []), "-n", name, prompt]
    try:
        r = _run_claude(api_launch_args(target, launch), target, capture=True, cwd=cwd, session=True)
    except LaunchBlocked as exc:
        die(f"在 {target} 上起不来：{exc}")
    output = f"{r.stdout or ''}\n{r.stderr or ''}"
    found = NEW_ID_RE.search(output)
    if r.returncode != 0 or not found:
        die(f"在 {target} 上没起来：{output.strip()[-300:] or f'退出码 {r.returncode}'}")
    short = found.group(1)
    say(f"已分出「{name}」：{target}/{short}（{'带完整对话' if forked else '只带交办'}），它会自己干，"
        f"做完或要拍板时向「{parent_name or parent[:8]}」回报。")
    say(f'给它发话：sideagent_send {{session: "{name}"}}；看它：cca agents；进去看：cca {target} attach {short}')
    return 0
