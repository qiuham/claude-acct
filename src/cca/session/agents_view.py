"""cca agents 的数据层：各号的 `claude agents` 合并成一张跨号总表（只读，不碰会话）。

取数、同一会话的合并都复用 takeover（candidates_checked / one_per_session / last_line / last_ask），
这里只加三件事：判断活没活着、排序与默认范围、给每行补「在干什么」和「多久前」。
"""

from __future__ import annotations

import time
from contextlib import suppress

from cca.config import AGENTS_RECENT_DAYS, AGENTS_TIMEOUT
from cca.session import takeover
from cca.session.watchdog import hits_limit, last_assistant_in

DEAD_STATES = ("stopped", "failed", "ended")
# 要人处理的状态：排最前
ATTENTION_STATES = ("blocked", "waiting", "quota")
DAY_SECONDS = 86400
# status 字段里只是状态词的不算「在干什么」
STATE_WORDS = ("busy", "idle", "working", "blocked", "waiting", "done", "stopped", "failed", "unknown")


def is_alive(row: dict) -> bool:
    """进程还在：登记里有 pid 且没停；或状态本身就是在跑 / 等人。done 且没 pid 只是历史。"""
    state = str(row.get("state") or "")
    if state in DEAD_STATES:
        return False
    return bool(row.get("pid")) or state in (*takeover.RUNNING_STATES, "blocked", "waiting", "idle")


def merge(rows: list) -> list:
    """同一会话在几个号上的记录合成一行。

    活着的那份为准（号 = 它现在所在的号）；都没活着的取最近的那份。
    `also_on` 记其余登记所在的号（旧号上的 stopped 之类），方便对照。
    """
    ordered = sorted(rows, key=lambda one: one.get("started_at") or 0, reverse=True)   # 新的在前，平手时取新
    groups: dict = {}
    for row in ordered:
        groups.setdefault(row["session_id"] or f"{row['account']}:{row['id']}", []).append(row)
    out = []
    for group in groups.values():
        alive = [one for one in group if is_alive(one)]
        chosen = takeover.one_per_session(alive)[0] if alive else group[0]   # 活着的里挑最像在用的那条
        others = sorted({one["account"] for one in group if one["account"] != chosen["account"]})
        out.append({**chosen, "alive": bool(alive), "also_on": others, "moved_from": _moved_from(chosen, group)})
    return out


MOVED_RECENT_SECONDS = 6 * 3600   # 换号后这么久内在总表里标「← 旧号」
MS_EPOCH = 1e11                   # 比这大的时间戳是毫秒（claude 登记里的 startedAt）


def _moved_from(chosen: dict, group: list) -> str:
    """刚换过号：活着的这份是最近几小时起的，且别的号上有更早的记录 → 那个号；否则空串。"""
    started = float(chosen.get("started_at") or 0) / (1000 if (chosen.get("started_at") or 0) > MS_EPOCH else 1)
    if not chosen.get("pid") or time.time() - started > MOVED_RECENT_SECONDS:
        return ""
    # 换号会把源号上的那份停掉（stopped）；别的状态的旧记录（done、残留登记）不算换号
    earlier = [one for one in group if one["account"] != chosen["account"] and one.get("state") == "stopped"
               and (one.get("started_at") or 0) <= (chosen.get("started_at") or 0)]
    return max(earlier, key=lambda one: one.get("started_at") or 0)["account"] if earlier else ""


def _active_at(row: dict) -> float:
    """最后活动：对话记录的修改时间；找不到就用登记的起始时间。"""
    path = takeover.transcript(row["session_id"]) if row.get("session_id") else None
    if path is not None:
        with suppress(OSError):
            return path.stat().st_mtime
    return float(row.get("started_at") or 0)


def _priority(row: dict) -> int:
    if row["state"] in ATTENTION_STATES:
        return 0
    if row["state"] in takeover.RUNNING_STATES:
        return 1
    return 2


def _doing(row: dict) -> str:
    """在干什么：登记里的状态说明，没有就是它最后说的一句，再没有就是人最后交给它的话。"""
    text = row.get("status_text") or ""
    if text and text not in STATE_WORDS:
        return " ".join(text.split())
    sid = row.get("session_id") or ""
    return takeover.last_line(sid) or takeover.last_ask(sid)


def build(*, show_all: bool = False, now: float | None = None, accounts: list | None = None) -> dict:
    """总表：{"rows": [...], "failed": [取不到的号], "hidden": 默认范围外被略去的条数}。"""
    now = time.time() if now is None else now
    rows, failed = takeover.candidates_checked(accounts, timeout=AGENTS_TIMEOUT)
    merged = merge(rows)
    for row in merged:
        row["active_at"] = _active_at(row)
    cutoff = now - AGENTS_RECENT_DAYS * DAY_SECONDS
    shown = [row for row in merged if show_all or row["alive"] or row["active_at"] >= cutoff]
    for row in shown:
        # 撞额度：活着但停在额度错误上，要人换号，算「要处理」
        if row["alive"] and row["state"] not in takeover.RUNNING_STATES and row["session_id"]:
            path = takeover.transcript(row["session_id"])
            if hits_limit(last_assistant_in(path)):
                row["state"] = "quota"
        row["title"] = row["name"] or row["id"]
        row["doing"] = _doing(row)
    shown.sort(key=lambda one: (_priority(one), -one["active_at"]))
    return {"rows": shown, "failed": failed, "hidden": len(merged) - len(shown)}
