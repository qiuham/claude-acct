"""sideagent 后端：按实例名选一种实现，发话、看状态、取结果、停、打断、同步都走这里。

两种 sideagent 在面板上长得一样，背后完全不同：

- 常驻进程（ProcessBackend）：`cca sideagent serve` 包着 claude 跑，我们自己的套接字、
  任务记录（`<名字>/result.json`），派活有任务编号。state.json 里 kind=spawned。
- 接管的原生会话（TakenOverBackend）：Claude Code 自己的后台会话（`claude --bg`），我们只
  读它的对话记录、发跨会话消息；主会话退出时不停它（见 takeover）。kind=attached，
  可以同时被好几个主会话接管。

调用方（MCP 工具、命令行、主会话收工）只问 `backend_for(名字)` 要实现，不再到处写
“如果是接管的会话就……”。实例种类记在状态文件的 `kind` 里，没有就是常驻进程。
"""

from __future__ import annotations

import json
import time

from cca.config import ROSTER_ADOPT_SECONDS
from cca.session import jobs, roster, sideagent, takeover


class Backend:
    """一种 sideagent 的做法。方法都按具体实例名（如 ds#ab12cd、003#bg-1234abcd）操作。"""

    kind = ""
    # 主会话退出、收工时要不要跟着停
    outlives_owner = False
    # sideagent_send 发给它时，是不是走后台任务（有任务编号、能按编号取结果）
    takes_jobs = True
    # 进度要不要靠同步（进程不归我们管、不自己写面板文件）
    needs_sync = False

    def tell(self, name: str, text: str, *, interject: bool = False) -> str:
        """只发不等。返回错误，空串是成功。"""
        raise NotImplementedError

    def status(self, name: str) -> dict:
        raise NotImplementedError

    def result(self, name: str, job_id: str = "") -> dict:
        raise NotImplementedError

    def stop(self, name: str) -> str:
        """停掉；返回给人看的一句话。"""
        raise NotImplementedError

    def interrupt(self, name: str) -> str:
        raise NotImplementedError

    def sync(self, name: str, *, summarize: bool = False) -> dict:  # noqa: ARG002 - 自己写文件的后端什么都不做
        """把外部的进度同步进面板读的文件；summarize 时顺带写进度短语。自己写文件的后端什么都不做。"""
        return {}


class ProcessBackend(Backend):
    kind = roster.SPAWNED

    def tell(self, name: str, text: str, *, interject: bool = False) -> str:
        try:
            if interject:
                sideagent.interject(name, text)
            else:
                sideagent._tell(name, text)  # noqa: SLF001
        except sideagent.SideagentError as exc:
            return str(exc)
        return ""

    def status(self, name: str) -> dict:
        info = sideagent.status(name)
        info["name"] = name
        record = jobs.read_job_exact(name)
        jobs.fill_record(info, record)
        job = jobs.pick_job(name, jobs.jobs_of(record))
        if job:
            info["state"] = job.get("state")
            info["job_id"] = job.get("id")
            info["job_started"] = job.get("started")
        return info

    def result(self, name: str, job_id: str = "") -> dict:
        data = jobs.read_job_by_id(name, job_id) if job_id else jobs.read_job(name)
        name = str(data.get("account") or name)
        # 和 sideagent_status 一个口径：进程死没死看 running()，任务状态看记录
        data["running"] = sideagent.running(name)
        # 实例退出后，会话编号、代码版本从留存的记录里取
        jobs.fill_record(data, {**jobs.read_job_exact(name), **{
            key: value for key, value in sideagent._read_state(name).items()  # noqa: SLF001
            if key in sideagent.RECORD_KEYS and value}})
        if data.get("state") == "running":
            data["progress"] = jobs.progress_tail(name)
            data["doing"] = sideagent.doing(name)
        if data["running"]:
            info = sideagent.status(name)
            data["tokens_in"] = info.get("tokens_in", 0)
            data["tokens_out"] = info.get("tokens_out", 0)
            # 和 Claude Code 子代理那一行同一个数：最近一次输入 + 累计输出 + 正在输出的估计
            data["token_count"] = info.get("token_count", 0)
            if info.get("stale"):
                data["stale"] = True
                data["stale_note"] = info.get("stale_note")
        return data

    def stop(self, name: str) -> str:
        # 先把在跑的任务记成 stopped，再停进程：后台线程随后拿到“连接断了”也不会盖掉
        jobs.mark_stopped(name)
        sideagent._stop([name])  # noqa: SLF001
        return f"{name}: 已停"

    def interrupt(self, name: str) -> str:
        reply = sideagent.interrupt(name)
        if reply and not reply.get("busy"):
            return f"{name}: 它这会儿闲着，没有要打断的"
        # 先记 interrupted：后台线程随后拿到这一轮的结果，也不会改回 finished
        jobs.mark_running(name, "interrupted")
        if not reply:
            return f"{name}: 已发打断（旧版本进程，不回确认）；这条任务记成 interrupted"
        return f"{name}: 已打断这一轮，进程还在，可以接着发；这条任务记成 interrupted"


