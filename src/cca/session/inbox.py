"""主会话的收件箱：sideagent 一件活结束，通知先存成文件，再送；送不到就留着，等主会话来取。

保证“结束必通知、只通知一次”：

1. 存（post）：一件活一个文件 ~/.cca/inbox/<主会话编号>/<名字>~<任务编号>.json。
   同名文件已经有了（或送过了）就不再存第二份——由调用方在任务记录锁里先认领（见 finalize）。
2. 认领（claim）：要送的人先把文件改名成 <文件>.taking-<进程号>。改名是原子的，
   两个人同时送，只有一个改名成功。送到了删掉（done），没送到改回去（release）。
   认领的进程死了（改名后没来得及删或改回）：下次谁来取，看进程号已经不在，就当它没被认领。
3. 送：sideagent 常驻进程存完马上往主会话的消息 socket 推一次（push）；推不到，
   主会话的面板在启动时、看到 sideagent 结束时来取（cca sideagent inbox take），用面板接口送。
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from contextlib import suppress
from pathlib import Path

INBOX_DIR = Path("~/.cca/inbox").expanduser()
CLAIM_MARK = ".taking-"


def session_dir(session: str) -> Path:
    return INBOX_DIR / _safe(session)


def _safe(text: str) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum() or ch in "-_#.") or "unknown"


def key_of(name: str, job: str) -> str:
    """文件名（不含 .json）：sideagent 名字 + 任务编号。"""
    return f"{_safe(name)}~{_safe(job)}"


def post(session: str, notice: dict) -> Path | None:
    """存一条通知。同一个键已经有文件（还没送、正在送）就不再存，返回 None。"""
    folder = session_dir(session)
    folder.mkdir(parents=True, exist_ok=True)
    key = str(notice["key"])
    path = folder / f"{key}.json"
    if path.exists() or any(folder.glob(f"{key}.json{CLAIM_MARK}*")):
        return None
    temp = folder / f".{key}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    temp.write_text(json.dumps({**notice, "session": session, "created_at": time.time()},
                               ensure_ascii=False), encoding="utf-8")
    try:
        # link 不覆盖已有文件：两个人同时存，只有一个成功
        os.link(temp, path)
    except FileExistsError:
        return None
    finally:
        with suppress(OSError):
            temp.unlink()
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _orphan_claims(folder: Path) -> None:
    """认领它的进程已经死了：改回待送。"""
    for path in folder.glob(f"*.json{CLAIM_MARK}*"):
        try:
            pid = int(path.name.rsplit(CLAIM_MARK, 1)[1])
        except ValueError:
            continue
        if not _pid_alive(pid):
            with suppress(OSError):
                path.rename(path.with_name(path.name.rsplit(CLAIM_MARK, 1)[0]))


def claim(path: Path, taker: int | None = None) -> Path | None:
    """认领一条通知：改名成 .taking-<进程号>。别人先认领了（文件已不在）返回 None。"""
    claimed = path.with_name(f"{path.name}{CLAIM_MARK}{taker or os.getpid()}")
    try:
        path.rename(claimed)
    except OSError:
        return None
    return claimed


def done(claimed: Path) -> None:
    """送到了：删掉。"""
    with suppress(OSError):
        claimed.unlink()


def release(claimed: Path) -> None:
    """没送到：改回待送，等下一个人来送。"""
    with suppress(OSError):
        claimed.rename(claimed.with_name(claimed.name.rsplit(CLAIM_MARK, 1)[0]))


def pending(sessions: list) -> list:
    """这些会话收件箱里待送的通知文件（按存的先后）。先把死掉的认领改回来。"""
    found = []
    for session in dict.fromkeys(str(one) for one in sessions if one):
        folder = session_dir(session)
        if not folder.is_dir():
            continue
        _orphan_claims(folder)
        found.extend(folder.glob("*.json"))
    return sorted(found, key=lambda path: path.stat().st_mtime if path.exists() else 0)


def read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def settle_dead(sessions: list) -> None:
    """这些会话派出的 sideagent 里，常驻进程已经没了却没收尾的（被强杀、机器重启，当时也没人在等）：

    把它还在跑的任务记成失败、各存一条通知，并标成已结束。面板来取件时先做这一步，
    所以主会话关着时死掉的 sideagent，会话续上后也能收到通知。
    """
    from cca.session import roster  # noqa: PLC0415

    for session in dict.fromkeys(str(one) for one in sessions if one):
        for name, entry in (roster.read(session).get("agents") or {}).items():
            if entry.get("kind") == roster.ATTACHED:
                continue
            settle_one(name, session)


def settle_one(name: str, session: str) -> bool:
    """一个 sideagent 的常驻进程已经没了却没收尾：还在跑的任务记成失败并各存一条通知，标成已结束。

    进程还在、还没起来、已经结束的，不动，返回 False。起新进程前（同名续上）也先调它，
    免得面板之后把新进程误当成旧的死进程。
    """
    from cca.session import sideagent as core  # noqa: PLC0415

    state = core._read_state(name)  # noqa: SLF001
    pid = int(state.get("pid") or 0)
    if pid <= 0 or state.get("finished_at") or _pid_alive(pid):
        return False
    record = core._read_record(core.job_path(name))  # noqa: SLF001
    title = str(state.get("description") or state.get("label") or name)
    session = session or str(state.get("owner_session") or "")
    for job in core.jobs_of(record):
        if job.get("state") != "running" or job.get("noticed"):
            continue
        error = "sideagent 的进程没了（被强杀或机器重启），这件活没做完"
        finalize(name, str(job.get("id") or ""), session=session, status="failed", settle=True,
                 notice={"text": notification_text(name, title, status="failed", error=error),
                         "instance_name": name, "caller_agent": str(state.get("caller_agent") or "")})
    core.mark_finished(name)
    return True


def owner_ending_path(name: str) -> Path:
    """主会话在退出、正在停它的标记文件（stop-owned 建，sideagent 起来时删）。

    单独一个文件：不和 state.json 的读改写混在一起，免得被常驻进程频繁的状态写入盖掉。
    """
    from cca.session import sideagent as core  # noqa: PLC0415

    return core.agent_dir(name) / "owner_ending"


def take(sessions: list, taker: int) -> list:
    """面板来取：认领这些会话里所有待送的通知，返回 [{claim, notice}]。taker 是面板所在会话的进程号。"""
    settle_dead(sessions)
    out = []
    for path in pending(sessions):
        claimed = claim(path, taker)
        if claimed is None:
            continue
        notice = read(claimed)
        if not notice:
            done(claimed)
            continue
        out.append({"claim": str(claimed), "notice": notice})
    return out


def frame_body(notice: dict) -> str:
    """推到主会话消息 socket 的那一段：跨会话消息外壳 + 通知正文。"""
    sender = str(notice.get("sender") or "")
    return (f'<cross-session-message from="{sender}" from-name="{notice.get("instance_name") or notice.get("name")}"'
            f' from-mode="{notice.get("permission_mode") or "bypass"}" cca-instance="{notice.get("name")}">\n'
            f'{notice.get("text")}\n</cross-session-message>')


def push(path: Path, socket_path: str) -> bool:
    """认领这一条，推到主会话的消息 socket；推到了删掉，推不到改回待送。返回推没推到。"""
    if not socket_path:
        return False
    claimed = claim(path)
    if claimed is None:
        # 别人（面板）正在送或已经送了
        return True
    notice = read(claimed)
    sender = str(notice.get("sender") or "")
    frame = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user",
             "message": {"role": "user", "content": frame_body(notice)},
             "priority": "next", "from": sender}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        release(claimed)
        return False
    finally:
        sock.close()
    done(claimed)
    return True


# ---- 收口：一件活结束只走这里，只生成一条通知

# 通知里的状态 → 任务记录里的状态
JOB_STATE = {"completed": "finished", "failed": "failed", "killed": "stopped", "interrupted": "interrupted"}
_WORDS = {"completed": "finished", "killed": "was stopped", "interrupted": "was interrupted"}
NOTIFY_RESULT_CHARS = 20000   # 回报里的结果最多带多少字（太长的消息通道会拒）


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notification_text(name: str, title: str, *, status: str, text: str = "",  # noqa: PLR0913
                      usage: dict | None = None, error: str = "") -> str:
    """照 Claude Code 子代理的完成通知（task-notification）写：编号、状态、一句摘要、结果、用量。"""
    word = f"failed: {error or 'Unknown error'}" if status == "failed" else _WORDS.get(status, status)
    note = (f"每次它停下来都会回报一次。要接着问它，用 sideagent_send 发给 {name}"
            "（它退出了也能接着，上下文还在）。")
    parts = ["<task-notification>", f"<task-id>{name}</task-id>", f"<status>{status}</status>",
             f"<summary>{_escape(f'Agent {chr(34)}{title or name}{chr(34)} {word}')}</summary>",
             f"<note>{_escape(note)}</note>"]
    if text:
        parts.append(f"<result>{_escape(text[:NOTIFY_RESULT_CHARS])}</result>")
    if usage:
        parts.append(f"<usage><subagent_tokens>{usage.get('tokens', 0)}</subagent_tokens>"
                     f"<tool_uses>{usage.get('tool_uses', 0)}</tool_uses>"
                     f"<duration_ms>{usage.get('duration_ms', 0)}</duration_ms></usage>")
    parts.append("</task-notification>")
    return "\n".join(parts)


def finalize(name: str, job: str, *, session: str, status: str, notice: dict, notify: bool = True,  # noqa: PLR0913
             settle: bool = False) -> Path | None:
    """一件活结束的唯一出口：在任务记录锁里看它通知过没有；没有就记成结束、存一条通知。

    同一件活不管从哪条路结束（做完、出错、卡住、被停、进程崩了、调用方发现连接断了），
    谁先到谁存；后到的什么都不做。返回存下的通知文件（不用通知、已经通知过时是 None）。
    settle：顺手把还在 running 的任务记成结束（没有别人会写它时用：被停、进程崩了、卡住、调用方断了）。
    """
    from cca.session import sideagent as core  # noqa: PLC0415 - sideagent 反过来要用本模块

    real = bool(job)
    job = job or f"turn-{uuid.uuid4().hex[:8]}"
    with core._record_locked(name):  # noqa: SLF001 - 同一把任务记录锁
        path = core.job_path(name)
        existing = core._read_record(path)  # noqa: SLF001
        current = next((one for one in core.jobs_of(existing) if str(one.get("id") or "") == job), {})
        if current.get("noticed"):
            return None
        if not real:
            # 没有任务编号的一轮（命令行 tell 等）：只存通知，不往任务记录里塞假任务
            return post(session, {**notice, "key": key_of(name, job), "name": name, "job": job,
                                  "status": status}) if notify and session else None
        posted = None
        if notify and session:
            posted = post(session, {**notice, "key": key_of(name, job), "name": name, "job": job,
                                    "status": status})
        update = {"id": job, "noticed": bool(notify), "noticed_at": time.time()}
        if settle and current.get("state") in (None, "", "running"):
            update["state"] = JOB_STATE.get(status, status)
        core._write_record(path, core.merged_record(name, existing, update))  # noqa: SLF001
    return posted
