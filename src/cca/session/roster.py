"""主会话名单：~/.cca/sessions/<主会话编号>.json，记这个会话用着哪些 sideagent。

设计见 docs/specs/sideagent-mn.md。要点：

- 一个文件只有一个写的人：sessions/<编号>.json 只由那个主会话自己的进程写（MCP 服务、它调的 cca 命令）。
- 查“我用着谁”：读我的文件；主会话编号会因为 /bg、← 换（session_chain 能顺着找到一串），
  一串编号的文件合起来算；写的时候写到最新的编号，旧编号文件里的条目并过来、旧文件删掉。
- 查“谁用着它”：扫 sessions/*.json，主人已经不在了的不算。
- 派出的（spawned）只有一个主人；接管的（attached）可以有多个（M 对 N）。

还管过期清理（cleanup）。老的平铺格式由 scripts/migrate_sideagent_layout.py 一次性迁移，运行时不认老格式。
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

from cca.config import (
    ROSTER_ADOPT_SECONDS,
    ROSTER_ALIVE_CACHE_SECONDS,
    SIDEAGENT_CLEANUP_SECONDS,
    SIDEAGENT_KEEP_DAYS,
)
from cca.session import sideagent as core

if TYPE_CHECKING:
    from pathlib import Path

SPAWNED = "spawned"
ATTACHED = "attached"
DAY_SECONDS = 86400
# 名单里每个 sideagent 那一条可以带的字段（主人那一份：横条名字、描述、哪个子代理派的）
ENTRY_FIELDS = ("label", "description", "caller_agent")


def sessions_dir() -> Path:
    """名单目录：和 sideagents/ 同一层（测试把 SIDEAGENT_DIR 换到临时目录时，这里跟着换）。"""
    return core.SIDEAGENT_DIR.parent / "sessions"


def roster_path(session: str) -> Path:
    return sessions_dir() / f"{session}.json"


def kind_of(state: dict) -> str:
    """state.json 里的种类：attached 是接管的，其余（没写的）是派出的。"""
    return ATTACHED if str(state.get("kind") or "") == ATTACHED else SPAWNED


def read(session: str) -> dict:
    """一个主会话的名单；没有或读不了就是空字典。"""
    if not session:
        return {}
    try:
        data = json.loads(roster_path(session).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not isinstance(data.get("agents"), dict):
        data["agents"] = {}
    return data


def _write(session: str, data: dict) -> None:
    """先写临时文件再换名：同时有人读，读到的要么是旧的、要么是新的。"""
    path = roster_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        temp.chmod(0o600)
    temp.replace(path)


def _all_files() -> list:
    folder = sessions_dir()
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.json") if not path.name.endswith(".tmp"))


_chain_cache: dict = {}   # 旧编号 → (查的时刻, 它的接力链)；接力链要读对话记录末尾，别每次都查


def _chain_of(session: str) -> list:
    now = time.time()
    cached = _chain_cache.get(session)
    if cached and now - cached[0] < ROSTER_ADOPT_SECONDS:
        return cached[1]
    chain = core.session_chain(session)
    _chain_cache[session] = (now, chain)
    return chain


def chain_sessions(session: str) -> list:
    """和我是“一串”的编号：我自己，加上名单目录里顺着接力记录最后落到我的那些旧编号。"""
    if not session:
        return []
    found = [session]
    for path in _all_files():
        other = path.stem
        if other != session and session in _chain_of(other)[1:]:
            found.append(other)
    return found


def mine(session: str, *, pid: int = 0, proc_start: str = "") -> dict:
    """查“我用着谁”（规则 1）：{名字: 条目}，一串编号的文件合起来算，新编号的条目优先。

    给了 pid 和 proc_start 时，同一个进程名下的名单也算（主会话在同一个进程里换了编号，
    例如 /resume 了别的对话，原来按进程认的归属不丢）。
    """
    files = list(reversed(chain_sessions(session)))
    if pid and proc_start:
        for path in _all_files():
            if path.stem in files:
                continue
            data = read(path.stem)
            if int(data.get("pid") or 0) == pid and str(data.get("proc_start") or "") == proc_start:
                files.insert(0, path.stem)
    merged: dict = {}
    for sid in files:
        merged.update(read(sid).get("agents") or {})
    return merged


def mine_of(entry: dict) -> dict:
    """按登记表条目（caller_now()）查本会话用着谁。"""
    return mine(str(entry.get("sessionId") or ""), pid=int(entry.get("pid") or 0),
                proc_start=str(entry.get("procStart") or ""))


def owner_fields(entry: dict, account: str = "") -> dict:
    """主人那部分（放名单顶层）：登记表条目 → 会话、号、进程号、进程启动时刻、消息 socket。"""
    return {"session": str(entry.get("sessionId") or entry.get("session") or ""),
            "account": account or str(entry.get("account") or ""),
            "pid": int(entry.get("pid") or 0),
            "proc_start": str(entry.get("procStart") or entry.get("proc_start") or ""),
            "instance_socket": str(entry.get("messagingSocketPath") or entry.get("instance_socket") or "")}


@contextmanager
def _locked():
    """改名单时串起来（同一个主会话的 MCP 服务和它调的 cca 命令可能同时写）。"""
    folder = sessions_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _adopt_locked(owner: dict) -> dict:
    """把一串旧编号的名单并进最新编号（owner["session"]）的文件，删掉旧文件；返回合好的名单。"""
    session = owner["session"]
    data = read(session)
    agents: dict = {}
    for sid in reversed(chain_sessions(session)):
        if sid == session:
            continue
        agents.update(read(sid).get("agents") or {})
    agents.update(data.get("agents") or {})
    fresh = {**data, **{key: value for key, value in owner.items() if value}, "agents": agents}
    _write(session, fresh)
    for sid in chain_sessions(session):
        if sid != session:
            with suppress(OSError):
                roster_path(sid).unlink()
    return fresh


def adopt(owner: dict) -> dict:
    """主会话自己调：并旧编号、刷新进程号和 socket（会话换了进程、换了号以后）。"""
    if not owner.get("session"):
        return {}
    # 还没用过 sideagent 的会话不建空名单
    if not any(roster_path(sid).exists() for sid in chain_sessions(owner["session"])):
        return {}
    with _locked():
        return _adopt_locked(owner)


def add(owner: dict, name: str, kind: str, **fields) -> None:
    """在我的名单里加一条（派出、接管时）；已有就只更新给了值的字段，since 不变。"""
    if not owner.get("session") or not name:
        return
    with _locked():
        data = _adopt_locked(owner)
        old = data["agents"].get(name) or {}
        entry = {**old, "kind": kind, "since": old.get("since") or time.time()}
        for key in ENTRY_FIELDS:
            value = str(fields.get(key) or "")
            if value or key not in entry:
                entry[key] = value or str(old.get(key) or "")
        data["agents"][name] = entry
        _write(owner["session"], data)


def remove(session: str, name: str) -> bool:
    """从我的名单（一串编号的每个文件）里删掉这一条；返回原来有没有。"""
    found = False
    with _locked():
        for sid in chain_sessions(session):
            data = read(sid)
            if name in data.get("agents", {}):
                data["agents"].pop(name)
                _write(sid, data)
                found = True
    return found


def owner_alive(data: dict) -> bool:
    """名单的主人还在不在：进程号 + 启动时刻对得上，或者同一段对话（含接力后的新编号）还有活着的进程。

    和 sideagent 投递回报时同一个办法（resolve_owner）：会话被 resume 到新进程、换号重开都算还在。
    """
    owner = {"pid": int(data.get("pid") or 0), "proc_start": str(data.get("proc_start") or ""),
             "session": str(data.get("session") or "")}
    if not owner["pid"] and not owner["session"]:
        return False
    # 要扫各号的登记表：几秒内问过同一个主人就用上次的答案（挑选框、清理会对同一个主人问很多遍）
    key = (owner["pid"], owner["proc_start"], owner["session"])
    now = time.time()
    cached = _alive_cache.get(key)
    if cached and now - cached[0] < ROSTER_ALIVE_CACHE_SECONDS:
        return cached[1]
    alive = bool(core.resolve_owner(core._caller_sessions(), owner))  # noqa: SLF001
    _alive_cache[key] = (now, alive)
    return alive


_alive_cache: dict = {}   # (进程号, 启动时刻, 会话编号) → (查的时刻, 在不在)


def holders(name: str, *, alive_only: bool = True) -> list:
    """查“谁用着它”（规则 2）：名单里有它的主会话 [{session, account, kind, ...}]；默认只要主人还在的。"""
    out = []
    for path in _all_files():
        data = read(path.stem)
        entry = (data.get("agents") or {}).get(name)
        if entry is None:
            continue
        if alive_only and not owner_alive(data):
            continue
        out.append({"session": str(data.get("session") or path.stem), "account": str(data.get("account") or ""),
                    **entry})
    return out


# ---------------------------------------------------------------- 清理


CLEANUP_STAMP = ".cleanup"


def cleanup(*, force: bool = False) -> None:
    """顺手清理（规则 9），在已有入口里调（cca sideagent ls、派活、MCP 同步线程），不另起进程：

    - 接管的会话没有活着的主人了：写 finished_at（规则 5、6）。
    - finished_at 超过 SIDEAGENT_KEEP_DAYS 的整个文件夹删掉，名单里指向它的条目也删掉。
    - 名单里主人不在了的：接管的条目删掉（只当作解开）；删完空了的名单文件删掉。
    没到 SIDEAGENT_CLEANUP_SECONDS 就直接返回（force 除外）。
    """
    root = core.SIDEAGENT_DIR
    stamp = root / CLEANUP_STAMP
    now = time.time()
    if not force:
        with suppress(OSError):
            if now - stamp.stat().st_mtime < SIDEAGENT_CLEANUP_SECONDS:
                return
    if root.is_dir():
        with suppress(OSError):
            stamp.write_text(str(now), encoding="utf-8")
    alive_by_file = {path.stem: owner_alive(read(path.stem)) for path in _all_files()}
    gone = _sweep_agents(now)
    if sessions_dir().is_dir():
        _sweep_rosters(gone, alive_by_file)


def _sweep_agents(now: float) -> set:
    """文件夹那一遍：接管的按有没有活着的主人写上/去掉 finished_at；过了保留天数的删掉。返回删掉的名字。"""
    gone = set()
    for name in core.agent_names():
        state = core._read_state(name)  # noqa: SLF001
        finished = float(state.get("finished_at") or 0)
        if kind_of(state) == ATTACHED:
            alive_holders = holders(name)
            if not finished and not alive_holders:
                core.mark_finished(name, now)
                continue
            if finished and alive_holders:
                # 有活着的主人却被记成停了（例如当时一时没查到主人的进程）：接着同步
                state.pop("finished_at", None)
                core._write_state(name, state)  # noqa: SLF001
                continue
        if finished and core._run_dir(name).exists():  # noqa: SLF001
            # 停了但 run/ 还在（进程被硬杀，来不及收拾）：删掉，免得旧套接字被当成在跑
            shutil.rmtree(core._run_dir(name), ignore_errors=True)  # noqa: SLF001
        if finished and now - finished > SIDEAGENT_KEEP_DAYS * DAY_SECONDS:
            shutil.rmtree(core.agent_dir(name), ignore_errors=True)
            gone.add(name)
    return gone


def _sweep_rosters(gone: set, alive_by_file: dict) -> None:
    """名单那一遍：删掉指向已删文件夹的条目；主人不在了的，接管的条目删掉，删空了整个文件删掉。"""
    with _locked():
        for path in _all_files():
            data = read(path.stem)
            agents = data.get("agents") or {}
            alive = alive_by_file.get(path.stem, False)
            kept = {name: entry for name, entry in agents.items()
                    if name not in gone and (alive or entry.get("kind") != ATTACHED)}
            if not alive and not kept:
                with suppress(OSError):
                    path.unlink()
            elif kept != agents:
                _write(path.stem, {**data, "agents": kept})
