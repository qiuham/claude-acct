"""会话记录的小修小补：跨家族搬家前，把 Anthropic 不认的字段去掉。

Gemini 那条网关会把 thinking 的 `signature` 写进 `tool_use`；Gemini 自己收，
搬到 Claude 号上整段历史重放时会被拒：

    API Error: 400 messages.810.content.1.tool_use.signature: Extra inputs are not permitted

但反过来，Gemini 的函数调用要靠这个签名接续，删了它 Gemini 下一轮会
400（Function call is missing a thought_signature）。所以**只搬到非 Gemini 号**时才清。
只删 `tool_use` 块里的 `signature`，`thinking` 块的签名原样保留（Claude 需要它）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from cca.errors import note

# 短编号（前 8 位）才拿来当前缀匹配，再短容易撞车
SHORT_ID_LEN = 8


def resume_id(claude_args: list) -> str:
    """从 claude 参数里取续会话的编号：`--resume <id>` / `--resume=<id>` / `-r <id>` / `-r=<id>`。"""
    for index, arg in enumerate(claude_args):
        text = str(arg)
        for flag in ("--resume", "-r"):
            if text == flag and index + 1 < len(claude_args):
                return str(claude_args[index + 1])
            if text.startswith(flag + "="):
                return text.split("=", 1)[1]
    return ""


def find_transcript(session_id: str, projects_dir: Path | None = None) -> Path | None:
    """按会话编号找对话记录（完整编号优先，短编号要求唯一）；多副本取最新改写的那份。

    传了 `projects_dir` 就在这个号自己的池子里找（隔离号的项目目录是实体目录），
    否则退回共享池。软链要 resolve：改写时不能把软链换成普通文件。
    """
    if not session_id:
        return None
    root = projects_dir or Path("~/.claude/projects").expanduser()
    hits = sorted(root.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not hits and len(session_id) >= SHORT_ID_LEN:
        prefix = sorted(root.glob(f"*/{session_id}*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
        hits = prefix if len(prefix) == 1 else []   # 撞了多条就不动，宁可不清
    return hits[0].resolve() if hits else None


def _sidecars(path: Path) -> list:
    """主记录旁边的子代理记录：`<会话id>/subagents/agent-*.jsonl`。"""
    folder = path.with_suffix("")
    return sorted(folder.rglob("*.jsonl")) if folder.is_dir() else []


def strip_session(session_id: str, projects_dir: Path | None = None) -> int:
    """主记录和子代理记录一起清，返回删掉的总处数。

    子代理各有自己的 transcript，里面也可能带 Gemini 写的 `tool_use.signature`；
    只清主文件，子代理续跑时照样 400。
    """
    path = find_transcript(session_id, projects_dir)
    if path is None:
        return 0
    total = strip_tool_use_signature(path)
    for sidecar in _sidecars(path):
        total += strip_tool_use_signature(sidecar)
    return total


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                     # 别人名下的进程，存在但没权限发信号
    return True


def session_live(session_id: str) -> bool:
    """这条会话还有活着的 claude 进程吗（各号登记表里登记的 pid）。

    活着的进程手里是改写前的历史，它会继续往旧 inode 追加——这时候覆盖文件，
    这些新回合会跟着旧文件一起消失。宁可这次不清，也别丢。
    """
    from cca.paths import accounts_root  # noqa: PLC0415 - 只在需要时读登记表

    root = accounts_root()
    if not session_id or not root.is_dir():
        return False
    for path in root.glob("*/sessions/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        registered = str(data.get("sessionId") or "")
        if not registered or not session_id:
            continue
        matched = (registered == session_id
                   or (len(session_id) >= SHORT_ID_LEN and registered.startswith(session_id)))
        if matched and _alive(data.get("pid")):
            return True
    return False


def target_needs_strip(account: str) -> bool:
    """搬到这个号要不要清签名：目标还是 Gemini 就不用（它要靠签名接续）。"""
    from cca.account.store import profile_kind, read_marker  # noqa: PLC0415

    if profile_kind(account) != "api":
        return True                     # 订阅号都是 Anthropic
    marker, _error = read_marker(account)
    env = (marker or {}).get("env") or {}
    text = " ".join(str(env.get(key) or "") for key in
                    ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_BASE_URL"))
    text += " " + str((marker or {}).get("provider") or "")
    return "gemini" not in text.lower()


def strip_tool_use_signature(path: Path | None) -> int:
    """删掉 `tool_use` 块里的 `signature`，返回删掉的处数。

    没有这种字段时不动文件（返回 0）。要改就先在同一个目录留一份
    `<文件名>.bak-signature-<时间>`（保留原权限），再用同目录临时文件换名写回、
    保持原文件的 0600。任何 I/O 失败都只提示一句，不让它挡住启动。
    """
    if path is None or not path.is_file():
        return 0
    try:
        raw = path.read_text(encoding="utf-8", errors="surrogateescape")
        mode = path.stat().st_mode
    except OSError as exc:
        note(f"读不了 {path.name}，没清 tool_use.signature：{exc}")
        return 0
    # 只按真正的换行切：splitlines() 还会按 U+2028 / U+2029 切，
    # 而这些字符可以合法出现在 JSON 字符串里，会把一条记录劈成两半。
    pieces = raw.split("\n")
    out, stripped = [], 0
    for index, piece in enumerate(pieces):
        line = piece + ("\n" if index < len(pieces) - 1 else "")
        if '"tool_use"' not in line or '"signature"' not in line:
            out.append(line)
            continue
        try:
            record = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and "signature" in block:
                block.pop("signature")
                stripped += 1
        out.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    if not stripped:
        return 0
    backup = path.with_name(f"{path.name}.bak-signature-{time.strftime('%Y%m%d-%H%M%S')}")
    temporary = None
    try:
        shutil.copy2(path, backup)      # 备份保留原权限
        handle, name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".new")
        temporary = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write("".join(out))
            fh.flush()
            os.fsync(fh.fileno())
        temporary.chmod(mode & 0o7777)
        temporary.replace(path)
    except (OSError, UnicodeError) as exc:
        note(f"{path.name} 没清成 tool_use.signature（原文没动，下次再试）：{exc}")
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return 0
    return stripped
