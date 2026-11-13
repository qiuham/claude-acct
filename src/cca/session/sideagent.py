"""跨号sideagent：在一个常驻的 claude 进程里连续指挥另一个号。

为什么不是反复 `-p`：每叫一次都要冷启动、重新读会话。这里用 claude 自己的
`--print --input-format stream-json --output-format stream-json` 模式，进程不退出，
从标准输入收消息、往标准输出吐事件，上下文一直在。全程是官方参数，不改客户端。

用法：

    cca sideagent start 002                 # 起一个sideagent（默认闲置 30 分钟后自己退）
    cca sideagent send 002 「先看下 tests/」  # 发一句，等它干完，打印回复
    cca sideagent send 002 「接着修第 3 个」   # 同一个会话，接上文
    cca sideagent ls                        # 看有哪些sideagent在跑
    cca sideagent stop 002                  # 停掉

    cca sideagent 002 「顺手看眼 README」      # 没起就起，然后发

sideagent 一个一个文件夹：`~/.cca/sideagents/<名字>/`（state.json 等），控制用的本地套接字在
它的 run/sock，只在跑的时候有。套接字权限 600，只有当前用户能连。
哪个主会话用着哪些 sideagent 记在 `~/.cca/sessions/<主会话编号>.json`（见 roster.py）。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

from cca.errors import die, note, say
from cca.paths import profile_dir

SIDEAGENT_DIR = Path("~/.cca/sideagents").expanduser()
DEFAULT_IDLE_MINUTES = 30
SEND_TIMEOUT = 600
# 流式文字最多多久写一次盘（秒）
LIVE_WRITE_INTERVAL = 0.2
# 进度往管道推的最短间隔（秒）
PUSH_MIN_INTERVAL = 0.3
# 一次性的sideagent干完后留多久，界面好显示结果
LINGER_SECONDS = 15
QUEUE_LIMIT = 20
JOB_HISTORY = 20      # 每个实例留最近几条后台任务
EVENTS_MAX_BYTES = 4_000_000
# 一轮里多久没有任何输出就认定卡住。为什么是 900：Claude Code 自己会重试 API 错误，单次尝试
# 最长的静默是等首字节——首次约 190 秒，重试时可到 599 秒（api_retry 的 no_response.retry_wait_ms），
# 再加退避。门槛必须比这个大，否则 CC 还在重试、cca 就先放弃了（以前 300 秒，慢网关上常被误判）。
# CC 重试时每次失败都会输出一行 api_retry，会刷新计时；900 秒没动静才是进程真的没反应。
STALL_SECONDS = int(os.environ.get("CCA_SIDEAGENT_STALL", "900"))
NOTIFY_RESULT_CHARS = 20000   # 回报里的结果最多带多少字（子代理带全文；太长的消息通道会拒）
SERVE_CODE = "from cca.session.sideagent import serve; serve()"   # 守护进程的启动命令
SUMMARY_INTERVAL = float(os.environ.get("CCA_SIDEAGENT_SUMMARY_INTERVAL", "30"))  # 进度短语多久写一次
STREAM_CHARS_PER_TOKEN = 4   # 正在输出时估词元：4 个字符算 1 个
STREAM_SAVE_EVERY = 50       # 估计值每涨这么多词元写一次状态
FOLLOWUP_GRACE = 20   # 插话另开的一轮，多少秒没输出就认定没有这一轮
SOCKET_TIMEOUT = 2
START_TIMEOUT = 120
TWO_ARGS = 2
# 只允许这些动作走子命令；其余第一个词当号名。
ACTIONS = ("start", "send", "stop", "ls", "status")
# sideagent固定要传的 claude 参数。
STREAM_ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages")
# 代码版本：这份文件内容的哈希。serve 起来时写进状态，查状态时比对，
# 对不上就说明是改代码前起的老进程，要重启才用上新功能。
# 代码版本：常驻进程拆成了几个文件（sideagent_*.py），哪个改了都算新版本
CODE_VERSION = hashlib.sha256(b"".join(
    path.read_bytes() for path in sorted([Path(__file__), Path(__file__).parent / "summary.py",
                                          *Path(__file__).parent.glob("sideagent_*.py")])
)).hexdigest()[:10]
# sideagent自己消费的 flag，不传给 claude。
OWN_FLAGS = ("--cwd", "--idle", "--timeout", "--context")
# 不带值的sideagent自有开关。
BOOL_FLAGS = ("--keep", "--json", "--interject")


def _config_dir() -> Path:
    """主会话的配置目录。sideagent 只能在 cca 起的会话里用，所以必须有这个变量。"""
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    if not folder:
        raise SystemExit("sideagent 只能在 cca 起的会话里用：当前没有 CLAUDE_CONFIG_DIR")
    return Path(folder)


def _ensure_inbound_setting() -> None:
    """主会话没开自动接收就补上：否则sideagent的汇报要手动批准。

    只补缺的那一项；用户明确写了别的值就只提醒，不改。
    配置目录由 CLAUDE_CONFIG_DIR 决定（cca 起的会话一定有）。
    """
    path = _config_dir() / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        note(f"{path} 读不了，没补 crossSessionInbound：sideagent汇报会要求手动确认。")
        return
    if not isinstance(data, dict):
        return
    current = data.get("crossSessionInbound")
    if current == "accept":
        return
    if current is not None:
        note(f"主会话的 crossSessionInbound 是 {current!r}：sideagent汇报会要求手动确认。")
        return
    data["crossSessionInbound"] = "accept"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except OSError:
        note(f"写不进 {path}：sideagent汇报会要求手动确认。")
        return
    note(f'已把 "crossSessionInbound": "accept" 写进 {path}（重启会话后生效）。')


def _context_session(context: str) -> str:
    """把 --context 的值换成会话编号：auto 用当前会话，none 不用。"""
    if context in ("", "none", "0"):
        return ""
    if context == "auto":
        # 当前会话的编号到登记表现查（会话被继续、恢复后环境变量里的是旧值）
        sid = str(caller_now().get("sessionId") or "") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
        if not sid:
            die("不在 Claude 会话里，--context auto 拿不到编号；请写完整会话编号")
        return sid
    return context


def _expose_transcript(name: str, sid: str) -> str:
    """让sideagent的配置目录能看见这段会话记录。

    普通号的 projects 是共享的，本来就看得见；隔离号（如 ds2）才需要补一个链接。
    """
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    if not sid or not folder:
        return ""
    projects = [Path(folder) / "projects", Path.home() / ".claude" / "projects"]
    source = None
    for base in projects:
        source = next(iter(base.glob(f"*/{sid}.jsonl")), None)
        if source is not None:
            break
    if source is None:
        die(f"找不到会话 {sid} 的记录（查过 {'、'.join(map(str, projects))}）")
    session_cwd = _session_cwd(source)
    account_projects = profile_dir(_account_of(name)) / "projects"
    if not account_projects.exists():
        return session_cwd
    if account_projects.resolve() == source.parent.parent.resolve():
        return session_cwd           # 同一个共享目录，不用动
    target_dir = account_projects / source.parent.name
    target_dir.mkdir(parents=True, exist_ok=True)
    link = target_dir / f"{sid}.jsonl"
    if not link.exists():
        with suppress(OSError):
            link.symlink_to(source)
    state = _read_state(name)
    state["context_link"] = str(link)
    _write_state(name, state)
    return session_cwd


def _session_cwd(path: Path) -> str:
    """会话记录里记的工作目录：resume 只在这个目录对应的会话里找。"""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("cwd"):
                    return str(entry["cwd"])
    except OSError:
        return ""
    return ""


def _caller_sessions() -> Path:
    """调用方（主会话）的登记表目录。

    登记表在配置目录下的 sessions/，由 CLAUDE_CONFIG_DIR 决定。
    """
    return _config_dir() / "sessions"


def _clip_input(data):
    """工具参数：保留结构，长字符串截断，界面要用来画 diff 和文件内容。"""
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        out[key] = value[:4000] if isinstance(value, str) else value
    return out


def _tool_summary(data) -> str:
    """从一个工具调用的参数里挑一句最短的说明。"""
    if not isinstance(data, dict):
        return ""
    skill = data.get("skill")
    if isinstance(skill, str) and skill.strip():
        # Skill 调用：写成用户敲的样子 `/code-review --high`
        args = " ".join(str(data.get("args") or "").split())
        return ("/" + skill.strip() + (" " + args if args else ""))[:80]
    for key in ("command", "file_path", "path", "pattern", "url", "query"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:80]
    description = data.get("description")
    if isinstance(description, str) and description.strip():
        return " ".join(description.split())[:80]
    return ""


TASK_SUBTYPES = ("task_started", "task_progress", "task_updated", "task_notification")


# 工作流进度里面板要用的字段；提示词开头、结果开头这类大字段不留
_WORKFLOW_KEYS = ("type", "index", "title", "label", "phaseIndex", "phaseTitle", "state", "agentId",
                  "model", "attempt", "lastToolName", "lastToolSummary", "tokens", "toolCalls",
                  "durationMs", "error", "startedAt")
# 一个工作流最多记这么多项（阶段 + 子代理），防止异常输入撑爆文件
WORKFLOW_MAX_ITEMS = 2000
_AGENT_RUNNING = ("start", "progress")


def _clip_progress(value):
    """工作流各阶段进度：Claude Code 每次给一份完整清单（阶段 + 每个子代理一项），全留，只砍掉大字段。"""
    if not isinstance(value, list):
        return None
    out = []
    for item in value[:WORKFLOW_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        out.append({key: (item[key][:160] if isinstance(item[key], str) else item[key])
                    for key in _WORKFLOW_KEYS
                    if key in item and (isinstance(item[key], (str, int, float, bool)) or item[key] is None)})
    return out


def workflow_counts(items: list) -> dict:
    """完整清单 → 面板底部那一行要的数：几个子代理、做完几个、出错几个、在跑几个、现在在哪个阶段。

    当前阶段：还有子代理没做完的阶段里最靠后的那个；都做完了就取最后一个有子代理的阶段。
    """
    phases = [str(item.get("title") or "") for item in items if item.get("type") == "workflow_phase"]
    agents = [item for item in items if item.get("type") == "workflow_agent"]
    counts = {"total": len(agents),
              "done": sum(1 for item in agents if item.get("state") == "done"),
              "error": sum(1 for item in agents if item.get("state") == "error"),
              "running": sum(1 for item in agents if item.get("state") in _AGENT_RUNNING)}
    open_ = [item for item in agents if item.get("state") not in ("done", "error")]
    pool = open_ or agents
    if pool:
        last = max(pool, key=lambda item: (int(item.get("phaseIndex") or 0), int(item.get("index") or 0)))
        counts["phase"] = str(last.get("phaseTitle") or "")
    if phases:
        counts["phases"] = phases
    return counts


def workflow_path(name: str, task_id: str) -> Path:
    """一个工作流最新的完整进度：sideagents/<名字>/workflows/<任务编号>.json，每次覆盖。"""
    safe = "".join(ch for ch in task_id if ch.isalnum() or ch in "-_") or "unknown"
    return agent_dir(name) / "workflows" / f"{safe}.json"


def task_event(event: dict) -> dict | None:
    """Claude Code 的后台任务事件（子代理、后台命令、监视、工作流）→ 事件文件里的一行 bgtask。

    原生界面输入框下面那几行就是按这些事件分类的（task_type），面板照着画，不再按工具名猜。
    """
    subtype = event.get("subtype")
    if subtype not in TASK_SUBTYPES:
        return None
    entry = {"kind": "bgtask", "subtype": subtype, "task_id": str(event.get("task_id") or "")}
    for key in ("task_type", "tool_use_id", "subagent_type", "last_tool_name", "workflow_name",
                "parent_task_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            entry[key] = value[:200]
    for key, limit in (("description", 200), ("summary", 300)):
        value = event.get(key)
        if isinstance(value, str) and value:
            entry[key] = " ".join(value.split())[:limit]
    if isinstance(event.get("is_backgrounded"), bool):
        entry["is_backgrounded"] = event["is_backgrounded"]
    status = event.get("status") or (event.get("patch") or {}).get("status")
    if isinstance(status, str) and status:
        entry["status"] = status
    usage = event.get("usage")
    if isinstance(usage, dict):
        entry["usage"] = {key: usage[key] for key in ("total_tokens", "tool_uses", "duration_ms")
                          if isinstance(usage.get(key), (int, float))}
    progress = _clip_progress(event.get("workflow_progress"))
    if progress:
        # 事件文件里只记几个数；完整清单由写事件的人另存一份（见 workflow_path），不往事件文件里追加
        entry["workflow_counts"] = workflow_counts(progress)
        entry["workflow_progress"] = progress
    return entry


def _account_of(name: str) -> str:
    """sideagent名字后面可能带 `#哈希`：`ds#a1b2c3` 是 ds 号上临时起的一个sideagent。

    同一个号可以同时跑好几个，靠哈希区分，互相不撞。
    """
    return name.split("#", 1)[0]


def account_of(name: str) -> str:
    """实例名（ds#ff2832）属于哪个号（ds）。"""
    return _account_of(name)


