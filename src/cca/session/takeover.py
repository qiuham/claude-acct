"""接管 Claude Code 原生后台会话（`claude --bg`），挂到主会话的 sideagent 面板上。

和 `cca sideagent` 的常驻进程不同：这里不包着 claude 跑，后台会话由 Claude Code 自己管，
我们只做三件事：

1. 接管（takeover）：找到会话在哪个号；没在跑就在原号用 `claude --bg --resume` 接着跑
   （原来就是后台会话的不带参数，保留原编号和原设置；普通会话加 bypassPermissions）；
   在 ~/.cca/sideagents/<名字>/ 写 state.json（kind=attached），在主会话的名单
   ~/.cca/sessions/<编号>.json 里加一条，面板照常显示。同一个会话可以被好几个主会话接管（M 对 N）：
   文件夹只有一份、同步只有一份，各主会话的名单里各有一条。
2. 同步（sync）：主会话的 MCP 服务后台线程每 2 秒同步一次（见 backends.sync_loop；
   MCP 不在时面板兜底调 `cca sideagent bg-sync <主会话>`），按上次读到的位置
   增量读它的对话记录（~/.claude/projects/<目录>/<会话编号>.jsonl，各号共用），
   翻译成面板认得的事件和进度行；状态从原号的登记表 <配置目录>/sessions/*.json 取。
3. 发话：走原生的跨会话消息（和 sideagent 回报主会话同一个套接字协议）。

总控（主会话）退出时不停这些会话，拿掉用 release（只从我的名单里删，最后一个主人放手才算停）。
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

from cca.account.model import discover_profiles
from cca.config import (
    TAKEN_BY_ASK_CHARS,
    TAKEOVER_CARD_ROUNDS,
    TAKEOVER_CARD_TOTAL_CHARS,
    TAKEOVER_CARD_TURN_CHARS,
)
from cca.errors import say
from cca.paths import profile_dir
from cca.session import inbox, roster, sideagent, summary
from cca.session.push import session_name

PROJECTS_DIR = Path("~/.claude/projects").expanduser()
# 第一次接上时只读对话记录最后这么多字节：详情页看最近的就够了
FIRST_READ_BYTES = 256_000
# 一次同步最多读这么多字节，免得一次读几十 MB
SYNC_READ_BYTES = 2_000_000
WAKE_TIMEOUT = 30
# 唤醒后等消息套接字出现最多多久
WAKE_DELIVER_WAIT = 30   # 唤醒后最多等多久把话投进去
BG_PREFIX = "bg-"

# 完整会话编号的长度（UUID 带连字符）；短了 claude --resume 会复制出新会话
FULL_SESSION_ID_LEN = 36


def record_name(account: str, session_id: str, job_id: str = "") -> str:
    """面板里的名字：<号>#bg-<短编号>，号取 # 前面那段，和 sideagent 实例一样。"""
    return f"{account}#{BG_PREFIX}{(job_id or session_id)[:8]}"


def is_bg(name: str) -> bool:
    """是不是接管的会话（state.json 里 kind=attached）。"""
    state = sideagent._read_state(name)  # noqa: SLF001
    return bool(state) and roster.kind_of(state) == roster.ATTACHED


def _claude() -> str:
    import shutil  # noqa: PLC0415

    return shutil.which("claude") or "claude"


AGENTS_LIST_TIMEOUT = 20   # handoff / takeover 取单号列表的默认超时；cca agents 总表传更短的


def agents_or_none(account: str, timeout: float = AGENTS_LIST_TIMEOUT) -> list | None:
    """这个号的 `claude agents --json --all`；取不到（起不来、超时、不是 JSON 列表）返回 None。"""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(profile_dir(account))}
    try:
        out = subprocess.run(  # noqa: S603 - 参数固定
            [_claude(), "agents", "--json", "--all"], env=env, capture_output=True,
            text=True, timeout=timeout, check=False)
        data = json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def _agents(account: str) -> list:
    """这个号的 `claude agents --json --all`；出错返回空列表。"""
    return agents_or_none(account) or []


def registry_entry(account: str, session_id: str) -> dict:
    """这个会话现在的登记条目（在跑才有）：status、waitingFor、套接字都在这。

    先看原号；原号没有，就到所有号里找。会话可能换了号重新拉起来（例如机器重启后在别的号上
    --resume），这时只认原号会把它当成已结束。找到的号写在 "account" 键里。
    """
    entry = _registry_entry_in(account, session_id) if account else {}
    if entry:
        return {**entry, "account": account}
    for other in discover_profiles():
        if other == account:
            continue
        entry = _registry_entry_in(other, session_id)
        if entry:
            return {**entry, "account": other}
    return {}


def _registry_entry_in(account: str, session_id: str) -> dict:
    folder = profile_dir(account) / "sessions"
    if not folder.is_dir():
        return {}
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != session_id:
            continue
        pid = int(data.get("pid") or 0)
        if pid > 0:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
        return data
    return {}


def transcript(session_id: str) -> Path | None:
    hits = sorted(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None



# ---------------------------------------------------------------- 列出、找会话


LAST_LINE_BYTES = 200_000


def last_line(session_id: str) -> str:
    """对话记录里它最后说的一句话（挑选框里给人认会话用）；只读文件末尾一段。"""
    path = transcript(session_id) if session_id else None
    if path is None:
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - LAST_LINE_BYTES))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in reversed(tail):
        if '"assistant"' not in raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if item.get("type") != "assistant" or item.get("isSidechain"):
            continue
        for block in reversed((item.get("message") or {}).get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip():
                return " ".join(str(block["text"]).split())[:200]
    return ""


def recap(session_id: str) -> str:
    """Claude Code 自己写的回顾（你离开一阵再回来时显示的 recap，记录里是 away_summary）；只读文件末尾一段。"""
    path = transcript(session_id) if session_id else None
    if path is None:
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - LAST_LINE_BYTES * 5))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in reversed(tail):
        if '"away_summary"' not in raw:
            continue
        with suppress(ValueError):
            item = json.loads(raw)
            if item.get("subtype") == "away_summary" and str(item.get("content") or "").strip():
                return " ".join(str(item["content"]).split())[:300]
    return ""


