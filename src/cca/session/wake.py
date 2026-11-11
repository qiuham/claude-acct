"""额度重置后把选中的会话叫醒：原号重开，不换号。

Claude Code 官方的 `autoContinueAtUsageLimit` 只在前台会话里有效；`--bg` 起来的会话
撞额度后就停在那里，重置了也不会自己跑。这个命令补这一段：

    cca wake 001        # 挑 001 上要等重置的会话（像 handoff 一样勾选）
    cca wake 001 002
    cca wake --only=fc52 --at=08:00    # 非交互；--at 是配额数据取不到时的兜底

唤醒时刻直接用 cca 已经知道的配额重置时间：这个号哪个窗口用满了，就等它重置；
两个都满就等更晚的那个。不猜、不解析对话记录。

    cca wake status | --once | --dry-run

常驻进程是 cca watchdog（全机一个，自动起，这里不再有自己的常驻进程）：它每一轮调 run_due() 叫醒到点的条目，
也会替撞墙、又没有别的号可换的后台会话预约一次**有条件的**叫醒（arm）：到点时它确实是撞墙停下的才叫；
做完了、还在跑、已经换到别的号上的，不叫。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from cca.errors import die, note, say
from cca.paths import accounts_root

NUDGE = "额度已重置，继续刚才没做完的事。"
# 一轮里所有「重开后等套接字登记再投话」共用的等待预算（每条的上限是 takeover.REGISTER_WAIT）；
# 常驻进程（watchdog）每轮都要调，不能让几条起不来的会话把一轮拖几分钟。用完的各试一次，
# 没送到的留在 state 里，下一轮见它闲着就直接发话。
ROUND_DELIVER_BUDGET = 60
RESET_MARGIN_SECONDS = 60
# 额度接口给的重置时刻偶尔差几秒：差这么多以内算同一个，不重排
ARM_SAME_SECONDS = 120
FULL_PERCENT = 100
# 叫不醒的条目（会话已删、目标被挡、找不到 claude）别每轮硬试：指数退避，试满这么多次就放弃
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600
MAX_TRIES = 8


def _state_path() -> Path:
    return accounts_root() / "wake.json"


def _log_path() -> Path:
    return accounts_root() / "watchdog.log"


def _read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def _state_lock():
    """改 wake.json 的人不止一个（常驻的 watchdog、cca wake）：读-改-写之间别让别人插进来。"""
    path = _state_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_state(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def reset_time(account: str) -> tuple[float, list[str]]:
    """这个号要等到什么时候：用满的窗口里最晚的重置时刻。返回 (时间戳, 用满的窗口名)。"""
    from cca.report import list_rows  # noqa: PLC0415 - 只有排期时才需要

    rows = list_rows([account])
    windows = ((rows[0] if rows else {}).get("usage") or {}).get("windows") or {}
    full = [window for window in windows.values()
            if window and not window.get("reset_passed")
            and int(window.get("percent") or 0) >= FULL_PERCENT and window.get("resets_at")]
    if not full:
        return 0.0, []
    return max(float(window["resets_at"]) for window in full), [str(w.get("label")) for w in full]


def pick(account: str, items: list, only: list) -> list:
    """挑要叫醒的会话：给了 --only 按前缀匹配，否则弹选择框（和 handoff 一样）。"""
    if only:
        picked = []
        for term in only:
            key = term.strip().lower()
            picked.extend(item for item in items
                          if str(item.get("full") or "").lower().startswith(key)
                          or str(item.get("id") or "").lower().startswith(key)
                          or str(item.get("name") or "").lower() == key)
        return picked
    import questionary  # noqa: PLC0415 - 只有交互时才需要

    from cca.account.handoff_picker import session_line  # noqa: PLC0415

    choices = [questionary.Choice(title=session_line(item), value=item["full"]) for item in items]
    if not choices:
        return []
    picked = questionary.checkbox(f"额度重置后叫醒 {account} 的哪些会话", choices=choices).ask()
    return [item for item in items if item.get("full") in set(picked or [])]


def _display_zone():
    """终端显示用上海时区（和 cca ls 一致）；不改 os.environ["TZ"]。"""
    from zoneinfo import ZoneInfo  # noqa: PLC0415 - 只在排期/打印时刻时用

    from cca.env import DISPLAY_TZ  # noqa: PLC0415 - env 顶部导入本模块族，延迟破环

    return ZoneInfo(DISPLAY_TZ)


def _at_timestamp(at: str) -> float:
    now = datetime.now(_display_zone())
    hour, minute = (int(part) for part in at.split(":", 1))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= now:
        moment += timedelta(days=1)
    return moment.timestamp()


def schedule(account: str, items: list, *, at: str = "", dry_run: bool = False) -> int:
    """把这些会话排上唤醒；时刻来自这个号的额度重置时间（或 --at）。"""
    wake_at, windows = reset_time(account)
    if not wake_at and at:
        wake_at = _at_timestamp(at)
    if not wake_at:
        note(f"{account}: 现在没有窗口用满，不用等（确实要排可以用 --at HH:MM）")
        return 0
    wake_at += RESET_MARGIN_SECONDS          # 正好在重置那一秒叫，可能早了一点又撞墙
    when = datetime.fromtimestamp(wake_at, _display_zone()).strftime("%m-%d %H:%M")
    reason = f"{'、'.join(windows)}用满" if windows else "--at 指定"
    added = 0
    with _state_lock():
        state = _read_state()
        for item in items:
            full = str(item.get("full") or "")
            if not full:
                continue
            say(f"{account}  {str(item.get('id'))[:8]}  {item.get('name') or '(无标题)'}  → {when} 叫醒（{reason}）")
            if not dry_run:
                state[full] = {"account": account, "wake_at": wake_at, "wake_time": when,
                               "name": str(item.get("name") or ""), "cwd": str(item.get("cwd") or "")}
                added += 1
        if added:
            _write_state(state)
    if added:
        from cca.session.watchdog import ensure_running  # noqa: PLC0415

        ensure_running()
    return added


def arm(account: str, session: str, *, resets_at: float, name: str = "", cwd: str = "") -> bool:
    """有条件地预约叫醒（watchdog 替撞墙、又没号可换的后台会话排）：到点时它确实是撞墙停下的才叫。

    人用 cca wake 排的无条件叫醒不覆盖；同一时刻已经排过的不重写。返回是不是新排上（或改了时刻）。
    """
    wake_at = float(resets_at) + RESET_MARGIN_SECONDS
    with _state_lock():
        state = _read_state()
        entry = state.get(session)
        same_time = abs(float((entry or {}).get("wake_at") or 0) - wake_at) < ARM_SAME_SECONDS
        if entry and (not entry.get("only_if_limited") or same_time):
            return False
        state[session] = {"account": account, "wake_at": wake_at,
                          "wake_time": datetime.fromtimestamp(wake_at, _display_zone()).strftime("%m-%d %H:%M"),
                          "name": name, "cwd": cwd, "only_if_limited": True}
        _write_state(state)
    return True


def pending() -> list:
    """排着的叫醒：[(会话完整编号, 条目)]，按时刻先后。"""
    return sorted(_read_state().items(), key=lambda kv: float(kv[1].get("wake_at") or 0))


def forget(session: str) -> bool:
    """会话已经换号接着跑了：它的叫醒条目不用留。返回有没有删。"""
    with _state_lock():
        state = _read_state()
        if state.pop(session, None) is None:
            return False
        _write_state(state)
    return True


def _live_entries(session: str) -> list:
    """它在哪些号上有活着的进程：[(号, 登记表条目)]。条目里 status 是 busy / idle / waiting。"""
    from cca.account.model import discover_profiles  # noqa: PLC0415
    from cca.session.takeover import registry_entry  # noqa: PLC0415

    return [(name, entry) for name in discover_profiles() if (entry := registry_entry(name, session))]


def _plan(session: str, entry: dict) -> tuple:
    """到点了怎么办：(动作, 原因, 登记表条目)。

    动作：keep（还在忙，过会儿再看）、drop（不用叫了）、tell（进程还在、闲着：发一句话）、
    resume（进程没了：原号 --bg --resume）。

    只看进程活没活不够：后台会话一轮结束（包括撞墙）后进程照样留着、闲着，按「还在跑」跳过就永远叫不醒；
    对它 --bg --resume 又会复制一份。所以看登记表的状态，闲着的直接发话。
    """
    from cca.session.watchdog import hits_limit, last_assistant_record, turn_state  # noqa: PLC0415

    conditional = bool(entry.get("only_if_limited"))
    try:
        live = _live_entries(session)
    except Exception:  # noqa: BLE001 - 查不到它在哪就先别动，下一轮再看（宁可晚叫也别造副本）
        return "keep", "查不到它在不在跑", {}
    account = str(entry.get("account") or "")

    def working(name: str, reg: dict) -> bool:
        # 挂着后台任务时登记表一直是 busy / shell：撞墙停下、这一轮已结束的不算在忙（否则永远叫不醒）
        return reg.get("status") != "idle" and turn_state(name, session) == "running"

    if any(working(name, reg) for name, reg in live):
        # 有条件的：到点了还在忙，说明没卡在墙上；人排的：等它闲下来再叫
        return ("drop" if conditional else "keep"), "还在跑", {}
    moved = [name for name, _ in live if name != account]
    if moved:
        return "drop", f"已经在 {'、'.join(moved)} 上了", {}
    if conditional and not hits_limit(last_assistant_record(account, session[:8])):
        return "drop", "不是撞墙停下的（已经做完了）", {}
    if live:
        return "tell", "", live[0][1]
    return "resume", "", {}


def _budget_left(budget_end: float) -> float:
    """这一轮还剩多少等待预算，单条不超过 takeover.REGISTER_WAIT。"""
    from cca.session.takeover import REGISTER_WAIT  # noqa: PLC0415

    return min(float(REGISTER_WAIT), max(0.0, budget_end - time.time()))


def _tell(registry: dict) -> str:
    """进程还在、闲着的会话：发一句话让它接着做（原生跨会话消息），不另起一份。"""
    from cca.session.takeover import deliver  # noqa: PLC0415

    return deliver(registry, NUDGE, {"name": "cca wake"})


def _wake(entry: dict, session: str, *, wait: float | None = None) -> str:
    """在原号上重开并说一句。返回错误，空串表示已发出。wait 是等套接字的上限，不给用 takeover 的默认。"""
    from cca.env import LaunchBlocked, session_env  # noqa: PLC0415 - 起进程时才需要

    exe = shutil.which("claude")
    if not exe:
        return "找不到 claude"
    try:
        # 起会话统一走 session_env：地区检查 + 出口时区 + 语言。
        env = session_env(entry["account"])
    except LaunchBlocked as exc:
        return str(exc)
    cwd = entry.get("cwd") if entry.get("cwd") and Path(entry["cwd"]).is_dir() else None
    from cca.session.takeover import deliver_after_resume, resume_launch  # noqa: PLC0415

    # 有原名：启动提示是 /rename <原名>，NUDGE 等登记好再以消息送到；没名字照旧
    launch, pending = resume_launch(entry["account"], session, NUDGE)
    log = _log_path().open("a", encoding="utf-8")
    try:
        subprocess.Popen(  # noqa: S603 - exe 来自 shutil.which
            [exe, "--bg", "--resume", session, *launch],
            env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        return str(exc)
    finally:
        log.close()
    kwargs = {} if wait is None else {"wait": wait}
    error = deliver_after_resume(entry["account"], session, pending, sender={"name": "cca wake"}, **kwargs)
    return f"重开了但话没送到：{error}" if error else ""


def _list_state(state: dict, due: dict) -> None:
    if not state:
        say("没有排上等额度重置的会话")
    for session, entry in sorted(state.items(), key=lambda kv: kv[1].get("wake_at") or 0):
        mark = "已到期" if session in due else "等"
        cond = "（撞墙才叫）" if entry.get("only_if_limited") else ""
        retry = f"（叫不醒 {entry['tries']} 次，退避中）" if entry.get("tries") else ""
        say(f"{mark} {entry.get('account')}  {session[:8]}  {entry.get('name') or '(无标题)'}  "
            f"{entry.get('wake_time') or ''}{cond}{retry}")


def run_due(*, dry_run: bool = False) -> int:
    """处理到点的条目（watchdog 每一轮调；`cca wake status` 用 dry_run 只列）。

    锁里只读和写状态，投递（要等套接字，可能几十秒）在锁外做，不拖住 `cca wake` 和 arm。
    """
    with _state_lock():
        state = _read_state()
    now = time.time()
    due = {session: entry for session, entry in state.items()
           if float(entry.get("wake_at") or 0) <= now and float(entry.get("retry_at") or 0) <= now}
    if dry_run:
        _list_state(state, due)
        return 0
    budget_end = time.time() + ROUND_DELIVER_BUDGET
    done: dict = {}
    failed: dict = {}
    for session, entry in due.items():
        action, why, registry = _plan(session, entry)
        if action == "keep":
            note(f"{session[:8]} {why}，过一会儿再看")
            continue
        if action == "drop":
            note(f"{session[:8]} {why}，不叫")
            done[session] = entry
            continue
        error = _tell(registry) if action == "tell" else _wake(entry, session, wait=_budget_left(budget_end))
        if error:
            tries = int(entry.get("tries") or 0) + 1
            if tries >= MAX_TRIES:
                note(f"{session[:8]} 叫不醒：{error}（试了 {tries} 次，放弃）")
                done[session] = entry
            else:
                note(f"{session[:8]} 叫不醒：{error}（第 {tries} 次，稍后再试）")
                failed[session] = (entry, tries)
            continue
        say(f"已叫醒 {entry.get('account')} {session[:8]}（{entry.get('name') or '无标题'}）")
        done[session] = entry
    if done or failed:
        with _state_lock():
            state = _read_state()
            for session, entry in done.items():
                if state.get(session) == entry:      # 投递期间被改排过的不删
                    state.pop(session)
            for session, (entry, tries) in failed.items():
                if state.get(session) == entry:
                    backoff = min(RETRY_BASE_SECONDS * 2 ** (tries - 1), RETRY_MAX_SECONDS)
                    state[session] = {**entry, "tries": tries, "retry_at": time.time() + backoff}
            _write_state(state)
    return 0


def cmd_wake(args: list) -> int:
    """cca wake [<号>...] [--only=<前缀>] [--at HH:MM] [--dry-run] | status | --once"""
    words = [a for a in args if not a.startswith("-")]
    action = words[0] if words else ""
    if action == "status":
        return run_due(dry_run=True)
    if "--once" in args:
        return run_due()
    dry_run = "--dry-run" in args
    only = [a.split("=", 1)[1] for a in args if a.startswith("--only=")]
    at = next((a.split("=", 1)[1] for a in args if a.startswith("--at=")), "")
    if not words:
        # 不带号：只看排了哪些（排期本身就来自 cca wake <号>）
        return run_due(dry_run=True)
    from cca.account.model import discover_profiles  # noqa: PLC0415

    unknown = [word for word in words if word not in discover_profiles()]
    if unknown:
        die(f"不认识的用法: {' '.join(unknown)}（用法: cca wake [<号>...] [--only=…] [--at=HH:MM] | status | --once；"
            "停常驻进程用 cca watchdog stop）")
    from cca.session.push import handoff_candidates  # noqa: PLC0415 - 只在这条路上用

    total = 0
    for account in words:
        items = [item for item in handoff_candidates(account)
                 if not item["running"] and item.get("movable", True)]
        if not items:
            say(f"{account}: 没有可叫醒的会话")
            continue
        total += schedule(account, pick(account, items, only), at=at, dry_run=dry_run)
    return 0
