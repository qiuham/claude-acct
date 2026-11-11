"""sideagent 的任务记录（`<名字>/result.json`）和归属判定。

派活（sideagent_send 后台任务）、查结果、停掉、打断都要读写这份记录；原来散在
MCP 层，后端（backends）也要用，挪到这里。归属按主会话进程认（见 is_mine）。
"""

from __future__ import annotations

import json
import time

from cca.session import roster, sideagent


def fill_record(info: dict, record: dict) -> None:
    """已经退出的实例：状态文件没了，会话编号、代码版本、目录、名字从留存的记录里补上。"""
    for key in sideagent.RECORD_KEYS:
        if not info.get(key) and record.get(key):
            info[key] = record[key]

def mark_stopped(name: str) -> None:
    mark_running(name, "stopped")


def mark_running(name: str, state: str) -> None:
    """把这个实例里在跑的任务改成 state（stopped 或 interrupted），记下时间。"""
    data = read_job_exact(name)
    for job in jobs_of(data):
        if job.get("state") == "running":
            write_job(name, {"id": job.get("id"), "state": state, f"{state}_at": time.time()})



def me() -> dict:
    """本会话：按 MCP 服务的父进程（主会话的 claude）在登记表里现查的条目。

    会话被继续、恢复后编号会变，环境变量里的编号是起服务时的旧值，所以不用它。
    """
    return sideagent.caller_now()


def my_session() -> str:
    """本会话现在的编号（从登记表来）。"""
    return str(me().get("sessionId") or "")


def owner_record(data: dict) -> dict:
    """状态或任务记录里的归属：主会话进程号、进程启动时刻、最近看到的会话编号。"""
    return {"pid": int(data.get("owner_pid") or 0), "proc_start": str(data.get("owner_proc_start") or ""),
            "session": str(data.get("owner_session") or "")}


def is_mine(data: dict) -> bool:
    """这条记录是不是本会话的：按归属到登记表找主会话现在的进程，是不是就是我这个进程。

    和 sideagent 投递时同一个办法（resolve_owner）。没记归属的老数据、查不到自己的，按可见处理。
    """
    owner = owner_record(data)
    if not owner["pid"] and not owner["session"]:
        return True
    current = me()
    if not current:
        return True
    found = sideagent.resolve_owner(sideagent._caller_sessions(), owner)  # noqa: SLF001
    return bool(found) and int(found.get("pid") or 0) == int(current.get("pid") or 0)



def owns(name: str, mine: dict | None = None) -> bool:
    """这个 sideagent 是不是本会话用着的（派出的或接管的）。

    先看本会话自己的名单（一串编号合起来，见 roster.mine）；谁的名单里都没有它的（老数据、
    不在会话里起的），退回看它自己记的主人（is_mine）。不在会话里调（命令行直接跑）按可见处理。
    mine 是事先查好的名单，一次看很多个时传进来省得每个都重查。
    """
    current = me()
    if not current:
        return True
    names = roster.mine_of(current) if mine is None else mine
    if name in names:
        return True
    if roster.holders(name, alive_only=False):
        return False
    return is_mine(sideagent._read_state(name))  # noqa: SLF001


def my_roster() -> dict:
    """本会话名单：{名字: 条目}；不在会话里是空字典。"""
    current = me()
    return roster.mine_of(current) if current else {}


def progress_tail(account: str, limit: int = 8) -> list:
    try:
        lines = sideagent._progress_path(account).read_text(encoding="utf-8").splitlines()  # noqa: SLF001
    except OSError:
        return []
    return lines[-limit:]



def job_path(name: str):
    return sideagent.job_path(name)


def job_files(account: str) -> list:
    """这个号的后台任务记录：长驻的 `<号>/result.json` 加临时的 `<号>#哈希/result.json`。"""
    if "#" in account:
        candidates = [job_path(account)]
    else:
        candidates = list(sideagent.SIDEAGENT_DIR.glob(f"{account}#*/result.json"))
        candidates.append(job_path(account))
    return [path for path in candidates
            if path.is_file() and not path.parent.name.endswith("#")]


def read_job_exact(name: str) -> dict:
    try:
        return json.loads(job_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


jobs_of = sideagent.jobs_of


def rank(job: dict, *, alive: bool) -> int:
    """挑结果的先后：在跑的 > 有内容的已完成 > 其他已完成/打断/插话 > 停掉、失败。"""
    state = job.get("state")
    if state == "running":
        return 3 if alive else 0
    if state == "finished" and job.get("text"):
        return 2
    if state in ("finished", "interrupted", "merged"):
        return 1
    return 0


def best(rows: list, alive) -> dict:
    """同一个排位里挑最新的；alive(row) 说它所在的进程还活着没有。"""
    if not rows:
        return {}
    return max(rows, key=lambda row: (rank(row, alive=alive(row)), row.get("started") or 0))


def pick_job(name: str, jobs: list) -> dict:
    """一个实例里挑最相关的一条，顺序见 rank。"""
    alive = sideagent.running(name) if jobs else False
    return best(jobs, lambda _job: alive)


def read_job(account: str) -> dict:
    rows = []
    for path in job_files(account):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not is_mine(data):
            continue
        name = str(data.get("account") or path.parent.name)
        job = pick_job(name, jobs_of(data))
        if job:
            rows.append({**job, "account": name})
    if not rows:
        return {"account": account, "state": "none"}
    # 跨实例再挑一次，和实例内同一个顺序：停掉的空结果不会盖过有内容的
    return best(rows, lambda row: sideagent.running(row["account"]))


def read_job_by_id(account: str, job_id: str) -> dict:
    """按任务编号取那一条；只在本会话、这个号的记录里找。"""
    for path in job_files(account):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not is_mine(data):
            continue
        name = str(data.get("account") or path.parent.name)
        for job in jobs_of(data):
            if str(job.get("id") or "") == job_id:
                return {**job, "account": name}
    return {"account": account, "id": job_id, "state": "none"}


def write_job(name: str, data: dict) -> None:
    """按任务编号合并进 <名字>/result.json（sideagent 进程也会写，锁和合并规则在那边）。"""
    sideagent.write_job(name, data)


def remember_session(name: str) -> None:
    """记下实例的 claude 会话编号、代码版本、工作目录、横条名字（状态文件退出后就没了）。

    写在 <名字>/result.json 顶上：退出后查状态、追问接着开，都靠这份记录。
    """
    state = sideagent._read_state(name)  # noqa: SLF001
    sideagent.remember_instance(name, {key: state.get(key) for key in sideagent.RECORD_KEYS})


def owner_fields() -> dict:
    """新任务记录的归属：和 sideagent 状态同一种写法（主会话进程 + 最近看到的编号）。"""
    owner = sideagent.owner_of(me())
    return {"owner_session": owner["session"], "owner_pid": owner["pid"],
            "owner_proc_start": owner["proc_start"]}