CARD_CLIP = 300


def _clip(text: str, limit: int = CARD_CLIP) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


# 对话压缩后续接时，Claude Code 以用户身份写进来的摘要开头
COMPACT_HEADER = "This session is being continued from a previous conversation"


def _ask_text(item: dict) -> str:
    """一条 user 记录里人交给它的话；工具结果、斜杠命令回显、元信息都不算。"""
    if item.get("type") != "user" or item.get("isMeta") or item.get("isSidechain"):
        return ""
    content = (item.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    else:
        text = " ".join(block.get("text", "") for block in content or []
                        if isinstance(block, dict) and block.get("type") == "text")
    # 斜杠命令回显、系统塞进来的通知（<task-notification>、<cross-session-message> 等都以 < 开头）、
    # 压缩后续接时自动写的摘要，都不是人交给它的话
    if text.lstrip().startswith(("<", COMPACT_HEADER)):
        return ""
    return text.strip()


def _said_text(item: dict) -> str:
    """一条 assistant 记录里它说的字（不含工具调用）。"""
    if item.get("type") != "assistant" or item.get("isSidechain"):
        return ""
    return " ".join(str(block.get("text") or "") for block in (item.get("message") or {}).get("content") or []
                    if isinstance(block, dict) and block.get("type") == "text").strip()


def recent_rounds(session_id: str, rounds: int = TAKEOVER_CARD_ROUNDS) -> list:
    """最近几轮问答的原话：[(问, 答)]，答取这一轮它最后说的那段；只读文件末尾一段。"""
    path = transcript(session_id) if session_id else None
    if path is None or rounds <= 0:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - LAST_LINE_BYTES * 5))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for raw in tail:
        if '"user"' not in raw and '"assistant"' not in raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        ask = _ask_text(item)
        if ask:
            found.append([ask, ""])
            continue
        said = _said_text(item)
        if said and found:
            found[-1][1] = said
    return [tuple(pair) for pair in found[-rounds:]]


def card(session_id: str, *, account: str = "", title: str = "", state: str = "") -> str:
    """接管时交给主会话的“卡片”：这个会话在干什么，几行字，不把整段对话塞进来。

    依次是：Claude Code 自己写的回顾（recap，只有人离开一阵再回来才有）、最近几轮问答的原话、
    对话记录的路径（要细节自己去翻）。轮数、每段字数、总字数在 config 里。
    """
    lines = [f"接管了 {account}/{session_id[:8]}「{title or '无标题'}」" + (f"（{state}）" if state else "")]
    summary = recap(session_id)
    if summary:
        lines.append("回顾：" + _clip(summary))
    pairs = recent_rounds(session_id)
    if pairs:
        lines.append(f"最近 {len(pairs)} 轮：")
        for ask, said in pairs:
            lines.append("  问：" + _clip(ask, TAKEOVER_CARD_TURN_CHARS))
            if said:
                lines.append("  答：" + _clip(said, TAKEOVER_CARD_TURN_CHARS))
    text = "\n".join(lines)
    if len(text) > TAKEOVER_CARD_TOTAL_CHARS:
        text = text[:TAKEOVER_CARD_TOTAL_CHARS] + "…"
    path = transcript(session_id) if session_id else None
    if path is not None:
        text += f"\n对话记录：{path}"
    return text


RUNNING_STATES = ("working", "busy")


def _owner_title(session: str) -> str:
    """别的主会话叫什么：会话标题，没有就是短编号。"""
    return session_name(transcript(session)) or session[:8]


def _taken(owner_session: str) -> tuple[set, dict]:
    """(被我接管的会话编号, {会话编号: [别的主人的标题或短编号]})。"""
    mine = roster.mine(owner_session) if owner_session else {}
    mine_sessions: set = set()
    mine_chain = set(roster.chain_sessions(owner_session)) if owner_session else set()
    others: dict = {}
    for name in sideagent.agent_names(include_finished=False):
        state = sideagent._read_state(name)  # noqa: SLF001
        if roster.kind_of(state) != roster.ATTACHED:
            continue
        session_id = str(state.get("session_id") or "")
        if name in mine:
            mine_sessions.add(session_id)
        for holder in roster.holders(name):
            if holder["session"] in mine_chain:
                continue
            others.setdefault(session_id, []).append(holder["session"])
    # 别的主人写成标题；标题撞了（例如“任务”和复制出来的“任务 (2)”之外真重名的）再带上它最后那句话的开头
    titles = {sid: _owner_title(sid) for sids in others.values() for sid in sids}
    seen: dict = {}
    for title in titles.values():
        seen[title] = seen.get(title, 0) + 1
    named = {sid: title if seen[title] == 1 else f"{title}（{last_ask(sid)[:TAKEN_BY_ASK_CHARS]}…）"
             for sid, title in titles.items()}
    return mine_sessions, {key: [named[sid] for sid in sids] for key, sids in others.items()}


def last_ask(session_id: str) -> str:
    """人最后交给这个会话的那句话（挑选框里给人认会话用）。"""
    rounds = recent_rounds(session_id, 1)
    return " ".join(rounds[-1][0].split()) if rounds else ""


# 同一个会话在几个号下都有登记时挑哪一条：越像“正在用”的越靠前
STATE_RANK = {"busy": 0, "working": 0, "waiting": 1, "idle": 2, "blocked": 2, "done": 3}


def process_config_dir(pid: int) -> str:
    """这个进程环境里的 CLAUDE_CONFIG_DIR（读 /proc/<pid>/environ）；进程没了、读不到、没设都返回空串。"""
    if pid <= 0:
        return ""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return ""
    found = [item.split(b"=", 1)[1].decode(errors="replace") for item in env if item.startswith(b"CLAUDE_CONFIG_DIR=")]
    return found[0] if found else ""


