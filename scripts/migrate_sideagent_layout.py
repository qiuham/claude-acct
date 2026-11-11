#!/usr/bin/env python3
"""一次性迁移：老的平铺 sideagent 文件 → 一个 sideagent 一个文件夹 + 每个主会话一张名单。

运行时（cca、MCP、面板）只认新格式，不带兼容代码；换代码时手动跑一次这个脚本：

    python3 scripts/migrate_sideagent_layout.py            # 搬
    python3 scripts/migrate_sideagent_layout.py --dry-run  # 只列出要搬什么

还在跑的老守护进程（老 .sock 能连上）跳过，并在最后列出来：等它们停了再跑一次。
新格式见 docs/specs/sideagent-mn.md。
"""

from __future__ import annotations

import fcntl
import json
import sys
import time
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cca.config import SIDEAGENT_KEEP_DAYS
from cca.session import roster, sideagent

# 等别的进程迁移完最多几秒
MIGRATE_LOCK_SECONDS = 30


# 老文件名的后缀 → 新文件名；.sock、.live、.last.lock 不搬（进程重启后在 run/ 里重建）
LEGACY_FILES = {".json": "state.json", ".events": "events.jsonl", ".progress": "progress",
                ".summary.log": "summary.log", ".last.json": "result.json", ".log": "log"}
LEGACY_DROP = (".sock", ".live", ".last.lock", ".owner")
# 一组老文件里至少有其中一个，才算一个 sideagent
LEGACY_KEYS = {".json", ".last.json", ".progress", ".events", ".owner", ".sock"}


def _legacy_name(filename: str) -> tuple[str, str]:
    """老文件名 → (名字, 后缀)；不是 sideagent 的文件返回 ("", "")。长后缀先比（.last.json 先于 .json）。"""
    if filename.startswith((".", "stream-")):
        return "", ""
    for suffix in sorted((*LEGACY_FILES, *LEGACY_DROP), key=len, reverse=True):
        if filename.endswith(suffix) and len(filename) > len(suffix):
            return filename[: -len(suffix)], suffix
    return "", ""


def _legacy_groups(root: Path) -> dict:
    """平铺的老文件按名字分组：{名字: {后缀: 路径}}。"""
    groups: dict = {}
    for path in root.iterdir():
        if not path.is_file() and not path.is_socket():
            continue
        name, suffix = _legacy_name(path.name)
        if name:
            groups.setdefault(name, {})[suffix] = path
    # 只有 .log 的（panel-debug.log 这类面板自己的日志）不是 sideagent
    return {name: files for name, files in groups.items() if set(files) & LEGACY_KEYS}


def _socket_alive(path: Path) -> bool:
    """老套接字还能连上：那个守护进程还在跑（老代码）。"""
    import socket  # noqa: PLC0415

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(str(path))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _move_one(source: Path, target: Path) -> None:
    """搬一个文件；新位置已经有了：日志类接在后面，结果记录按任务编号合并，其余留新的。"""
    if not target.exists():
        source.replace(target)
        return
    if target.name == "result.json":
        old, new = sideagent._read_record(source), sideagent._read_record(target)  # noqa: SLF001
        jobs = {str(job.get("id") or index): job for index, job in enumerate(sideagent.jobs_of(old))}
        jobs.update({str(job.get("id") or f"n{index}"): job for index, job in enumerate(sideagent.jobs_of(new))})
        sideagent._write_record(target, {**old, **new, "jobs": list(jobs.values())[-sideagent.JOB_HISTORY:]})  # noqa: SLF001
    elif target.name in ("events.jsonl", "progress", "summary.log", "log"):
        with target.open("a", encoding="utf-8") as handle:
            handle.write(source.read_text(encoding="utf-8", errors="replace"))
    source.unlink()


