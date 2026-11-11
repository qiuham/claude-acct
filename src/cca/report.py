"""表格渲染：ls / status / sessions 的文字输出与 Rich 辅助。"""

from __future__ import annotations

import concurrent.futures
import json
import sys
import time
from datetime import datetime
from importlib import metadata
from zoneinfo import ZoneInfo

from cca.account.model import (
    discover_profiles,
    has_subscription_login,
    read_identity,
    require_profile,
)
from cca.account.store import (
    _settings_env,
    api_env_vars,
    credential_expiry,
    key_state,
    profile_kind,
    read_api_key,
    read_marker,
)
from cca.api.client import api_display
from cca.api.usage import (
    live_subscriptions,
    live_usage,
    local_subscription,
    profile_subscription,
    usage_roominess,
    usage_snapshot,
)
from cca.cli import parse_flags
from cca.config import (
    AGENTS_RECENT_DAYS,
    AGENTS_USAGE,
    EMAIL_MAX_WIDTH,
    EMAIL_MIN_WIDTH,
    LIST_COUNTDOWN_MIN_WIDTH,
    LIST_USAGE,
    RICH_VERSION,
    SESSIONS_DAYS,
    SESSIONS_USAGE,
    STATE_STYLES,
    STATUS_USAGE,
    SUBSCRIPTION_STATUS,
    USAGE_COLUMNS,
    USAGE_LIVE_TTL,
    USAGE_STALE_SECONDS,
    WIDE_CONSOLE_WIDTH,
)
from cca.env import DISPLAY_TZ
from cca.errors import die, say
from cca.paths import credentials_file, profile_dir
from cca.session import agents_view
from cca.session.push import (
    active_workers,
    all_states_index,
    background_summary,
    format_age,
    is_job,
    last_session_model,
    live_index,
    older_session_count,
    resumable_sessions,
    session_transcript,
    shares_sessions,
)

# 额度样式的分界；凭据刷新令牌剩余天数低于这个值就提醒。
USAGE_WARN_PERCENT = 80
USAGE_TIGHT_PERCENT = 50
REFRESH_WARN_DAYS = 7
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24


def require_rich() -> None:
    """展示类命令要求固定的 Rich 版本（与 podjob / podgraph 一致）。"""
    try:
        installed = metadata.version("rich")
    except metadata.PackageNotFoundError:
        die(f"表格输出需要 rich=={RICH_VERSION}，请先 pip install rich=={RICH_VERSION}")
    if installed != RICH_VERSION:
        die(f"表格输出需要 rich=={RICH_VERSION}，当前为 rich=={installed}")

def rich_available() -> bool:
    """装着**钉住版本**的 Rich 吗？不带副作用、不会 die —— 展示可选时用它探路。"""
    try:
        from importlib import metadata  # noqa: PLC0415 - 探路函数，避免模块导入时就碰依赖

        return metadata.version("rich") == RICH_VERSION
    except Exception:                              # noqa: BLE001 —— 没有就没有
        return False

def make_console(**kwargs):
    """构造 Rich Console；Rich 只在真正渲染时导入。"""
    require_rich()
    from rich.console import Console  # noqa: PLC0415 - Rich 只在真正渲染时导入

    return Console(**kwargs)

def _plain(value="", *, style=None):
    from rich.text import Text  # noqa: PLC0415 - Rich 只在真正渲染时导入

    return Text(str(value), style=style)

def state_text(value):
    """状态 → 带样式的 Text；未知状态原样显示（dim）。"""
    label, style = STATE_STYLES.get(str(value), (str(value), "dim"))
    return _plain(label, style=style)

def _table(console, *, min_width=100):
    from rich.table import Table  # noqa: PLC0415 - Rich 只在真正渲染时导入

    return Table(
        box=None,
        collapse_padding=True,
        expand=True,
        pad_edge=False,
        header_style="bold",
        width=max(console.width, min_width),
    )

def _state_counts_text(counts: dict):
    """`busy 3 · blocked 2`，每个状态带自己的颜色。"""
    from rich.text import Text  # noqa: PLC0415 - Rich 只在真正渲染时导入

    if not counts:
        return _plain("-")
    order = ("busy", "working", "unknown", "blocked", "idle", "done", "failed")
    keys = [k for k in order if counts.get(k)] + sorted(k for k in counts if k not in order and counts[k])
    text = Text()
    for index, key in enumerate(keys):
        if index:
            text.append(" · ", style="dim")
        label, style = STATE_STYLES.get(key, (key, "dim"))
        text.append(f"{label} {counts[key]}", style=style)
    return text

# 终端显示固定上海：换算成目标时区再格式化，而不是改 os.environ["TZ"]。
# 改进程环境会让子进程（claude 的后台守护进程、cca 自己派生的 cca）也变成上海时区。
_DISPLAY_ZONE = ZoneInfo(DISPLAY_TZ)


def format_clock(ts) -> str:
    """epoch 秒 → 上海时刻 `09-24 18:00`。

    默认视图只给绝对时刻：`3 小时后` 要读的人自己做减法，还丢掉"跨没跨天"这类信息
    （重置窗口跨天与否直接决定这个号明天还能不能跑）。
    """
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts), _DISPLAY_ZONE).strftime("%m-%d %H:%M")


def format_remaining(seconds) -> str:
    """从现在到重置还剩多久：`2h 13m`、`4d 03h`、`0h 43m`。

    严格等宽 7 列：第一段的数字用空格补到两位，第二段补零（` 2h 10m` / `10h 04m` /
    ` 3d 02h`）。单位位置上下对齐，扫一列就能比较谁先放开。
    不足 1 分钟给 `    <1m`（同样 7 列）。
    """
    if seconds is None:
        return ""
    minutes = max(0, int(seconds)) // SECONDS_PER_MINUTE
    days, rest = divmod(minutes, MINUTES_PER_HOUR * HOURS_PER_DAY)
    hours, minutes = divmod(rest, MINUTES_PER_HOUR)
    if days:
        return f"{days:>2}d {hours:02d}h"
    if hours or minutes:
        return f"{hours:>2}h {minutes:02d}m"
    return "    <1m"