def _process_account(row: dict) -> bool:
    """这条登记的号是不是那个会话进程真正用的号（看进程环境里的 CLAUDE_CONFIG_DIR，读不到就不算）。"""
    pid = int(registry_entry(str(row.get("account") or ""), str(row.get("session_id") or "")).get("pid") or 0)
    home = str(profile_dir(str(row.get("account") or "")))
    return bool(pid) and process_config_dir(pid) == home


def one_per_session(rows: list) -> list:
    """一个会话只留一行：在跑的优先；都在跑时留进程真正用的那个号（别的号的登记表里有时也会冒出它）。"""
    groups: dict = {}
    for row in rows:
        groups.setdefault(row["session_id"], []).append(row)
    out = []
    for group in groups.values():
        if len(group) > 1:
            group.sort(key=lambda one: (STATE_RANK.get(str(one.get("state") or ""), len(STATE_RANK)),
                                        not _process_account(one)))
        out.append(group[0])
    return out


def picker_rows(*, include_running: bool = False, owner_session: str = "") -> list:
    """挑选框用的列表：默认只列没在跑的；一个会话一行，带上标题、人最后交给它的话、被谁接管。"""
    taken_over, taken_by = _taken(owner_session)
    # 派出的 sideagent 自己的会话不能接管（只有一个主人），也不列；它的登记表条目会被抄到主会话那个号里，看着像普通会话
    spawned = _spawned_sessions()
    rows = []
    for row in one_per_session([one for one in candidates()
                                 if one["state"] not in ("stopped", "failed") and one["session_id"] not in spawned]):
        # 停掉的、失败的会话不列（人主动停的、早就收掉的、起不来的）
        if row["session_id"] == owner_session:
            continue                     # 自己不能接管自己
        if not include_running and row["state"] in RUNNING_STATES:
            continue
        path = transcript(row["session_id"]) if row["session_id"] else None
        active_at = 0.0
        with suppress(OSError):
            if path is not None:
                active_at = path.stat().st_mtime   # 最后一次写对话记录：挑选框里显示“多久前”
        rows.append({**row, "title": row["name"] or session_name(path), "last": last_line(row["session_id"]),
                     "ask": last_ask(row["session_id"]),
                     "taken_over": row["session_id"] in taken_over,
                     # M 对 N：被别的会话接管了照样能选，挑选框写“已被 X 接管”
                     "taken_by": taken_by.get(row["session_id"], []), "active_at": active_at})
    # 最近还在用的排在上面
    rows.sort(key=lambda one: one["active_at"], reverse=True)
    return rows


def candidates(accounts: list | None = None) -> list:
    """本机各号能接管的会话：后台会话（含已结束的）和在跑的交互会话。"""
    return candidates_checked(accounts)[0]


def candidates_checked(accounts: list | None = None, timeout: float = AGENTS_LIST_TIMEOUT) -> tuple[list, list]:
    """同 candidates，另外返回取不到的号：(行, [取不到的号])。一个号失败或超时不影响别的号。"""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    from cca.account.model import discover_profiles  # noqa: PLC0415

    names = list(accounts or discover_profiles())
    # 每个号要起一次 `claude agents --json`（约 0.15 秒），十个号挨个跑要 1.5 秒；并行跑
    with ThreadPoolExecutor(max_workers=max(1, min(len(names), 32))) as pool:   # 一次全发出去：慢号最多拖到 timeout，不会排第二轮
        listed = dict(zip(names, pool.map(lambda name: agents_or_none(name, timeout), names), strict=True))
    failed = [account for account in names if listed[account] is None]
    rows = []
    for account in names:
        listed[account] = listed[account] or []
        rows.extend({
                "account": account,
                "id": str(item.get("id") or str(item.get("sessionId") or "")[:8]),
                "session_id": str(item.get("sessionId") or ""),
                "name": str(item.get("name") or ""),
                "kind": str(item.get("kind") or ""),
                "cwd": str(item.get("cwd") or ""),
                "state": str(item.get("state") or item.get("status") or ""),
                "running": bool(item.get("pid")),
                # 这条登记是什么时候起的：同一会话在几个号上都有登记时，越新的越可能是它现在挂的号
                "started_at": float(item.get("startedAt") or 0) / 1000,
                "pid": item.get("pid") or 0,
                # agents 自带的状态说明（有的版本有 status 文字，且和 state 不同）
                "status_text": str(item.get("status") or "") if item.get("state") else "",
            } for item in listed[account])
    return rows, failed


def find(key: str, rows: list) -> list:
    """按编号前缀、完整会话编号或名字找；名字要完全一样。"""
    key = key.strip()
    if not key:
        return []
    exact = [row for row in rows if key in (row["id"], row["session_id"], row["name"])]
    if exact:
        return exact
    return [row for row in rows if row["session_id"].startswith(key) or row["id"].startswith(key)]


def narrow_by_name(hits: list) -> tuple[list, str]:
    """按名字对上了几段不同的对话时，挑出现在要找的那段：(这段对话的全部登记, 还是分不清时的说明)。

    同名的常常只有一段在跑，其余是早就停了的旧会话或残留登记（11-12 发给 train-h004 时，
    在跑的 bb183830 旁边还有一段从没活过的同名 e77b8edb）。所以：只有一段在跑就用它；
    都没在跑就用最近有动静的那段；好几段都在跑才真的分不清。
    """
    by_session: dict = {}
    for row in hits:
        by_session.setdefault(row["session_id"], []).append(row)
    if len(by_session) <= 1:
        return hits, ""
    live = [rows for rows in by_session.values() if any(row.get("running") for row in rows)]
    if len(live) == 1:
        return live[0], ""
    if not live:
        newest = max(by_session.values(), key=lambda rows: _last_activity(rows[0]["session_id"]))
        return newest, ""
    names = "、".join(f"{rows[0]['account']}/{rows[0]['id']}({rows[0]['name']})" for rows in live[:5])
    return [], f"对上了好几段都在跑的会话：{names}；写完整编号"