def transcript_exists(name: str, session_id: str) -> bool:
    """这个实例的 claude 会话记录还在不在：在它那个号的 projects/*/<会话编号>.jsonl。"""
    projects = profile_dir(_account_of(name)) / "projects"
    return bool(session_id) and any(projects.glob(f"*/{session_id}.jsonl"))


def _oneshot_name(account: str) -> str:
    """临时sideagent的名字：号 + 6 位哈希。"""
    return f"{account}#{uuid.uuid4().hex[:6]}"


# 一个 sideagent 一个文件夹（见 docs/specs/sideagent-mn.md）：
#   sideagents/<名字>/state.json、events.jsonl、progress、summary.log、result.json、log、lock
#   sideagents/<名字>/workflows/<任务编号>.json  工作流最新的完整进度（每次覆盖）
#   sideagents/<名字>/run/sock、run/live   只在跑的时候有，停了整个 run/ 删掉
# 谁用着它不记在这里，记在主会话自己的名单 ~/.cca/sessions/<会话编号>.json（见 roster.py）。


def agent_dir(name: str) -> Path:
    """这个 sideagent 的文件夹。"""
    return SIDEAGENT_DIR / name


def _run_dir(name: str) -> Path:
    """只在跑的时候有的东西（套接字、正在生成的文字）：停了整个删掉。"""
    return agent_dir(name) / "run"