def tz_label() -> str:
    """时区标签（`UTC+08:00`）。终端显示固定上海，见 env.DISPLAY_TZ。"""
    now = datetime.now(_DISPLAY_ZONE)
    offset = now.strftime("%z")
    if not offset:
        return now.tzname() or "本地"
    return f"UTC{offset[:3]}:{offset[3:]}" if offset != "+0000" else "UTC"

def display_width(value: str) -> int:
    """字符串占多少终端列（中文 / 全角按 2 列）—— 自己算列宽时必须用它，不能用 len()。"""
    import unicodedata  # noqa: PLC0415 - 只有算列宽时才需要

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in value)

def usage_line(snap: dict, *, with_snapshot: bool = True) -> str:
    """额度摘要，给 `switch` / `status` 用（绝对时刻）。`with_snapshot=False` 只给窗口。"""
    if not snap["windows"]:
        return "无快照（claude 下次启动会刷新）"
    parts = []
    for w in snap["windows"].values():
        if w["reset_passed"]:
            parts.append(f"{w['label']} {w['percent']}%*（{format_clock(w['resets_at'])} 已重置，旧值）")
        else:
            parts.append(f"{w['label']} {w['percent']}%（{format_clock(w['resets_at'])} 重置）")
    if with_snapshot and snap["fetched_at"]:
        parts.append(f"快照 {format_clock(snap['fetched_at'])}")
    return " · ".join(parts)

def usage_style(percent: int, *, binding: bool = False) -> str:
    """百分比样式：越紧越红；`binding`（该号最紧的那个窗口）加粗 —— 瓶颈先看它。"""
    if percent >= USAGE_WARN_PERCENT:
        return "bold red"
    style = "green" if percent < USAGE_TIGHT_PERCENT else "yellow"
    return f"bold {style}" if binding else style

def usage_cell_text(window, *, binding: bool = False, stale: bool = False,
                    countdown: bool = True):
    """面板单元格：`99% 09-24 18:00 · 2h 13m`。

    绝对时刻和倒计时都给，中点后面是距重置还剩多久（`2h 13m` 这种紧凑写法）。
    重置已过的标 `*`（表尾解释）。百分比与时间放同一格：拆成两列会把表撑宽，
    窄终端下尾列就被裁掉了。
    """
    from rich.text import Text  # noqa: PLC0415 - Rich 只在真正渲染时导入

    if not window:
        return _plain("-", style="dim")
    text = Text()
    # 百分比补到 4 列（`100%` / ` 13%`）：不补齐的话后面的时间会左右参差。
    label = f"{window['percent']:>3}%"
    # `~` = 这个数不是刚取的实时值（用的是缓存或 claude 的本地快照）—— 不标出来的话，
    # 面板会"理直气壮"地显示几分钟前的旧数字（真机：005 缓存 79%，claude 快照已经是 100%）。
    if stale and not window["reset_passed"]:
        label = f"{label}~"
    elif window["reset_passed"]:
        label = f"{label}*"
    else:
        # 正常行补一个空格：和带 `~` / `*` 的行对齐，也正好当后面的分隔符。
        label = f"{label} "
    gap = "" if label.endswith(" ") else " "
    if window["reset_passed"]:
        # 已重置的旧值没有倒计时可说，照旧给绝对时刻 + `*`。
        text.append(label, style="dim")
        text.append(f"{gap}{format_clock(window['resets_at'])}", style="dim")
        return text
    text.append(label, style=usage_style(window["percent"], binding=binding))
    if not window.get("resets_at"):
        text.append(f"{gap}-", style="dim")
        return text
    clock = f"{gap}{format_clock(window['resets_at'])}"
    if not countdown:
        text.append(clock, style="dim")
        return text
    left = format_remaining(window["resets_at"] - time.time())
    text.append(f"{clock} · {left}", style="dim")
    return text


# 席位套餐按席位档给倍数：公开定价页写 Standard = 1.25× Pro、Premium = 6.25× Pro。
# Anthropic 没有公开 team_tier_1 / team_tier_2 分别对应哪一档；当前这个号是 Premium
# 席位（用户确认），所以按 6.25× 显示。对不上就在管理后台 Tier 列核对后改这里。
TEAM_SEAT_LABELS = {
    "team_tier_1": "6.25x",
}


def rate_tier_label(tier, org_type: str = "", seat_tier: str = "") -> str:
    """把 claude 的内部限流等级写成人能读的：`default_claude_max_5x` → `max 5x`。

    个人号看 `userRateLimitTier`（缺失时取 `organizationRateLimitTier`）；这个值
    决定 5 小时/周窗口的额度大小，不是我们编的。

    席位套餐（Team / Enterprise）的限流档和个人 Max 不是一回事，倍数由席位档决定，
    所以显示成 `team 6.25x`，前面带套餐名，一眼分得出不是个人号。
    """
    t = str(tier or "").strip()
    if t:
        t = t.replace("default_", "").replace("claude_", "")
        label = t.replace("_", " ")
    else:
        label = ""
    plan = str(org_type or "").strip().lower()
    if plan in ("claude_team", "claude_enterprise"):
        plan = plan.replace("claude_", "")
        seat = TEAM_SEAT_LABELS.get(str(seat_tier or "").strip())
        return f"{plan} {seat or label}" if (seat or label) else plan
    return label or "-"

def subscription_label(sub: dict) -> str:
    """表格里的一格：`max·apple·已取消·09-23`，缺的项自动省略。"""
    sub = sub or {}
    parts = []
    kind = str(sub.get("type") or "").replace("claude_", "")
    if kind:
        parts.append(kind)
    status = str(sub.get("status") or "")
    status = SUBSCRIPTION_STATUS.get(status, status)
    if status:
        parts.append(status)
    return "·".join(parts) or "-"