def _last_activity(session_id: str) -> float:
    path = transcript(session_id)
    try:
        return path.stat().st_mtime if path is not None else 0.0
    except OSError:
        return 0.0


def _from_transcript(key: str, account: str) -> dict:
    """agents 列表里没有（普通会话已结束）：按对话记录文件找，号用调用方给的。"""
    hits = sorted(PROJECTS_DIR.glob(f"*/{key}*.jsonl"))
    if len(hits) != 1:
        return {}
    path = hits[0]
    session_id = path.stem
    cwd = ""
    with suppress(OSError), path.open(encoding="utf-8") as handle:
        for line in handle:
            if '"cwd"' in line:
                with suppress(ValueError):
                    cwd = str(json.loads(line).get("cwd") or "")
                if cwd:
                    break
    return {"account": account, "id": session_id[:8], "session_id": session_id,
            "name": session_name(path), "kind": "session", "cwd": cwd,
            "state": "ended", "running": False}


# ---------------------------------------------------------------- 接管


REGISTER_WAIT = 60
REGISTER_POLL = 0.5


def resume_launch(account: str, session: str, prompt: str = "", *, path: Path | None = None) -> tuple[list, str]:
    """`--bg --resume <完整id>` 后面该跟的启动参数，所有「续一条后台会话」的地方共用。

    返回 (启动参数, 待投递的话)。会话有名字：启动提示是 `/rename <原名>`（本地命令，不调模型，
    名字不被 CC 按提示词改掉），原提示 prompt 作为待投递的话，等会话登记好后用 deliver_after_resume 送。
    没有名字：原提示直接当启动提示，没有待投递的话。不能用 --name / -n：2.1.321+ 带 --name 续会话会分叉。
    """
    from cca.session.transcript import find_transcript  # noqa: PLC0415

    if path is None:
        path = find_transcript(str(session), profile_dir(account) / "projects")
    name = session_name(path)
    if not name:
        return ([prompt] if prompt else []), ""
    return [f"/rename {name}"], prompt


def deliver_after_resume(account: str, session: str, text: str, *, sender: dict | None = None,
                         wait: float = REGISTER_WAIT) -> str:
    """续上以后把话投进去：在 wait 秒内持续重试，直到投递成功（套接字登记出来、连得上）。

    登记表常常先于套接字开始监听写出，会话也可能还在处理 /rename，所以重试按期限而不是按次数。
    返回错误，空串是成功（text 为空也算成功）。调用方必须把非空错误报出来。
    """
    if not text:
        return ""
    deadline = time.time() + wait
    while True:
        entry = registry_entry(account, session)
        if entry.get("messagingSocketPath"):
            error = deliver(entry, text, sender or {"name": "cca"})
            if not error:
                return ""
        else:
            error = f"等了 {wait:g} 秒，{account} 上还没出现它的消息套接字"
        if time.time() >= deadline:
            return error
        time.sleep(REGISTER_POLL)


def resume_quietly(account: str, session_id: str, cwd: str = "") -> str:
    """在原号把一条后台会话重新挂上：编号、名字、原设置都不变，不跑模型。返回错误，空串是成功。"""
    return _wake({"account": account, "session_id": session_id, "kind": "background", "cwd": cwd})