def _state_path(name: str) -> Path:
    return agent_dir(name) / "state.json"


def _socket_path(name: str) -> Path:
    return _run_dir(name) / "sock"


def _log_path(name: str) -> Path:
    return agent_dir(name) / "log"


def _progress_path(name: str) -> Path:
    return agent_dir(name) / "progress"


def _summary_log_path(name: str) -> Path:
    """进度短语的调用记录：一行一次。"""
    return agent_dir(name) / "summary.log"


def _lock_path(name: str) -> Path:
    """跨进程锁：改 result.json、同步接管的会话都用它。

    放在文件夹顶上、不放 run/：停了以后 MCP 还要加锁改 result.json，放 run/ 会让 run/ 又冒出来。
    """
    return agent_dir(name) / "lock"


def agent_names(*, include_finished: bool = True) -> list:
    """有 state.json 的 sideagent 名字（perm/ 这类不是 sideagent 的文件夹没有它）。

    include_finished 为假时只要还没停的（state.json 里没写 finished_at）。
    """
    if not SIDEAGENT_DIR.is_dir():
        return []
    names = []
    for path in sorted(SIDEAGENT_DIR.glob("*/state.json")):
        name = path.parent.name
        if not include_finished and _read_state(name).get("finished_at"):
            continue
        names.append(name)
    return names


def mark_finished(name: str, at: float = 0.0) -> None:
    """停了：删 run/，state.json 写 finished_at，其余留着（规则 8）。没有 state.json 的不新建。"""
    import shutil  # noqa: PLC0415

    with suppress(OSError):
        shutil.rmtree(_run_dir(name))
    state = _read_state(name)
    if not state:
        return
    if not state.get("finished_at"):
        state["finished_at"] = at or time.time()
    state["busy"] = False
    _write_state(name, state)


def _account_from_config() -> str:
    """当前会话属于哪个号：配置目录的目录名。"""
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    path = Path(folder) if folder else None
    if path is None or path.parent.name != ".claude-accounts":
        raise SystemExit("认不出当前号：这个会话不是 cca 起的（没有 CLAUDE_CONFIG_DIR）")
    return path.name


