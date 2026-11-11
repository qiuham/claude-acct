"""sideagent 任务记录（<名字>/result.json）的底层读写：跨进程文件锁、按任务编号合并、留存实例信息。

常驻进程、MCP、jobs 模块都经这里写；从 sideagent.py 拆出来，引用一律写 core.X。"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

from cca.session import sideagent as core

if TYPE_CHECKING:
    from pathlib import Path


def job_path(name: str) -> Path:
    """实例留存的记录：会话编号等放顶上，后台任务放 jobs。实例退出后也留着。"""
    return core.agent_dir(name) / "result.json"


# MCP 服务（后台任务线程、停掉、打断）和 sideagent 进程都会改 <名字>/result.json：
# 读改写要串起来，不然互相丢更新。同一进程里的线程用线程锁，跨进程用文件锁。
_RECORD_LOCK = threading.Lock()


@contextmanager
def _record_locked(name: str):
    core.agent_dir(name).mkdir(parents=True, exist_ok=True)
    with core._RECORD_LOCK, core._lock_path(name).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _read_record(path: Path) -> dict:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return existing if isinstance(existing, dict) else {}


def _write_record(path: Path, payload: dict) -> None:
    """先写临时文件再换名：同时有人读，读到的要么是旧的、要么是新的，不会是半截。"""
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def remember_instance(name: str, fields: dict) -> None:
    """把实例的 claude 会话编号、代码版本、目录、名字记进 <名字>/result.json 顶上。

    状态文件在实例退出时删掉；这份记录留着，退出后查状态、追问接着开都靠它。
    """
    kept = {key: value for key, value in fields.items() if value}
    if not kept:
        return
    path = core.job_path(name)
    with core._record_locked(name):
        existing = core._read_record(path)
        if all(existing.get(key) == value for key, value in kept.items()):
            return
        core._write_record(path, {**existing, "account": existing.get("account") or name, **kept})


def jobs_of(data: dict) -> list:
    """一条记录里可能有多次任务（同一个 sideagent 收过多条消息），老格式当成一条。"""
    jobs = data.get("jobs")
    if isinstance(jobs, list):
        return [job for job in jobs if isinstance(job, dict)]
    return [data] if data.get("state") else []


def write_job(name: str, data: dict) -> None:
    """按任务编号合并进 <名字>/result.json，谁的结果都不会被后来的盖掉。"""
    path = core.job_path(name)
    with core._record_locked(name):
        existing = core._read_record(path)
        key = str(data.get("id") or "")
        merged, replaced = [], False
        for job in core.jobs_of(existing):
            if key and str(job.get("id") or "") == key:
                both = {**job, **data}
                # 已经记成 stopped/interrupted 的，随后写的 failed/finished 不盖掉它
                if job.get("state") in ("stopped", "interrupted"):
                    both["state"] = job.get("state")
                merged.append(both)
                replaced = True
            else:
                merged.append(job)
        if not replaced:
            merged.append(data)
        payload = {**{k: v for k, v in existing.items() if k not in ("jobs", "state")},
                   "account": name, "jobs": merged[-core.JOB_HISTORY:]}
        for field in ("owner_session", "owner_pid", "owner_proc_start", "session_id", "cwd"):
            if data.get(field):
                payload[field] = data[field]
        core._write_record(path, payload)