def _profile_row(name: str) -> dict:
    ident = read_identity(name)
    marker = read_marker(name)[0] if profile_kind(name) == "api" else None
    return {
        "profile": name,
        "kind": profile_kind(name),
        "provider": (marker or {}).get("provider", ""),
        "base_url": (marker or {}).get("base_url", ""),
        "api_label": api_display((marker or {}).get("provider", ""), (marker or {}).get("base_url", "")),
        "dir": str(profile_dir(name)),
        "config_dir_override": str(profile_dir(name)),
        "logged_in": has_subscription_login(name),
        # 隔离号：对话目录自己一份，别的号的对话搬不过去（handoff 面板不列它）
        "isolated": not shares_sessions(name),
        "email": ident.get("email", ""),
        "org_type": ident.get("org_type", ""),
        "seat_tier": ident.get("seat_tier", ""),
        "rate_tier": ident.get("rate_tier", ""),
        "extra_usage": ident.get("extra_usage"),
        "subscription": local_subscription(name),
        "usage": usage_snapshot(name),
        "background": background_summary(name),
    }

def _background_sessions_text(sessions: list, profile: str = ""):
    """后台会话明细（id / 状态 / 名字 / 模型 / 目录）—— 面板只给在跑几个，明细在这里。"""
    from rich.text import Text  # noqa: PLC0415 - Rich 只在真正渲染时导入

    if not sessions:
        return _plain("-")
    text = Text()
    for index, item in enumerate(sessions):
        if index:
            text.append("\n")
        text.append_text(state_text(item.get("state", "unknown")))
        text.append(f"  {item.get('id', '')}  {item.get('name') or '(无标题)'}")
        model = ""
        if profile:
            transcript = session_transcript(profile_dir(profile), item.get("id", ""))
            if transcript is not None:
                model = last_session_model(transcript)
        if model:
            text.append(f"  [{model}]", style="dim")
        if item.get("cwd"):
            text.append(f"  {item['cwd']}", style="dim")
        if not is_job(item):
            text.append("（交互会话，claude stop 停不掉）", style="dim")
    return text

def cmd_status(args: list) -> int:
    """不带号 = 每个号一段摘要；带号 = 单号详情（身份 / 目录 / 额度 / 后台会话明细）。"""
    rest, flags = parse_flags(args, bool_flags=("--json",), usage=STATUS_USAGE)
    if rest[1:]:
        die(f"多余参数 {' '.join(rest[1:])}；用法: {STATUS_USAGE}")
    as_json = bool(flags.get("--json"))
    names = [require_profile(rest[0], usage=STATUS_USAGE)] if rest else discover_profiles()
    if not names:
        die("还没有任何号；先 cca add <name> --provider … 或 cca new <name>")
    if as_json:
        payload = [status_payload(n) for n in names]
        # 点名一个号仍是对象（脚本照旧）；不带号汇总才是数组。
        print(json.dumps(payload[0] if rest else payload, ensure_ascii=False, indent=2))  # noqa: T201 - CLI JSON 输出边界
        return 0
    for index, name in enumerate(names):
        if index:
            say("")
        render_status(name)
    return 0

def status_payload(name: str) -> dict:
    """一个号的 JSON 形态（`cca status --json` 与多号汇总共用）。"""
    if profile_kind(name) == "api":
        _marker, error = read_marker(name)
        if _marker is None:
            die(error)
    row = _profile_row(name)
    row["subscription"] = profile_subscription(name)
    sessions = active_workers(name)
    return {"profile": name, **row, "background": {**row["background"], "sessions": sessions}}

def _status_grid():
    """详情页的两列网格（标签 + 值）。"""
    from rich.table import Table  # noqa: PLC0415 - Rich 只在真正渲染时导入

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", width=10, no_wrap=True)
    grid.add_column(overflow="fold")
    return grid

def _status_identity(grid, row: dict, name: str) -> None:
    """账号与目录两行。"""
    from rich.text import Text  # noqa: PLC0415 - Rich 只在真正渲染时导入

    is_api = row["kind"] == "api"
    identity = Text()
    identity.append(name, style="bold")
    identity.append(f"  {row['api_label'] if is_api else (row['email'] or '(未登录/无记录)')}")
    grid.add_row("账号", identity)
    grid.add_row("目录", _plain(row["dir"]))

def _add_api_status_rows(grid, row: dict, name: str) -> None:
    """API 号特有的端点/模型/密钥/代理/额度行。"""
    marker = read_marker(name)[0] or {}
    state = key_state(name)
    grid.add_row("端点", _plain(row["base_url"]))
    grid.add_row("模型", _plain(str(marker.get("env", {}).get("ANTHROPIC_MODEL") or "-")))
    grid.add_row("密钥", _plain(state, style="green" if state != "缺失" else "yellow"))
    grid.add_row("代理", _plain(str(marker.get("proxy") or "none")))
    drift = {k: v for k, v in api_env_vars(marker).items() if _settings_env(name).get(k) != v}
    if drift:
        grid.add_row(
            "",
            _plain(
                f"→ settings.json 与清单不一致（{', '.join(sorted(drift))}）：cca sync {name}",
                style="yellow",
            ),
        )
    if state == "缺失":
        grid.add_row("", _plain(f"→ 修复: cca login {name} --key <key>", style="yellow"))
    grid.add_row("额度", _plain("-（API 号按量计费，用量看供应商控制台）"))
    grid.add_row("后台会话", _background_sessions_text(active_workers(name), name))

def _add_subscription_rows(grid, sub: dict) -> None:
    """订阅状态行。计费和首订日已经去掉（不再自己请求平台接口）。"""
    if sub.get("status"):
        status_text = SUBSCRIPTION_STATUS.get(str(sub["status"]), str(sub["status"]))
        style = ("green" if sub["status"] == "active"
                 else "red" if sub["status"] in ("canceled", "expired", "past_due") else "")
        grid.add_row("订阅", _plain(status_text, style=style) if style else _plain(status_text))

def _add_extra_usage_row(grid, row: dict) -> None:
    """额外用量行：None=没有该字段，True=可用，False=已关闭。"""
    extra = row["extra_usage"]
    if extra is None:
        extra_text = _plain("-")
    elif extra:
        extra_text = _plain("可用", style="green")
    else:
        reason = row["usage"].get("extra_disabled_reason") or ""
        extra_text = _plain(f"已关闭（{reason}）" if reason else "已关闭", style="yellow")
    grid.add_row("额外用量", extra_text)