def _parent_pid(pid: int) -> int:
    """读 /proc/<pid>/stat 的第 4 项：父进程号。读不到就是 0。"""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    # 第 2 项是进程名，带括号，里面可能有空格：从最后一个右括号往后数
    fields = text[text.rfind(")") + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def caller_entry_by_pid(sessions: Path, start_pid: int = 0, *, depth: int = 8) -> dict:
    """顺着父进程往上找主会话：第一个在登记表里有 <pid>.json 的进程就是主会话的 claude。

    登记表的文件名就是 claude 进程号，里面的 sessionId 和 messagingSocketPath 是它**现在**的值：
    会话被继续、恢复以后编号会变，环境变量里的编号却是旧的，所以每次都现查。
    """
    pid = start_pid or os.getppid()
    for _ in range(depth):
        if pid <= 1:
            break
        path = sessions / f"{pid}.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                return {**data, "pid": int(data.get("pid") or pid)}
        pid = _parent_pid(pid)
    return {}


def caller_now() -> dict:
    """调用方（MCP 服务所在的主会话）现在的登记条目；找不到是空字典。"""
    return caller_entry_by_pid(_caller_sessions())


def owner_of(entry: dict) -> dict:
    """归属：派活的主会话是哪个 claude 进程（进程号 + 进程启动时刻），外加它当时的会话编号。

    会话编号只是“最近一次看到的值”，用来认重开后的新进程；真正的来源是登记表。
    """
    return {"pid": int(entry.get("pid") or 0), "proc_start": str(entry.get("procStart") or ""),
            "session": str(entry.get("sessionId") or "")}


def _pid_alive(pid: int) -> bool:
    """进程还在不在（登记表里死掉的进程的文件会留着，不能只看文件在不在）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _session_dirs(first: Path) -> list:
    """要找的登记表目录：先找派活那个号的，再找其余每个号的（换号重开后，新进程登记在新号下）。"""
    from cca.account.model import discover_profiles  # noqa: PLC0415 - 避免循环导入

    dirs = [first]
    for name in discover_profiles():
        path = profile_dir(name) / "sessions"
        if path not in dirs:
            dirs.append(path)
    return dirs


TRANSCRIPTS_DIR = Path("~/.claude/projects").expanduser()   # 各号的 projects 都链到这里
CONTINUED_TAIL_BYTES = 65536
CHAIN_LIMIT = 20
_continued_cache: dict = {}   # 会话编号 → (对话记录大小, 接着在哪个会话里跑)


def continued_in(session: str) -> str:
    """这段对话被 Claude Code 挪到了哪个新会话（/bg、← 挪后台都会复制成新编号接着跑）。

    依据：原来那份对话记录的最后会写一行 {"type":"continued-in","continuedInSessionId":...}。
    这是 Claude Code 内部格式（2.1.278 前后才有），只当线索；找不到就是空串。按文件大小缓存，
    文件没变就不重读；只读末尾一段。
    """
    if not session:
        return ""
    hits = sorted(TRANSCRIPTS_DIR.glob(f"*/{session}.jsonl"))
    if not hits:
        return ""
    path = hits[0]
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    cached = _continued_cache.get(session)
    if cached and cached[0] == size:
        return cached[1]
    found = ""
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - CONTINUED_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        tail = []
    for raw in reversed(tail):
        if '"continued-in"' not in raw:
            continue
        with suppress(ValueError):
            item = json.loads(raw)
            if item.get("type") == "continued-in":
                found = str(item.get("continuedInSessionId") or "")
                break
    _continued_cache[session] = (size, found)
    return found


def _cca_lineage() -> dict:
    """cca 自己记的换号流水（看门狗把终端会话复制到别的号时写，编号会变）：{旧编号: 新编号}。

    Claude Code 自己挪后台会写 continued-in；看门狗用 `--bg --resume` 复制出来的新会话没有，
    所以看门狗搬完往 ~/.cca/sideagents/lineage.jsonl 记一行，这里读回来，同一行里新旧编号相同的跳过。
    """
    path = SIDEAGENT_DIR / "lineage.jsonl"
    links: dict = {}
    with suppress(OSError):
        for raw in path.read_text(encoding="utf-8").splitlines():
            with suppress(ValueError):
                item = json.loads(raw)
                old, new = str(item.get("from") or ""), str(item.get("to") or "")
                if old and new and old != new:
                    links[old] = new
    return links


def session_chain(session: str) -> list:
    """顺着接力记录往后走：[原来的, 接着的, 再接着的, ...]，最后一个是最新的那一份。

    先看 Claude Code 写的 continued-in，没有再看 cca 自己记的换号流水。
    """
    chain = [session] if session else []
    links = None
    while chain and len(chain) < CHAIN_LIMIT:
        nxt = continued_in(chain[-1])
        if not nxt:
            links = _cca_lineage() if links is None else links
            nxt = links.get(chain[-1], "")
        if not nxt or nxt in chain:
            break
        chain.append(nxt)
    return chain


def latest_session(session: str) -> str:
    """这段对话现在最新的那一份的编号（没被挪过就是它自己）。"""
    chain = session_chain(session)
    return chain[-1] if chain else ""


def _alive_entries(dirs: list) -> list:
    """各号登记表里还活着的进程条目。"""
    out = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            pid = int(data.get("pid") or (path.stem if path.stem.isdigit() else 0))
            if _pid_alive(pid):
                out.append({**data, "pid": pid})
    return out


def resolve_owner(sessions: Path, owner: dict) -> dict:
    """按归属在登记表里找主会话现在的条目（唯一的来源：<配置目录>/sessions/<进程号>.json）。

    1. 那个进程还活着：<进程号>.json 存在、进程启动时刻对得上（防进程号被别的程序复用），
       里面的会话编号和 socket 就是现在的值（会话被继续、恢复后编号会变）。
    2. 那个进程没了、主会话重开成新进程：在每个号的登记表里找会话编号等于最近一次看到的那个、
       而且进程还活着的条目，就是继续了同一段对话的新进程。先找派活那个号，再找别的号：
       换号重开（003 → 002）后，新进程登记在 002 下。死掉的进程的登记文件会留着，一律跳过。
    都找不到就是空字典，调用方不投递并写明原因。
    """
    dirs = _session_dirs(sessions)
    session = str(owner.get("session") or "")
    alive = None
    # 0. 对话被挪走了（/bg 等会复制成新编号接着跑）：先找最新那一份还活着的进程，原来的进程可能还开着
    chain = session_chain(session)
    if len(chain) > 1:
        alive = _alive_entries(dirs)
        for sid in reversed(chain[1:]):
            for entry in alive:
                if entry.get("sessionId") == sid:
                    return entry
    pid = int(owner.get("pid") or 0)
    if pid > 0 and _pid_alive(pid):
        start = str(owner.get("proc_start") or "")
        for path in dirs:
            entry = caller_entry_by_pid(path, pid, depth=1)
            if entry and (not start or str(entry.get("procStart") or "") == start):
                return _follow_parked(entry, dirs)
    if not session:
        return {}
    alive = alive if alive is not None else _alive_entries(dirs)
    for entry in alive:
        if entry.get("sessionId") == session:
            return _follow_parked(entry, alive)
    return {}


def _follow_parked(entry: dict, dirs_or_alive: list) -> dict:
    """接力记录不在时的退路：这个进程把对话停到了后台（登记里有 parkedJobId），就跟到那个后台会话。"""
    parked = str(entry.get("parkedJobId") or "")
    if not parked:
        return entry
    alive = dirs_or_alive if dirs_or_alive and isinstance(dirs_or_alive[0], dict) else _alive_entries(dirs_or_alive)
    for other in alive:
        if str(other.get("jobId") or "") == parked or str(other.get("sessionId") or "").startswith(parked):
            return other
    return entry


def _owner_account() -> str:
    return os.environ.get("CCA_OWNER_ACCOUNT") or _account_from_config()


def _stream_path(session_id: str) -> Path:
    """主会话的进度管道：守护进程写，界面读。"""
    return SIDEAGENT_DIR / f"stream-{session_id}.fifo"


def _push_line(session_id: str, text: str) -> None:
    """往管道写一行；没有读者就丢掉，不阻塞。"""
    if not session_id:
        return
    path = _stream_path(session_id)
    with suppress(OSError):
        if not path.exists():
            SIDEAGENT_DIR.mkdir(parents=True, exist_ok=True)
            os.mkfifo(path, 0o600)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    with suppress(OSError):
        os.write(fd, (text + "\n").encode("utf-8"))
    os.close(fd)


def _live_path(name: str) -> Path:
    """正在生成的那段文字，边写边更新。"""
    return _run_dir(name) / "live"


def _events_path(name: str) -> Path:
    """一步一行 JSON，界面用来还原完整过程。"""
    return agent_dir(name) / "events.jsonl"


def _tool_output(block: dict) -> str:
    """工具结果的内容：可能是字符串，也可能是一串块。"""
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts = [str(item.get("text") or "")
             for item in content or [] if isinstance(item, dict)]
    return "".join(parts)


def _read_state(name: str) -> dict:
    path = _state_path(name)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(name: str, data: dict) -> None:
    """先写临时文件再换名：同时有人读，读到的要么是旧的、要么是新的，不会是空的。

    以前直接覆盖写：别的线程正好在读，读到空文件当成 {}，改一项再写回去，
    pid、会话编号、代码版本就全丢了（ds#1dafcc 就是这样“看起来退出了”）。
    """
    path = _state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with suppress(OSError):
        temp.chmod(0o600)
    temp.replace(path)


RECORD_KEYS = ("session_id", "code_version", "cwd", "label", "description", "model", "caller_agent")


def _model_arg(args: list) -> str:
    """claude 参数里的 --model 值（追问已退出的实例时照原样接着用）。"""
    for index, item in enumerate(args):
        if item == "--model" and index + 1 < len(args):
            return str(args[index + 1])
        if str(item).startswith("--model="):
            return str(item).split("=", 1)[1]
    return ""


def _record_of(state: dict) -> dict:
    return {key: state.get(key) for key in RECORD_KEYS}


def running(name: str) -> bool:
    """有进程在、套接字能连上，才算在跑。"""
    pid = int(_read_state(name).get("pid") or 0)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.connect(str(_socket_path(name)))
    except OSError:
        return False
    finally:
        sock.close()
    return True


# ---------------------------------------------------------------- 命令入口


# 一行登记表镜像的说明见下。
# 主会话那边的登记册按配置目录分。sideagent待在自己号的目录里（凭据不动），
# 只把双方的登记条目互相放一份，这样两边都能看见对方、互相发消息。


# ---------------------------------------------------------------- 起进程


def _ensure_started(name: str, cwd: str, idle_seconds: int, claude_args: list,  # noqa: PLR0913
                    *, context: str = "", resume: str = "", label: str = "", description: str = "",
                    model: str = "", caller_agent: str = "") -> bool:
    """没在跑就起一个；已经在跑就什么都不做。返回这次是不是新起的。"""
    from cca.session import roster  # noqa: PLC0415 - roster 反过来要用本模块

    _ensure_inbound_setting()
    roster.cleanup()
    if running(name):
        return False
    account = _account_of(name)
    profile_dir(account).exists() or die(f"{account} 不是配置好的号")
    agent_dir(name).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # 归属只记“主会话是哪个 claude 进程”；会话编号、socket 每次投递前到登记表现查
    caller = caller_now()
    owner = owner_of(caller)
    owner_account = _owner_account()
    env["CCA_OWNER"] = json.dumps(owner)
    env["CCA_OWNER_ACCOUNT"] = owner_account
    # 横条上显示的名字：派活时给的，不给就显示号名（像子代理显示代理类型）
    env["CCA_LABEL"] = label
    env["CCA_DESCRIPTION"] = description
    # 派活的是主会话里的哪个子代理（插件从 tool.call 的 agentId 塞进来）；空 = 主对话派的
    env["CCA_CALLER_AGENT"] = caller_agent
    # 记进主会话自己的名单（派出的只有这一个主人）；横条名字、描述、哪个子代理派的也记在这条里
    roster.add(roster.owner_fields(caller, owner_account), name, roster.SPAWNED, label=label,
               description=description, caller_agent=caller_agent)
    src = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if model:
        # 照子代理的 model 参数：这个实例用哪个模型（按它那个号的配置解析别名）
        claude_args = ["--model", model, *claude_args]
    if resume:
        # 追问已经退出的实例：同一个 claude 会话接着开，上下文还在。
        # 历史里有 Gemini 写的 tool_use.signature 时先清掉（这个号不是 Gemini 才清）；
        # 会话要是还活着就别动它的记录。
        from cca.session.transcript import (  # noqa: PLC0415 - 只在续会话时需要
            session_live,
            strip_session,
            target_needs_strip,
        )

        if target_needs_strip(account) and not session_live(resume):
            stripped = strip_session(resume)
            if stripped:
                say(f"{name}: 清掉了 {stripped} 处 tool_use.signature（Gemini 历史）")
        claude_args = ["--resume", resume, *claude_args]
    elif context:
        sid = _context_session(context)
        session_cwd = _expose_transcript(name, sid)
        cwd = cwd or session_cwd
        claude_args = ["--resume", sid, "--fork-session", "--no-session-persistence",
                       *claude_args]
    cwd = cwd or str(Path.cwd())
    with _log_path(name).open("a", encoding="utf-8") as log:
        # 守护进程的工作目录设成源码目录：`python -c` 会把当前目录排在最前面，
        # 如果用户的目录里碰巧有同名 cca.py，就会把包挡掉。claude 自己的 cwd
        # 另外传给 serve()。
        process = subprocess.Popen(  # noqa: S603 - 参数由本模块拼好
            [sys.executable, "-c", SERVE_CODE,
             name, cwd, str(idle_seconds), *claude_args],
            env=env, cwd=src, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if process.poll() is not None:
            die(f"{name}: sideagent启动失败，看日志 {_log_path(name)}")
        if running(name):
            say(f"{name}: sideagent已起（会话 {_read_state(name).get('session_id') or '启动中'}）")
            return True
        time.sleep(0.2)
    with suppress(OSError):
        process.kill()
    die(f"{name}: sideagent {START_TIMEOUT} 秒内没起来，看日志 {_log_path(name)}")
    return False


# ---------------------------------------------------------------- 发消息


def _send(name: str, text: str, timeout: float, *, raw: bool) -> int:
    """发一句到sideagent，流式打印它的回复。"""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(_socket_path(name)))
    except OSError as exc:
        die(f"{name}: 连不上sideagent（{exc}）")
    request = json.dumps({"text": text}, ensure_ascii=False) + "\n"
    sock.sendall(request.encode("utf-8"))
    try:
        return 0 if _pump(sock, raw=raw) else 1
    except TimeoutError:
        die(f"{name}: 等了 {int(timeout)} 秒还没干完")
    finally:
        sock.close()


def _pump(sock: socket.socket, *, raw: bool) -> bool:
    """把sideagent发回的事件流读完，返回是否见到了 result。"""
    buffer = b""
    got_result = False
    printer = _Printer(raw=raw)
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return got_result
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            got_result |= printer.feed(line)


class _Printer:
    """把事件流变成人能看的东西。

    开流式后模型吐一个字就显示一个字，不用等整段生成完；工具开始时也打一行，
    长任务能看见它在干什么。
    """

    def __init__(self, *, raw: bool) -> None:
        self.raw = raw
        self.streamed = False

    def feed(self, line: bytes) -> bool:
        """打印一条事件；返回它是不是 result。"""
        event = _parse(line)
        if event is None:
            return False
        kind = event.get("type")
        if kind == "error":
            die(str(event.get("message") or "sideagent报错"))
        if self.raw:
            say(line.decode("utf-8", "replace"))
        elif kind == "stream_event":
            self._stream(event.get("event") or {})
        if kind == "result":
            if not self.raw:
                if self.streamed:
                    sys.stdout.write("\n")
                say(f"— 完成（{event.get('num_turns') or '?'} 轮，"
                    f"{event.get('duration_ms') or 0} 毫秒）")
            return True
        return False

    def _stream(self, inner: dict) -> None:
        if inner.get("type") == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._newline()
                say(f"→ 工具 {block.get('name') or '?'}")
        elif inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            text = delta.get("text") if delta.get("type") == "text_delta" else ""
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()
                self.streamed = True

    def _newline(self) -> None:
        if self.streamed:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.streamed = False


class _Collector:
    """收集一条回复的正文和结果，不打印。给 MCP 用。"""

    def __init__(self) -> None:
        self.text = ""
        self.result: dict | None = None
        self.error = ""

    def feed(self, line: bytes) -> bool:
        event = _parse(line)
        if event is None:
            return False
        if event.get("type") == "error":
            self.error = str(event.get("message") or "sideagent报错")
            return False
        if event.get("type") == "cca_held":
            # 前一轮的结果被压下了（后台还有任务）：那一轮说的话不算最终回复，重新收
            self.text = ""
            return False
        if event.get("type") == "stream_event":
            inner = event.get("event") or {}
            delta = inner.get("delta") or {}
            if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                self.text += str(delta.get("text") or "")
        if event.get("type") == "result":
            if not self.text:
                self.text = str(event.get("result") or "")
            self.result = event
            return True
        return False


def _summary(collector: _Collector) -> dict:
    result = collector.result or {}
    return {"text": collector.text,
            "turns": result.get("num_turns"),
            "duration_ms": result.get("duration_ms"),
            "tool_uses": result.get("cca_tool_uses"),
            "token_count": result.get("cca_token_count"),
            "interrupted": bool(result.get("cca_interrupted"))}


def ask(name: str, text: str, timeout: float | None = SEND_TIMEOUT, *,  # noqa: PLR0913 - 都是可选开关
        notify: bool = False, job: str = "", interject: bool = False, keep: bool = True) -> dict:
    """发一句并等它完成，返回 {text, turns, duration_ms, interrupted}；错误抛 SideagentError。

    `notify` 为真时，sideagent这一轮干完还会把结果投到主会话的消息通道。
    默认不投：调用方已经拿到返回值，再投一遍就重复了。
    `job` 是调用方的任务编号，sideagent 记着它，插话时好说明并进了哪一条。
    连接断了又没见到结果（例如被停掉）算失败，不当成“做完了、回复是空的”。
    `interject` 为真：它正忙就插话进当前这一轮，马上返回 {"merged": True, "merged_into": 任务编号}；
    它闲着就和普通的一样开一轮、等结果。
    `keep` 为假：sideagent 干完这一轮自己退出（调用方半路没了也照样退）。
    `timeout` 为 None：不限时，一直等到结果或连接断开（后台派活用：sideagent 结束时一定交结果或报错）。
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(SOCKET_TIMEOUT if timeout is None else timeout)
    try:
        sock.connect(str(_socket_path(name)))
    except OSError as exc:
        msg = f"{name}: 连不上sideagent（{exc}）"
        raise SideagentError(msg) from exc
    try:
        request = json.dumps({"text": text, "notify": notify, "job": job,
                              "interject": interject, "keep": keep}, ensure_ascii=False) + "\n"
        sock.sendall(request.encode("utf-8"))
        sock.settimeout(timeout)
        collector = _Collector()
        buffer = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                ack = _parse(line) or {}
                if ack.get("type") == "cca_ack" and ack.get("mode") == "interject":
                    return {"merged": True, "merged_into": str(ack.get("merged_into") or ""),
                            "text": "", "turns": None, "duration_ms": None}
                if collector.feed(line):
                    if collector.error:
                        raise SideagentError(collector.error)
                    return _summary(collector)
        if collector.error:
            raise SideagentError(collector.error)
        msg = f"{name}: 连接断了，没拿到结果（sideagent 被停掉或退出了）"
        raise SideagentError(msg)
    except TimeoutError as exc:
        msg = f"{name}: 等了 {int(timeout)} 秒还没干完"
        raise SideagentError(msg) from exc
    finally:
        sock.close()


def _request_ack(name: str, request: dict) -> dict:
    """发一个控制请求，读回一行确认。老进程不回确认，就返回空字典。"""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(str(_socket_path(name)))
    except OSError as exc:
        msg = f"{name}: 连不上sideagent（{exc}）"
        raise SideagentError(msg) from exc
    try:
        sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        reply = _parse(_read_line(sock)) or {}
    except OSError:
        reply = {}
    finally:
        sock.close()
    if reply.get("type") == "error":
        msg = f"{name}: {reply.get('message') or '被拒绝'}"
        raise SideagentError(msg)
    return reply


def interject(name: str, text: str, *, notify: bool = False, job: str = "") -> dict:
    """插话，只发不等（面板回车用）：它正忙就立刻送进这一轮，下一个工具调用结束时它就能看到。

    返回 {"mode": "interject", "merged_into": 当前任务编号} 或 {"mode": "queue"}（它闲着，
    这句就当成新的一轮开跑）。老进程不认这个请求，会当成普通排队，返回空字典。
    """
    return _request_ack(name, {"text": text, "interject": True, "detach": True,
                               "notify": notify, "job": job})


def interrupt(name: str) -> dict:
    """打断当前这一轮，进程不退出。返回 {"busy": 打断时是不是在干活}。"""
    return _request_ack(name, {"interrupt": True})


def ensure(name: str, *, cwd: str = "", idle_minutes: int = DEFAULT_IDLE_MINUTES,  # noqa: PLR0913
           context: str = "", resume: str = "", label: str = "", description: str = "",
           model: str = "", caller_agent: str = "") -> bool:
    """对外入口：没起就起。context 传给 `--resume --fork-session`（分叉主会话）；
    resume 是这个实例自己的 claude 会话编号（退出后接着开）。返回是不是新起的。"""
    return _ensure_started(name, cwd or str(Path.cwd()), idle_minutes * 60, [],
                           context=context, resume=resume, label=label, description=description,
                           model=model, caller_agent=caller_agent)


def stop(name: str) -> None:
    """对外入口：停掉sideagent。"""
    _stop([name])


def stop_later(name: str, seconds: float = LINGER_SECONDS) -> None:
    """干完先留一会儿，让界面把结果和耗时显示出来，再销毁。

    到点时它又在干活、有排队的、或这期间接过新的话（追问、插话），就不停：
    那件活干完时会再安排一次。
    """
    asked_at = time.time()

    def run() -> None:
        time.sleep(seconds)
        if not running(name):
            return
        state = _read_state(name)
        if state.get("busy") or int(state.get("queued") or 0) > 0:
            return
        if float(state.get("last_request") or 0) > asked_at:
            return
        _stop([name])

    threading.Thread(target=run, daemon=True).start()


def status(name: str) -> dict:
    state = _read_state(name)
    alive = running(name)
    info = {"account": name, "running": alive,
            "busy": bool(state.get("busy")),
            "queued": int(state.get("queued") or 0),
            "session_id": state.get("session_id") or "",
            "idle_seconds": (int(time.time() - float(state.get("last_activity") or time.time()))
                             if alive else None),
            "tokens_in": int(state.get("tokens_in") or 0),
            "tokens_out": int(state.get("tokens_out") or 0),
            "token_count": int(state.get("token_count") or 0),
            "code_version": str(state.get("code_version") or "")}
    if info["busy"]:
        info["doing"] = doing(name)
        # 和横条同一句：这个号的小模型写的进度短语（照 Claude Code 子代理），没有就是空
        info["summary"] = str(state.get("summary") or "")
        if state.get("busy") and state.get("api_retry"):
            info["api_retry"] = str(state["api_retry"])
    if alive and info["code_version"] != CODE_VERSION:
        info["stale"] = True
        info["stale_note"] = "旧版本：这个进程是改代码前起的，最近改的修复它用不上；插话照常能用"
    return info


def usage_in(usage) -> int:
    """一次调用的输入：没缓存的 + 写缓存的 + 读缓存的，三项都算。"""
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(key) or 0) for key in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def _kilo(count: int) -> str:
    return f"{count / 1000:.1f}k"


