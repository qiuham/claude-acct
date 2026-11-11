"""额度看门狗 v2：全机一个常驻进程，管订阅号后台会话的换号和叫醒。

**什么时候搬**（只对订阅号的后台会话；交互会话搬不了，由提醒请人处理）：

- 号上某个窗口到了提醒点（`quota_hint.thresholds()`：5 小时 95%、7 天 98%），这个号上还在干活的会话，
  在**这一轮结束时**搬走：登记表连续两次 idle，或对话记录末尾是 turn_duration（后台 shell 还在跑时登记表是
  shell，只看 idle 抓不到）连续两次。不等后台任务：换号时它们会被停掉，续上后会话会收到通知。
- 已经撞墙的会话：不看阈值和下面的例外，立刻搬（源号额度已经恢复的除外，交给 wake 叫醒）。
- 例外，不搬：卡住的窗口不到 15 分钟就重置 · 会话闲了 30 分钟以上（不耗额度）· 会话里运行过
  `cca handoff --stay`（同一个重置时刻内有效）。
- 搬去哪：`quota_hint.pick_target`（带会话编号，一批会话分散开）。全都满、没有可去的号：撞墙的会话
  排进 wake（`wake.json`），等最早的重置后叫醒。

**怎么跑**：`ensure_running()` 由 `cca quota-hint`（面板插件定时调）、`cca <号>` 启动订阅号会话、
`cca wake` 排期、`cca handoff --current/--stay` 顺手调；全机靠 `watchdog.lock` 的 flock 保证只有一个，
崩了内核自动放锁，下次调用再拉起；订阅号上没有活着的后台会话、也没有待叫醒的条目和意图，连续 10 轮
（且启动满 5 分钟）后自己退出。

**意图**写在 `watchdog.json`：`moves`（`--current`：会话要求这一轮结束就搬）、`stays`（`--stay`）。
续跑一律走 `lifecycle.move_session` → `run_handoff` → `_move_one`，用 `/rename` 保名，绝不给 `--resume` 加 `--name`。

用法：

    cca watchdog [status]       # 在不在跑、各号额度、接下来搬哪条、哪些不搬、哪些待叫醒
    cca watchdog --dry-run      # 只算一遍要做什么，不动手（只用缓存的额度）
    cca watchdog stop
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from cca.account.model import discover_profiles
from cca.account.store import profile_kind
from cca.api.usage import live_usage, usage_snapshot
from cca.errors import die, say
from cca.paths import accounts_root, profile_dir
from cca.session.push import session_name, session_transcript, shares_sessions
from cca.session.takeover import process_config_dir

FULL = 100                       # 窗口用量到这个数就算用满
ROUND_SECONDS = 30               # 一整轮（读额度、决策、叫醒）的间隔
ARMED_POLL = 2                   # 有「这一轮结束就搬」的会话时，复查它们的间隔
SETTLED_COUNT = 2                # 连续观察到这一轮结束几次才搬（避开轮与轮之间一闪而过的停顿）
RESET_SOON_SECONDS = 15 * 60     # 卡住的窗口这么快就重置：等它重置，不搬
IDLE_SECONDS = 30 * 60           # 会话这么久没动静：它不耗额度，不搬
RECOVER_MARGIN = 15              # 撞墙后所有窗口都低于 阈值 - 这个数，算源号额度已经恢复
STAY_SAME_SECONDS = 120          # 重置时刻差这么多以内算同一段（和 wake.ARM_SAME_SECONDS 一致）
MOVE_TTL_SECONDS = 12 * 3600     # 「这一轮结束就搬」的登记放这么久还没搬成就作废
COOLDOWN_SECONDS = 300           # 搬失败的会话冷却这么久再试（每多失败一次翻倍）
COOLDOWN_MAX_SECONDS = 6 * 3600  # 冷却的上限
MAX_FAILS = 4                    # 同一条会话累计搬失败这么多次就不再自动搬（写在 watchdog.json 的 fails 里）
WAKE_COOLDOWN_SECONDS = 600      # 同一条会话两次「额度已恢复，直接叫」之间至少隔这么久（避免 429 之类反复撞墙刷屏）
LOCK_WAIT_SECONDS = 2.0          # 常驻进程抢单例锁：探测方会瞬间占锁，抢不到先重试一小会儿再认输
MAX_MOVES_PER_ROUND = 3          # 搬迁串行且慢（要等源号退出、等套接字），一轮最多做这么多条
QUIET_ROUNDS = 10                # 连续这么多轮没事可做才退出
START_GRACE_SECONDS = 300        # 启动后这么久内不退，免得刚起就退
SPAWN_STAMP_SECONDS = 10         # 同时到来的多次 ensure_running 只 fork 一个
TURN_TAIL_BYTES = 1_000_000      # 判断这一轮停没停只看对话记录末尾这么多
STATE_VERSION = 1

# 一条会话这一轮的决定
MOVE_NOW = "move-now"        # 立刻搬（撞墙）
ARMED = "armed"              # 这一轮结束时搬
WAIT_WAKE = "wait-wake"      # 撞墙又没号可去：排进 wake，等重置后叫醒
WAKE_NOW = "wake-now"        # 撞墙了但源号额度已经恢复：交给 wake 叫醒
SKIP = "skip"
DUPLICATE = "duplicate"    # 同一条会话在几个号上同时跑：留一份，停掉其余的
STOP_TIMEOUT_SECONDS = 60    # claude stop 子进程本身的超时
DUP_RETRY_SECONDS = 300      # 重复份没处理掉（停不掉等）时，隔多久再试；期间不重复写日志、不重复发通知

SHORT_LABELS = {"five_hour": "5h", "seven_day": "7d"}


# ---------------------------------------------------------------- 额度

def log(text: str) -> None:
    """写一行带时间的日志：后台跑的时候没人看终端，事后要能查。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        root = accounts_root()
        root.mkdir(parents=True, exist_ok=True)
        with (root / "watchdog.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {text}\n")
    except OSError:
        return


def subscription_profiles() -> list[str]:
    """所有订阅号（没有清单文件的号）。

    号清单统一走 `discover_profiles()`（和 `cca ls` 一样）：它只认合法的号名，
    不会把 `.cache` 这类缓存目录当成号。
    """
    return sorted(name for name in discover_profiles()
                  if profile_kind(name) == "oauth")


def has_room(windows: dict) -> bool:
    """所有窗口都没用满，才算还有额度。"""
    return all(int(window.get("percent") or 0) < FULL for window in (windows or {}).values())


def reset_at(windows: dict) -> float:
    """用满的窗口里最早的重置时间（epoch 秒）；没有就返回 0。"""
    times = [float(w.get("resets_at") or 0) for w in (windows or {}).values()
             if int(w.get("percent") or 0) >= FULL and w.get("resets_at")]
    return min(times) if times else 0.0


