"""额度快用完时给会话的提醒：要不要换号、换到哪个号、怎么换。

面板插件每隔几分钟在会话里跑 `cca quota-hint --json`；命中时把 `text` 附在下一次工具结果后面
（模型读得到、人看不到、不另起一轮）。到点怎么搬由常驻的 cca watchdog 管（这一轮结束时搬走，见
watchdog.py），这里只是告知模型会搬去哪、不想搬时怎么留（`cca handoff --stay`）；
同一个号同一个窗口只提醒一次（`key` 给插件去重）。每次被调用时顺手保证 watchdog 在跑。

Claude Code 二进制里有一句给模型的「Usage limit approaching. Checkpoint now …」，但受服务端开关控制：
2026-11 在本机共享池里 311 个撞过额度的会话中一次都没出现过；界面上给人看的「已用 N%」模型看不到。
所以模型要知道额度快满，只能靠这里。

提醒点按窗口分：5 小时默认 95%，7 天默认 98%。越晚本号剩下的越少，但不等到撞墙（撞墙后模型调不了
API，没法自己换号）。周窗口涨得慢（一个百分点够跑很久），98% 也来得及换号；5 小时窗口几条会话一起用时
几分钟就能涨几个点，所以早一点。离阈值不远时查得勤一点（2 分钟一问、额度超过 2 分钟就现取），免得提醒晚到；远的时候 5 分钟一问。
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path


# 用到多少开始提醒（%），按窗口分：越晚本号剩下的越少（浪费越少），但要留出做完这一步、换号的余地。
# 不等到撞墙：撞墙后模型调不了 API，没法自己换号，「撞墙」的报错也不一定每次都有。
# 5 小时窗口涨得快（几条会话一起用，几分钟就是几个点），95%；7 天窗口涨得慢，98% 也来得及。
DEFAULT_THRESHOLDS = {"five_hour": 95, "seven_day": 98}
THRESHOLD_ENV = {"five_hour": "CCA_QUOTA_HINT_PERCENT", "seven_day": "CCA_QUOTA_HINT_WEEKLY_PERCENT"}
WINDOWS = ("five_hour", "seven_day")
# 远的时候按 cca ls 的缓存规则（5 分钟），插件 5 分钟问一次就够：从 80% 涨到 95% 一般要几十分钟。
# 离阈值 15 个百分点以内查得勤一点：2 分钟一问、当前号的额度超过 2 分钟才现取，最坏落后约 4 分钟。
# 现取（起一次 claude -p /usage）按号共享：同一个号不管开着几个会话，2 分钟最多取一次。
NEAR_MARGIN = 15
# 同一个号上的会话一起到提醒点、一起换号时，分散到这么多个余量充足的号上（按周额度重置先后取前几个），
# 免得一窝蜂搬到同一个号、把它的 5 小时窗口几十分钟就打满、再集体搬一次。
SPREAD_TARGETS = 3
NEAR_FRESH_SECONDS = 120
POLL_NEAR_SECONDS = 120
POLL_FAR_SECONDS = 300
POLL_API_SECONDS = 3600      # API 号按量计费，不提醒；偶尔看一眼号有没有换就行
WINDOW_LABELS = {"five_hour": "5 小时", "seven_day": "7 天"}
FULL_PERCENT = 100
SECONDS_PER_MINUTE = 60
MINUTES_SHOWN = 90           # 超过这么多分钟改用小时说


def thresholds() -> dict:
    """每个窗口的提醒阈值（%）：默认 5 小时 95、7 天 98，环境变量可调，夹在 50~99。"""
    limits = {}
    for key, default in DEFAULT_THRESHOLDS.items():
        raw = os.environ.get(THRESHOLD_ENV[key], "")
        try:
            value = int(raw) if raw else default
        except ValueError:
            value = default
        limits[key] = min(99, max(50, value))
    return limits


def live_windows(windows: dict, now: float) -> dict:
    """还没重置的窗口：{窗口名: 窗口}。"""
    return {key: w for key in WINDOWS if (w := live_window(windows.get(key), now))}


def _percent_of(window: dict) -> int:
    return int(window.get("percent") or 0)


def at_limit(windows: dict, limits: dict, now: float) -> list:
    """用到各自阈值的窗口：[(窗口名, 窗口)]。"""
    return [(key, w) for key in WINDOWS
            if (w := live_window(windows.get(key), now)) and _percent_of(w) >= limits[key]]


def current_account() -> str:
    """这条命令在哪个号的会话里：看 CLAUDE_CONFIG_DIR。没设就不属于任何号，返回空串
    （别把终端里随手跑的命令当成某个号的）。"""
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    return Path(folder).name if folder else ""


def live_window(window: dict | None, now: float) -> dict | None:
    """还没重置的窗口才算数：重置时刻已过的是旧值。"""
    if not window or window.get("reset_passed"):
        return None
    resets = window.get("resets_at")
    if resets and resets <= now:
        return None
    return window


def _hot_window(windows: dict, limits: dict, now: float) -> tuple:
    """用到阈值的窗口里最紧的那个：(窗口名, 窗口)；都没到返回 ("", None)。"""
    hot = at_limit(windows, limits, now)
    if not hot:
        return "", None
    return max(hot, key=lambda pair: _percent_of(pair[1]))


def _near(windows: dict, limits: dict, now: float) -> bool:
    """离提醒阈值不远了：该查得勤一点。"""
    return any(_percent_of(w) >= limits[key] - NEAR_MARGIN
               for key in WINDOWS if (w := live_window(windows.get(key), now)))


def pick_target(rows: list, account: str, *, limits: dict, shares, now: float,  # noqa: PLR0913 - 后四个都是关键字参数
                session: str = "") -> dict | None:
    """换到哪个号。

    不给 session（终端里人看）：ranked_targets 的第一个。给了 session：在排前面的几个余量充足的号里
    按会话编号固定挑一个——同一个号上的一批会话分散开，同一条会话每次问都是同一个答案。
    """
    ranked = ranked_targets(rows, account, limits=limits, shares=shares, now=now)
    if not ranked:
        return None
    spread = [row for row in ranked if roomy(row, limits, now)][:SPREAD_TARGETS]
    if not session or len(spread) < 2:  # noqa: PLR2004 - 少于两个就没得分
        return ranked[0]
    return spread[int(hashlib.sha256(session.encode()).hexdigest(), 16) % len(spread)]


def roomy(row: dict, limits: dict, now: float) -> bool:
    """两个窗口离各自的提醒点都还差 NEAR_MARGIN 个点以上：搬一批会话过去也撑得住。

    只差几个点的号（例如周额度 89%、提醒点 98%）接一批会话，几小时就又到提醒点，会话得再搬一次；
    每搬一次都要把整段上下文重读一遍（不走缓存），代价付两次。
    """
    windows = (row.get("usage") or {}).get("windows") or {}
    return not any(_percent_of(w) >= limits[key] - NEAR_MARGIN
                   for key in WINDOWS if (w := live_window(windows.get(key), now)))


def ranked_targets(rows: list, account: str, *, limits: dict, shares, now: float) -> list:
    """能换过去的号，按先用谁排：两个窗口都还没到各自阈值的订阅号里，余量充足的（roomy）在前，
    同一档里周额度最早重置的先用。都不充足时照样给出最好的那个，总比没有强。"""
    from cca.api.usage import weekly_reset_key  # noqa: PLC0415

    def room(row: dict) -> bool:
        windows = (row.get("usage") or {}).get("windows") or {}
        # 两个窗口的数据都要有：不知道就别推荐（和 usage_roominess 一样，不参与比较）；
        # 周窗口缺了也没法按「周额度最早重置」排
        if not all(key in windows for key in WINDOWS):
            return False
        return not at_limit(windows, limits, now)

    candidates = [row for row in rows
                  if row.get("profile") != account and row.get("kind") != "api"
                  and row.get("logged_in") and shares(row["profile"]) and room(row)]

    def key(row: dict) -> tuple:
        windows = (row.get("usage") or {}).get("windows") or {}
        fullness = max((int(w.get("percent") or 0) for w in windows.values()), default=0)
        return (not roomy(row, limits, now), weekly_reset_key(windows, now), fullness)

    return sorted(candidates, key=key)


def _why(target: dict, limits: dict, now: float) -> str:
    """为什么推荐这个号（一句，放进括号里）。"""
    if roomy(target, limits, now):
        return "余量充足、周额度较早重置的订阅号"
    return "各号余量都不多，这个周额度最早重置"


def _until(seconds: float) -> str:
    minutes = int(max(0, seconds) // SECONDS_PER_MINUTE)
    return f"约 {minutes} 分钟后" if minutes < MINUTES_SHOWN else f"约 {round(minutes / 60)} 小时后"


def _percent(row: dict, key: str, now: float) -> str:
    """窗口已用多少：没有数据写「未知」，重置时刻已过的算 0%（已经重置了）。"""
    window = ((row.get("usage") or {}).get("windows") or {}).get(key)
    if not window:
        return "未知"
    live = live_window(window, now)
    return f"{int(live.get('percent') or 0)}%" if live else "0%"


def advise(account: str, session: str, *, rows: list, shares, kind: str) -> dict:
    """算这条会话该不该收到提醒、提醒什么。rows 是 `cca ls --json` 那样的每号一行。"""
    from cca.report import format_clock  # noqa: PLC0415

    now = time.time()
    limits = thresholds()
    mine = next((row for row in rows if row.get("profile") == account), None)
    if mine is None or mine.get("kind") == "api":
        return {"account": account, "warn": False, "next_check_seconds": POLL_API_SECONDS}
    windows = (mine.get("usage") or {}).get("windows") or {}
    poll = POLL_NEAR_SECONDS if _near(windows, limits, now) else POLL_FAR_SECONDS
    name, window = _hot_window(windows, limits, now)
    if window is None:
        return {"account": account, "warn": False, "next_check_seconds": poll}
    resets = window.get("resets_at")
    percent = int(window.get("percent") or 0)
    when = f"{format_clock(resets)} 重置（{_until((resets or now) - now)}）"
    target = pick_target(rows, account, limits=limits, shares=shares, now=now, session=session)
    facts = ""
    if target is not None:
        facts = (f"{target['profile']}（{_why(target, limits, now)}，5 小时已用 {_percent(target, 'five_hour', now)}、"
                 f"7 天已用 {_percent(target, 'seven_day', now)}）")
    used = f"{account} 的 {WINDOW_LABELS[name]}额度已用 {percent}%，{when}。"
    # 撞墙后要等到哪个点：到阈值的窗口里最晚重置的那个（两个都快满时，只等短窗口会再撞一次）
    wake_at = max(float(w.get("resets_at") or 0) for _key, w in at_limit(windows, limits, now))
    if kind == "cli":
        text = _text_for_person(account, used, target, facts, full=percent >= FULL_PERCENT)
    elif kind != "bg":
        text = _text_for_interactive(account, session, used, target, facts)
    else:
        text = _text_for_background(used, target, facts)
    return {"account": account, "session": session, "warn": True, "next_check_seconds": poll, "wake_at": wake_at,
            "key": f"{account}:{name}:{int(resets or 0)}", "target": (target or {}).get("profile", ""),
            "text": text}


def _text_for_background(used: str, target, facts: str) -> str:
    """给后台会话里的模型：告知式。到点由常驻的 cca watchdog 在这一轮结束时搬，模型只在不想搬时说一声。"""
    head = f"【cca 额度提醒】本号 {used}"
    if target is None:
        return (f"{head}其它订阅号也没有余量，没有可换的号：接着做就行；"
                "撞墙停下后 cca 会在额度重置后叫醒它。")
    return (f"{head}这一轮结束后 cca 会把会话换到 {facts}"
            "（编号、名字不变，后台任务会停掉，续上后会收到通知）。"
            "如果这一轮就收尾、之后没活了，运行 `cca handoff --stay` 留在本号。")


def _text_for_interactive(account: str, session: str, used: str, target, facts: str) -> str:
    """给交互会话里的模型：它换不了自己，转告人。"""
    head = f"【cca 额度提醒】本号 {used}这是交互会话，cca 不能替它换号。"
    if target is None:
        return f"{head}其它订阅号也没有余量，没有可换的号：接着做，撞墙后等 {account} 的额度重置。"
    return (f"{head}剩下的活还多的话，告诉用户：可以退出后用 "
            f"`cca {target['profile']} --resume {session or '<完整会话编号>'}` 在 {facts}接着聊。")


def _text_for_person(account: str, used: str, target, facts: str, *, full: bool) -> str:
    """给终端里的人看：不在会话里，没有「本号」，命令写成显式的源号 → 目标号。"""
    state = "这个号上的会话已经停下了。" if full else ""
    wait = f"不换号也可以等重置后叫醒：`cca wake {account} --only=<编号>`。"
    if target is None:
        return f"{used}{state}其它订阅号也没有余量，只能等重置：`cca wake {account} --only=<编号>`。"
    dest = target["profile"]
    return (f"{used}{state}\n"
            f"- 换号接着跑：首选 {facts}——`cca handoff {account} {dest} --only=<编号>`"
            f"（在会话里也可以让它自己运行 `cca handoff {dest} --current`）。\n"
            f"- {wait}")


def overview(rows: list, *, shares) -> dict:
    """不在哪个号的会话里、也没写号：给出现在要换号的话先用谁。"""
    now = time.time()
    ranked = ranked_targets(rows, "", limits=thresholds(), shares=shares, now=now)[:3]
    if not ranked:
        text = "不在哪个号的会话里。现在没有两个窗口都还有余量的订阅号。看某个号：cca quota-hint <号>。"
    else:
        first, rest = ranked[0], [row["profile"] for row in ranked[1:]]
        text = (f"不在哪个号的会话里（看某个号：cca quota-hint <号>）。现在要换号的话，首选 {first['profile']}"
                f"（{_why(first, thresholds(), now)}，5 小时已用 {_percent(first, 'five_hour', now)}、"
                f"7 天已用 {_percent(first, 'seven_day', now)}）"
                + (f"，其次 {'、'.join(rest)}。" if rest else "。"))
    return {"account": "", "warn": False, "targets": [row["profile"] for row in ranked],
            "next_check_seconds": POLL_FAR_SECONDS, "text": text}


def _slot_path(account: str) -> Path:
    return Path("~/.cca/quota-hint").expanduser() / f"{account}.lock"


@contextlib.contextmanager
def _refresh_slot(account: str):
    """同一个号同一时间只让一个会话去现取额度（起一次 claude -p /usage）；拿不到锁就用缓存。"""
    path = _slot_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _last_read(account: str, row: dict) -> float:
    """这个号的额度上次是什么时候读的：claude 的快照、cca 的缓存、上次去现取（成败都算）里最新的那个。

    只看快照不够：现取写的是 cca 的缓存；只看缓存也不够：现取失败时什么都不写，不记一笔就会每次都重试。
    """
    from cca.api.usage import read_usage_cache  # noqa: PLC0415

    stamp = _slot_path(account).with_suffix(".tried")
    tried = stamp.stat().st_mtime if stamp.exists() else 0.0
    cached = float((read_usage_cache().get(account) or {}).get("fetched_at") or 0)
    return max(float((row.get("usage") or {}).get("fetched_at") or 0), cached, tried)


def cached_rows() -> list:
    """每号一行，全部走缓存和本地快照（不起 claude，不现取）。"""
    from cca.account.model import discover_profiles  # noqa: PLC0415
    from cca.report import _fetch_list_rows  # noqa: PLC0415

    return _fetch_list_rows(discover_profiles(), [], refresh=False)


def account_rows(account: str) -> list:
    """每号一行：别的号用缓存和快照，只有当前号在数据旧了时现取。

    「旧」的标准看离阈值远近：远的时候 5 分钟（和 cca ls 一样），近的时候 2 分钟。
    每个会话每隔一会儿就跑一次，所以现取按号共享：拿锁、拿到锁后再看一眼别人是不是刚取过、
    取之前记一笔（失败了也不会每次重试）；同一个号不管开着几个会话，一段时间里最多现取一次。
    """
    from cca.account.model import discover_profiles  # noqa: PLC0415
    from cca.config import USAGE_LIVE_TTL  # noqa: PLC0415
    from cca.report import _fetch_list_rows  # noqa: PLC0415

    rows = _fetch_list_rows(discover_profiles(), [], refresh=False)
    mine = next((row for row in rows if row.get("profile") == account), None)
    if mine is None or mine.get("kind") == "api":
        return rows
    near = _near((mine.get("usage") or {}).get("windows") or {}, thresholds(), time.time())
    max_age = NEAR_FRESH_SECONDS if near else USAGE_LIVE_TTL
    if time.time() - _last_read(account, mine) <= max_age:
        return _with_cache(rows, account)          # 别人刚取过（或刚试过）：只读，不起 claude
    with _refresh_slot(account) as got:
        if not got or time.time() - _last_read(account, mine) <= max_age:
            return _with_cache(rows, account)
        _slot_path(account).with_suffix(".tried").touch()   # 先记一笔：取失败了也不会每次重试
        # 近的时候强制现取（缓存还没过 5 分钟也取）；远的时候按缓存规则，过期才取
        fresh = _fetch_list_rows([account], [account], refresh=near)
    return [fresh[0] if fresh and row.get("profile") == account else row for row in rows]


def _with_cache(rows: list, account: str) -> list:
    """快照之外，cca 现取写下的缓存要是更新，就用缓存那份（读文件，不起 claude）。"""
    from cca.api.usage import read_usage_cache  # noqa: PLC0415

    cached = read_usage_cache().get(account) or {}

    def newer(row: dict) -> dict:
        usage = row.get("usage") or {}
        if (row.get("profile") != account or not isinstance(cached.get("windows"), dict)
                or float(cached.get("fetched_at") or 0) <= float(usage.get("fetched_at") or 0)):
            return row
        return {**row, "usage": {**usage, "windows": cached["windows"],
                                 "fetched_at": cached["fetched_at"], "source": "cache"}}

    return [newer(row) for row in rows]


def _session_kind(account: str, session: str) -> str:
    """这条会话是后台会话（bg）还是交互会话：看原号登记表。认不出当后台。"""
    if not session:
        return "bg"
    from cca.session.takeover import registry_entry  # noqa: PLC0415

    return str(registry_entry(account, session).get("kind") or "bg")


def _ensure_watchdog(account: str) -> None:
    """每次提醒被调用（不分交互、后台、命令行）：保证常驻的看门狗在（它负责到点换号、撞墙叫醒）。

    探测是微秒级，真要起时只 fork 一个进程；出什么事都不该影响提醒本身。
    """
    from cca.account.store import profile_kind  # noqa: PLC0415
    from cca.session.watchdog import ensure_running  # noqa: PLC0415

    with contextlib.suppress(Exception):
        if profile_kind(account) != "api":
            ensure_running()


def cmd_quota_hint(args: list) -> int:
    """cca quota-hint [<号>] [--json] [--session <编号>]"""
    from cca.account.model import discover_profiles, require_profile  # noqa: PLC0415
    from cca.errors import say  # noqa: PLC0415
    from cca.report import _fetch_list_rows  # noqa: PLC0415
    from cca.session.push import shares_sessions  # noqa: PLC0415

    session, named = os.environ.get("CLAUDE_CODE_SESSION_ID") or "", ""
    words = iter(args)
    for arg in words:
        if arg.startswith("--session="):
            session = arg.split("=", 1)[1]
        elif arg == "--session":
            session = next(words, "")
        elif not arg.startswith("-"):
            named = require_profile(arg, usage="cca quota-hint [<号>] [--json]")
    account = named or current_account()
    if not account:
        result = overview(_fetch_list_rows(discover_profiles(), [], refresh=False), shares=shares_sessions)
    else:
        # 点了号、或者不在会话里：是人在终端里看，不是会话里的模型
        kind = "cli" if named or not session else _session_kind(account, session)
        result = advise(account, "" if kind == "cli" else session, rows=account_rows(account),
                        shares=shares_sessions, kind=kind)
        _ensure_watchdog(account)
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False))  # noqa: T201 - 给面板插件读的 JSON
    elif result.get("text"):
        say(result["text"])
    else:
        limits = thresholds()
        say(f"{account}: 额度还够（提醒阈值：5 小时 {limits['five_hour']}%、7 天 {limits['seven_day']}%），不用换号")
    return 0