def _wake(row: dict) -> str:
    """没在跑：在原号接着跑。原来是后台会话就不带参数（保留编号和原设置）。返回错误，空串是成功。"""
    # 必须用完整会话编号：只给前 8 位，claude 会复制出一个新编号的会话（实测）
    if len(str(row["session_id"])) < FULL_SESSION_ID_LEN:
        return f"会话编号不完整：{row['session_id']}，要完整编号才能原样接着跑"
    # 有名字时启动提示换成 /rename 保住原名；用户的话唤醒后由外层 deliver，这里不用管 pending
    launch, _pending = resume_launch(str(row["account"]), row["session_id"])
    cmd = [_claude(), "--bg", "--resume", row["session_id"], *launch]
    if row["kind"] != "background":
        # 普通会话：没有存下的设置，接着跑时一律全部自动批准，后台不会卡在等确认
        cmd += ["--permission-mode", "bypassPermissions"]
    from cca.env import LaunchBlocked, session_env  # noqa: PLC0415 - env 顶部导入本模块族，延迟破环
    from cca.session.transcript import strip_session, target_needs_strip  # noqa: PLC0415

    if target_needs_strip(str(row["account"])):
        stripped = strip_session(row["session_id"])
        if stripped:
            say(f"清掉了 {stripped} 处 tool_use.signature（Gemini 历史），再接着跑")
    # 起会话统一走 session_env：代理注入、地区检查、出口时区、语言。
    try:
        env = session_env(str(row["account"]))
    except LaunchBlocked as exc:
        return f"起不来：{exc}"
    cwd = row["cwd"] if row["cwd"] and Path(row["cwd"]).is_dir() else None
    try:
        out = subprocess.run(  # noqa: S603 - 参数由本模块拼好
            cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"起不来：{exc}"
    if out.returncode != 0:
        return ((out.stderr or out.stdout or "").strip().splitlines() or ["起不来"])[-1]
    deadline = time.time() + WAKE_TIMEOUT
    while time.time() < deadline:
        if registry_entry(row["account"], row["session_id"]):
            return ""
        time.sleep(0.5)
    return f"等了 {WAKE_TIMEOUT} 秒没在登记表里看到它"


def _spawned_sessions() -> set:
    """所有派出的 sideagent 自己的会话编号。"""
    out = set()
    for name in sideagent.agent_names():
        state = sideagent._read_state(name)  # noqa: SLF001
        if roster.kind_of(state) == roster.SPAWNED and state.get("session_id"):
            out.add(str(state["session_id"]))
    return out


def _spawned_owner(session_id: str) -> str:
    """这个会话是不是某个主会话派出的 sideagent（它自己的 claude 会话）；是就返回主人的标题或短编号。"""
    for name in sideagent.agent_names():
        state = sideagent._read_state(name)  # noqa: SLF001
        if roster.kind_of(state) != roster.SPAWNED or str(state.get("session_id") or "") != session_id:
            continue
        found = roster.holders(name, alive_only=False)
        owner = found[0]["session"] if found else str(state.get("owner_session") or "")
        return _owner_title(owner) if owner else name
    return ""


def takeover(key: str, owner: dict, *, account: str = "") -> dict:
    """接管一个会话；返回 {name, ...} 或 {error}。owner 是主会话的归属（owner_of，外加 account）。

    M 对 N：这个会话已经被别人接管过（有 sideagents/<名字>/）就不重建、不覆盖，只在我的名单里加一条；
    同步只有一份，大家共用。别人派出的 sideagent 不能接管（它只有一个主人）。
    """
    rows = candidates([account] if account else None)
    hits = find(key, rows)
    if not hits:
        row = _from_transcript(key, account or owner.get("account") or sideagent._owner_account())  # noqa: SLF001
        hits = [row] if row else []
    if not hits:
        return {"error": f"找不到会话 {key}；不带参数调 sideagent_takeover 看能接管哪些"}
    hits, ambiguous = narrow_by_name(hits)
    if ambiguous:
        return {"error": f"{key} {ambiguous}"}
    # 同一会话在几个号上都有登记（handoff 留下的 stopped 旧登记等）：接它现在挂的那个号
    row = min(hits, key=_registration_rank)
    spawner = _spawned_owner(row["session_id"])
    if spawner:
        return {"error": f"这是 {spawner} 派出的 sideagent，不能接管"}
    entry = registry_entry(row["account"], row["session_id"])
    woke = False
    if not entry:
        error = _wake(row)
        if error:
            return {"error": f"{row['account']}/{row['id']} 接着跑失败：{error}"}
        woke = True
        entry = registry_entry(row["account"], row["session_id"])
    name = (_attached_name_for(row["session_id"])
            or record_name(row["account"], row["session_id"], str(entry.get("jobId") or row["id"])))
    path = transcript(row["session_id"])
    label = str(entry.get("name") or row["name"] or session_name(path))
    with STATE_LOCK:
        existing = sideagent._read_state(name)  # noqa: SLF001
        if existing and roster.kind_of(existing) == roster.ATTACHED:
            # 别人接管过：不重建、不覆盖；大家都放手后停过的，接着同步（读到哪还记着）
            if existing.get("finished_at"):
                existing.pop("finished_at", None)
                sideagent._write_state(name, existing)  # noqa: SLF001
        else:
            offset = 0
            if path is not None:
                with suppress(OSError):
                    offset = max(0, path.stat().st_size - FIRST_READ_BYTES)
            sideagent._write_state(name, {  # noqa: SLF001
                # 谁用着它记在各主会话的名单里，这里不记主人
                "kind": roster.ATTACHED, "account": row["account"], "session_id": row["session_id"],
                "job_id": str(entry.get("jobId") or ""), "cwd": row["cwd"],
                "transcript": str(path or ""), "offset": offset, "label": label,
                "started": time.time(), "taken_over_at": time.time(), "notify": True,
                "code_version": sideagent.CODE_VERSION, "busy": False,
            })
    roster.add(roster.owner_fields(owner, str(owner.get("account") or "")), name, roster.ATTACHED, label=label)
    sync_one(name)
    return {"name": name, "account": row["account"], "session_id": row["session_id"],
            "label": label, "woke": woke,
            "card": card(row["session_id"], account=row["account"], title=label, state=str(row.get("state") or ""))}


def _attached_name_for(session_id: str) -> str:
    """这个会话已经有接管记录（可能记在旧号下，例如换号重启前接管的）：复用它，面板上不出两条。"""
    for other in sideagent.agent_names(include_finished=True):
        state = sideagent._read_state(other)  # noqa: SLF001
        if roster.kind_of(state) == roster.ATTACHED and state.get("session_id") == session_id:
            return other
    return ""


def release(name: str, session: str) -> int:
    """放手（规则 5）：只从我的名单里删这一条；会话本身不动。返回还有几个活着的主人挂着它（-1 表示不是接管的）。

    没有活着的主人了：删 run/、state.json 写 finished_at，其余文件留着（过了保留天数再清）。
    """
    if not is_bg(name):
        return -1
    roster.remove(session, name)
    left = len(roster.holders(name))
    if not left:
        sideagent.mark_finished(name)
    return left


# ---------------------------------------------------------------- 发话


def tell(name: str, text: str, sender: dict) -> str:
    """给接管的会话发一句（原生跨会话消息）。返回错误，空串是成功。sender 是主会话登记条目。"""
    state = sideagent._read_state(name)  # noqa: SLF001
    entry = registry_entry(str(state.get("account") or ""), str(state.get("session_id") or ""))
    if not entry.get("messagingSocketPath"):
        return f"{name} 没在跑；用 sideagent_send {{session: ...}} 发，会自动唤醒"
    return deliver(entry, text, sender)


# ---------------------------------------------------------------- 按会话发话（不接管）


def _registration_rank(row: dict) -> tuple:
    """同一会话的几条登记里，哪条是它现在挂的号：在跑的 > 没停的（越新越前）> stopped / failed。

    handoff 会把源号的登记标成 stopped、在目标号续跑，所以 stopped 那条通常是旧的。
    """
    stopped = str(row.get("state") or "") in ("stopped", "failed")
    return (not row.get("running"), stopped, -float(row.get("started_at") or 0))


def message(key: str, text: str, sender: dict, *, account: str = "", usable=None) -> dict:
    """给一条会话发一句话，**不接管**：它挂在哪个号就发到哪个号，没在跑就先唤醒。

    返回 {account, session_id, label, woke} 或 {error}。sender 是主会话的登记条目（含 sessionId、
    messagingSocketPath、name），对方回话照原生跨会话消息回到它那里。
    usable(号) 返回这个号用不了的原因（空串 = 能用）：会话挂的号额度用满时，换一个能用的号唤醒，
    同一段对话接着跑；都用不了就说清楚，不硬起。account 给了就只在这个号上找、在这个号上唤醒。
    """
    mine = str(sender.get("account") or "")
    hits, error = _locate(key, account or mine, account=account)
    if error:
        return {"error": error}
    session_id = hits[0]["session_id"]
    if session_id == str(sender.get("sessionId") or ""):
        return {"error": "这是你自己"}
    label = next((row["name"] for row in hits if row.get("name")), "") or session_name(transcript(session_id))
    for row in hits:
        entry = registry_entry(row["account"], session_id)
        if entry.get("messagingSocketPath"):
            error = deliver(entry, text, sender)
            return {"error": error} if error else {
                "account": row["account"], "session_id": session_id, "label": label, "woke": False}
    row, why = _wake_target(hits, mine, account=account, usable=usable)
    if row is None:
        return {"error": f"{label or session_id[:8]} 没在跑，也没有能唤醒它的号：{why}"}
    error = _wake(row)
    if error:
        return {"error": f"{row['account']}/{session_id[:8]} 唤醒失败：{error}"}
    # 刚唤醒的会话：登记先出来、套接字晚一点开始监听，会话还可能在处理 /rename——按期限重试着投
    error = deliver_after_resume(row["account"], session_id, text, sender=sender, wait=WAKE_DELIVER_WAIT)
    return {"error": error} if error else {
        "account": row["account"], "session_id": session_id, "label": label, "woke": True}


def _locate(key: str, fallback: str, *, account: str) -> tuple:
    """按编号前缀 / 完整编号 / 标题找会话的全部登记，按「现在挂的号」排好。返回 (登记列表, 错误)。

    哪个号都没登记的（已经退出的普通会话）按对话记录找，号先记成 fallback。
    """
    hits, ambiguous = narrow_by_name(find(key, candidates([account] if account else None)))
    if ambiguous:
        return [], f"{key} {ambiguous}"
    if not hits:
        row = _from_transcript(key, fallback)
        hits = [row] if row else []
    if not hits:
        return [], f"找不到会话 {key}：cca sessions --all 看完整编号"
    spawner = _spawned_owner(hits[0]["session_id"])
    if spawner:
        return [], f"这是 {spawner} 派出的 sideagent，只能由它的主人追问"
    return sorted(hits, key=_registration_rank), ""


def _wake_target(hits: list, mine: str, *, account: str, usable) -> tuple:
    """没在跑的会话在哪个号上唤醒：按登记顺序挑第一个能用的号；都不行退回发话人自己的号。

    返回 (row, 原因)：row 为 None 时原因说清每个号为什么不行。
    """
    def problem(name: str) -> str:
        return usable(name) if usable else ""

    tried = []
    for row in hits:
        why = problem(row["account"])
        if not why:
            return row, ""
        tried.append(f"{row['account']}（{why}）")
    if not account and mine and mine not in {row["account"] for row in hits}:
        why = problem(mine)
        if not why:
            # 换号接着跑：这个号上没有它的后台登记，按普通会话起（全部自动批准，后台不会卡在等确认）
            return {**hits[0], "account": mine, "kind": "session"}, ""
        tried.append(f"{mine}（{why}）")
    return None, "、".join(tried) or "没有候选"


def deliver(entry: dict, text: str, sender: dict) -> str:
    """往一个在跑会话的消息套接字投一条原生跨会话消息。返回错误，空串是成功。"""
    target = str(entry.get("messagingSocketPath") or "")
    if not target:
        return "它没在跑"
    address = f"uds:{sender.get('messagingSocketPath') or ''}"
    from_name = str(sender.get("name") or "sideagent")
    body = (f'<cross-session-message from="{address}" from-name="{from_name}" from-mode="bypass">\n'
            f"{text.replace('</cross-session-message>', '</cross-session-message >')}\n"
            "</cross-session-message>")
    frame = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user",
             "message": {"role": "user", "content": body}, "priority": "next", "from": address}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect(target)
        sock.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError as exc:
        return f"发不出去：{exc}"
    finally:
        sock.close()
    return ""


