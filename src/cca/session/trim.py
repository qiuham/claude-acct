"""会话裁剪：trim_plan / cmd_trim。"""

from __future__ import annotations

import contextlib
import json
import shutil
from typing import TYPE_CHECKING

from cca.cli import parse_flags
from cca.config import TRIM_KEEP_DEFAULT, TRIM_USAGE
from cca.errors import die, say
from cca.paths import accounts_root, shared_pool_root
from cca.session.push import is_running, live_index, resumable_sessions

if TYPE_CHECKING:
    from pathlib import Path

# `--keep` 的下限：再小就只剩一两轮，没有实用价值。
MIN_KEEP_TOKENS = 1000


def find_transcript(session_id: str):
    """按完整 id 在共享池里找 transcript 文件（找不到返回 None）。"""
    root = shared_pool_root()
    if not root.is_dir():
        return None
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        f = proj / f"{session_id}.jsonl"
        if f.is_file():
            return f
    return None

def trim_plan(path: Path, keep_tokens: int) -> dict:
    """算出"裁到哪里"：不改文件，只返回计划（调用方负责备份与落地）。

    token 估算用**最后一条真实 usage**（input + cache_read + cache_creation ≈ 当时的上下文）
    除以它之前的字节数，得到这条会话自己的 token/字节比 —— 比拍一个固定系数准。
    切点必须是**干净的 user 边界**（不是 tool_result 回填），否则会切断 tool_use/result 配对。
    """
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    off, ctx = 0, []
    for ln in lines:
        if b'"usage"' in ln:
            with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                usage = (json.loads(ln).get("message") or {}).get("usage") or {}
                total = (
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                )
                if total and off:
                    ctx.append((off, total))
        off += len(ln) + 1
    if not ctx:
        die("这条会话没有 token 记录（没有 usage 条目），不猜着裁；要清空上下文请用 /clear")
    base_off, base_tokens = ctx[-1]
    ratio = base_tokens / base_off
    want = len(raw) - keep_tokens / ratio
    off, cut = 0, None
    for i, ln in enumerate(lines):
        if cut is None and off >= want:
            try:
                entry = json.loads(ln)
            except (json.JSONDecodeError, UnicodeDecodeError):
                entry = {}
            content = (entry.get("message") or {}).get("content")
            is_tool_result = isinstance(content, list) and any(
                isinstance(x, dict) and x.get("type") == "tool_result" for x in content
            )
            if entry.get("type") == "user" and not is_tool_result:
                cut = i
        off += len(ln) + 1
    if cut is None:
        die(f"找不到干净的切点（--keep={keep_tokens} 可能太小）；调大一点再试")
    keep = lines[cut:]
    kept_bytes = sum(len(x) + 1 for x in keep)
    return {
        "path": path,
        "lines": len(lines),
        "cut": cut,
        "total_bytes": len(raw),
        "est_total": int(len(raw) * ratio),
        "kept_bytes": kept_bytes,
        "est_kept": int(kept_bytes * ratio),
        "payload": b"\n".join(keep),
    }

def cmd_trim(args: list) -> int:
    """把一条超限会话的历史裁到 `--keep` 以内（先备份，再按干净的 user 边界切）。

    用途：会话涨过端点上限后，**连压缩请求本身都发不出去**（压缩要把整段历史发给模型），
    这时唯一的出路是裁掉更早的部分。裁之前一律备份到 `<accounts>/_session-backups/`，
    裁完打印"原 N tokens → 现 M tokens"。正在跑的会话拒绝裁（进程还在写这个文件）。
    """
    rest, flags = parse_flags(
        args, bool_flags=("--dry-run", "--force"), value_flags=("--keep",), usage=TRIM_USAGE
    )
    if len(rest) != 1:
        die(f"要写清裁哪条会话。用法: {TRIM_USAGE}")
    try:
        keep = int(flags.get("--keep") or TRIM_KEEP_DEFAULT)
    except ValueError:
        die(f"--keep 要是 token 数（收到 {flags.get('--keep')!r}）")
    if keep < MIN_KEEP_TOKENS:
        die("--keep 太小（至少 1000 tokens），别把会话裁成只剩一两轮")

    term = rest[0].strip()
    items = resumable_sessions(days=0)
    exact = [s for s in items if s["id"] == term]
    hits = exact or [s for s in items if s["id"].startswith(term.lower()) or s["short"].startswith(term.lower())]
    if not hits:
        die(f"没有匹配 {term!r} 的会话；cca sessions 看共享池清单")
    if len(hits) > 1:
        for c in hits[:20]:
            say(f"   {c['id']}  {c['title'] or '(无标题)'}")
        die(f"{term!r} 匹配到 {len(hits)} 条会话，请给更长的前缀或完整 id")

    session = hits[0]
    path = find_transcript(session["id"])
    if path is None:
        die(f"找不到 {session['id']} 的 transcript 文件")

    # 安全闸：只要有后台登记就不动这个文件 —— 活着的进程手里是裁剪前的历史，它会继续往裁过的
    # 文件里追加，两边都会坏掉。正在跑的硬拒绝；挂起/失败这类先 stop 再来，确认登记是残留
    # （进程已退出）时用 --force 放行。
    live = live_index().get(session["short"], [])
    running = [f"{p}({st})" for p, st in live if is_running(st)]
    if running:
        die(
            f"{session['short']} 正在跑（{'、'.join(running)}）—— 进程还在写这个文件，裁了会被覆盖；"
            f"先 cca <号> stop {session['short']}，或用 cca <号> attach {session['short']} 进去 /clear"
        )
    if live and not flags.get("--force"):
        stayed = "、".join(f"{p}({st})" for p, st in live)
        die(
            f"{session['short']} 还有后台登记（{stayed}）：登记活着就可能有进程握着裁剪前的历史。"
            f"先 cca <号> stop {session['short']} 再裁；确认那条登记是残留（进程已退出）时加 --force"
        )

    plan = trim_plan(path, keep)
    say(
        f"{session['id']}  {session['title'] or '(无标题)'}\n"
        f"  现在: {plan['total_bytes']/1048576:.1f}MB / {plan['lines']} 条 ≈ {plan['est_total']:,} tokens\n"
        f"  裁后: {plan['kept_bytes']/1048576:.1f}MB / {plan['lines']-plan['cut']} 条 ≈ {plan['est_kept']:,} tokens"
        f"（从第 {plan['cut']} 条这个干净的 user 边界切起）"
    )
    if flags.get("--dry-run"):
        say("--dry-run：以上是计划，没有备份、没有改文件")
        return 0

    backup_dir = accounts_root() / "_session-backups"
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = backup_dir / path.name
    shutil.copy2(path, backup)
    path.write_bytes(plan["payload"])
    say(f"  已备份: {backup}")
    say(f"  已重写: {path}")
    say(f"接回去: cca <号> --resume {session['id']}（若它还挂在别的号上，先 cca <号> stop {session['short']}）")
    return 0
