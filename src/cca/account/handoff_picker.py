"""`cca handoff` 的交互选择框（questionary）：选会话、选源号和目标号、确认。

只负责“问人”：每个函数返回人的选择；人按 Ctrl+C 返回 None（调用方什么都不动）。
questionary 只在这里用，测试里把 `questionary` 换成 mock 即可，不会真起交互。
"""

from __future__ import annotations

from datetime import datetime

import questionary

from cca.config import (
    PICKER_ACCOUNT_WIDTH,
    PICKER_AGO_WIDTH,
    PICKER_ASK_WIDTH,
    PICKER_STATE_WIDTH,
    PICKER_TITLE_WIDTH,
)
from cca.errors import say
from cca.report import display_width, state_text

SESSION_HINT = "↑↓ 移动，空格 勾选，a 全选，回车 确认，Ctrl+C 取消"
# 等输入 / 挂起中的状态：默认不勾，但可以一键全选
WAITING_STATES = ("blocked", "idle")
SELECT_WAITING = 0



def clip(text: str, width: int) -> str:
    """按终端列宽截断并补空格到 width 列；超出的结尾换成省略号。"""
    text = " ".join(str(text).split())
    if display_width(text) <= width:
        return text + " " * (width - display_width(text))
    out, used = "", 0
    for char in text:
        step = display_width(char)
        if used + step > width - 1:
            break
        out += char
        used += step
    return out + "…" + " " * (width - used - 1)


def _ago(item: dict) -> str:
    """多久前有动静；active_at 是 UTC 的 ISO 时间，读不出来就空着。"""
    from cca.session.push import format_age  # noqa: PLC0415 - push 顶部导入本模块族，延迟破环

    try:
        return format_age(datetime.fromisoformat(item["active_at"]).timestamp())
    except (KeyError, TypeError, ValueError):
        return ""


def _last_ask(item: dict) -> str:
    """人最后交给这个会话的那句话；读不到就空着。"""
    from cca.session.takeover import last_ask  # noqa: PLC0415 - takeover 很重，用到才导入

    try:
        return last_ask(item["full"])
    except Exception:  # noqa: BLE001 - 只是给人认会话用的一句话，读不出来不该挡住选择
        return ""


def session_line(item: dict) -> str:
    """选择框里一个会话的一行：标题、状态、多久前、最后交给它的那句话。"""
    return "  ".join((
        clip(item.get("name") or "(无标题)", PICKER_TITLE_WIDTH),
        clip(state_text(item.get("state", "")).plain, PICKER_STATE_WIDTH),
        clip(_ago(item), PICKER_AGO_WIDTH),
        clip(_last_ask(item), PICKER_ASK_WIDTH),
    )).rstrip()


def waiting_indexes(items: list) -> list:
    """等输入 / 挂起中、且搬得动的会话编号（1 基）——“一键全选”用。"""
    return [index for index, item in enumerate(items, start=1)
            if item.get("state") in WAITING_STATES and item.get("movable", True)]


def session_choices(items: list, default: list) -> list:
    """每个会话一个选项：搬不了的灰掉并写原因；默认集合预先勾上。值是 1 基编号。

    第一行是“全选等输入/挂起的”（值 0）：默认一个都不勾，要搬哪几条自己挑。
    """
    choices = []
    waiting = waiting_indexes(items)
    if waiting:
        choices.append(questionary.Choice(
            title=f"全选等输入/挂起的（{len(waiting)} 条）",
            value=SELECT_WAITING,
            checked=SELECT_WAITING in default,
        ))
    for index, item in enumerate(items, start=1):
        movable = item.get("movable", True)
        choices.append(questionary.Choice(
            title=session_line(item),
            value=index,
            checked=movable and index in default,
            disabled=None if movable else (item.get("reason") or "搬不了"),
        ))
    return choices