# ---------------------------------------------------------------- 对话记录 → 面板事件


def events_of(item: dict, *, parent: str = "") -> tuple[list, list]:
    """对话记录的一行 → (面板事件, 进度行)。

    主对话记录里的子代理行（isSidechain）跳过：子代理有自己的记录文件，由 _sync_subagents
    带着 parent（派它的 Agent 工具调用编号）单独读，面板据此画成底部那几行、能点进去。
    """
    if not isinstance(item, dict) or (item.get("isSidechain") and not parent):
        return [], []
    if parent:
        found, _notes = events_of({**item, "isSidechain": False})
        # 子代理收到的交办就是 Agent 工具的输入，详情页已经有了；进度行只留主对话的
        return [{**event, "parent": parent} for event in found if event["kind"] != "task"], []
    kind = item.get("type")
    message = item.get("message") or {}
    if kind == "user":
        return _user_events(item, message)
    if kind == "assistant":
        return _assistant_events(message)
    return [], []


def _user_events(item: dict, message: dict) -> tuple[list, list]:
    origin = item.get("origin") or {}
    if isinstance(origin, dict) and origin.get("kind") == "peer":
        text = str(origin.get("body") or "")
        who = str(origin.get("name") or "")
        return [{"kind": "task", "text": text[:4000], "from": who}], [f"TASK {text[:200]}"]
    content = message.get("content")
    if isinstance(content, str):
        if item.get("isMeta") or content.startswith("<"):
            return [], []
        return [{"kind": "task", "text": content[:4000]}], [f"TASK {content[:200]}"]
    events = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            events.append({"kind": "tool_result", "text": sideagent._tool_output(block)[:4000],  # noqa: SLF001
                           "id": block.get("tool_use_id") or "", "error": bool(block.get("is_error"))})
        elif block.get("type") == "text" and not item.get("isMeta"):
            text = str(block.get("text") or "")
            if text and not text.startswith("<"):
                events.append({"kind": "task", "text": text[:4000]})
    return events, []