def _migrate_group(name: str, files: dict, now: float) -> dict:
    """搬一个 sideagent 的老文件；返回要写进名单的归属 {主会话: {...}}（没有归属是空）。"""
    note = {}
    with suppress(OSError, ValueError):
        note = json.loads(files[".owner"].read_text(encoding="utf-8")) if ".owner" in files else {}
    note = note if isinstance(note, dict) else {}
    state = sideagent._read_record(files[".json"]) if ".json" in files else {}  # noqa: SLF001
    record = sideagent._read_record(files[".last.json"]) if ".last.json" in files else {}  # noqa: SLF001
    newest = max((path.stat().st_mtime for path in files.values() if path.exists()), default=now)
    # 只有结果记录的老记录：超过保留天数的直接删
    only_record = set(files) <= {".last.json", ".last.lock"}
    if only_record and now - newest > SIDEAGENT_KEEP_DAYS * roster.DAY_SECONDS:
        for path in files.values():
            with suppress(OSError):
                path.unlink()
        return {}
    folder = sideagent.agent_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    for suffix, path in files.items():
        if suffix in LEGACY_DROP:
            with suppress(OSError):
                path.unlink()
            continue
        _move_one(path, folder / LEGACY_FILES[suffix])
    # state.json：老格式退出时会删掉它，补一份（字段从结果记录来），并写上 kind 和 finished_at
    state = state or {key: record.get(key) for key in (*sideagent.RECORD_KEYS, "owner_session") if record.get(key)}
    legacy = {key: state.get(key) or record.get(key) for key in
              ("owner_session", "owner_pid", "owner_proc_start", "owner_account")}
    kind = roster.ATTACHED if str(state.get("kind") or "") in ("bg", roster.ATTACHED) else roster.SPAWNED
    state["kind"] = kind
    state.pop("external", None)
    if kind == roster.ATTACHED:
        # 接管的谁用着它记在名单里，state.json 不再记主人
        for key in ("owner_session", "owner_pid", "owner_proc_start", "owner_account"):
            state.pop(key, None)
    if kind == roster.SPAWNED and not state.get("finished_at"):
        # 在跑的老进程已经跳过了，到这里的都已经停了
        state["finished_at"] = newest
    sideagent._write_record(sideagent._state_path(name), state)  # noqa: SLF001
    owner = str(note.get("session") or legacy["owner_session"] or "")
    if not owner:
        return {}
    entry = {"kind": kind, "since": float(state.get("taken_over_at") or state.get("started") or newest)}
    for key in roster.ENTRY_FIELDS:
        entry[key] = str(note.get(key) or state.get(key) or record.get(key) or "")
    top = {"session": owner, "account": str(note.get("account") or legacy["owner_account"] or ""),
           "pid": int(legacy["owner_pid"] or 0), "proc_start": str(legacy["owner_proc_start"] or "")}
    if note.get("instance_socket") or note.get("peer_socket"):
        # 老纸条里记的是 sideagent 自己的消息 socket（面板按发件地址认实例）：放回它的 state.json
        state["instance_socket"] = state.get("instance_socket") or note.get("instance_socket") or note.get("peer_socket")
        sideagent._write_record(sideagent._state_path(name), state)  # noqa: SLF001
    return {owner: {"top": top, "name": name, "entry": entry}}


def _wait_lock(handle) -> bool:
    """等别的进程迁移完（最多 MIGRATE_LOCK_SECONDS 秒）；拿到锁返回真。"""
    deadline = time.time() + MIGRATE_LOCK_SECONDS
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if time.time() > deadline:
                return False
            time.sleep(0.2)
        else:
            return True


def migrate() -> tuple[int, list]:
    """老的平铺文件搬进 sideagents/<名字>/，并从 .owner 和 owner_session 生成 sessions/<主人>.json。

    有锁（同时跑两次，后一个等前一个做完）。在跑的老守护进程（.sock 能连上）跳过：它还在往平铺的文件里写，
    等它停了再跑一次本脚本。返回 (搬了几个, 跳过的名字)。
    """
    root = sideagent.SIDEAGENT_DIR
    if not root.is_dir() or not _legacy_groups(root):
        return 0, []
    moved = 0
    skipped: list = []
    with (root / ".migrate.lock").open("a") as handle:
        if not _wait_lock(handle):
            return 0, []
        now = time.time()
        owners: dict = {}
        for name, files in sorted(_legacy_groups(root).items()):
            sock = files.get(".sock")
            if sock is not None and _socket_alive(sock):
                skipped.append(name)
                continue
            for owner, item in _migrate_group(name, files, now).items():
                slot = owners.setdefault(owner, {"top": {}, "agents": {}})
                # 同一个主人的几个 sideagent 各记了一部分（号、进程号）：缺的用后面的补上
                for key, value in item["top"].items():
                    slot["top"][key] = slot["top"].get(key) or value
                slot["agents"][item["name"]] = item["entry"]
            moved += 1
        with roster._locked():  # noqa: SLF001
            for owner, slot in owners.items():
                data = roster.read(owner)
                top = {key: value for key, value in slot["top"].items() if value}
                roster._write(owner, {**top, **data,  # noqa: SLF001
                                      "agents": {**slot["agents"], **(data.get("agents") or {})}})
    return moved, skipped


def main(argv: list) -> int:
    root = sideagent.SIDEAGENT_DIR
    groups = _legacy_groups(root) if root.is_dir() else {}
    if "--dry-run" in argv:
        for name, files in sorted(groups.items()):
            print(name, " ".join(sorted(files)))  # noqa: T201
        print(f"共 {len(groups)} 个要搬")  # noqa: T201
        return 0
    moved, skipped = migrate()
    print(f"搬了 {moved} 个")  # noqa: T201
    if skipped:
        print("还在跑、这次没搬（等它们停了再跑一次）：" + "、".join(skipped))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