def pick_sessions(items: list, default: list) -> list | None:
    """多选要搬的会话；返回 1 基编号列表，Ctrl+C 返回 None，一条都能选不了返回空列表。

    勾了第一行“全选等输入/挂起的”，就把它展开成那些会话（可以再单独加几条）。
    """
    choices = session_choices(items, default)
    if all(choice.disabled for choice in choices):
        say("这些会话都搬不了，没有可选的")
        return []
    picked = questionary.checkbox("选择要搬的会话", choices=choices, instruction=SESSION_HINT).ask()
    if picked is None:
        return None
    chosen = set(picked)
    if SELECT_WAITING in chosen:
        chosen |= set(waiting_indexes(items))
    chosen.discard(SELECT_WAITING)
    return sorted(chosen)


def confirm_move(count: int, source: str, target: str) -> bool:
    """搬之前最后问一次；Ctrl+C 当作不搬。"""
    answer = questionary.confirm(f"把 {source} 的 {count} 条会话搬到 {target}？", default=False).ask()
    return bool(answer)


def _percent(row: dict, window: str) -> str:
    value = ((row.get("usage") or {}).get("windows") or {}).get(window, {}).get("percent")
    return "-" if value is None else f"{value}%"


def _age(row: dict) -> str:
    """额度数据是多久以前的（选择框不等实时额度，用的是本地快照）。"""
    from cca.session.push import format_age  # noqa: PLC0415 - 延迟破环

    fetched = float((row.get("usage") or {}).get("fetched_at") or 0)
    return f"（{format_age(fetched)}）" if fetched else "（没有额度数据）"


def _account_line(row: dict, note: str) -> str:
    if row.get("kind") == "api":
        quota = "按量号，没有额度"
    else:
        quota = f"5 小时 {_percent(row, 'five_hour')}  7 天 {_percent(row, 'seven_day')}  {_age(row)}"
    return f"{clip(row['profile'], PICKER_ACCOUNT_WIDTH)}  {quota}{('  ' + note) if note else ''}"


def _full(row: dict) -> bool:
    """订阅号有一个窗口用满了就算满；api 号没有额度，不算。"""
    from cca.session.watchdog import has_room  # noqa: PLC0415 - watchdog 顶部导入本模块族，延迟破环

    if row.get("kind") == "api":
        return False
    return not has_room((row.get("usage") or {}).get("windows") or {})


def source_choices(rows: list, shares) -> list:
    """源号选项：隔离号灰掉；每行写后台在跑几条。"""
    choices = []
    for row in rows:
        name = row["profile"]
        running = (row.get("background") or {}).get("running", 0)
        choices.append(questionary.Choice(
            title=_account_line(row, f"后台在跑 {running} 条"),
            value=name,
            disabled=None if shares(name) else "隔离号，对话不和别的号共用",
        ))
    return choices


def target_choices(rows: list, source: str, shares) -> list:
    """目标号选项：源号自己、隔离号、额度用满的、没登录的订阅号都灰掉并写原因。

    排序：订阅号在前，按周额度从早到晚重置（到点作废的额度先花掉）；api 号按量计费，排在后面。
    """
    from cca.api.usage import weekly_reset_key  # noqa: PLC0415 - 只在选目标号时用

    rows = sorted(rows, key=lambda row: (row.get("kind") == "api",
                                         weekly_reset_key((row.get("usage") or {}).get("windows") or {})))
    choices = []
    for row in rows:
        name = row["profile"]
        reason = None
        if name == source:
            reason = "这是源号"
        elif not shares(name):
            reason = "隔离号，对话不和别的号共用"
        elif _full(row):
            reason = "额度用满"
        elif row.get("kind") != "api" and not row.get("logged_in"):
            reason = "没登录"
        choices.append(questionary.Choice(title=_account_line(row, ""), value=name, disabled=reason))
    return choices


def _select(message: str, choices: list) -> str | None:
    if all(choice.disabled for choice in choices):
        say(f"{message}：没有可选的号")
        return None
    return questionary.select(message, choices=choices).ask()


def pick_pair(rows: list, shares) -> tuple | None:
    """先选源号、再选目标号；任何一步 Ctrl+C 或没有可选的就返回 None。"""
    source = _select("从哪个号搬走", source_choices(rows, shares))
    if source is None:
        return None
    target = _select(f"搬到哪个号（从 {source}；订阅号按周额度最早重置排在前面）",
                     target_choices(rows, source, shares))
    if target is None:
        return None
    return source, target