def _assistant_events(message: dict) -> tuple[list, list]:
    events, notes = [], []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "thinking":
            thought = str(block.get("thinking") or "").strip()
            if thought:
                events.append({"kind": "think", "text": thought[:4000]})
        elif kind == "tool_use":
            name = str(block.get("name") or "?")
            summary = sideagent._tool_summary(block.get("input"))  # noqa: SLF001
            events.append({"kind": "tool", "name": name, "id": block.get("id") or "",
                           "input": sideagent._clip_input(block.get("input")), "summary": summary})  # noqa: SLF001
            notes.append(f"TOOL {name}" + (f" · {summary}" if summary else ""))
        elif kind == "text":
            text = str(block.get("text") or "").strip()
            if text:
                events.append({"kind": "say", "text": text[:4000]})
                notes.append("SAY " + " ".join(text.split())[:160])
    return events, notes


def usage_of(message: dict) -> tuple[int, int]:
    """(这次调用的输入含缓存, 输出)；和子代理那一行同一个口径。"""
    usage = message.get("usage") or {}
    return sideagent.usage_in(usage), int(usage.get("output_tokens") or 0)


# ---------------------------------------------------------------- 同步


def _read_new(path: Path, offset: int) -> tuple[list, int]:
    """从 offset 读到最后一个完整的行；返回 (行, 新 offset)。文件变短了就从头读。"""
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read(min(size - offset, SYNC_READ_BYTES))
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    body = chunk[: end + 1]
    lines = body.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and not _at_line_start(path, offset):
        lines = lines[1:]
    return lines, offset + len(body)


def _at_line_start(path: Path, offset: int) -> bool:
    with suppress(OSError), path.open("rb") as handle:
        handle.seek(offset - 1)
        return handle.read(1) == b"\n"
    return True


def _scan(lines: list, state: dict) -> tuple[list, list, dict]:
    """新读到的几行 → (事件, 进度行, 词元和模型)。"""
    events, notes = [], []
    acc = {"tokens_in": int(state.get("tokens_in") or 0), "tokens_out": int(state.get("tokens_out") or 0),
           "latest_in": int(state.get("latest_in") or 0), "acted": bool(state.get("acted")),
           "model": str(state.get("model") or "")}
    if acc["model"].startswith("<"):
        acc["model"] = ""  # 老记录里存过 <synthetic>，丢掉
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        found, noted = events_of(item)
        events += found
        notes += noted
        if item.get("type") == "assistant" and not item.get("isSidechain"):
            message = item.get("message") or {}
            used_in, used_out = usage_of(message)
            acc["latest_in"] = used_in or acc["latest_in"]
            acc["tokens_out"] += used_out
            acc["tokens_in"] = max(acc["tokens_in"], used_in)
            # 打断、出错时 Claude Code 写的是合成消息（model 为 <synthetic>），不是真用的模型
            model = str(message.get("model") or "")
            if model and not model.startswith("<"):
                acc["model"] = model
            acc["acted"] = acc["acted"] or any(f["kind"] in ("say", "tool") for f in found)
        if any(f["kind"] == "task" for f in found):
            acc["acted"] = False
    return events, notes, acc


def _post_turn_end(name: str, state: dict, event: dict, offset: int) -> None:
    """接管的会话一轮结束：给每个挂着它的主会话的收件箱存一条通知（面板来取）。

    键用读到的位置：同一轮结束不管同步几次，都只有一条。
    """
    title = str(state.get("label") or state.get("title") or name)
    usage = {"tokens": int(event.get("token_count") or 0), "tool_uses": 0,
             "duration_ms": int(float(event.get("seconds") or 0) * 1000)}
    text = inbox.notification_text(name, title, status="completed", text=str(event.get("text") or ""),
                                   usage=usage)
    for holder in roster.holders(name):
        session = str(holder.get("session") or "")
        if session:
            # 面板看到收件箱里有文件就来取
            inbox.post(session, {"key": inbox.key_of(name, f"turn-{offset}"), "name": name,
                                 "job": f"turn-{offset}", "status": "completed", "text": text, "instance_name": name})


def _turn_end(state: dict, events: list, now: float) -> tuple[dict, str]:
    """忙 → 闲：补一条结果（最后一句话），面板据此显示“完成”并叫醒主会话。"""
    last = next((e for e in reversed(events) if e["kind"] == "say" and not e.get("parent")), None) \
        or state.get("last_say") or {}
    text = str(last.get("text") or "") if isinstance(last, dict) else ""
    event = {"kind": "result", "text": text[:4000],
             "token_count": int(state.get("latest_in") or 0) + int(state.get("tokens_out") or 0),
             "seconds": round(now - float(state.get("turn_started") or now), 1), "at": round(now, 1)}
    return event, "RESULT " + " ".join(text.split())[:200]


def subagent_dir(transcript: Path) -> Path:
    """原生会话的子代理记录：<会话编号>.jsonl 旁边的 <会话编号>/subagents/agent-<编号>.jsonl。"""
    return transcript.with_suffix("") / "subagents"


def _sync_subagents(transcript: Path | None, state: dict) -> list:
    """增量读每个子代理的记录，翻成带 parent 的事件；读到哪记在 state["sub_offsets"]。"""
    if transcript is None:
        return []
    folder = subagent_dir(transcript)
    if not folder.is_dir():
        return []
    offsets = dict(state.get("sub_offsets") or {})
    events = []
    for path in sorted(folder.glob("agent-*.jsonl")):
        agent = path.stem.removeprefix("agent-")
        try:
            meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        parent = str(meta.get("toolUseId") or "")
        if not parent:
            continue
        # 头一次看到的子代理从头读（子代理记录都不大）
        lines, offset = _read_new(path, int(offsets.get(agent) or 0))
        offsets[agent] = offset
        for line in lines:
            with suppress(ValueError):
                events += events_of(json.loads(line), parent=parent)[0]
    state["sub_offsets"] = offsets
    return events