def _add_credential_rows(grid, name: str) -> None:
    """凭据有效期与刷新令牌到期行。"""
    cred = credential_expiry(name)
    now = time.time()
    if cred["access"] is None:
        grid.add_row("凭据", _plain("-"))
    elif cred["access"] > now:
        grid.add_row("凭据", _plain(f"{format_clock(cred['access'])} 到期（有效）"))
    else:
        grid.add_row(
            "凭据",
            _plain(
                f"{format_clock(cred['access'])} 已过期（claude 启动时会用刷新令牌自动续）",
                style="yellow",
            ),
        )
    if cred["refresh"] is None:
        grid.add_row("重新登录", _plain("-"))
        return
    days = (cred["refresh"] - now) / 86400
    label = format_clock(cred["refresh"])
    if days <= 0:
        grid.add_row("重新登录", _plain(f"{label} 已过期", style="bold red"))
    elif days <= REFRESH_WARN_DAYS:
        grid.add_row("重新登录", _plain(f"{label}（{days:.0f} 天内到期）", style="yellow"))
    else:
        grid.add_row("重新登录", _plain(f"{label}（刷新令牌到期时间）"))

def _add_oauth_status_rows(grid, row: dict, name: str, sessions: list) -> None:
    """订阅号特有的登录/档位/订阅/额外用量/凭据/额度行。"""
    grid.add_row(
        "登录",
        _plain("是" if row["logged_in"] else "否", style="green" if row["logged_in"] else "red"),
    )
    grid.add_row("档位", _plain(rate_tier_label(row["rate_tier"], row.get("org_type", ""), row.get("seat_tier", ""))))
    _add_subscription_rows(grid, profile_subscription(name))
    _add_extra_usage_row(grid, row)
    _add_credential_rows(grid, name)
    grid.add_row("额度", _plain(usage_line(row["usage"], with_snapshot=False)))
    grid.add_row(
        "快照",
        _plain(
            f"{format_clock(row['usage']['fetched_at'])}（claude 下次启动会刷新）"
            if row["usage"]["fetched_at"]
            else "无（claude 启动一次后会有）"
        ),
    )
    grid.add_row("后台会话", _background_sessions_text(sessions, name))

def render_status(name: str, *, as_json: bool = False) -> None:
    """一个号的详情（给人看的那版）。"""
    if as_json:
        print(json.dumps(status_payload(name), ensure_ascii=False, indent=2))  # noqa: T201 - CLI JSON 输出边界
        return
    if profile_kind(name) == "api":
        _marker, error = read_marker(name)
        if _marker is None:
            die(error)
    row = _profile_row(name)
    sessions = active_workers(name)
    console = make_console()
    grid = _status_grid()
    _status_identity(grid, row, name)
    if row["kind"] == "api":
        _add_api_status_rows(grid, row, name)
    else:
        _add_oauth_status_rows(grid, row, name, sessions)
    console.print(grid)

_LIST_HEADERS = {
    "five": USAGE_COLUMNS[0][1], "seven": USAGE_COLUMNS[1][1], "bg": "后台",
    "tier": "档位", "substate": "订阅",
    "login": "登录", "snap": "快照", "extra": "额外",
}
_LIST_ORDER = ("five", "seven", "bg", "tier", "substate", "login", "snap", "extra")


def _list_options(args: list) -> tuple:
    """解析 ls 的开关与"只看这些号"的位置参数。

    位置参数 = 号名（`cca ls 006 gpt`），只刷这几个号的实时额度、只画这几行；不认识的
    名字直接报错（以前会落到"多余参数"上，看不出是号名打错了）。
    """
    rest, flags = parse_flags(args, bool_flags=("--wide", "--json", "--cached", "--refresh"),
                              usage=LIST_USAGE)
    known = set(discover_profiles())
    unknown = [n for n in rest if n not in known]
    if unknown:
        die(f"没有这个号: {' '.join(unknown)}；cca ls 看看有哪些号")
    return (bool(flags.get("--json")), bool(flags.get("--wide")), bool(flags.get("--cached")),
            bool(flags.get("--refresh")), list(rest))

def _list_targets(names: list, *, cached_only: bool) -> list:
    """要实时拉额度的号：订阅号且已登录；--cached 时一个都不拉。"""
    if cached_only:
        return []
    return [n for n in names if profile_kind(n) != "api" and credentials_file(n).is_file()]