def _tail_events(name: str, limit: int = 40, tail_bytes: int = 64_000) -> list:
    """事件文件最后几行；只读文件尾巴，文件大了也不慢。"""
    path = _events_path(name)
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            lines = handle.read().decode("utf-8", "replace").splitlines()[-limit:]
    except OSError:
        return []
    return [event for event in (_parse(line.encode("utf-8")) for line in lines) if event]


def doing_of(events: list, live: str = "") -> str:
    """它这会儿在干什么，一句话：正在说的话 > 最后一步（工具、思考、说话）。

    和主界面的子代理行一样：工具写成 `Bash(make test)`，思考写 `Thinking…`。
    """
    live = " ".join(str(live or "").split())
    if live:
        return live[-80:]
    for event in reversed(events):
        if event.get("parent"):
            continue
        describe = _DOING.get(str(event.get("kind") or ""))
        if describe is not None:
            return describe(event)
    return ""


def _one_line(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _tool_doing(event: dict) -> str:
    summary = str(event.get("summary") or "")
    return f"{event.get('name') or '?'}" + (f"({summary[:60]})" if summary else "")


# 每种事件怎么写成“在干什么”；result 是上一轮已经结束，这一轮还没有新动作
_DOING = {
    "tool": _tool_doing,
    "think": lambda _event: "Thinking…",
    "say": lambda event: _one_line(event.get("text"), 80),
    "task": lambda event: "开始：" + _one_line(event.get("text"), 60),
    "result": lambda _event: "",
}


def doing(name: str) -> str:
    state = _read_state(name)
    retry = str(state.get("api_retry") or "") if state.get("busy") else ""
    if retry:
        # API 正在出错重试：这比“最后一步”更说明它为什么没动静
        return retry
    live = ""
    with suppress(OSError):
        live = _live_path(name).read_text(encoding="utf-8")
    return doing_of(_tail_events(name), live)


class SideagentError(Exception):
    """sideagent不可用或这一轮失败。"""


def _parse(line: bytes) -> dict | None:
    if not line.strip():
        return None
    try:
        data = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _text_of(event: dict) -> str:
    content = (event.get("message") or {}).get("content") or []
    parts = [str(c.get("text") or "") for c in content
             if isinstance(c, dict) and c.get("type") == "text"]
    return "".join(parts)


# ---------------------------------------------------------------- sideagent进程


# 拆出去的几部分：命令行入口（sideagent_cli）、任务记录（sideagent_records）、常驻进程（sideagent_server）。
# 旧名字留在这里，SERVE_CODE、cli.py 和测试照旧能用；拆出去的代码也经本模块互相调用。
from cca.session.sideagent_cli import (  # noqa: E402, F401 - 放最后（它反过来要用本模块）；转出旧名字
    _interrupt_command,
    _list,
    _run_once,
    _send_command,
    _serve_pids,
    _shorthand,
    _split,
    _start_command,
    _state_files,
    _stop,
    _tell,
    _tell_command,
    _usage,
    cmd_sideagent,
    prepare,
    stop_owned,
)
from cca.session.sideagent_records import (  # noqa: E402, F401 - 同上
    _RECORD_LOCK,
    _read_record,
    _record_locked,
    _write_record,
    job_path,
    jobs_of,
    remember_instance,
    write_job,
)
from cca.session.sideagent_server import (  # noqa: E402, F401 - 放最后（它反过来要用本模块）；转出旧名字
    _escape_xml,
    _read_line,
    _reply,
    _reply_error,
    _SideagentServer,
    serve,
)