def quotas(profiles: list[str], *, force: bool = False) -> dict:
    """{号: {"windows": …}}，取不到的号不带窗口（当作有额度，别误判）。"""
    return quota_snapshot(profiles, force=force)[0]


def quota_snapshot(profiles: list[str], *, force: bool = False) -> tuple:
    """取额度并保留失败原因：`({号: 窗口表}, {号: 失败原因}, 整体失败原因)`。

    单个号取不到 → 记在第二个返回值（面板挂到那个号上）。
    整批请求本身出错 → 记在第三个返回值（面板挂到最外层）。
    `live_usage` 自己有超时；这里再兜一层异常，面板不能因为取额度失败就整个崩掉。
    """
    try:
        snapshots, failures = live_usage(profiles, force=force)
    except Exception as exc:  # noqa: BLE001
        return {}, {}, f"{type(exc).__name__}: {exc}"
    failures = failures or {}
    quota = {}
    for name in profiles:
        windows = (snapshots.get(name) or {}).get("windows") or {}
        if not windows and name not in failures:
            # live_usage 有时只“建议用本地快照”就跳过该号、不把快照放进返回值；
            # 这里直接读一次 `.claude.json` 里的快照，别把有额度的号显示成空白。
            windows = (usage_snapshot(name) or {}).get("windows") or {}
        quota[name] = windows
    return quota, failures, ""




# ---------------------------------------------------------------- 记录：通知

def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sideagent_dir() -> Path:
    """sideagent 的记录目录；看门狗只往这里追加通知流水。"""
    return Path("~/.cca/sideagents").expanduser()


def _append_jsonl(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def notify_move(source: str, target: str, title: str) -> None:
    """搬成功写一条通知，面板会把它变成横幅，例如「dagset：b005 → b004」。"""
    _append_jsonl(sideagent_dir() / "notices.jsonl", {
        "at": _now_iso(),
        "text": f"{title}：{source} → {target}",
    })


def session_title(account: str, session: str) -> str:
    """展示用标题：规范取名（push.session_name）；没有就用短编号。"""
    path = session_transcript(profile_dir(account), str(session)[:8])
    return session_name(path) or str(session)[:8] or "这条会话"



# ---------------------------------------------------------------- 撞墙的会话

# 只在「明确是错误记录」里认这些词；裸 "429" 太松（正文里也可能出现），不用
LIMIT_WORDS = ("usage limit", "rate limit", "you've hit your", "hit your weekly",
               "hit your 5-hour", "hit your 7-day", "api error: 429")


def hits_limit(record: dict) -> bool:
    """这条记录是不是额度错误。

    认结构化字段 `isApiErrorMessage`，再要求文字里出现额度相关词；
    只看文字会误判（正文里也可能提到 rate limit）。
    """
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return False
    text = _assistant_text(record).lower()
    if not text:
        return False
    if not any(word in text for word in LIMIT_WORDS):
        return False
    # 老版本不带这个字段：文字命中也算，但必须整条只是那句错误
    return bool(record.get("isApiErrorMessage")) or text.lstrip().startswith("api error")


def _assistant_text(record: dict) -> str:
    blocks = ((record.get("message") or {}).get("content") or [])
    return "".join(str(block.get("text") or "") for block in blocks
                   if isinstance(block, dict) and block.get("type") == "text")


TAIL_BYTES = 2_000_000   # 找最后一条 assistant 记录时读对话记录末尾多少字节


def last_assistant_record(profile: str, short_id: str) -> dict:
    """这条会话最后一条 assistant 记录；拿不到返回空字典。"""
    return last_assistant_in(session_transcript(profile_dir(profile), short_id))


def last_assistant_in(path) -> dict:
    """对话记录文件里最后一条 assistant 记录；没有文件或读不到返回空字典。

    只看 assistant：错误后面还会跟 system / user(tool_result) 之类，看最后一行会漏。
    """
    if path is None:
        return {}
    # 只读文件末尾一段：对话记录常有十几 MB，整份读一遍要几百毫秒，status 一次要读好几份
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    for line in reversed(lines[-500:]):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "assistant":
            return record
    return {}


def fresh_room(name: str) -> bool:
    """搬之前现查一次这个号：缓存可能是旧的，搬过去才发现也满了就白搬。"""
    return has_room(quotas([name], force=True).get(name) or {})


# ---------------------------------------------------------------- 意图：watchdog.json

def _state_path() -> Path:
    return accounts_root() / "watchdog.json"


@contextlib.contextmanager
def _state_lock():
    """改 watchdog.json 的人不止一个（常驻进程、会话里的 --current / --stay）：读-改-写之间别让别人插进来。"""
    path = accounts_root() / "watchdog.state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_intents() -> dict:
    """{"moves": {会话: {account, target, at}}, "stays": {会话: {account, at, windows}}, "fails": {会话: {account, n, until}}}；读不到就是空的。"""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"version": STATE_VERSION,
            "moves": data.get("moves") if isinstance(data.get("moves"), dict) else {},
            "stays": data.get("stays") if isinstance(data.get("stays"), dict) else {},
            "fails": data.get("fails") if isinstance(data.get("fails"), dict) else {}}