def _append(path: Path, lines: list) -> None:
    if lines:
        with suppress(OSError), path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


# 同步线程和写进度短语的线程都会改状态文件：同一进程里串起来，免得互相盖掉
STATE_LOCK = threading.RLock()


def _first_pending(name: str) -> dict | None:
    from cca.session import permission  # noqa: PLC0415

    rows = permission.pending(name)
    if not rows:
        return None
    row = rows[0]
    return {key: row.get(key) for key in ("id", "tool", "summary", "input", "created")} | {"count": len(rows)}


def sync_one(name: str) -> dict:
    """把一个接管的会话同步一次：新事件写进 events.jsonl/progress，状态写进 state.json。

    好几个主会话接管同一个时，各自的 MCP 后台线程都会来同步：同一时刻只让一个做（跨进程文件锁），
    别的直接拿现在的状态走，事件不会写两遍。
    """
    with STATE_LOCK:
        lock = sideagent._lock_path(name)  # noqa: SLF001
        if not lock.parent.is_dir():
            return {}
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return sideagent._read_state(name)  # noqa: SLF001
            return _sync_one(name)


def _sync_one(name: str) -> dict:
    state = sideagent._read_state(name)  # noqa: SLF001
    if not state or roster.kind_of(state) != roster.ATTACHED or state.get("finished_at"):
        return state if state.get("finished_at") else {}
    entry = registry_entry(str(state.get("account") or ""), str(state.get("session_id") or ""))
    status = str(entry.get("status") or "")
    path = Path(state["transcript"]) if state.get("transcript") else transcript(str(state.get("session_id")))
    lines, offset = _read_new(path, int(state.get("offset") or 0)) if path else ([], 0)
    now = time.time()
    events, notes, acc = _scan(lines, state)
    events += _sync_subagents(path, state)
    busy = status in ("busy", "waiting")
    was_busy = bool(state.get("busy"))
    if busy and not was_busy:
        state["turn_started"] = now
    state.update(acc)
    if was_busy and not busy:
        event, note = _turn_end(state, events, now)
        events.append(event)
        notes.append(note)
        _post_turn_end(name, state, event, offset)
    started = float(state.get("turn_started") or state.get("started") or now)
    _append(sideagent._events_path(name),  # noqa: SLF001
            [json.dumps({**e, "t": round(now - started, 1)}, ensure_ascii=False) for e in events])
    _append(sideagent._progress_path(name), notes)  # noqa: SLF001
    says = [e for e in events if e["kind"] == "say" and not e.get("parent")]
    if says:
        state["last_say"] = says[-1]
    if entry.get("account") and entry["account"] != state.get("account"):
        # 会话换了号重新拉起来：面板跟到新号上（名字、进程号也在下面跟着换）
        state["account"] = entry["account"]
    state.update({
        "offset": offset, "busy": busy, "status": status or "ended",
        "waiting_for": str(entry.get("waitingFor") or "") if status == "waiting" else "",
        "pid": int(entry.get("pid") or 0), "label": str(entry.get("name") or state.get("label") or ""),
        "token_count": acc["latest_in"] + acc["tokens_out"],
        "code_version": sideagent.CODE_VERSION, "last_activity": now, "kind": roster.ATTACHED,
        # 钩子转来的“请确认”：面板据此画“允许/拒绝”。没有就是 None（等原生确认框的仍看 status=waiting）
        "permission": _first_pending(name),
    })
    sideagent._write_state(name, state)  # noqa: SLF001
    return state


# ---------------------------------------------------------------- 进度短语


_summarizing: set = set()


def summary_due(state: dict, now: float) -> bool:
    """在忙、离上一句超过 SUMMARY_INTERVAL（默认 30 秒）就该写一句了。"""
    return bool(state.get("busy")) and now - float(state.get("summary_at") or 0) >= sideagent.SUMMARY_INTERVAL


def summarize_later(name: str, runner=None) -> bool:
    """另起一条线程写一句进度短语（要调小模型，几秒）；同一个会话同时只跑一条。"""
    if name in _summarizing:
        return False
    _summarizing.add(name)

    def run() -> None:
        try:
            summarize_once(name, runner)
        finally:
            _summarizing.discard(name)
    threading.Thread(target=run, daemon=True, name=f"summary-{name}").start()
    return True


def summarize_once(name: str, runner=None) -> str:
    """照常驻 sideagent 的做法写一句进度短语（小模型用原会话那个号配置的）；返回结果，测试用。"""
    state = sideagent._read_state(name)  # noqa: SLF001
    if not state.get("busy"):
        return "skip idle"
    account = str(state.get("account") or "")
    model = summary.configured_model(profile_dir(account))
    if not model:
        return "skip off"
    cwd = str(state.get("cwd") or "")
    events = [event for event in sideagent._tail_events(name, limit=200, tail_bytes=400_000)  # noqa: SLF001
              if not event.get("parent")]
    notes = summary.build_notes(events, cwd)
    if not notes:
        return "skip empty"
    turn = state.get("turn_started")
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(profile_dir(account))}
    reply = (runner or summary.run_model)(model, summary.user_prompt(notes, str(state.get("summary") or "")),
                                         env, cwd if cwd and Path(cwd).is_dir() else str(Path.home()))
    with STATE_LOCK:
        state = sideagent._read_state(name)  # noqa: SLF001
        if state.get("turn_started") != turn or not state.get("busy"):
            return "drop stale"
        label = summary.accept(str(reply.get("text") or "")) if reply.get("ok") else None
        if label is None:
            state.update(summary_at=time.time(), summary_error=str(reply.get("error") or "unusable")[:200])
            sideagent._write_state(name, state)  # noqa: SLF001
            return "fail"
        state.update(summary=label, summary_at=time.time(), summary_model=model, summary_error="")
        sideagent._write_state(name, state)  # noqa: SLF001
    _append(sideagent._progress_path(name), [f"SUMMARY {label}"])  # noqa: SLF001
    return "ok"