class TakenOverBackend(Backend):
    kind = roster.ATTACHED
    outlives_owner = True
    takes_jobs = False
    needs_sync = True

    def tell(self, name: str, text: str, *, interject: bool = False) -> str:  # noqa: ARG002 - 原生消息本来就是插话
        return takeover.tell(name, text, sideagent.caller_now())

    def status(self, name: str) -> dict:
        state = takeover.sync_one(name)
        # shell：会话停在终端里（例如刚跑完 ! 命令），人没在等它，按空闲算，不是结束
        word = {"busy": "running", "waiting": "blocked", "idle": "idle", "shell": "idle"}.get(
            str(state.get("status")), "ended")
        return {"name": name, "kind": self.kind, "account": state.get("account"),
                "session_id": state.get("session_id"), "label": state.get("label"), "state": word,
                "waiting_for": state.get("waiting_for") or "", "model": state.get("model") or "",
                "token_count": state.get("token_count") or 0,
                "last_say": (state.get("last_say") or {}).get("text", "")[:2000]}

    def result(self, name: str, job_id: str = "") -> dict:  # noqa: ARG002 - 接管的会话没有任务编号
        return self.status(name)

    def stop(self, name: str) -> str:
        return (f"{name} 是接管的原生后台会话，不归 sideagent 停；"
                "要停用 claude stop <编号>，只从本会话的面板拿掉用 sideagent_release")

    def interrupt(self, name: str) -> str:
        return f"{name} 是接管的原生后台会话，打断不了；要停用 claude stop，要拿掉用 sideagent_release"

    def sync(self, name: str, *, summarize: bool = False) -> dict:
        state = takeover.sync_one(name)
        # 进度短语要调小模型，另起线程；只在常驻的同步线程里做（命令行兜底的 bg-sync 一下就退）
        if summarize and takeover.summary_due(state, time.time()):
            takeover.summarize_later(name)
        return state


BACKENDS = {backend.kind: backend for backend in (ProcessBackend(), TakenOverBackend())}
DEFAULT = BACKENDS[roster.SPAWNED]


def backend_for(name: str) -> Backend:
    """按状态文件里的 kind 选实现；没有记录、没写 kind 的都是常驻进程。"""
    state = sideagent._read_state(name) if name else {}  # noqa: SLF001
    return BACKENDS[roster.kind_of(state)] if state else DEFAULT


SYNC_INTERVAL = 2.0
# 面板看这个心跳文件新不新，决定要不要自己起 `cca sideagent bg-sync`
BEAT_STALE_SECONDS = 10


def sync_owned(session_id: str, *, summarize: bool = False) -> int:
    """同步这个主会话名单里所有要同步的实例（常驻进程自己写文件，sync 什么都不做）。"""
    count = 0
    if not session_id:
        return 0
    for name in roster.mine(session_id):
        backend = backend_for(name)
        if not backend.needs_sync or sideagent._read_state(name).get("finished_at"):  # noqa: SLF001
            continue
        try:
            backend.sync(name, summarize=summarize)
            count += 1
        except Exception:  # noqa: BLE001, S112 - 一个同步失败不影响别的
            continue
    return count


def beat_path(session_id: str):
    return sideagent.SIDEAGENT_DIR / f".sync-{session_id}.beat"


def sync_loop(stop, session_of, interval: float = SYNC_INTERVAL) -> None:
    """MCP 服务里的后台线程：每隔 interval 秒同步一次本会话名下的实例，并写心跳。

    session_of() 现查本会话编号（会话被继续、恢复后会变）；stop 是 threading.Event。
    """
    adopted_at = 0.0
    while not stop.is_set():
        session_id = ""
        try:
            session_id = str(session_of() or "")
            if session_id and time.time() - adopted_at >= ROSTER_ADOPT_SECONDS:
                # 本会话自己写自己的名单：一串旧编号并到最新编号，刷新进程号；顺手清理（有间隔，没到点直接返回）
                adopted_at = time.time()
                entry = sideagent.caller_now()
                if str(entry.get("sessionId") or "") == session_id:
                    roster.adopt(roster.owner_fields(entry, sideagent._owner_account()))  # noqa: SLF001
                roster.cleanup()
            if session_id:
                sync_owned(session_id, summarize=True)
                path = beat_path(session_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(time.time()), encoding="utf-8")
        except Exception:  # noqa: BLE001, S110 - 后台线程不能把 MCP 服务带崩
            pass
        stop.wait(interval)


def dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)