def _write_intents(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def request_move(account: str, target: str, session: str) -> None:
    """`cca handoff <目标> --current`：这条会话这一轮结束就搬到 target。同一条会话只留一条意图，后写的覆盖先写的。"""
    with _state_lock():
        data = read_intents()
        data["stays"].pop(session, None)
        data["moves"][session] = {"account": account, "target": target, "at": int(time.time())}
        _write_intents(data)
    ensure_running()


def request_stay(account: str, session: str) -> dict:
    """`cca handoff --stay`：记下此刻各未重置窗口的重置时刻，这一段内不因提醒点搬这条会话。返回 {窗口: 重置时刻}。"""
    from cca.session import quota_hint  # noqa: PLC0415

    now = time.time()
    try:
        rows = quota_hint.account_rows(account)
    except Exception:  # noqa: BLE001
        rows = []
    live = quota_hint.live_windows(_windows_of(rows, account), now)
    windows = {key: float(w["resets_at"]) for key, w in live.items() if w.get("resets_at")}
    with _state_lock():
        data = read_intents()
        data["moves"].pop(session, None)
        data["stays"][session] = {"account": account, "at": int(now), "windows": windows}
        _write_intents(data)
    ensure_running()
    return windows


ABSENT_ROUNDS = 3     # 会话连续这么多轮都不在，才算真没了（读登记表偶尔撞上正在重写，一次读不到不能删意图）


def _missing(data: dict, live: set, absent: dict | None) -> set:
    """意图里出现、但这一轮没在登记表看到的会话里，算「真没了」的那些（连续 ABSENT_ROUNDS 轮不在；absent 为 None 则一次即算）。"""
    keys = {(str(item.get("account")), full)
            for kind in ("moves", "stays", "fails") for full, item in data[kind].items()}
    missing = set()
    for account, full in keys:
        if (account, full) in live:
            if absent is not None:
                absent.pop(full, None)
        elif absent is None:
            missing.add(full)
        else:
            absent[full] = absent.get(full, 0) + 1
            if absent[full] >= ABSENT_ROUNDS:
                missing.add(full)
    if absent is not None:
        for full in set(absent) - {full for _a, full in keys}:
            del absent[full]
    return missing


def gc_intents(live: set, now: float, absent: dict | None = None) -> bool:
    """清掉作废的意图：会话已经不在那个号上、stay 的窗口都过了重置、move 放太久。live 是 {(号, 完整编号)}。

    absent 是常驻进程跨轮保存的 {会话: 连续不在的轮数}：连续 ABSENT_ROUNDS 轮不在才清
    （读登记表偶尔撞上正在重写，一次读不到不能删 --stay）。不给就一次不在即清。
    """
    with _state_lock():
        data = read_intents()
        missing = _missing(data, live, absent)
        stale = {
            "moves": lambda full, item: full in missing or now - float(item.get("at") or 0) > MOVE_TTL_SECONDS,
            "stays": lambda full, item: full in missing or not item.get("windows")
            or all(float(t) <= now for t in item["windows"].values()),
            "fails": lambda full, _item: full in missing,
        }
        changed = False
        for kind, is_stale in stale.items():
            for full, item in list(data[kind].items()):
                if is_stale(full, item):
                    del data[kind][full]
                    changed = True
        if changed:
            _write_intents(data)
    return changed


# ---------------------------------------------------------------- 观察

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def live_bg_sessions(account: str) -> list[dict]:
    """这个号上进程活着的后台会话（读登记表，不起 claude）。条目里有 sessionId、status、name、cwd、statusUpdatedAt。"""
    folder = profile_dir(account) / "sessions"
    out = []
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("kind") != "bg" or not data.get("sessionId"):
            continue
        pid = int(data.get("pid") or 0)
        if pid > 0 and not _alive(pid):
            continue
        out.append(data)
    return out


def last_activity(account: str, entry: dict) -> float:
    """会话最后一次动静（epoch 秒）：对话记录的修改时间和登记表状态更新时间里较晚的那个。"""
    stamp = float(entry.get("statusUpdatedAt") or 0) / 1000
    path = session_transcript(profile_dir(account), str(entry.get("sessionId") or "")[:8])
    if path is not None:
        with contextlib.suppress(OSError):
            stamp = max(stamp, path.stat().st_mtime)
    return stamp


_TURN_CACHE: dict = {}


def turn_state(account: str, full: str) -> str:
    """看对话记录末尾最后一条有意义的记录：running（这一轮还在进行）、ended（这一轮结束了）、walled（撞墙停下）。

    - system/turn_duration：这一轮结束了（后台任务可能还在跑，登记表会一直是 busy / shell，不管它）；
    - assistant 且是额度错误：撞墙停下；
    - 其它 user / assistant：这一轮还在进行。
    排队、附件、away_summary、标题之类的记录跳过。按文件修改时间缓存：没变就沿用上次的答案。
    """
    path = session_transcript(profile_dir(account), full[:8])
    if path is None:
        return "running"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return "running"
    cached = _TURN_CACHE.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    state = _read_turn(path)
    _TURN_CACHE[str(path)] = (stamp, state)
    return state


def _read_turn(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - TURN_TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return "running"
    ended = False
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "turn_duration":
            ended = True              # 继续往前找：撞墙之后也可能跟一条 turn_duration
        elif kind == "assistant":
            return "walled" if hits_limit(record) else ("ended" if ended else "running")
        elif kind == "user":
            return "ended" if ended else "running"
    return "ended" if ended else "running"


def _started(entry: dict) -> float:
    return float(entry.get("startedAt") or 0)


def real_live(raw: dict) -> tuple[dict, dict]:
    """按进程真正用的号整理各号登记表：返回 ({号: [条目]}, {会话: [(号, 条目), ...]})，后者是重复会话（在 2 个以上的号上各有进程）。

    同一个 pid 有时在几个号的 sessions/ 下都留着登记，属于残留：按 /proc/<pid>/environ 里的 CLAUDE_CONFIG_DIR
    认它真正在哪个号，只算一次。读不到环境、或环境指向不在订阅号里的目录（API 号、隔离目录）时，沿用登记的号，
    照旧观察；但读不到环境又在几个号下都有登记的 pid，认不出真实号，那条会话这一轮不做重复处理（不 stop、不 rm）。
    pid 不同、真实号也不同才是重复；同一个号里同一条会话有几个进程，只留最近启动的那个。条目里的 "account" 写成真实的号。
    """
    homes = {str(profile_dir(account)).rstrip("/"): account for account in raw}
    seen: dict = {}                    # (pid 或号, 会话) → (真实号, 登记的号, 条目)
    unsure: set = set()                # 认不出真实号的会话
    for account, entries in raw.items():
        for entry in entries:
            pid = int(entry.get("pid") or 0)
            full = str(entry["sessionId"])
            home = process_config_dir(pid).rstrip("/")
            real = homes.get(home)
            key = (pid if pid > 0 else account, full)
            if real is None:
                real = account
                if pid > 0 and key in seen:
                    unsure.add(full)    # 同一个 pid 在别的号下也有登记，又没法从环境确认
                    continue
            if key not in seen or (account == real and seen[key][1] != real):
                seen[key] = (real, account, entry)
    per_session: dict = {}             # 会话 → {真实号: 最近启动的条目}
    for real, _registered, entry in seen.values():
        mine = per_session.setdefault(str(entry["sessionId"]), {})
        if real not in mine or _started(entry) > _started(mine[real]):
            mine[real] = {**entry, "account": real}
    live: dict = {account: [] for account in raw}
    dups: dict = {}
    for full, mine in per_session.items():
        for account, entry in mine.items():
            live[account].append(entry)
        if len(mine) > 1 and full not in unsure:
            dups[full] = sorted(mine.items())
    return live, dups


def pick_keeper(group: list, rows: list, limits: dict, now: float) -> str:
    """重复会话留哪一份（返回号）：所在号没到提醒点的优先；同一档里正在干活的优先（别打断一轮）；再留最近启动的。"""
    from cca.session import quota_hint  # noqa: PLC0415

    def rank(pair: tuple) -> tuple:
        account, entry = pair
        hot = bool(quota_hint.at_limit(_windows_of(rows, account), limits, now))
        busy = str(entry.get("status") or "") in ("busy", "working")
        return (hot, not busy, -_started(entry), account)

    return min(group, key=rank)[0]


def duplicate_decision(full: str, group: list, rows: list, limits: dict, now: float) -> Decision:
    keep = pick_keeper(group, rows, limits, now)
    entries = dict(group)
    drops = tuple({"account": account, "job": str(entry.get("jobId") or full[:8]), "pid": int(entry.get("pid") or 0)}
                  for account, entry in group if account != keep)
    name = str(entries[keep].get("name") or "")
    where = "、".join(account for account, _entry in group)
    stopped = "、".join(drop["account"] for drop in drops)
    return Decision(DUPLICATE, full, keep, name, f"同时在 {where} 上跑；保留 {keep}，停掉 {stopped}",
                    cwd=str(entries[keep].get("cwd") or ""), drops=drops, notable=True)


def _windows_of(rows: list, account: str) -> dict:
    mine = next((row for row in rows if row.get("profile") == account), None)
    return ((mine or {}).get("usage") or {}).get("windows") or {}


def source_recovered(windows: dict, limits: dict, now: float) -> bool:
    """撞墙之后源号额度是不是已经恢复：有数据，且没重置的窗口都低于 阈值 - 15。没数据当没恢复。"""
    from cca.session import quota_hint  # noqa: PLC0415

    if not windows:
        return False
    live = quota_hint.live_windows(windows, now)
    return all(int(w.get("percent") or 0) < limits[key] - RECOVER_MARGIN for key, w in live.items())


# ---------------------------------------------------------------- 决策

@dataclass
class Obs:
    """一条会话此刻的事实，decide() 只看这些。"""

    session: str
    account: str
    name: str
    turn: str              # running / ended / walled
    idle: bool             # 登记表 status == idle
    last_active: float
    windows: dict
    limits: dict
    now: float
    move: dict | None = None
    stay: dict | None = None
    cwd: str = ""


@dataclass
class Decision:
    action: str
    session: str
    account: str
    name: str = ""
    reason: str = ""
    target: str = ""
    target_why: str = ""
    idle: bool = False         # 本次观察到登记表 idle
    settled: bool = False      # 本次观察到对话记录显示这一轮结束
    notable: bool = False      # SKIP 里值得在 status 里列出来的（例外、无处可去）
    resets_at: float = 0.0     # 排叫醒用：撞墙的窗口重置时刻
    cwd: str = ""
    explicit: bool = False     # 会话自己要求换号（--current）
    drops: tuple = ()          # DUPLICATE：要停掉的那几份，每份 {"account", "job", "pid"}


def _hot_text(hot: list) -> str:
    return "、".join(f"{SHORT_LABELS[key]} {int(w.get('percent') or 0)}%" for key, w in hot)


def stay_covers(stay: dict | None, hot: list) -> float:
    """这条 stay 是否覆盖所有到阈值的窗口（同一个重置时刻）。覆盖返回这一段的结束时刻，否则 0。"""
    if not stay:
        return 0.0
    recorded = stay.get("windows") or {}
    ends = []
    for key, window in hot:
        resets = float(window.get("resets_at") or 0)
        old = recorded.get(key)
        if not resets or old is None or abs(float(old) - resets) > STAY_SAME_SECONDS:
            return 0.0
        ends.append(resets)
    return max(ends) if ends else 0.0


def _clock(moment: float) -> str:
    from cca.report import format_clock  # noqa: PLC0415

    return format_clock(moment)


def decide(obs: Obs) -> Decision:  # noqa: PLR0911
    """搬还是不搬（纯函数）。顺序：撞墙 → 会话自己要求（不看阈值） → 没到提醒点 → 快重置 → stay → 闲置 → 这一轮结束时搬。"""
    from cca.session import quota_hint  # noqa: PLC0415

    def make(action: str, reason: str, **extra) -> Decision:
        return Decision(action, obs.session, obs.account, obs.name, reason, cwd=obs.cwd,
                        idle=obs.idle, settled=obs.turn == "ended", **extra)

    hot = quota_hint.at_limit(obs.windows, obs.limits, obs.now)
    if obs.turn == "walled":
        if source_recovered(obs.windows, obs.limits, obs.now):
            return make(WAKE_NOW, f"撞墙停下，但 {obs.account} 的额度已经恢复：交给 wake 叫醒", notable=True)
        full = [(k, w) for k, w in quota_hint.live_windows(obs.windows, obs.now).items()
                if int(w.get("percent") or 0) >= FULL] or hot
        return make(MOVE_NOW, "撞墙", resets_at=max((float(w.get("resets_at") or 0) for _k, w in full), default=0.0),
                    explicit=bool(obs.move))
    if obs.move:
        # `cca handoff <目标> --current` 是明确的要求：不看阈值，也不受快重置 / stay / 闲置影响
        return make(ARMED, f"{_hot_text(hot)}；会话自己要求换号" if hot else "会话自己要求换号", explicit=True)
    if not hot:
        return make(SKIP, "没到提醒点")
    why = _hot_text(hot)
    resets = [float(w.get("resets_at") or 0) for _k, w in hot]
    if all(0 < r - obs.now < RESET_SOON_SECONDS for r in resets):
        minutes = int(max(r - obs.now for r in resets) // 60) + 1
        return make(SKIP, f"卡住的窗口 {minutes} 分钟后重置，等它重置", notable=True)
    stay_until = stay_covers(obs.stay, hot)
    if stay_until:
        return make(SKIP, f"--stay 至 {_clock(stay_until)}", notable=True)
    if obs.now - obs.last_active > IDLE_SECONDS:
        return make(SKIP, f"闲置 {int((obs.now - obs.last_active) // 60)} 分钟", notable=True)
    return make(ARMED, why)


def _target_why(row: dict, limits: dict, now: float) -> str:
    from cca.session import quota_hint  # noqa: PLC0415

    return ("余量充足、周额度较早重置" if quota_hint.roomy(row, limits, now)
            else "各号余量都不多，这个周额度最早重置")


def target_candidates(rows: list, decision: Decision, move: dict | None, limits: dict, now: float) -> list:
    """这条会话的目标号候选，先试哪个在前：会话指定的（--current）、pick_target 的挑法、其余按 ranked_targets。"""
    from cca.session import quota_hint  # noqa: PLC0415

    ranked = quota_hint.ranked_targets(rows, decision.account, limits=limits, shares=shares_sessions, now=now)
    names = [row["profile"] for row in ranked]
    first = []
    if move and move.get("target") in names:
        first = [move["target"]]
    else:
        picked = quota_hint.pick_target(rows, decision.account, limits=limits, shares=shares_sessions,
                                        now=now, session=decision.session)
        if picked:
            first = [picked["profile"]]
    return first + [name for name in names if name not in first]


def _with_target(decision: Decision, rows: list, move: dict | None, limits: dict, now: float) -> Decision:
    names = target_candidates(rows, decision, move, limits, now)
    if names:
        row = next(r for r in rows if r.get("profile") == names[0])
        return replace(decision, target=names[0], target_why=_target_why(row, limits, now))
    if decision.action == MOVE_NOW:
        return replace(decision, action=WAIT_WAKE, notable=True,
                       reason=f"{decision.reason}；全满，没有可去的号：排叫醒，等重置")
    return replace(decision, action=SKIP, notable=True, reason=f"{decision.reason}；全满，没有可去的号")


def _quota_rows(profiles: list, live: dict, *, fresh: bool) -> list:
    """每号一行。fresh：有活会话的号走提醒共用的现取规则（近阈值 2 分钟、远 5 分钟，按号共享）；否则只用缓存。"""
    from cca.session import quota_hint  # noqa: PLC0415

    rows = None
    if fresh:
        for account in profiles:
            if not live.get(account):
                continue
            try:
                got = quota_hint.account_rows(account)
            except Exception as exc:  # noqa: BLE001
                log(f"{account} 的额度读不到：{exc}")
                continue
            if rows is None:
                rows = got
            else:
                mine = next((row for row in got if row.get("profile") == account), None)
                rows = [mine if mine and row.get("profile") == account else row for row in rows]
    return rows if rows is not None else quota_hint.cached_rows()


def snapshot(now: float | None = None, *, fresh: bool = False) -> tuple:
    """算一遍：(每条活着的后台会话的 Decision, 各号额度行)。不改任何东西。"""
    from cca.session import quota_hint  # noqa: PLC0415

    now = time.time() if now is None else now
    profiles = subscription_profiles()
    live, dups = real_live({account: live_bg_sessions(account) for account in profiles})
    rows = _quota_rows(profiles, live, fresh=fresh)
    limits = quota_hint.thresholds()
    intents = read_intents()
    plans = []
    # 重复会话：这一轮只做一个决定（留哪份、停哪份），其余份不再当普通会话观察
    for full, group in sorted(dups.items()):
        decision = duplicate_decision(full, group, rows, limits, now)
        plans.append(decision)
        for account, _entry in group:
            live[account] = [entry for entry in live[account] if str(entry["sessionId"]) != full]
    for account in profiles:
        windows = _windows_of(rows, account)
        for entry in live[account]:
            full = str(entry["sessionId"])
            move = intents["moves"].get(full)
            if move and move.get("account") != account:
                move = None
            obs = Obs(session=full, account=account, name=str(entry.get("name") or ""),
                      turn=turn_state(account, full), idle=entry.get("status") == "idle",
                      last_active=last_activity(account, entry), windows=windows, limits=limits, now=now,
                      move=move, stay=intents["stays"].get(full), cwd=str(entry.get("cwd") or ""))
            decision = decide(obs)
            if decision.action in (MOVE_NOW, ARMED):
                decision = _with_target(decision, rows, move, limits, now)
            plans.append(decision)
    return plans, rows


def plan_round(now: float | None = None, *, fresh: bool = False) -> list[Decision]:
    """这一轮要干什么（不改任何东西）。fresh=False 时只用缓存的额度，不触发 `claude -p /usage` 现取。"""
    return snapshot(now, fresh=fresh)[0]


# ---------------------------------------------------------------- 动作

def recheck(decision: Decision, entry: dict, rows: list, limits: dict, now: float) -> str:
    """搬之前按此刻的事实重判一遍（整轮的结论可能是 30 秒前的）。返回放弃的原因，空串表示可以搬。

    要求：这一轮确实结束了（对话记录末尾是 turn_duration，或登记表 idle）；意图（--current / --stay）读最新的；
    重跑 decide()，快重置、stay、闲置、没到提醒点这些例外都再看一遍。
    """
    source, full = decision.account, decision.session
    turn = turn_state(source, full)
    if turn not in {"walled", "ended"} and entry.get("status") != "idle":
        return "新的一轮已经开始了"
    intents = read_intents()
    move = intents["moves"].get(full)
    if move and move.get("account") != source:
        move = None
    fresh = decide(Obs(session=full, account=source, name=decision.name, turn=turn,
                       idle=entry.get("status") == "idle", last_active=last_activity(source, entry),
                       windows=_windows_of(rows, source), limits=limits, now=now,
                       move=move, stay=intents["stays"].get(full), cwd=decision.cwd))
    if fresh.action not in (ARMED, MOVE_NOW):
        return fresh.reason
    return ""


def execute(decision: Decision, *, now: float | None = None, outcome: dict | None = None) -> bool:
    """真搬一条会话。返回有没有搬成。搬之前再确认：会话还在源号上、情况没变（recheck）、目标号过预检。

    outcome 给了的话，前置条件不满足而放弃（不是尝试了又失败）时写 outcome["abandoned"] = True：
    放弃不算失败，不计入失败次数。
    """
    from cca.account import lifecycle  # noqa: PLC0415
    from cca.session import quota_hint, wake  # noqa: PLC0415

    now = time.time() if now is None else now
    full, source = decision.session, decision.account
    short = f"{decision.name or full[:8]} ({full[:8]})"
    outcome = {} if outcome is None else outcome
    entries = {str(entry["sessionId"]): entry for entry in live_bg_sessions(source)}
    if full not in entries:
        log(f"不搬：{short} 已经不在 {source} 上跑了")
        outcome["abandoned"] = True
        return False
    limits = quota_hint.thresholds()
    rows = quota_hint.account_rows(source)
    if decision.action == MOVE_NOW:
        if source_recovered(_windows_of(rows, source), limits, now):
            log(f"不搬：{short} 撞墙后 {source} 的额度已经恢复，交给 wake 叫醒")
            wake.arm(source, full, resets_at=now - wake.RESET_MARGIN_SECONDS, name=decision.name, cwd=decision.cwd)
            outcome["abandoned"] = True
            return False
    else:
        problem = recheck(decision, entries[full], rows, limits, now)
        if problem:
            log(f"不搬：{short} {problem}")
            outcome["abandoned"] = True
            return False
    move = read_intents()["moves"].get(full)
    title = session_title(source, full)
    for target in target_candidates(rows, decision, move, limits, now):
        problem = lifecycle.target_problem(target)
        if problem:
            log(f"{short}：{target} 不行（{problem}），换下一个")
            continue
        code = lifecycle.move_session(source, target, full, prechecked=True)
        if code != 0:
            if code == lifecycle.MOVE_ALREADY_THERE:
                # 目标号上已经有一份：重复会话，不是搬失败；不计失败次数，下一轮的重复处理会停掉多余的那份
                log(f"没搬：{short} 在 {target} 上已经有一份在跑，当重复会话，交给下一轮处理（不计失败）")
                outcome["abandoned"] = True
            else:
                log(f"没搬成：{short} {source} → {target}（冷却 {COOLDOWN_SECONDS // 60} 分钟）")
            return False
        log(f"已搬：{source} → {target} {short}（{decision.reason}）")
        notify_move(source, target, title)
        _forget_intents(full)
        wake.forget(full)
        return True
    log(f"没搬成：{short} 没有能过预检的目标号（冷却 {COOLDOWN_SECONDS // 60} 分钟）")
    return False


def notify_duplicate(title: str, text: str) -> None:
    """重复会话处理完写一条通知，面板会把它变成横幅。"""
    _append_jsonl(sideagent_dir() / "notices.jsonl", {"at": _now_iso(), "text": f"{title}：{text}"})


def _stop_copy(account: str, drop: dict, full: str) -> tuple[bool, str]:
    """停掉某个号上的那份重复进程并清掉面板条目；返回 (进程是不是确认没了, 给日志的一句话)。"""
    from cca.account import lifecycle  # noqa: PLC0415
    from cca.session.push import _run_claude, live_session_ids  # noqa: PLC0415

    job, pid = drop["job"], drop["pid"]
    home = str(profile_dir(account)).rstrip("/")
    # 动手前再确认：那个 pid 还活着的话，它得还在这个号上；已经没了就只清面板
    if pid > 0 and _alive(pid) and process_config_dir(pid).rstrip("/") not in ("", home):
        return False, f"没停 {account} 上的 {job}：pid {pid} 实际不在这个号上了"
    if full in live_session_ids(account):
        r = _run_claude(["stop", job], account, capture=True, timeout=STOP_TIMEOUT_SECONDS)
        if r.returncode != 0:
            return False, f"没停掉 {account} 上的 {job}：{(r.stderr or r.stdout or '').strip()[:120]}"
        if not lifecycle._wait_stopped(account, full):  # noqa: SLF001
            return False, f"停了 {lifecycle.HANDOFF_STOP_WAIT_SECONDS} 秒 {account} 上的 {job} 进程还在，没清面板"
    return True, lifecycle.drop_entry(account, job, full, why="重复会话")


_DUP_TRIED: dict = {}      # (会话, 号) → 上次没处理掉的时刻；限频用，只在常驻进程内存里


def resolve_duplicate(decision: Decision, *, now: float | None = None) -> bool:
    """重复会话：停掉不保留的那几份并清掉它们在面板里的条目；写日志、发通知。不算搬迁失败，也清掉这条会话的失败记录。

    某份没停掉时，隔 DUP_RETRY_SECONDS 再试，期间不重复写日志和通知；通知只在有一份真的停了、或首次没停掉时发。
    返回这一轮是不是动手处理了。
    """
    now = time.time() if now is None else now
    full = decision.session
    short = f"{decision.name or full[:8]} ({full[:8]})"
    todo = [drop for drop in decision.drops if now - _DUP_TRIED.get((full, drop["account"]), -DUP_RETRY_SECONDS) >= DUP_RETRY_SECONDS]
    if not todo:
        return False
    log(f"重复会话：{short} {decision.reason}")
    stopped, failed, first_failure = [], [], False
    for drop in todo:
        account = drop["account"]
        try:
            ok, result = _stop_copy(account, drop, full)
        except Exception as exc:  # noqa: BLE001
            ok, result = False, f"处理 {account} 上的 {drop['job']} 出错：{type(exc).__name__}: {exc}"
        log(result)
        if ok:
            _DUP_TRIED.pop((full, account), None)
            stopped.append(account)
        else:
            first_failure = first_failure or (full, account) not in _DUP_TRIED
            _DUP_TRIED[(full, account)] = now
            failed.append(account)
    _forget_fails(full)               # 以前因为“已经在 X 上跑着了”记的失败和冷却作废；move/stay 意图不动
    if stopped or first_failure:
        text = decision.reason
        if failed:
            text += f"（{'、'.join(failed)} 上的没停掉，稍后重试）"
        notify_duplicate(session_title(decision.account, full), text)
    return True


def _forget_fails(full: str) -> None:
    """只清这条会话的失败记录和冷却；move 意图（会话自己 --current / handoff 要求的）和 stay 不碰。"""
    with _state_lock():
        data = read_intents()
        if data["fails"].pop(full, None) is not None:
            _write_intents(data)


def _forget_intents(full: str) -> None:
    with _state_lock():
        data = read_intents()
        dropped = [data[kind].pop(full, None) for kind in ("moves", "stays", "fails")]
        if any(item is not None for item in dropped):
            _write_intents(data)


def record_failure(account: str, full: str, now: float) -> float:
    """搬失败一次：累计次数写进 watchdog.json，冷却翻倍；到 MAX_FAILS 次不再自动搬（冷却到意图作废为止）。返回冷却到什么时候。"""
    with _state_lock():
        data = read_intents()
        count = int((data["fails"].get(full) or {}).get("n") or 0) + 1
        if count >= MAX_FAILS:
            until = now + MOVE_TTL_SECONDS
            log(f"{full[:8]} 累计搬失败 {count} 次，不再自动搬（要搬请人手动 cca handoff）")
        else:
            until = now + min(COOLDOWN_SECONDS * 2 ** (count - 1), COOLDOWN_MAX_SECONDS)
        data["fails"][full] = {"account": account, "n": count, "until": until}
        _write_intents(data)
    return until


def _arm_wake(decision: Decision, now: float) -> None:
    """撞墙又没号可去：排一条有条件的叫醒（重置后它确实是撞墙停下的才叫）。"""
    from cca.session import wake  # noqa: PLC0415

    if decision.action == WAKE_NOW:
        resets = now - wake.RESET_MARGIN_SECONDS         # 额度已经恢复：这一轮就叫
    else:
        resets = decision.resets_at or now + 300
    if wake.arm(decision.account, decision.session, resets_at=resets, name=decision.name, cwd=decision.cwd):
        log(f"排叫醒：{decision.name or decision.session[:8]} ({decision.session[:8]}) {decision.reason}")


# ---------------------------------------------------------------- 常驻进程

class Loop:
    """常驻进程的一轮一轮：状态（连续观察次数、冷却）只放内存，崩溃重启后从磁盘事实重新算，只是「连续两次」重新累计。"""

    def __init__(self) -> None:
        self.armed: dict = {}         # 会话 → {"decision", "idle", "settled"}
        self.cooldown: dict = {}      # 会话 → 冷却到什么时候（失败次数和冷却同时写在 watchdog.json 的 fails 里，重启不丢）
        self.wake_cooldown: dict = {}  # 会话 → 下次允许「额度已恢复，直接叫」的时刻
        self.absent: dict = {}        # 会话 → 意图里有它、但连续几轮没在登记表里看到
        self.started = time.time()
        self.quiet = 0
        self.stopping = False

    # -- 一整轮
    def round(self, now: float | None = None) -> bool:
        """读额度、决策、该搬的搬、叫醒到点的。返回这一轮是不是还有事（有活着的会话 / 待叫醒 / 意图）。"""
        from cca.session import wake  # noqa: PLC0415

        now = time.time() if now is None else now
        plans = plan_round(now, fresh=True)
        gc_intents({(d.account, d.session) for d in plans}, now, self.absent)
        for full, fail in read_intents()["fails"].items():
            self.cooldown[full] = max(self.cooldown.get(full, 0), float(fail.get("until") or 0))
        budget = MAX_MOVES_PER_ROUND
        seen = set()
        for decision in plans:
            if decision.action == DUPLICATE:
                # 不看冷却：它就是要解掉“已经在 X 上跑着了”那类卡住的会话
                self.armed.pop(decision.session, None)
                self.cooldown.pop(decision.session, None)
                resolve_duplicate(decision)
                continue
            if self.cooldown.get(decision.session, 0) > now:
                continue
            if decision.action == ARMED:
                seen.add(decision.session)
                entry = self.armed.setdefault(decision.session, {"idle": 0, "settled": 0})
                entry["decision"] = decision
                if self._seen_end(entry, decision) and budget > 0:
                    budget -= int(self._run(decision, now))
            elif decision.action == MOVE_NOW and budget > 0:
                budget -= int(self._run(decision, now))
            elif decision.action == WAKE_NOW:
                if self.wake_cooldown.get(decision.session, 0) <= now:
                    self.wake_cooldown[decision.session] = now + WAKE_COOLDOWN_SECONDS
                    _arm_wake(decision, now)
            elif decision.action == WAIT_WAKE:
                _arm_wake(decision, now)
        for session in set(self.armed) - seen:
            del self.armed[session]
        wake.run_due()
        return bool(plans or wake.pending() or read_intents()["moves"] or read_intents()["stays"])

    @staticmethod
    def _seen_end(entry: dict, decision: Decision) -> bool:
        """累计「这一轮结束」的观察次数，连续 SETTLED_COUNT 次才算。"""
        entry["idle"] = entry["idle"] + 1 if decision.idle else 0
        entry["settled"] = entry["settled"] + 1 if decision.settled else 0
        return max(entry["idle"], entry["settled"]) >= SETTLED_COUNT

    def _run(self, decision: Decision, now: float) -> bool:
        self.armed.pop(decision.session, None)
        moved, outcome = False, {}
        try:
            moved = execute(decision, now=now, outcome=outcome)
        except Exception as exc:  # noqa: BLE001
            log(f"搬 {decision.session[:8]} 出错：{type(exc).__name__}: {exc}")
        if moved:
            self.cooldown.pop(decision.session, None)
        elif outcome.get("abandoned"):
            self.cooldown[decision.session] = now + ARMED_POLL * SETTLED_COUNT   # 条件变了：下一整轮再判断
        else:
            self.cooldown[decision.session] = record_failure(decision.account, decision.session, now)
        return True

    # -- 两秒一次，只复查「这一轮结束就搬」的那几条
    def poll_armed(self) -> None:
        from cca.session.takeover import registry_entry  # noqa: PLC0415

        now = time.time()
        for session, entry in list(self.armed.items()):
            decision: Decision = entry["decision"]
            registry = registry_entry(decision.account, session)
            if not registry:
                self.armed.pop(session, None)        # 没了（被停掉或搬走）：下一整轮再判断
                continue
            turn = turn_state(decision.account, session)
            seen = replace(decision, idle=registry.get("status") == "idle", settled=turn == "ended")
            if turn == "walled":
                self._run(replace(seen, action=MOVE_NOW, reason="撞墙"), now)
            elif self._seen_end(entry, seen):
                self._run(decision, now)

    # -- 退出
    def should_exit(self, *, busy: bool, now: float | None = None) -> bool:
        """没事可做连续 QUIET_ROUNDS 轮、且启动满 START_GRACE_SECONDS。"""
        now = time.time() if now is None else now
        self.quiet = 0 if busy else self.quiet + 1
        return self.quiet >= QUIET_ROUNDS and now - self.started >= START_GRACE_SECONDS

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stopping and time.time() < end:
            time.sleep(min(1.0, max(0.0, end - time.time())))


def _lock_path() -> Path:
    return accounts_root() / "watchdog.lock"


def _try_lock(*, shared: bool = False, wait: float = 0.0):
    """抢全机唯一的锁；抢到返回打开的文件（持有到进程退出，崩溃时内核自动放），抢不到返回 None。

    常驻进程拿排它锁；「探测在不在」的人（ensure_running / status / stop）只拿共享锁：
    探测方之间互不挡，也就不会因为两个探测碰在一起让新起的常驻进程抢不到锁。
    排它锁仍会被探测方的共享锁短暂挡住，所以 wait > 0 时重试一小会儿。
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(handle, mode)
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return None
            time.sleep(0.1)
        else:
            return handle


def _release(handle) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(handle, fcntl.LOCK_UN)
    handle.close()


def _pid_path() -> Path:
    return accounts_root() / "watchdog.pid"


def read_pid() -> int:
    try:
        return int(_pid_path().read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


def running() -> bool:
    """常驻进程在不在：排它锁被它拿着就是在（探测只用共享锁）。"""
    handle = _try_lock(shared=True)
    if handle is None:
        return True
    _release(handle)
    return False


def has_work() -> bool:
    """还有没有理由留着：订阅号上有活着的后台会话、有待叫醒的条目、有意图。"""
    from cca.session import wake  # noqa: PLC0415

    intents = read_intents()
    return bool(any(live_bg_sessions(account) for account in subscription_profiles())
                or wake.pending() or intents["moves"] or intents["stays"])


def daemon() -> int:
    """常驻进程的入口。拿不到锁说明已经有一个了，静默退出。SIGTERM 干净退出。"""
    handle = _try_lock(wait=LOCK_WAIT_SECONDS)
    if handle is None:
        return 0
    loop = Loop()
    _pid_path().write_text(str(os.getpid()) + "\n", encoding="utf-8")
    signal.signal(signal.SIGTERM, lambda *_: setattr(loop, "stopping", True))
    log(f"看门狗启动 pid {os.getpid()}（每 {ROUND_SECONDS} 秒一轮）")
    for old in ("watchdog.sessions", "watchdog.order"):
        if (accounts_root() / old).exists():
            log(f"发现旧文件 {old}：v2 不再读它，可以删掉")
    try:
        while not loop.stopping:
            try:
                busy = loop.round()
            except Exception as exc:  # noqa: BLE001
                log(f"这一轮出错了：{type(exc).__name__}: {exc}")
                busy = True
            if loop.should_exit(busy=busy):
                handle = _confirm_idle(loop, handle)
                if handle is None:
                    break
            deadline = time.time() + ROUND_SECONDS
            while not loop.stopping and time.time() < deadline:
                loop.sleep(ARMED_POLL if loop.armed else 1.0)
                if loop.armed:
                    loop.poll_armed()
    finally:
        log("看门狗退出")
        with contextlib.suppress(OSError):
            _pid_path().unlink()
        if handle is not None:
            _release(handle)
    return 0


def _confirm_idle(loop: Loop, handle):
    """退出前的最后一次检查：放锁、再看一眼。写意图的一方总是先写再 ensure_running，
    如果它赶在放锁前探测，会以为还有人在跑而不拉起；所以放锁后有活就重新抢锁接着干。
    返回还持着的锁（接着干），或 None（可以退了：真没事，或别人已经另起了一个）。
    """
    _release(handle)
    if not has_work():
        return None
    loop.quiet = 0
    return _try_lock(wait=LOCK_WAIT_SECONDS)


def _autostart_allowed() -> bool:
    """CCA_WATCHDOG_AUTOSTART=0 关掉自动拉起；单元测试里（加载了 unittest）默认不拉起，
    免得测试走到真的 Popen、在临时 HOME 下留一堆常驻进程。"""
    flag = os.environ.get("CCA_WATCHDOG_AUTOSTART", "")
    if flag:
        return flag != "0"
    return "unittest" not in sys.modules


def ensure_running() -> bool:
    """保证有一个常驻进程；已经在跑就什么都不做。返回这次是不是新起的。

    探测是对锁文件做一次非阻塞共享 flock（微秒级）；拿到说明没人持排它锁，立刻放掉再 fork。
    并发起多个没关系（多出来的拿不到锁就退出）；10 秒的 spawn 戳避免同时到来的多次调用反复 fork Python。
    """
    if not _autostart_allowed():
        return False
    handle = _try_lock(shared=True)
    if handle is None:
        return False
    _release(handle)
    root = accounts_root()
    stamp = root / "watchdog.spawn"
    try:
        if time.time() - stamp.stat().st_mtime < SPAWN_STAMP_SECONDS:
            return False
    except OSError:
        pass
    stamp.touch()
    src = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with (root / "watchdog.log").open("a", encoding="utf-8") as out:
        subprocess.Popen(
            [sys.executable, "-u", "-c", "from cca.session.watchdog import daemon; daemon()"],
            env=env, cwd=str(Path.home()), stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    return True


def stop() -> bool:
    """停掉常驻进程（发 SIGTERM）。返回有没有发出去。"""
    pid = read_pid()
    if pid <= 0 or not _alive(pid) or not running():
        return False
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    return True


# ---------------------------------------------------------------- 展示

def _hm(moment: float) -> str:
    return time.strftime("%H:%M", time.localtime(moment))


def _account_line(account: str, rows: list, limits: dict, now: float) -> str:
    from cca.session import quota_hint  # noqa: PLC0415

    windows = _windows_of(rows, account)
    if not windows:
        return f"  {account}  没有额度数据"
    cells = []
    for key in quota_hint.WINDOWS:
        window = quota_hint.live_window(windows.get(key), now)
        cells.append(f"{SHORT_LABELS[key]} {int(window.get('percent') or 0) if window else 0}%")
    hot = quota_hint.at_limit(windows, limits, now)
    if hot:
        key, window = max(hot, key=lambda pair: int(pair[1].get("percent") or 0))
        resets = float(window.get("resets_at") or 0)
        tail = f"，{_hm(resets)} 重置，还有 {int(max(0, resets - now) // 60)} 分钟" if resets else ""
        state = f"→ 到提醒点（{SHORT_LABELS[key]}{tail}）"
    else:
        row = next((r for r in rows if r.get("profile") == account), {})
        state = "余量充足" if quota_hint.roomy(row, limits, now) else "有余量，但离提醒点不远"
    return f"  {account}  {'  '.join(cells)}  {state}"


def _who(decision: Decision) -> str:
    return f"{decision.name or '(无标题)'} ({decision.session[:8]})"


def render_status(plans: list, rows: list, *, dry_run: bool = False, process: str = "") -> str:
    """status / dry-run 的输出：额度 → 下一步 → 不搬 → 待叫醒。"""
    from cca.session import quota_hint, wake  # noqa: PLC0415

    now = time.time()
    limits = quota_hint.thresholds()
    lines = [process] if process else []
    lines.append(f"额度（阈值 5h {limits['five_hour']} / 7d {limits['seven_day']}）")
    lines += [_account_line(account, rows, limits, now) for account in subscription_profiles()] or ["  （没有订阅号）"]
    todo = [d for d in plans if d.action in (DUPLICATE, MOVE_NOW, ARMED, WAIT_WAKE, WAKE_NOW)]
    lines.append("下一步" + ("  [只看不做]" if dry_run else ""))
    for d in todo:
        if d.action == DUPLICATE:
            what = f"重复会话：{d.reason}"
        elif d.action == MOVE_NOW:
            what = f"{d.account} → {d.target}   立即搬；原因：{d.reason}；目标：{d.target_why}"
        elif d.action == ARMED:
            what = f"{d.account} → {d.target}   这一轮结束后搬；原因：{d.reason}；目标：{d.target_why}"
        else:
            what = f"{d.account}   {d.reason}"
        lines.append(f"  {_who(d)}  {what}")
    if not todo:
        lines.append("  （没有）")
    skipped = [d for d in plans if d.action == SKIP]
    lines.append("不搬")
    notable = [d for d in skipped if d.notable]
    lines += [f"  {_who(d)}  {d.account}   {d.reason}" for d in notable]
    quiet = len(skipped) - len(notable)
    if quiet:
        lines.append(f"  另有 {quiet} 条会话所在的号没到提醒点")
    if not skipped:
        lines.append("  （没有）")
    lines.append("待叫醒")
    waiting = wake.pending()
    for session, entry in waiting:
        cond = "（撞墙才叫）" if entry.get("only_if_limited") else ""
        lines.append(f"  {entry.get('account')}  {session[:8]}  {entry.get('name') or '(无标题)'}  "
                     f"{entry.get('wake_time') or ''}  {cond}".rstrip())
    if not waiting:
        lines.append("  （没有）")
    return "\n".join(lines)


def _process_line() -> str:
    if not running():
        return "watchdog: 没在跑"
    pid = read_pid()
    started = ""
    with contextlib.suppress(OSError):
        started = f"，启动于 {_hm(_pid_path().stat().st_mtime)}"
    return f"watchdog: 在跑 pid {pid}{started}（日志 {accounts_root() / 'watchdog.log'}）"


USAGE = "cca watchdog [status] | cca watchdog --dry-run | cca watchdog stop"


def cmd_watchdog(args: list) -> int:
    """`cca watchdog` 的入口：status（默认）、--dry-run、stop。不会手动起常驻进程——它由别的入口自动拉起。"""
    from cca.cli import parse_flags  # noqa: PLC0415

    rest, flags = parse_flags(args, bool_flags=("--dry-run",), usage=USAGE)
    if rest and rest[0] == "stop":
        say("看门狗已停" if stop() else "看门狗本来就没在跑")
        return 0
    if rest and (rest[0] != "status" or len(rest) > 1):
        die(f"用法: {USAGE}")
    dry_run = bool(flags.get("--dry-run"))
    plans, rows = snapshot(fresh=False)
    say(render_status(plans, rows, dry_run=dry_run, process="" if dry_run else _process_line()))
    return 0
