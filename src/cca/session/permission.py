"""接管会话的“请确认”转到总控面板上点（Claude Code 官方的 PermissionRequest 钩子）。

流程：

1. 各号 settings.json 里配一个 PermissionRequest 钩子：`cca sideagent permission-hook`。
2. 会话要弹确认框时，Claude Code 先跑这个钩子，把会话编号、工具名、参数从标准输入给它。
3. 钩子查这个会话有没有被总控接管（~/.cca/sideagents/<名字>/state.json 里 kind=attached、
   session_id 对得上，而且至少一个主会话的名单 ~/.cca/sessions/*.json 里有它、主人还在）：
   - 没被接管：立刻无输出退出，Claude Code 照常弹原生确认框，和没装钩子一样。
   - 被接管：写一条待确认请求（~/.cca/sideagents/perm/<实例名>/<请求编号>.req.json），
     接管它的每个主会话的面板都会显示（M 对 N），谁先答算谁的；
     然后等结果文件（同目录 <请求编号>.res.json）。面板上按“允许/拒绝”就是写这个结果文件
     （`cca sideagent permission-answer <实例名> allow|deny`）。
4. 拿到结果就按官方格式输出 allow/deny；等满 WAIT_SECONDS（默认 600 秒）没人点，回 deny 并写明原因。

实测（2026-11-02，Claude Code 2.1.316）：
- 后台会话里也会跑这个钩子；会话启动后才加进 settings.json 的钩子也生效（文件监视热加载）。
- 钩子等着的时候，会话状态显示 waiting / permission prompt，乖乖等，不报错。
- 钩子超时被 Claude Code 杀掉时，不是拒绝，而是退回原生确认框。所以钩子自己的等待上限
  （WAIT_SECONDS）要比 settings.json 里配的 timeout（HOOK_TIMEOUT）短，到点自己回 deny。
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

PERM_DIR = Path("~/.cca/sideagents/perm").expanduser()
WAIT_SECONDS = float(os.environ.get("CCA_PERMISSION_WAIT", "600"))
HOOK_TIMEOUT = 660           # settings.json 里配的钩子超时（秒）：比 WAIT_SECONDS 长，让我们自己先回 deny
POLL_SECONDS = 0.3
HOOK_MARK = "sideagent permission-hook"   # 认出“我们装的那条”钩子
INPUT_LIMIT = 4000           # 请求文件里参数最多留多少字


def _taken_over_name(session_id: str) -> str:
    """这个会话被接管了就返回实例名（例：003#bg-36709e4a），没有就空串。

    接管的可以有好几个主人：只要有一个还在，就算被接管（规则 7：发给所有活着的主人，谁先答算谁的）。
    """
    from cca.session import roster, sideagent  # noqa: PLC0415 - 没被接管时尽量少导入

    root = sideagent.SIDEAGENT_DIR
    if not session_id or not root.is_dir():
        return ""
    for path in root.glob("*#bg-*/state.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(state, dict) and roster.kind_of(state) == roster.ATTACHED
                and state.get("session_id") == session_id and not state.get("finished_at")
                and roster.holders(path.parent.name)):
            return path.parent.name
    return ""


def _still_taken(name: str) -> bool:
    """等的时候还有没有主人挂着它：都放手了（state.json 写了 finished_at 或没了）就不再替人做主。"""
    from cca.session import sideagent  # noqa: PLC0415

    state = sideagent._read_state(name)  # noqa: SLF001
    return bool(state) and not state.get("finished_at")


def _summary(tool: str, tool_input: dict) -> str:
    """一句话说清要干什么：Bash(echo A > a.txt)、Edit(/w/a.py)。"""
    for key in ("command", "file_path", "path", "url", "pattern", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool}({' '.join(value.split())[:200]})"
    return tool


def _decision(behavior: str, message: str = "") -> dict:
    decision = {"behavior": behavior}
    if behavior == "deny" and message:
        decision["message"] = message
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


def pending(name: str) -> list:
    """这个实例还没回答的请求，按时间先后。面板和同步线程用。"""
    folder = PERM_DIR / name
    if not folder.is_dir():
        return []
    rows = []
    for path in folder.glob("*.req.json"):
        if (folder / path.name.replace(".req.json", ".res.json")).exists():
            continue
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: float(row.get("created") or 0))


def answer(name: str, behavior: str, request_id: str = "", by: str = "panel") -> str:
    """回答最早那条（或指定编号的）待确认请求。返回错误，空串是成功。"""
    if behavior not in ("allow", "deny"):
        return "只能是 allow 或 deny"
    rows = pending(name)
    if request_id:
        rows = [row for row in rows if row.get("id") == request_id]
    if not rows:
        return f"{name} 没有等确认的请求"
    row = rows[0]
    result = {"id": row["id"], "behavior": behavior, "by": by, "at": time.time()}
    target = PERM_DIR / name / f"{row['id']}.res.json"
    # 好几个主会话的面板都能答：谁先答算谁的。先写临时文件，再用硬链接占位（已经有人答了就失败）
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    try:
        os.link(temp, target)
    except FileExistsError:
        return f"{name} 这条请求已经有人答过了"
    finally:
        temp.unlink(missing_ok=True)
    return ""


def _wait(name: str, request: dict, deadline: float) -> dict:
    folder = PERM_DIR / name
    result_path = folder / f"{request['id']}.res.json"
    while time.time() < deadline:
        if result_path.exists():
            try:
                return json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        # 等的时候所有主人都放手了：不再替人做主，退回原生确认框
        if not _still_taken(name):
            return {"behavior": ""}
        time.sleep(POLL_SECONDS)
    return {"behavior": "deny", "timeout": True}


def _cleanup(name: str, request_id: str) -> None:
    folder = PERM_DIR / name
    for suffix in (".req.json", ".res.json"):
        (folder / f"{request_id}{suffix}").unlink(missing_ok=True)


def hook(stdin_text: str, *, wait_seconds: float = WAIT_SECONDS) -> str:
    """钩子本体：返回要打印到标准输出的文字（空串 = 不表态，照常弹原生确认框）。"""
    try:
        data = json.loads(stdin_text or "{}")
    except ValueError:
        return ""
    name = _taken_over_name(str(data.get("session_id") or ""))
    if not name:
        return ""
    tool = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    request = {
        "id": uuid.uuid4().hex[:12], "name": name, "session_id": data.get("session_id"),
        "tool": tool, "summary": _summary(tool, tool_input),
        "input": json.dumps(tool_input, ensure_ascii=False)[:INPUT_LIMIT],
        "created": time.time(), "pid": os.getpid(),
    }
    folder = PERM_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{request['id']}.req.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    try:
        result = _wait(name, request, time.time() + wait_seconds)
    finally:
        _cleanup(name, request["id"])
    behavior = str(result.get("behavior") or "")
    if behavior == "allow":
        return json.dumps(_decision("allow"))
    if behavior == "deny":
        why = (f"等了 {int(wait_seconds)} 秒，总控面板上没人确认，按拒绝处理" if result.get("timeout")
               else "总控面板上拒绝了这次操作")
        return json.dumps(_decision("deny", why), ensure_ascii=False)
    return ""


def hook_main() -> int:
    """`cca sideagent permission-hook`：任何意外都不表态，绝不因为钩子出错卡住别人的会话。"""
    try:
        out = hook(sys.stdin.read())
    except Exception:  # noqa: BLE001 - 钩子出错就当没装，退回原生确认框
        out = ""
    if out:
        print(out)  # noqa: T201 - 钩子协议：结果写标准输出
    return 0


# ---------------------------------------------------------------- 安装进各号 settings.json


def _hook_entry(command: str) -> dict:
    return {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT}]}


def _is_ours(group: dict) -> bool:
    return any(HOOK_MARK in str(item.get("command") or "") for item in group.get("hooks") or [])


def install_into(path: Path, command: str) -> bool:
    """往这个 settings.json 合并我们的钩子（已有就更新命令和超时）。先备份；返回有没有改。"""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    groups = data.setdefault("hooks", {}).setdefault("PermissionRequest", [])
    wanted = _hook_entry(command)
    if [group for group in groups if _is_ours(group)] == [wanted]:
        return False
    data["hooks"]["PermissionRequest"] = [group for group in groups if not _is_ours(group)] + [wanted]
    _backup(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def uninstall_from(path: Path) -> bool:
    """去掉我们装的那条，别的钩子不动；去完空了就把空键也删掉。返回有没有改。"""
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    groups = hooks.get("PermissionRequest") or []
    kept = [group for group in groups if not _is_ours(group)]
    if len(kept) == len(groups):
        return False
    if kept:
        hooks["PermissionRequest"] = kept
    else:
        hooks.pop("PermissionRequest", None)
    if not hooks:
        data.pop("hooks", None)
    _backup(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def _backup(path: Path) -> None:
    if path.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path.with_name(f"{path.name}.bak-permission-hook-{stamp}").write_bytes(path.read_bytes())