def _fetch_list_rows(names: list, targets: list, *, refresh: bool) -> list:
    """并发取每行的本地信息、实时额度、订阅信息；失败一律降级，不拖垮面板。"""
    live, subs = {}, {}
    if refresh:
        sys.stderr.write("正在取实时额度（每号起一次 claude /usage，最多约 30 秒；--cached 可跳过）\n")
    workers = min(16, max(4, len(names) + len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        row_futures = {name: pool.submit(_profile_row, name) for name in names}
        live_future = pool.submit(live_usage, targets, force=refresh)
        subs_future = pool.submit(live_subscriptions, targets, force=refresh)
        rows = [row_futures[name].result() for name in names]
        live_failures = {}
        if targets:
            try:
                live, live_failures = live_future.result()
            except Exception:  # noqa: BLE001 - 取额度出任何事都不许拖垮面板
                live, live_failures = {}, {}
            try:
                subs = subs_future.result()
            except Exception:  # noqa: BLE001 - 取订阅同理：失败就只剩本地那两栏
                subs = {}
            for row in rows:
                if subs.get(row["profile"]):
                    row["subscription"] = subs[row["profile"]]
    for row in rows:
        name = row["profile"]
        got = live.get(name)
        # **谁新用谁**：claude 自己在会话启动/结束时会刷新本地快照，它可能比我们 5 分钟的缓存还新
        # （真机：005 的缓存 79%、claude 的快照 100%，面板端旧数字，和 CLI 里看到的不一致）。
        live_at = float(got.get("fetched_at") or 0) if got and got.get("windows") else 0.0
        snap_at = float(row["usage"].get("fetched_at") or 0)
        stale_live = bool(got and got.get("windows") and live_at >= snap_at)
        if stale_live:
            row["usage"]["windows"] = got["windows"]
            row["usage"]["fetched_at"] = live_at
            row["usage"]["source"] = got["source"]
        elif got:
            row["usage"]["source"] = "snapshot-newer"
        else:
            row["usage"]["source"] = "snapshot"
        if live_failures.get(name):
            row["usage"]["live_error"] = str(live_failures[name])
    return rows

def list_rows(names: list, *, cached_only: bool = False, refresh: bool = False) -> list:
    """每个号一行的数据（`cca ls --json` 的 profiles）；handoff 选择框和 cca ls 共用这一个函数。"""
    return _fetch_list_rows(names, _list_targets(names, cached_only=cached_only), refresh=refresh)

def _list_roomiest(rows: list):
    """最空闲的号；只有一个号时没有比较对象，返回 None。"""
    scored = [(usage_roominess(r["usage"]), r["profile"]) for r in rows
              if usage_roominess(r["usage"]) is not None]
    return min(scored)[1] if len(scored) > 1 else None

def _usage_value_is_fresh(snap: dict) -> bool:
    """这个额度值算不算"刚取的"：5 分钟内的快照/缓存都算（claude 自己也会刷同源数据）。"""
    fetched = snap.get("fetched_at")
    return bool(fetched) and time.time() - float(fetched) <= USAGE_LIVE_TTL


def _list_cells(row: dict, *, wide: bool, countdown: bool = True) -> tuple:
    """算一行的单元格；返回 `(cells, 是否出现已重置窗口)`。"""
    snap = row["usage"]
    windows = snap["windows"]
    roominess = usage_roominess(snap)
    cells = {}
    for idx, window_key in enumerate(("five_hour", "seven_day")):
        window = windows.get(window_key)
        # 该号最紧的窗口加粗：一眼看出瓶颈在短窗口还是周窗口。
        cells[("five", "seven")[idx]] = usage_cell_text(
            window, binding=bool(window) and window["percent"] == roominess,
            stale=not _usage_value_is_fresh(snap), countdown=countdown)
    if wide:
        # 后台会话数只在 --wide 里给：它不是"该用哪个号"的判断依据，
        # 默认视图放它只会挤掉档位和订阅。明细用 cca status <profile>。
        bg = row["background"]
        if not bg["total"]:
            cells["bg"] = _plain("-", style="dim")
        else:
            # 在跑/总数：只给在跑数的话，一个挂起的会话看起来像"没有会话"。
            cells["bg"] = _plain(f"{bg['running']}/{bg['total']}",
                                 style="yellow" if bg["running"] else "dim")
    sub = row.get("subscription") or {}
    # 档位决定额度大小，是选号的关键信息，默认视图就要有。
    cells["tier"] = _plain(
        rate_tier_label(row["rate_tier"], row.get("org_type", ""), row.get("seat_tier", "")) if row["kind"] != "api" else "-")
    sub_status = SUBSCRIPTION_STATUS.get(str(sub.get("status") or ""),
                                         str(sub.get("status") or ""))
    sub_style = ("green" if sub.get("status") == "active"
                 else "red" if sub.get("status") in ("canceled", "expired", "past_due")
                 else "dim" if not sub_status else "")
    cells["substate"] = (_plain(sub_status or "-", style=sub_style) if sub_style
                         else _plain(sub_status or "-"))
    if wide:
        if row["kind"] == "api":
            has_key = bool(read_api_key(row["profile"]))
            cells["login"] = _plain("是" if has_key else "否", style="green" if has_key else "yellow")
            cells["snap"] = _plain("-")
            cells["extra"] = _plain("-")
        else:
            cells["login"] = _plain("是" if row["logged_in"] else "否",
                                    style="green" if row["logged_in"] else "red")
            cells["snap"] = _plain(format_clock(snap["fetched_at"]))
            cells["extra"] = _plain("可用" if row["extra_usage"] else "关闭",
                                    style="green" if row["extra_usage"] else "dim")
    return cells, any(w["reset_passed"] for w in windows.values())

def _collect_list_cells(rows: list, *, wide: bool, countdown: bool = True) -> dict:
    """把每行的单元格与表尾需要提示的事一起收集起来。"""
    stale, extra_on, missing_key, body = [], [], [], []
    cached_only, live_failed = [], []
    reset_seen = False
    for row in rows:
        snap = row["usage"]
        if snap["fetched_at"] and time.time() - snap["fetched_at"] > USAGE_STALE_SECONDS:
            stale.append(f"{row['profile']}（{format_clock(snap['fetched_at'])}）")
        # `~` 只标"真的旧"的值：快照/缓存只要在 5 分钟内，就和实时值一样可信
        # （它本来就是 claude 自己刷的）。否则默认视图每行都挂个 `~`，反而变成噪音。
        if snap["windows"] and not _usage_value_is_fresh(snap):
            cached_only.append(row["profile"])
        if snap.get("live_error"):
            live_failed.append(f"{row['profile']}（{snap['live_error']}）")
        if row["extra_usage"]:
            extra_on.append(row["profile"])
        if row["kind"] == "api" and not read_api_key(row["profile"]):
            missing_key.append(row["profile"])
        cells, reset = _list_cells(row, wide=wide, countdown=countdown)
        reset_seen = reset_seen or reset
        body.append(cells)
    api_rows = [row for row in rows if row["kind"] == "api"]
    return {"body": body, "stale": stale, "extra_on": extra_on, "missing_key": missing_key,
            "reset_seen": reset_seen, "api_rows": api_rows, "wide": wide,
            "cached_only": cached_only, "live_failed": live_failed}

def _column_width(key: str, body: list) -> int:
    return max([display_width(_LIST_HEADERS[key])] + [cells[key].cell_len for cells in body])

def _list_fixed_widths(rows: list, body: list) -> dict:
    """固定列宽：按实际内容算，写死宽度会把精确时刻截断。"""
    fixed = {"profile": max(7, *(len(r["profile"]) for r in rows))}
    for key in _LIST_HEADERS:
        if body and key in body[0]:
            fixed[key] = _column_width(key, body)
    return fixed

def _build_list_table(cols: list, fixed: dict, email: int, *, with_email: bool):
    """按选定列建表；邮箱列可选。"""
    from rich.table import Table  # noqa: PLC0415 - Rich 只在真正渲染时导入

    table = Table(box=None, collapse_padding=True, pad_edge=False,
                  header_style="bold", expand=False)
    table.add_column("PROFILE", width=fixed["profile"], no_wrap=True)
    if with_email:
        table.add_column("邮箱", width=email, overflow="ellipsis", no_wrap=True)
    for key in cols:
        table.add_column(_LIST_HEADERS[key], width=fixed[key], no_wrap=True)
    return table

def _needed_width(fixed: dict, keys: list, *, with_email: bool = False, email_width: int = 0) -> int:
    """表格真正需要的宽度。

    不能用 `console.measure()`：Rich 会把结果**夹到终端宽度**，
    超出部分看不见，判断就会误以为放得下（实测 60 列时返回 60，实际要 65）。
    列与列之间固定 1 个空格，所以按列宽加总即可。
    """
    widths = [fixed["profile"]] + ([email_width] if with_email else []) + [fixed[k] for k in keys]
    return sum(widths) + len(widths) - 1

def _fit_list_columns(cols: list, fixed: dict, width: int) -> tuple:
    """按"邮箱先省略 → 订阅 → 档位"的顺序让位，绝不裁列。"""
    include_email = False
    while True:
        fit_email = _needed_width(fixed, cols, with_email=True, email_width=EMAIL_MIN_WIDTH) <= width
        if _needed_width(fixed, cols, with_email=fit_email, email_width=EMAIL_MIN_WIDTH) <= width:
            include_email = fit_email
            break
        drop = next((k for k in ("substate", "tier") if k in cols), None)
        if drop is None:
            include_email = fit_email
            break
        cols.remove(drop)
    email_width = EMAIL_MIN_WIDTH
    if include_email:
        spare = width - _needed_width(fixed, cols) - 1
        email_width = max(EMAIL_MIN_WIDTH, min(EMAIL_MAX_WIDTH, spare))
    return cols, include_email, email_width

def _print_list_json(rows: list, roomiest) -> None:
    """--json 输出（机器可读）。"""
    print(  # noqa: T201 - CLI JSON 输出边界
        json.dumps(
            {"now": format_clock(time.time()), "roomiest": roomiest, "profiles": rows},
            ensure_ascii=False,
            indent=2,
        )
    )

def _print_list_notes(console, data: dict) -> None:
    """表尾说明、图例与提示。"""
    console.print(_plain(""))
    console.print(
        _plain(
            f"额度默认实时取（claude 的 /usage 接口，{USAGE_LIVE_TTL // 60} 分钟缓存；"
            f"--cached 只看本地快照、--refresh 强制刷新），只用你已有的登录凭据读，不刷新也不上报；"
            f"表中百分比 = 已用，不是剩余；中点后面是距重置还剩多久"
            f"（终端窄于 {LIST_COUNTDOWN_MIN_WIDTH} 列时只显示时刻；时刻 = 上海时间 {tz_label()}）",
            style="dim",
        )
    )
    if data["reset_seen"]:
        console.print(_plain("* = 重置时刻已过，百分比是重置前的旧值（claude 下次启动会刷新）", style="dim"))
    if data.get("live_failed"):
        console.print(_plain(
            f"实时额度没取到: {', '.join(data['live_failed'])}（显示的是较新的缓存/本地快照）",
            style="yellow"))
    if data.get("cached_only"):
        console.print(_plain(
            f"~ = 不是刚取的实时值（缓存或 claude 本地快照）: {', '.join(data['cached_only'])}",
            style="dim"))
    if data["stale"]:
        console.print(_plain(f"快照偏旧、仅供参考: {', '.join(data['stale'])}", style="yellow"))
    if data["extra_on"]:
        console.print(_plain(f"额外用量可用: {', '.join(data['extra_on'])}（其余号已关闭）", style="yellow"))
    if data["api_rows"]:
        console.print(_plain("api 号没有订阅额度缓存（按量计费），用量看供应商控制台", style="dim"))
    for profile in data["missing_key"]:
        console.print(_plain(f"key 缺失: {profile}（修复: cca login {profile} --key …）", style="yellow"))
    console.print(_plain("档位 = claude 的限流等级（max 5x = Max 5 倍额度）；"
                         "档位 = 限流等级（max 5x）；订阅状态来自 claude auth status（由 Claude Code 自己查询）",
                         style="dim"))
    console.print(_plain("claude 没有到期日接口，所以不显示续费日", style="dim"))
    if data.get("wide"):
        console.print(_plain("后台 = 在跑/全部后台会话；明细用 cca status <profile>", style="dim"))

def _render_list_table(rows: list, data: dict, console) -> None:
    """算列宽、建表、打印表头与表尾。"""
    body = data["body"]
    fixed = _list_fixed_widths(rows, body)
    cols = [k for k in _LIST_ORDER if body and k in body[0]]
    cols, include_email, email_width = _fit_list_columns(cols, fixed, console.width)
    table = _build_list_table(cols, fixed, email_width, with_email=include_email)
    for row, cells in zip(rows, body, strict=True):
        head = [_plain(row["profile"])]
        if include_email:
            head.append(
                _plain(row["api_label"] if row["kind"] == "api" else (row["email"] or "-"),
                       style="dim" if row["kind"] == "api" else "")
            )
        table.add_row(*head, *(cells[k] for k in cols))
    console.print(_plain("现在 " + format_clock(time.time()) + "（" + tz_label() + "）", style="dim"))
    console.print(table)
    _print_list_notes(console, data)

def cmd_list(args: list) -> int:
    """号面板：一行一个号 —— 身份 + 额度（5 小时 / 7 天）+ 后台在跑数。

    面板回答"现在该用哪个号"（额度是唯一卡脖子的约束），`cca status <profile>` 回答
    "这个号具体什么情况"。时间一律绝对时刻（`09-24 18:00`）；相对时间（`3 小时后`）
    要读的人自己做减法，还丢掉跨天信息 —— 重置窗口跨没跨天直接决定明天还能不能跑。

    列宽全部自己算：Rich 对 `no_wrap` 的列按"最长内容"取最小宽度，几列一撑超过终端宽度
    就**静默裁掉尾列**（实测 72~96 列下 `ls` 的登录/后台整列消失过）。窄的时候按
    "邮箱先省略 → 最空闲列让位 → 邮箱整列让位"的顺序牺牲，绝不裁列。
    """
    as_json, wide, cached_only, refresh, only = _list_options(args)
    names = only or discover_profiles()
    if not names:
        if as_json:
            print(json.dumps({"profiles": []}, ensure_ascii=False, indent=2))  # noqa: T201 - CLI JSON 输出边界
        else:
            make_console().print(_plain("没有发现任何 profile"))
        return 0
    # 两件慢事并发做：① 每个号要跑一次 `claude agents --json`（约 0.3s/号）；
    # ② 实时额度要打网络（每个号 0.6–1s）。它们互不依赖 —— 逐个等是 1.7s + 1.4s，
    # 并发之后只等最慢的那一个（实测 3.1s → 1.9s；缓存命中时 ~0.4s）。
    rows = list_rows(names, cached_only=cached_only, refresh=refresh)
    roomiest = _list_roomiest(rows)
    if as_json:
        _print_list_json(rows, roomiest)
        return 0

    console = make_console(width=WIDE_CONSOLE_WIDTH) if wide else make_console()
    countdown = console.width >= LIST_COUNTDOWN_MIN_WIDTH
    _render_list_table(rows, _collect_list_cells(rows, wide=wide, countdown=countdown), console)
    return 0

def _sessions_payload(items: list, live: dict) -> list:
    """--json 的会话列表。"""
    return [
        {
            "id": s["id"],
            "short": s["short"],
            "mtime": s["mtime"],
            "age": format_age(s["mtime"]),
            "when": format_clock(s["mtime"]),
            "model": s.get("model", ""),
            "cwd": s["cwd"],
            "title": s["title"],
            "prompt": s["prompt"],
            "live": [{"profile": p, "state": st} for p, st in live.get(s["short"], [])],
        }
        for s in items
    ]

def _print_sessions_json(items: list, live: dict, window: str, older: int) -> None:
    """--json 输出（机器可读）。"""
    payload = _sessions_payload(items, live)
    print(  # noqa: T201 - CLI JSON 输出边界
        json.dumps(
            {
                "pool": "shared",
                "window": window,
                "total": len(payload),
                "older_not_shown": older,
                "sessions": payload,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

def _session_table_kwargs(*, wide: bool) -> tuple:
    """标题/提问两列的宽度策略；--wide 不截断，其余按列宽省略。"""
    text_kwargs = {"min_width": 12, "ratio": 2}
    prompt_kwargs = {"min_width": 20, "ratio": 3}
    if wide:
        text_kwargs = {"no_wrap": True, "overflow": "ignore"}
        prompt_kwargs = {"no_wrap": True, "overflow": "ignore"}
    else:
        text_kwargs.update(no_wrap=True, overflow="ellipsis")
        prompt_kwargs.update(no_wrap=True, overflow="ellipsis")
    return text_kwargs, prompt_kwargs

def _owners_text(owners: list):
    """一格的归属列：`002 在跑、003 空闲`。"""
    from rich.text import Text  # noqa: PLC0415 - Rich 只在真正渲染时导入

    owners_text = Text()
    if owners:
        for index, (owner, state) in enumerate(owners):
            if index:
                owners_text.append("、", style="dim")
            owners_text.append(f"{owner} ", style="bold")
            owners_text.append_text(state_text(state))
    else:
        owners_text.append("归属未知", style="dim")
    return owners_text

SESSIONS_NARROW_WIDTH = 120
SESSIONS_ROWS_PER_PROJECT = 15      # 每个项目默认只列最近这些；--all 全列


def _render_session_lines(console, group: list, live: dict) -> None:
    """窄终端：一行一个会话，和 handoff 的选择框一个信息量（号/状态、标题、多久前）。"""
    from rich.text import Text  # noqa: PLC0415 - Rich 只在真正渲染时导入

    for s in group:
        line = Text()
        line.append(s["short"], style="bold")
        line.append("  ")
        line.append_text(_owners_text(live.get(s["short"])))
        line.append("  ")
        line.append((s["title"] or s.get("first") or "(无标题)").replace("\n", " "))
        line.append("  ")
        line.append(format_clock(s["mtime"]), style="dim")
        console.print(line, no_wrap=True, overflow="ellipsis")


def _render_session_group(console, cwd: str, group: list, live: dict, kwargs: tuple) -> None:
    """一个项目目录下的会话表；终端太窄就换成每行一条。"""
    text_kwargs, prompt_kwargs = kwargs
    console.print(_plain(""))
    console.print(_plain(f"{cwd}   （{len(group)} 条）", style="bold"))
    if console.width < SESSIONS_NARROW_WIDTH:
        _render_session_lines(console, group, live)
        return
    table = _table(console, min_width=120)
    table.add_column("SESSION", min_width=36, no_wrap=True, overflow="ignore")
    table.add_column("模型", min_width=14, no_wrap=True, overflow="ellipsis")
    table.add_column("最后活动", min_width=11, justify="right", no_wrap=True)
    table.add_column("归属", min_width=13, no_wrap=True)
    table.add_column("标题", **text_kwargs)
    table.add_column("最后提问", **prompt_kwargs)
    for s in group:
        table.add_row(
            _plain(s["id"]),
            _plain(s.get("model") or "-"),
            _plain(format_clock(s["mtime"])),
            _owners_text(live.get(s["short"])),
            _plain((s["title"] or s.get("first") or "(无标题)").replace("\n", " ")),
            _plain((s["prompt"] or "-").replace("\n", " ")),
        )
    console.print(table)

def _render_sessions(items: list, live: dict, meta: tuple, console) -> None:
    """按项目目录分组打印会话表与表尾。meta = (window, older, wide)。"""
    window, older, wide = meta
    groups = {}
    for s in items:
        groups.setdefault(s["cwd"], []).append(s)
    kwargs = _session_table_kwargs(wide=wide)
    console.print(_plain("可续会话（共享池：会话不绑号，任何号都能续；命令形如 cca <号> --resume <完整id>）"))
    for cwd, group in sorted(groups.items(), key=lambda kv: kv[1][0]["mtime"], reverse=True):
        shown = group if wide or window == "全部" else group[:SESSIONS_ROWS_PER_PROJECT]
        _render_session_group(console, cwd, shown, live, kwargs)
        if len(shown) < len(group):
            console.print(_plain(f"  …另有 {len(group) - len(shown)} 条：cca sessions --all", style="dim"))
    console.print(_plain(""))
    tail = f"共 {len(items)} 条（{window}）"
    if older:
        tail += f"；另有 {older} 条更早的：cca sessions --all"
    console.print(_plain(tail, style="dim"))

def cmd_sessions(args: list) -> int:
    """列出共享会话池里的可续会话（跨项目目录）。

    补的是 CLI 的一个空缺：`/resume` 的选择器和 `-c` 都只看**当前目录**，
    换到别的目录就"看不见"其它项目的会话（它们没丢，只是按项目分目录存着）；
    按 ID `-r <id>` 是跨目录可用的。
    """
    show_all = "--all" in args
    as_json = "--json" in args
    wide = "--wide" in args
    rest = [a for a in args if a not in ("--all", "--json", "--wide")]
    if rest:
        die(f"多余参数 {' '.join(rest)}；用法: {SESSIONS_USAGE}")

    window = "全部" if show_all else f"最近 {SESSIONS_DAYS} 天"
    items = resumable_sessions(days=0 if show_all else SESSIONS_DAYS)
    live = live_index()
    # 和 handoff 一样：已经 stopped / failed 的会话不列（人主动停的、起不来的）
    states = all_states_index()
    def keep(item: dict) -> bool:
        if live.get(item["short"]):
            return True                 # 有号在跑/挂着
        known = states.get(item["short"]) or []
        return not known or any(state not in ("stopped", "failed") for _account, state in known)

    items = [item for item in items if keep(item)]
    older = 0 if show_all else older_session_count()

    if as_json:
        _print_sessions_json(items, live, window, older)
        return 0

    console = make_console(width=WIDE_CONSOLE_WIDTH) if wide else make_console()
    if not items:
        hint = f"；另有 {older} 条更早的：cca sessions --all" if older else ""
        console.print(_plain(f"没有可续会话（{window}）{hint}"))
        return 0
    _render_sessions(items, live, (window, older, wide), console)
    return 0


def _agents_payload(view: dict) -> list:
    return [
        {
            "name": r["name"], "id": r["id"], "session_id": r["session_id"], "account": r["account"],
            "state": r["state"], "alive": r["alive"], "doing": r["doing"], "cwd": r["cwd"],
            "active_at": r["active_at"], "also_on": r["also_on"], "moved_from": r.get("moved_from", ""),
        }
        for r in view["rows"]
    ]

def _agent_groups(rows: list) -> list:
    """按号分组（对应「一个号一个终端开 cca <号> agents」的用法）：有要处理的号在前，再按条数。"""
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["account"], []).append(row)
    def key(item):
        name, members = item
        urgent = sum(1 for r in members if r["state"] in agents_view.ATTENTION_STATES)
        return (-urgent, -len(members), name)
    return sorted(groups.items(), key=key)


def _render_agents(view: dict, console, *, show_all: bool) -> None:
    rows = view["rows"]
    if not rows:
        console.print(_plain("没有会话" + ("" if show_all else f"（活着的或 {AGENTS_RECENT_DAYS} 天内有活动的）")))
    else:
        # 窄终端：列不折行，名字和「在干什么」两列按比例省略
        table = _table(console, min_width=0)
        table.add_column("号 / 名字", min_width=6, ratio=2, no_wrap=True, overflow="ellipsis")
        table.add_column("编号", no_wrap=True)
        table.add_column("状态", no_wrap=True)
        table.add_column("在干什么 / 最近一句", min_width=8, ratio=5, no_wrap=True, overflow="ellipsis")
        table.add_column("多久前", justify="right", no_wrap=True)
        for index, (account, members) in enumerate(_agent_groups(rows)):
            if index:
                table.add_section()
            urgent = sum(1 for r in members if r["state"] in agents_view.ATTENTION_STATES)
            head = f"{account}  {len(members)} 条" + (f"，{urgent} 条要处理" if urgent else "")
            table.add_row(_plain(head, style="bold cyan"), _plain(""), _plain(""), _plain(""), _plain(""))
            for r in members:
                name = _plain("  ") + _plain(r["title"], style="bold")
                if r.get("moved_from"):
                    name += _plain(f" ← {r['moved_from']}", style="yellow")
                table.add_row(
                    name, _plain(r["id"]),
                    state_text(r["state"] or "unknown"),
                    _plain((r["doing"] or "-").replace("\n", " ")),
                    _plain(format_age(r["active_at"]) if r["active_at"] else "-", style="dim"),
                )
        console.print(table)
    console.print(_plain(""))
    tail = f"共 {len(rows)} 条"
    if view["hidden"]:
        tail += f"；另有 {view['hidden']} 条更早的已停会话：cca agents --all"
    console.print(_plain(tail, style="dim"))
    if view["failed"]:
        console.print(_plain(f"没取到：{'、'.join(view['failed'])}（已跳过，这几个号的会话可能不全）", style="yellow"))
    console.print(_plain("进入某条：cca <号> attach <短编号>", style="dim"))

def cmd_agents(args: list) -> int:
    """跨号会话总表：每个号取一次 claude agents，合并后看现在的会话在哪个号、什么状态。只读。"""
    rest, flags = parse_flags(args, bool_flags=("--all", "--json"), usage=AGENTS_USAGE)
    if rest:
        die(f"多余参数 {' '.join(rest)}；用法: {AGENTS_USAGE}")
    show_all = bool(flags.get("--all"))
    view = agents_view.build(show_all=show_all)
    if flags.get("--json"):
        print(  # noqa: T201 - CLI JSON 输出边界
            json.dumps(
                {"total": len(view["rows"]), "older_not_shown": view["hidden"],
                 "failed_accounts": view["failed"], "agents": _agents_payload(view)},
                ensure_ascii=False, indent=2,
            )
        )
        return 0
    _render_agents(view, make_console(), show_all=show_all)
    return 0
