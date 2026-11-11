"""会话文件操作与推送：会话池、后台任务、push_session_once。"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from cca.account.model import discover_profiles
from cca.account.store import profile_kind
from cca.api.usage import _parse_ts
from cca.cli import parse_flags
from cca.config import (
    AGENTS_TIMEOUT,
    LIVE_INDEX_WORKERS,
    NON_RUNNING_STATES,
    SESSIONS_DAYS,
    STALE_LOG_MARKERS,
    SYNTHETIC_MODEL,
    TRANSCRIPT_MARKERS,
    WAIT_BG_VERIFY_SECONDS,
    WAIT_BOOKKEEPING_TYPES,
    WAIT_COPY_MARKERS,
    WAIT_ENV_MARKERS,
    WAIT_HELD_MARKERS,
    WAIT_LIMIT_MARKERS,
    WAIT_LOG_MOTION_SECONDS,
    WAIT_LOGS_TIMEOUT,
)
from cca.env import build_env, session_env
from cca.errors import die, note, say, warn
from cca.paths import (
    accounts_root,
    profile_dir,
    shared_dir,
    shared_pool_root,
    stopped_bg_file,
    transcript_cache_file,
)

# 时长换算与日志保留上限。
HOUR_SECONDS = 3600
DAY_SECONDS = 86400


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def _roster_sessions(name: str) -> list:
    """回退判据：daemon/roster.json 里 pid 仍存活的 worker，状态记 unknown。

    它只看得到"进程还在"：分不清在跑还是空闲预热，也认不出被回收的 pid，
    因此只在 `claude agents --json` 不可用时使用。
    """
    f = profile_dir(name) / "daemon" / "roster.json"
    if not f.is_file():
        return []
    try:
        roster = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    workers = roster.get("workers") or {}
    items = workers.items() if isinstance(workers, dict) else ((None, w) for w in workers)
    alive = []
    for key, w in items:
        if not isinstance(w, dict):
            continue
        pids = [w.get("pid"), w.get("replPid")]
        if any(_pid_alive(p) for p in pids):
            alive.append(
                {
                    "id": str(w.get("sessionId") or key or "")[:8],
                    # roster 的 key 就是 job id（agents --json 不可用时的回退判据）。
                    "job": str(key or ""),
                    "full": str(w.get("sessionId") or ""),
                    "kind": "",
                    "name": "",
                    "cwd": w.get("cwd", ""),
                    "state": "unknown",
                }
            )
    return alive

def live_session_ids(name: str) -> set:
    """这个号上进程还活着的会话（完整 id），读 daemon/roster.json，不起 claude，很快。

    会话说完一轮状态就是 done，但进程还在；要不要先 stop、目标号上是不是已经在跑，都看这个，
    不看状态字。
    """
    return {item["full"] for item in _roster_sessions(name) if item.get("full")}

def shares_sessions(name: str) -> bool:
    """这个号的对话目录是不是和大家共用的那一份（隔离号自己一份，别的号的对话它看不到）。"""
    mine = profile_dir(name) / "projects"
    shared = shared_dir() / "projects"
    try:
        return mine.resolve() == shared.resolve()
    except OSError:
        return False

def _agents_sessions(name: str, *, include_all: bool = False):
    """`claude agents --json` 给出的权威后台会话清单；不可用时返回 None 让调用方回退。

    只在该 profile 出现过 daemon 时才调用：新配置目录首次运行 claude 会自建
    .claude.json / daemon/ 等文件，只为看一眼后台会话不该产生这种副作用。
    `include_all=True` 时加 `--all`：不带 `--all` 只看得到仍在登记的后台会话，
    已结束（done/stopped）的不会出现。
    """
    if not (profile_dir(name) / "daemon").is_dir():
        return []
    exe = shutil.which("claude")
    if not exe:
        return None
    command = [exe, "agents", "--json"]
    if include_all:
        command.append("--all")
    try:
        r = subprocess.run(  # noqa: S603 - exe 来自 shutil.which("claude")，参数是固定常量
            command,
            env=build_env(name),
            capture_output=True,
            text=True,
            timeout=AGENTS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    items = data if isinstance(data, list) else data.get("agents") or data.get("sessions") or []
    sessions = []
    for a in items:
        if not isinstance(a, dict):
            continue
        sessions.append(
            {
                "id": str(a.get("id") or a.get("sessionId") or "")[:8],
                # `claude stop` 只认**短 id**（实测 2.1.296：`stop c1f70950` 有效，完整 UUID 回
                # `No job matching`）。老版本 `id` 就是短 id；新版本只给 `sessionId`，取前 8 位
                # —— 推送路径要先用它 stop 再 resume，给错等于没停。
                "job": str(a.get("id") or a.get("sessionId") or "")[:8],
                # 展示与匹配用短 id；`--bg --resume` / `-r` 只认完整 UUID，所以要留着它。
                "full": str(a.get("sessionId") or a.get("id") or ""),
                "kind": str(a.get("kind") or ""),
                # 交互登记带 pid：用来判断终端进程是否还活着（spare / parked 要看 registry）。
                "pid": a.get("pid"),
                "name": str(a.get("name") or ""),
                "cwd": a.get("cwd") or "",
                # 老版本字段是 `state`，2.1.296 起改叫 `status`（值还是 busy/idle/…）
                "state": str(a.get("state") or a.get("status") or "unknown"),
            }
        )
    return sessions

def active_workers(name: str) -> list:
    """该 profile 的后台会话，每项含 state: busy / idle / blocked / unknown。

    busy 表示正在跑，idle / blocked 只是挂着不消耗，unknown 表示只能确认进程存活。
    """
    sessions = _agents_sessions(name)
    return _roster_sessions(name) if sessions is None else sessions

def is_running(state: str) -> bool:
    """该状态是否算"在跑"。白名单式排除非运行状态，未知状态一律按在跑处理。"""
    return state not in NON_RUNNING_STATES

def is_job(session: dict) -> bool:
    """这条登记是不是 `claude stop` 认的后台任务。

    `claude agents --json` 里会混进交互会话（`kind: interactive`、没有 job id）：它们只是
    "附着在 daemon 上的前台会话"，不是后台任务，`stop` 只会回 `No job matching '<id>'`。
    这种条目要跳过并说清楚，而不是一个个去撞墙。
    """
    return bool(session.get("job")) and session.get("kind", "") in ("", "background")

def background_summary(name: str) -> dict:
    """后台会话按状态汇总；running 计入所有非"已停下"状态的会话。"""
    sessions = active_workers(name)
    counts = {}
    for s in sessions:
        state = s.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
    return {
        "total": len(sessions),
        "running": sum(n for st, n in counts.items() if is_running(st)),
        "counts": counts,
        "sessions": sessions,
    }

def format_state_counts(counts: dict) -> str:
    """状态计数摘要，如 `busy 4 · idle 1 · blocked 2`；无会话时返回 `-`。"""
    order = ("busy", "unknown", "blocked", "idle")
    keys = [k for k in order if counts.get(k)]
    keys += sorted(k for k in counts if k not in order and counts[k])
    return " · ".join(f"{k} {counts[k]}" for k in keys) or "-"

def format_running_detail(name: str, summary: dict, limit: int = 3) -> str:
    """`001(在跑 4: sam、daily、snap_emb…)` —— 一行说清哪个号在跑什么。"""
    running = [s for s in summary["sessions"] if is_running(s.get("state", "unknown"))]
    names = [s.get("name") or s.get("id") or "?" for s in running]
    head = "、".join(names[:limit]) + ("…" if len(names) > limit else "")
    return f"{name}(在跑 {len(running)}: {head})"

def clear_stale_resume_job(name: str, claude_args: list) -> None:
    """`--resume <id>` 前清掉这条会话的陈旧后台 job 登记。

    真机上反复踩：一次无头跑（`-p --resume`）会留下 `jobs/<id>/state.json`，之后前台
    `--resume` 一律被拒（"That session is running in the background …"），而 `attach`
    又要求**有** job —— 两边互相打架，使用者只能手工 `claude stop` + 删目录。
    这里在启动前替他做掉，只动这一条会话的脚手架，打一行提示。
    """
    resume_id = ""
    for index, arg in enumerate(claude_args):
        if arg == "--resume" and index + 1 < len(claude_args):
            resume_id = str(claude_args[index + 1])
            break
        if str(arg).startswith("--resume="):
            resume_id = str(arg).split("=", 1)[1]
            break
    if not resume_id:
        return
    job_dir = profile_dir(name) / "jobs"
    if not job_dir.is_dir():
        return
    hits = [d for d in job_dir.iterdir() if d.is_dir() and d.name.startswith(resume_id[:8])]
    if not hits:
        return
    exe = shutil.which("claude")
    for job in hits:
        if exe:
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(  # noqa: S603
                    [exe, "stop", job.name], capture_output=True, text=True,
                    timeout=30, env={**os.environ, "CLAUDE_CONFIG_DIR": str(profile_dir(name))},
                    check=False,
                )
        shutil.rmtree(job, ignore_errors=True)
    note(f"{name}: 清掉了 {resume_id[:8]} 的陈旧后台登记（{len(hits)} 个），可以直接 --resume")

def background_guard(name: str) -> None:
    """启动或切号前提示其它号的后台会话：有在跑 → 警告，仅挂起 → 提示。

    **API 号不参与串行约定**：串行是为了"同一个订阅别被多号并行消耗、也别让多份订阅同时跑"，
    而 API 号是另一家供应商、按量计费、不占 Claude 订阅额度 —— 让 ds 跑批量的同时用 002 干活
    是正常用法，不该每次启动都挨一句警告。涉及 API 号就不提；只有两个订阅号同时跑才警告。
    """
    if profile_kind(name) == "api":
        return
    running, hanging = [], []
    for n in discover_profiles():
        if n == name or profile_kind(n) == "api":
            continue
        summary = background_summary(n)
        if summary["running"]:
            running.append(format_running_detail(n, summary))
        elif summary["total"]:
            hanging.append(f"{n}({format_state_counts(summary['counts'])})")
    if running:
        # 始终提示：后台/自动化场景恰好是非 tty，正是最需要这条提醒的时候。
        warn("其它号仍有后台会话在跑: " + ", ".join(running) + " —— 按约定建议串行使用")
    elif hanging:
        note("其它号有挂起的后台会话（未在跑）: " + ", ".join(hanging))

def format_age(mtime: float) -> str:
    delta = max(0.0, time.time() - mtime)
    if delta < HOUR_SECONDS:
        return f"{int(delta // 60)} 分钟前"
    if delta < DAY_SECONDS:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // DAY_SECONDS)} 天前"

# ---- 会话记录扫描结果缓存：(路径, 大小, 修改时间) 不变就不重扫
# 一共几百条、上 GB 的会话记录，全扫一遍要十几秒；缓存后只重扫变了的那几条（在跑的会话）。
_transcript_cache: dict | None = None
_transcript_cache_dirty = False


def _cache_table() -> dict:
    global _transcript_cache  # noqa: PLW0603 - 进程内只读一次
    if _transcript_cache is None:
        try:
            data = json.loads(transcript_cache_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _transcript_cache = data if isinstance(data, dict) else {}
        atexit.register(_save_transcript_cache)
    return _transcript_cache


def _save_transcript_cache() -> None:
    """进程退出时写回；文件已经没了的条目顺手删掉。几个 cca 同时写，后写的赢，丢的下次重扫就回来了。"""
    if not _transcript_cache_dirty or _transcript_cache is None:
        return
    table = {key: value for key, value in _transcript_cache.items() if Path(key).exists()}
    path = transcript_cache_file()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)


def _cached(path: Path, field: str, compute):
    """按 (路径, 大小, 修改时间) 取缓存的一项；变了或没有就算一次存起来。"""
    global _transcript_cache_dirty  # noqa: PLW0603
    try:
        stat = path.stat()
    except OSError:
        return compute(path)
    stamp = [stat.st_size, stat.st_mtime_ns]
    table = _cache_table()
    key = str(path)
    entry = table.get(key)
    if not isinstance(entry, dict) or entry.get("stamp") != stamp:
        entry = {"stamp": stamp}
        table[key] = entry
    if field not in entry:
        entry[field] = compute(path)
        _transcript_cache_dirty = True
    return entry[field]


def _scan_transcript(path: Path) -> dict:
    """取一条 transcript 的展示信息（有缓存，文件没变不重扫）。"""
    # 缓存字段名带版本：加字段时改这里，旧缓存自然失效（一次全量重扫，之后照常）
    return dict(_cached(path, "info2", _scan_transcript_uncached))


def _scan_transcript_uncached(path: Path) -> dict:
    """取一条 transcript 的展示信息：标题、最后一条提问、会话起始目录。

    标题优先用 `custom-title`（用户手设的名字），回退 `ai-title` / `agent-name`。
    cwd 取**第一条**：会话中途的记录会带上别的目录（例如 memory 工具落在
    `<config>/projects/<项目>/memory` 下的 attachment），按最后一条会把项目归错。
    """
    info = {"title": "", "ai_title": "", "prompt": "", "cwd": "", "first": ""}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(m in line for m in TRANSCRIPT_MARKERS):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("type")
                if kind == "custom-title" and rec.get("customTitle"):
                    info["title"] = rec["customTitle"]
                elif kind in ("ai-title", "agent-name"):
                    info["ai_title"] = rec.get("aiTitle") or rec.get("agentName") or info["ai_title"]
                elif kind == "last-prompt":
                    info["prompt"] = rec.get("lastPrompt") or info["prompt"]
                elif kind == "user" and not info["first"]:
                    info["first"] = _user_text(rec)
                if not info["cwd"] and rec.get("cwd"):
                    info["cwd"] = rec["cwd"]
    except OSError:
        return info
    if not info["title"]:
        info["title"] = info["ai_title"]
    return info


NAME_TAIL_BYTES = 4 << 20


def _tail_custom_title(path: Path) -> str:
    """只读文件尾部 NAME_TAIL_BYTES，取最后一个 custom-title；尾部没有返回空串。

    CC 每跑完一段会在末尾重写一组元数据，所以用户起过名的会话尾部几乎总有 custom-title；
    GB 级的记录不必整份读完。尾部里的最后一个就是全文最后一个，结果是精确的。
    """
    title = ""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - NAME_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return ""
    for line in data.splitlines():
        if b'"custom-title"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "custom-title" and rec.get("customTitle"):
            title = str(rec["customTitle"])
    return title


def session_name(path: Path | None) -> str:
    """一条会话的名字（规范取法，全仓只此一份）。

    对话记录里最后一个 `custom-title`（/rename 或 -n 设的）；没有就用最后一个 `ai-title` / `agent-name`；
    都没有返回空串。换行和连续空白压成一个空格。先只读尾部找 custom-title，找不到再整份扫（有缓存）。
    """
    if path is None:
        return ""
    name = _tail_custom_title(path) or _scan_transcript(path).get("title") or ""
    return " ".join(str(name).split())


def _user_text(rec: dict) -> str:
    """一条 user 记录里的纯文本；工具结果、斜杠命令不算标题。"""
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(block.get("text") or "" for block in content
                        if isinstance(block, dict) and block.get("type") == "text")
    else:
        return ""
    text = " ".join(text.split())
    if not text or text.startswith("<"):
        return ""
    return text[:160]


def last_session_model(path: Path) -> str:
    """一条 transcript 最后用的模型（有缓存，文件没变不重读）。"""
    return _cached(path, "model", _last_session_model_uncached)


def _last_session_model_uncached(path: Path) -> str:
    """一条 transcript 最后用的模型（只读文件尾 256KB）。

    错误条目记的是 `<synthetic>`，那不是模型，跳过；读不到就返回空串。
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 262144))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for match in reversed(re.findall(r'"model"\s*:\s*"([^"]+)"', tail)):
        if match and match != SYNTHETIC_MODEL:
            return match
    return ""

def resumable_sessions(days: int = SESSIONS_DAYS) -> list:
    """共享会话池里可续的会话，按最后活动倒序（`days=0` 表示不限时间）。

    `projects/` 是跨号共享的软链（所有号都指向内置号那一份），所以这里列出的会话任何号都能
    `-r` 续 —— 会话**不绑账号**。transcript 也不记账号，因此历史会话只能标"归属未知"；
    live 会话的归属由 live_index() 从各号 daemon 的登记里补出来。

    **不截断**：这个命令的用途就是"找会话"，看不见等于没有（曾按 20 条截断，还把小字写成
    "共 20 条（全部）"，把截断当总数报出来）。要缩短清单用时间窗（默认最近 14 天）或 --all。
    """
    root = shared_pool_root()
    if not root.is_dir():
        return []
    cutoff = time.time() - days * 86400 if days else 0.0
    items = []
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            info = _scan_transcript(f)
            items.append(
                {
                    "id": f.stem,
                    "short": f.stem[:8],
                    "mtime": mtime,
                    "cwd": info["cwd"] or proj.name,
                    "title": info["title"] or info.get("first", ""),
                    "first": info.get("first", ""),
                    "prompt": info["prompt"],
                    "model": last_session_model(f),
                }
            )
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items

def older_session_count(days: int = SESSIONS_DAYS) -> int:
    """时间窗**之外**还有多少条会话（只 stat，不解析 transcript —— 表尾要报真实总数）。"""
    root = shared_pool_root()
    if not root.is_dir() or not days:
        return 0
    cutoff = time.time() - days * 86400
    count = 0
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    count += 1
            except OSError:
                continue
    return count

def all_states_index() -> dict:
    """短会话 id → [(号, 状态)]，含已结束的（`claude agents --json --all`）。

    `cca sessions` 用它把 stopped/failed 的会话藏起来（和 handoff 一致）。
    """
    from cca.account.model import discover_profiles  # noqa: PLC0415

    index: dict = {}
    for name in discover_profiles():
        for item in _agents_sessions(name, include_all=True) or []:
            short = str(item.get("id") or "")
            if short:
                index.setdefault(short, []).append((name, str(item.get("state") or "")))
    return index


def live_index() -> dict:
    """短会话 id → [(profile, state), …]：同一 id 可能同时挂在多个号上（副本），全部保留。"""
    names = discover_profiles()
    # 每个号要起一次 `claude agents --json`，挨个跑十几个号要两三秒；并行跑（同 takeover.candidates）
    with ThreadPoolExecutor(max_workers=max(1, min(len(names), LIVE_INDEX_WORKERS))) as pool:
        results = list(pool.map(active_workers, names))
    index = {}
    for n, sessions in zip(names, results, strict=True):
        for s in sessions:
            if s.get("id"):
                index.setdefault(s["id"], []).append((n, s.get("state", "")))
    return index

def stale_sessions(name: str) -> list:
    """该号清单里"进程已不在"的后台会话。

    判据是 `claude logs <id>`：进程还在时能读到输出，已退出则返回
    `job not found — it may have already exited`。只查没在跑的会话（在跑的不用查）。
    """
    stale = []
    for s in active_workers(name):
        if is_running(s.get("state", "unknown")):
            continue
        r = _run_claude(["logs", s["id"]], name, capture=True)
        text = f"{r.stdout or ''}{r.stderr or ''}".lower()
        if r.returncode != 0 and any(marker in text for marker in STALE_LOG_MARKERS):
            stale.append(s)
    return stale

def session_sidecar_paths(path: Path) -> list:
    """这条会话的附属 transcript：`<会话id>/subagents/agent-*.jsonl`（子代理各写各的文件）。

    实测：只剥主文件救不了子代理 —— 它们有自己的 transcript，里面的 `deferred_tools_record`
    照样会被展开成 `tool_definition` → 搬到第三方端点后子代理报同样的 422（真机踩到）。
    """
    sidecar = path.with_suffix("")
    return sorted(sidecar.rglob("*.jsonl")) if sidecar.is_dir() else []

def tool_search_records(path: Path) -> int:
    """transcript 里有多少条 tool search 的延迟工具记录（`deferred_tools_record` 附件）。

    这些记录只在**第一方端点**上有意义：resume 到第三方（DeepSeek 这类 Anthropic 兼容端点）时
    CLI 会把它们展开成 `tool_definition` 内容块，而对方 schema 只认 `tool_reference`
    → `422 Failed to deserialize … unknown variant tool_definition`（真机踩到，实测剥掉即可）。
    """
    count = 0
    for target in [path, *session_sidecar_paths(path)]:
        try:
            with target.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"deferred_tools_record"' in line:
                        count += 1
        except OSError:
            continue
    return count

def strip_tool_search_records(path: Path) -> tuple:
    """剥掉这条会话（含子代理文件）里的记录：`(剥掉条数, [备份路径…])`。

    每个被动过的文件都先备份到 `<accounts>/_session-backups/`（子代理文件按它自己的文件名备份）。
    """
    backup_dir = accounts_root() / "_session-backups"
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dropped, backups = 0, []
    for target in [path, *session_sidecar_paths(path)]:
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if '"deferred_tools_record"' not in raw:
            continue
        backup = backup_dir / f"{target.stem}.before-detool.jsonl"
        shutil.copy2(target, backup)
        backups.append(backup)
        kept = []
        for line in raw.splitlines(keepends=True):
            if '"deferred_tools_record"' in line:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
                if (entry.get("type") == "attachment"
                        and (entry.get("attachment") or {}).get("type") == "deferred_tools_record"):
                    dropped += 1
                    continue
            kept.append(line)
        target.write_text("".join(kept), encoding="utf-8")
    return dropped, backups

# 已结束的状态：这类会话不用先 stop 源号，直接在目标号 `--bg --resume` 就行。
COMPLETED_STATES = ("done", "stopped", "completed", "finished", "exited", "failed", "killed")

# 搬不了 / 有说明的原因（面板直接显示给用户）。
HOME_DIR_REASON = "家目录的会话要先建工作目录 ~/llm 才能换号"
INTERACTIVE_REASON = "普通终端会话，先按 ← 挪到后台"

# 读 transcript 末尾这么多字节：回顾和最后一句都在文件尾。
TRANSCRIPT_TAIL_BYTES = 200_000


def work_dir() -> Path:
    """开会话的工作目录（默认 ~/llm，环境变量 CCA_WORK_DIR 可改）。

    家目录不能长期信任，换号时家目录的会话改到这里接着跑；所以它必须存在。
    定义在 lifecycle 里（`launch_dir` 也用它）；这里延迟导入，避免循环依赖。
    """
    from cca.account.lifecycle import work_dir as _work_dir  # noqa: PLC0415 - 延迟导入破环

    return _work_dir()


def _is_home(cwd: str) -> bool:
    if not cwd:
        return False
    try:
        return Path(cwd).expanduser().resolve() == Path.home().resolve()
    except OSError:
        return False


def home_dir_blocked(cwd: str) -> bool:
    """家目录里开的会话现在能不能换号。

    家目录本身不能长期信任（"The home directory is trusted one session at a time"），
    换号时改到工作目录（~/llm）接着跑。工作目录还没建就搬不了。
    能不能搬只在这一个函数里判断，以后规则变了只改这里。
    """
    return _is_home(cwd) and not work_dir().is_dir()


def home_dir_note(cwd: str) -> str:
    """家目录会话的说明：它会改到工作目录接着跑；不是家目录返回空串。"""
    if not _is_home(cwd) or home_dir_blocked(cwd):
        return ""
    target = work_dir()
    return f"家目录的会话会改在 {target} 里接着跑，之后的命令在 {target} 执行"


def is_completed(state: str) -> bool:
    """这条状态算不算"已经结束"（不需要再停源号）。"""
    return state in COMPLETED_STATES


def _transcript_tail(path: Path | None, limit: int) -> list:
    """只读 transcript 末尾一段，返回按行拆开的文本；读不到就返回空。"""
    if path is None:
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def last_assistant_text(path: Path | None) -> str:
    """对话记录里它最后说的一句话（挑选框给人认会话用）。

    逻辑与 takeover.last_line 一致；这里按 transcript 路径读，因为 handoff 看的是
    源号的目录，而 takeover 只读当前号的 projects 池。
    """
    for raw in reversed(_transcript_tail(path, TRANSCRIPT_TAIL_BYTES)):
        if '"assistant"' not in raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if item.get("type") != "assistant" or item.get("isSidechain"):
            continue
        for block in reversed((item.get("message") or {}).get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip():
                return " ".join(str(block["text"]).split())[:200]
    return ""


def latest_recap(path: Path | None) -> str:
    """Claude Code 自己写的回顾（transcript 里的 `away_summary`）；没有就返回空。

    逻辑与 takeover.recap 一致，只是按路径读源号的 transcript。
    """
    for raw in reversed(_transcript_tail(path, TRANSCRIPT_TAIL_BYTES * 5)):
        if '"away_summary"' not in raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if item.get("subtype") == "away_summary" and str(item.get("content") or "").strip():
            return " ".join(str(item["content"]).split())[:300]
    return ""


def transcript_active_at(path: Path | None) -> str:
    """transcript 的最后修改时间（UTC，ISO 8601）；读不到返回空串。"""
    if path is None:
        return ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(mtime, tz=UTC).isoformat(timespec="seconds")


def _registry_by_pid(source: str) -> dict:
    """`<号>/sessions/<pid>.json`，按 pid 索引。

    用来区分交互进程的三种情况（字段来自 Claude Code 自己写的登记）：
    - `spare: true`：预热/待派工的进程，不代表有人正开着这段对话；
    - `parkedJobId`：终端按过 ←，已经把对话放到后台，这个终端不再占用对话；
    - 其余（kind=interactive 且 pid 还活着）：终端正开着这段对话。
    """
    folder = profile_dir(source) / "sessions"
    entries = {}
    if not folder.is_dir():
        return entries
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        if isinstance(pid, int):
            entries[pid] = data
    return entries


def _terminal_owns_conversation(entry: dict, registries: dict, *, background_live: bool) -> bool:
    """这条交互登记是不是"这段对话正开在终端里"（开着就整条会话不能搬）。

    判断依据（都来自 `claude agents --json` 和 sessions/<pid>.json）：
    - pid 还活着才算开着；
    - `spare` 或 `parkedJobId` 的进程不算：它们没有占用这段对话；
    - 后台登记还活着（没结束）时不算：这时同 sessionId 的交互登记是
      "在 agents 面板里 attach 到这条后台会话"的视图，agent 视图的归属仍按后台那行。
      后台已结束（failed/stopped…）后新开的终端不是 attach，而是有人把这段对话
      重新打开在用，这时要算占用。
    """
    if not _pid_alive(entry.get("pid")):
        return False
    registry = registries.get(entry.get("pid")) or {}
    if registry.get("spare") is True or registry.get("parkedJobId"):
        return False
    return not background_live


def _pick_text(source: str, short: str) -> dict:
    """一条会话在 transcript 里的展示信息：目录、回顾、最后一句、最后活动时间。"""
    transcript = session_transcript(profile_dir(source), short)
    return {
        "cwd": (_scan_transcript(transcript)["cwd"] if transcript is not None else ""),
        "recap": latest_recap(transcript),
        "last": last_assistant_text(transcript),
        "active_at": transcript_active_at(transcript),
    }


def _merge_candidates(source: str, sessions: list, registries: dict) -> list:
    """按完整会话 id 合并：同一段对话只出一行。

    `claude agents --json --all` 会把同一段对话列两次：一次是后台登记，另一次是
    还开着的终端（sessionId 相同）。不合并的话面板会看到两条一模一样的会话。
    合并规则：
    1. 有后台登记时优先用后台那行（状态、标题、cwd 都取它的）；
    2. 但同 id 的终端还开着、且不是 attach 视图时（见 _terminal_owns_conversation），
       整条标 movable=False，说明它在哪个终端里开着；
    3. 只有交互登记时保持原样（movable=False，先按 ← 挪到后台）。
    """
    groups = {}
    order = []
    for entry in sessions:
        full = entry.get("full") or entry.get("id", "")
        if full not in groups:
            groups[full] = []
            order.append(full)
        groups[full].append(entry)

    merged = []
    for full in order:
        entries = groups[full]
        backgrounds = [item for item in entries if item.get("kind") != "interactive"]
        interactives = [item for item in entries if item.get("kind") == "interactive"]
        # 同一个 id 有多条后台登记时优先活的那条。
        background = next(
            (item for item in backgrounds if not is_completed(item.get("state", ""))),
            backgrounds[0] if backgrounds else None,
        )
        background_live = background is not None and not is_completed(background.get("state", ""))
        owner = next(
            (item for item in interactives
             if _terminal_owns_conversation(item, registries, background_live=background_live)),
            None,
        )
        base = background or (interactives[0] if interactives else entries[0])
        state = base.get("state", "unknown")
        short = full[:8]
        text = _pick_text(source, short)
        if owner is not None:
            terminal = owner.get("name") or owner.get("id") or short
            reason = f"这段对话正在终端 {terminal} 里开着，先在那边按 ← 挪到后台或关掉"
            note = ""
        elif background is None:
            reason = INTERACTIVE_REASON
            note = ""
        elif home_dir_blocked(text["cwd"] or base.get("cwd") or ""):
            reason = HOME_DIR_REASON
            note = ""
        else:
            reason = ""
            note = home_dir_note(text["cwd"] or base.get("cwd") or "")
        completed = is_completed(state)
        merged.append(
            {
                "profile": source,
                "id": base.get("id") or short,
                "full": full,
                "state": state,
                "name": base.get("name", ""),
                "cwd": text["cwd"] or base.get("cwd") or "",
                "running": is_running(state) and not completed,
                "completed": completed,
                "recap": text["recap"],
                "last": text["last"],
                "active_at": text["active_at"],
                "current": False,        # 由调用方按 --current 填
                "movable": not reason,
                "reason": reason,
                "note": note,
            }
        )
    return merged


def handoff_candidates(source: str, *, current: str = "", include_interactive: bool = False) -> list:
    """源号的会话清单（含已结束的），按完整 id 去重，附能否搬及原因。

    用 `claude agents --json --all`：不带 `--all` 时已结束（done/stopped）的会话
    永远不出现，面板就搬不了它们。
    交互会话（`kind: interactive`）默认不算候选（普通 handoff 的老行为：搬它只会 fork
    一份副本）。面板用的 JSON 清单传 include_interactive=True 把它们也列出来，
    合并规则见 _merge_candidates。
    """
    sessions = _agents_sessions(source, include_all=True)
    if sessions is None:
        # agents 不可用（CLI 旧版 / 输出坏了）：退回 roster，至少给出进程还活着的会话。
        sessions = _roster_sessions(source)
    if not include_interactive:
        sessions = [item for item in sessions if item.get("kind") != "interactive"]
    items = _merge_candidates(source, sessions, _registry_by_pid(source))
    # 已经搬走的：源号上只剩一条结束的登记（claude agents --all 还列它），对话其实在别的号上跑着
    elsewhere = {}
    for name in discover_profiles():
        if name != source:
            for full in live_session_ids(name):
                elsewhere.setdefault(full, name)
    items = [item for item in items if not (item.get("completed") and item["full"] in elsewhere)]
    wanted = (current or "").strip()
    if wanted:
        for item in items:
            item["current"] = item["full"] == wanted or item["id"] == wanted[:8]
    # 按最后一次活动倒序，和接管挑选框一致：刚用过的最上面；没有活动时间的排最后
    items.sort(key=lambda x: x.get("active_at") or "", reverse=True)
    return items

def session_transcript(config_dir: Path, short_id: str):
    """按短 id 在共享池里找 transcript 文件（用于取起始目录）。"""
    root = config_dir / "projects"
    if not root.is_dir() or not short_id:
        return None
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        for f in proj.glob(f"{short_id}*.jsonl"):
            return f
    return None

def _run_claude(args: list, name: str, **options):
    """跑 claude 子进程；options 收 capture / cwd / timeout / env_extra / session。

    session=True 表示这一条是**起会话**：走 session_env（地区检查、出口时区、语言）。
    查状态、看日志、停会话这类辅助命令不要开，它们不该因为地区不对就被拦。
    """
    capture = bool(options.pop("capture", False))
    cwd = options.pop("cwd", None)
    timeout = options.pop("timeout", None)
    env_extra = options.pop("env_extra", None)
    session = bool(options.pop("session", False))
    exe = shutil.which("claude")
    if not exe:
        die("找不到 claude 可执行文件，请确认已安装且在 PATH 中")
    env = session_env(name) if session else build_env(name)
    if env_extra:
        env.update(env_extra)
    kwargs = {"env": env, "timeout": timeout}
    if cwd:
        kwargs["cwd"] = str(cwd)
    if capture:
        return subprocess.run(  # noqa: S603 - exe 来自 shutil.which("claude")
            [exe, *args], capture_output=True, text=True, check=False, **kwargs,
        )
    return subprocess.run([exe, *args], check=False, **kwargs)  # noqa: S603 - exe 来自 shutil.which("claude")

def wait_log_motion(profile: str, job: str, *, wait: float = WAIT_LOG_MOTION_SECONDS) -> bool:
    """这条会话的终端输出现在还在不在动（`claude logs` 取两次比对）。

    这是**动手 stop 之前**的最后一道闸：stop 会掐掉正在跑的任务（长跑的回测就是这种），
    所以宁可多花几秒确认。实测四条被额度卡住的会话输出 6 秒内逐字节相同（包括那条还在收
    监控通知的 `f034cc6a`）；真在跑的会话会持续刷新（转圈、流式输出、工具调用）。
    """
    try:
        first = _run_claude(["logs", job], profile, capture=True, timeout=WAIT_LOGS_TIMEOUT)
        time.sleep(wait)
        second = _run_claude(["logs", job], profile, capture=True, timeout=WAIT_LOGS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False                     # 读不到日志就没有"还在动"的证据；判定已经确认它停着
    before = f"{first.stdout or ''}{first.stderr or ''}"
    after = f"{second.stdout or ''}{second.stderr or ''}"
    return before != after

def wait_job_id(profile: str, sid: str) -> str:
    """这条会话在面板上的 job id —— `claude stop` 只认它。

    实测：`claude stop <完整会话 UUID>` 回 `No job matching`，`claude stop <短 id>`（面板
    `id` 字段，如 `52753a7a`）才认。给错 id 等于没停，后台登记还在，接下来 resume 只会拿到
    `running as a background session`，一条都推不动。
    """
    for item in active_workers(profile):
        full = str(item.get("full") or "")
        short = str(item.get("id") or "")
        if (full and (full == sid or full.startswith(sid))) or (short and short == sid[:8]):
            return str(item.get("job") or "")
    return ""

def push_session_command(profile: str, sid: str, prompt: str, *, launch: list | None = None) -> list:
    """推一轮的完整命令（单独拆出来，方便用例断言）。

    用 `--bg --resume <完整id> "<提示>"`（提示是**位置参数**，`--bg` 与 `-p` 不能同用）：
    会话**重新挂回该号面板**，一边跑一边能 `cca <号> attach <短id>` 看。
    实测 `-p --resume` 是前台跑：会话在面板里消失、跑完才回来，使用者既看不到进度，
    中途自己去 resume 还会被 fork 成副本（真机踩到）。
    """
    return ["cca", profile, "--bg", "--resume", sid, *(launch if launch is not None else [prompt])]

def push_print_command(profile: str, sid: str, prompt: str) -> list:
    """回退用的前台推送命令：`-p` 把提示交给**原会话**（`--bg` 那条路被 CLI 判成"要开副本"时用）。"""
    return ["cca", profile, "-p", prompt, "--resume", sid]

def wait_last_conversation_stamp(profile: str, sid: str) -> float:
    """这条会话当前最后一条对话的时间戳（推之前记下来，用来判断"动了没有"）。"""
    path = session_transcript(profile_dir(profile), sid)
    entry = (last_transcript_entry(path, skip_types=WAIT_BOOKKEEPING_TYPES, need_ts=True)
             if path is not None else None)
    return (_parse_ts(entry.get("timestamp")) or 0.0) if entry else 0.0

def wait_push_outcome(profile: str, sid: str, *, before: float,
                      seconds: float = WAIT_BG_VERIFY_SECONDS) -> tuple:
    """挂回面板之后盯一会儿，判断它真的动起来了没有、有没有立刻撞回额度。

    返回 `(state, note)`，state ∈ sent / retry / ""（看不出，交给调用方兜底）。
    `--bg` 是立刻返回的，所以"额度还没放开"必须靠**事后看 transcript** 才能判出来 ——
    不看的话退避重试就成了假成功。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(2.0)
        path = session_transcript(profile_dir(profile), sid)
        entry = (last_transcript_entry(path, skip_types=WAIT_BOOKKEEPING_TYPES, need_ts=True)
                 if path is not None else None)
        if entry is None or (_parse_ts(entry.get("timestamp")) or 0.0) <= before:
            continue                       # 还是老内容，还没动
        if entry_is_limit_error(entry):
            return "retry", "额度还没放开（挂回面板后立刻又撞上）"
        return "sent", "已挂回面板并在跑（attach 可见）"
    return "", ""

def wait_session_cwd(profile: str, sid: str) -> str:
    """这条会话的起始目录（resume 要在它自己的目录里跑）。

    先问面板（免费、账号内），问不到再退到全局会话清单（那一步会扫所有 transcript，慢）。
    """
    for item in active_workers(profile):
        full = str(item.get("full") or "")
        if item.get("cwd") and full and (full == sid or full.startswith(sid)):
            return str(item["cwd"])
    for item in resumable_sessions(days=0):
        if item["id"] == sid or item["id"].startswith(sid):
            return item.get("cwd") or ""
    return ""

def _push_copy(profile: str, sid: str, prompt: str, ctx: tuple, output: tuple) -> tuple:
    """CLI 判成"要开副本"时的处理：清掉副本，改用前台 -p 推给原会话。

    ctx = (workdir, run)；output = (text, lowered)。
    """
    workdir, run = ctx
    text, lowered = output
    copy_id = re.search(r"started a copy as\s+([0-9a-fA-F][0-9a-fA-F-]{5,})", text)
    if copy_id:
        run(["cca", profile, "stop", copy_id.group(1)], capture_output=True, text=True, timeout=120)
        run(["cca", profile, "rm", copy_id.group(1)], capture_output=True, text=True, timeout=120)
    result = run(push_print_command(profile, sid, prompt), capture_output=True, text=True,
                 timeout=3600, cwd=workdir)
    text = f"{result.stdout or ''}{result.stderr or ''}"
    lowered = text.lower()
    if any(marker in lowered for marker in WAIT_HELD_MARKERS):
        return "skipped", "会话还在后台登记里（判断为在跑），留到下一轮"
    if result.returncode != 0:
        if any(marker in lowered for marker in WAIT_LIMIT_MARKERS):
            return "retry", "额度还没放开"
        return "failed", text.strip().replace("\n", " ")[-200:] or f"退出码 {result.returncode}"
    return "sent", ("CLI 会给副本，已清掉并改用 -p 推给原会话："
                    + text.strip().replace("\n", " ")[-140:])

def _push_failure(result, text: str, lowered: str) -> tuple:
    """推送失败的结论：额度问题记 retry，环境不可用也记 retry，其余记 failed。

    "环境不可用"是**可恢复**的：代理域名一时解析不了、出口被判成不支持地区、`CCA_PROXY`
    没配 —— 这些修一下/等一下就好了，判成终结 `failed` 会把整条等待记录废掉
    （真机踩过：一次 DNS 抖动，6 条会话全被判 failed，到点的续跑一条没成）。
    """
    if any(marker in lowered for marker in WAIT_LIMIT_MARKERS):
        return "retry", "额度还没放开"
    if any(marker in lowered for marker in WAIT_ENV_MARKERS):
        return "retry", "环境不可用（代理/出口）：" + text.strip().replace("\n", " ")[-160:]
    return "failed", text.strip().replace("\n", " ")[-200:] or f"退出码 {result.returncode}"

def push_session_once(profile: str, sid: str, prompt: str, cwd: str, **hooks) -> tuple:
    """给一条会话推一轮：`(state, note)`，state ∈ sent / retry / skipped / failed。

    顺序：查到 job id → **先确认日志不再动**（在跑的不碰，留到下一轮）→ 按 job id `stop`
    清掉后台登记（`claude stop` 不认会话 UUID；本来没登记就不停）→ 在该会话自己的目录里
    `-p --resume <完整会话 id>`。带着后台登记去 resume 只会拿到 `running as a background
    session`，一条都推不动。

    hooks 收三个可替换的依赖：run / motion / verify（测试注入用）。
    """
    run = hooks.get("run") or subprocess.run
    motion = hooks.get("motion") or wait_log_motion
    verify = hooks.get("verify") or wait_push_outcome
    job = wait_job_id(profile, sid)
    if job:
        if motion(profile, job):
            return "skipped", "日志还在动，判断为真在跑，这轮不推（不打断）"
        run(["cca", profile, "stop", job], capture_output=True, text=True, timeout=120)
    before = wait_last_conversation_stamp(profile, sid)
    workdir = cwd if cwd and Path(cwd).is_dir() else None
    # 有原名：启动提示换成 /rename（CC 否则按提示词自动起名覆盖原名），prompt 续上后以消息送到
    from cca.session.takeover import resume_launch  # noqa: PLC0415 - takeover 顶部不导入本模块，延迟避免循环
    path = session_transcript(profile_dir(profile), sid)
    full = path.stem if path is not None else sid
    launch, pending = resume_launch(profile, full, prompt, path=path)
    result = run(
        push_session_command(profile, sid, prompt, launch=launch),
        capture_output=True, text=True, timeout=600, cwd=workdir,
    )
    text = f"{result.stdout or ''}{result.stderr or ''}"
    lowered = text.lower()
    if any(marker in lowered for marker in WAIT_HELD_MARKERS):
        return "skipped", "会话还在后台登记里（判断为在跑），留到下一轮"
    if any(marker in lowered for marker in WAIT_COPY_MARKERS):
        # CLI 判成"要开副本"：把副本清掉（不然用户会看到一条凭空多出来的会话），
        # 改用前台 `-p` 把提示交给**原会话** —— 原 id 不变，只是那一轮它不在面板里跑。
        return _push_copy(profile, sid, prompt, (workdir, run), (text, lowered))
    if result.returncode != 0 or "backgrounded" not in lowered:
        # 额度字样只在**失败**时算数：成功那一轮的自由文本里提到 429 / rate limit 很常见
        # （比如回复里就在聊限流），拿它推"额度还没放开"会把推成功的一轮记成待重试。
        return _push_failure(result, text, lowered)
    error = ""
    if pending:
        from cca.session.takeover import deliver_after_resume  # noqa: PLC0415

        # 基线保持推之前取的那个：/rename 写的是 system 记录，WAIT_BOOKKEEPING_TYPES 已经滤掉，
        # 不会被当成"动了"；投递之后再取基线会把投递那句话触发的一轮（含撞额度）吞进基线。
        error = deliver_after_resume(profile, full, pending)
    state, note = ("retry", f"已挂回面板，但提示没送到：{error}") if error else verify(profile, sid, before=before)
    if state:
        return state, note
    return "sent", ("已挂回面板（attach 可见）："
                    + (text.strip().replace("\n", " ")[-160:] or "backgrounded"))

def stop_sessions(name: str, *, force: bool = False, exclude_ids=()) -> tuple:
    """停掉某号的后台会话（只 stop 不 rm），返回 (已停, 跳过在跑, 非后台任务, 失败)。

    默认跳过正在跑的会话：`stop` 会掐掉进行中的任务；而挂起/跑完的会话停掉只是省掉进程，
    对话都保留（`cca <profile> attach <id>` 可回来）。`force=True` 才连在跑的一起停。

    只对真正的后台任务动手（`is_job`）：交互会话停不掉，硬停只会得到 `No job matching`；
    `exclude_ids` 里的会话（已被接管的）也不动。
    """
    sessions = active_workers(name)
    targets, skipped, not_jobs = [], [], []
    for s in sessions:
        if s.get("id") in exclude_ids:
            continue
        if not is_job(s):
            not_jobs.append(s)
        elif force or not is_running(s.get("state", "unknown")):
            targets.append(s)
        else:
            skipped.append(s)
    failures = []
    for s in targets:
        label = s.get("name") or s.get("id")
        say(f"停止 {name} {s.get('id')} [{s.get('state')}] {label}".rstrip())
        r = _run_claude(["stop", s["job"]], name, capture=True)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()[:120]
            if "No job matching" in msg:
                # 登记在册、CLI 却不认：多半是 daemon 重启过，job 记录已经过期。
                msg += f"（CLI 不认这条 job，登记可能过期；cca {name} agents 可看当前清单）"
            failures.append(f"{s.get('id')}: {msg}")
    return targets, skipped, not_jobs, failures

def describe_not_jobs(name: str, not_jobs: list) -> None:
    """回报那些"不是后台任务、停不掉"的登记：说清是什么、该用哪条命令看。"""
    if not not_jobs:
        return
    listed = ", ".join(f"{s.get('id')}{' ' + s['name'] if s.get('name') else ''}" for s in not_jobs)
    note(
        f"{name}: 跳过 {len(not_jobs)} 条非后台任务（{listed}）—— "
        f"交互会话不是 job，claude stop 停不掉；要看用 cca {name} agents"
    )

STOP_BG_USAGE = "cca stop-bg <profile>|--all [--force]"


def cmd_stop_bg(args: list) -> int:
    """停掉某个号（或 --all 所有号）的后台会话：只 stop，不删记录。

    默认跳过正在跑的会话 —— 停会话是有破坏性的（正在跑的任务会被掐掉），
    所以这个动作必须显式发起，且要 --force 才会连在跑的一起停。
    """
    rest, flags = parse_flags(args, bool_flags=("--force", "--all"), usage=STOP_BG_USAGE)
    force = bool(flags.get("--force"))
    if flags.get("--all"):
        if rest:
            die(f"--all 不跟号名一起用；用法: {STOP_BG_USAGE}")
        return _stop_bg_all(force=force)
    if not rest:
        die(f"用法: {STOP_BG_USAGE}")
    if rest[1:]:
        die(f"多余参数 {' '.join(rest[1:])}；用法: {STOP_BG_USAGE}")
    name = rest[0]
    if not profile_dir(name).is_dir():
        die(f"profile {name} 不存在（{profile_dir(name)}）")
    if not active_workers(name):
        say(f"{name}: 没有后台会话")
        return 0
    _stopped, failures = _stop_bg_one(name, force=force)
    return 1 if failures else 0


def _stop_bg_one(name: str, *, force: bool, exclude_ids=()) -> tuple:
    """停一个号并报告，返回 (停掉的会话, 失败清单)。"""
    targets, skipped, not_jobs, failures = stop_sessions(name, force=force, exclude_ids=exclude_ids)
    describe_not_jobs(name, not_jobs)
    if not targets:
        if skipped:
            say(f"{name}: {len(skipped)} 个会话都在跑，未停止（确认要一起停就加 --force）")
        else:
            say(f"{name}: 没有可停的后台会话")
        return [], failures
    if skipped:
        note(f"{name}: 跳过 {len(skipped)} 个正在跑的会话（要一起停加 --force）")
    for f in failures:
        warn(f"{name} 停止失败 {f}")
    say(f"{name}: 已停止 {len(targets) - len(failures)}/{len(targets)} 个后台会话"
        f"（对话保留：cca {name} agents 里还在，选中就续）")
    failed = {f.split(":", 1)[0] for f in failures}
    return [s for s in targets if s.get("id") not in failed], failures


def _current_job() -> tuple:
    """执行这条命令的后台会话：(号, 短 id)；不在后台会话里返回 ("", "")。"""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")[:8]
    folder = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if not sid or not folder:
        return "", ""
    for name in discover_profiles():
        if profile_dir(name).resolve() == Path(folder).resolve():
            return name, sid
    return "", ""


def _stop_bg_all(*, force: bool) -> int:
    """所有号的后台会话一起停：换 workspace 前用。停掉的记进清单，到新 workspace 用 resume-bg 照单续。

    家目录是共享盘，进程和 /tmp 套接字跟着 pod 走：两个 workspace 跑同一个号会互相误判。
    在会话里敲这条命令时，本会话放到最后停 —— 先停它，命令自己就跟着断了；
    所以它在停之前先进清单。
    """
    my_name, my_id = _current_job()
    mine = [s for s in active_workers(my_name) if s.get("id") == my_id and is_job(s)] if my_id else []
    failures = []
    for name in discover_profiles():
        if active_workers(name):
            stopped, failed = _stop_bg_one(name, force=force, exclude_ids=(my_id,) if mine else ())
            _record_stopped(name, stopped)
            failures += failed
    if mine and force:
        _record_stopped(my_name, mine)
        say(f"已记下停掉的会话（{stopped_bg_file()}）；到新 workspace 用 cca resume-bg 照单续上")
        say(f"最后停本会话 {my_name} {my_id}（这条命令会随它一起结束）")
        r = _run_claude(["stop", mine[0]["job"]], my_name, capture=True)
        if r.returncode != 0:
            failures.append(f"{my_id}: {(r.stderr or r.stdout or '').strip()[:120]}")
            warn(f"{my_name} 停止失败 {failures[-1]}")
        return 1 if failures else 0
    if mine:
        note(f"本会话 {my_name} {my_id} 正在跑，没停（要连它一起停加 --force）")
    if _read_stopped():
        say(f"已记下停掉的会话（{stopped_bg_file()}）；到新 workspace 用 cca resume-bg 照单续上")
    return 1 if failures else 0


# ---------------------------------------------------------------- 换 workspace：记下来、照单续

RESUME_BG_USAGE = "cca resume-bg [--dry-run]"
# 停之前在跑的会话，续上后说这一句；挂着的只重新登记，不跑模型
RESUME_BG_NUDGE = ("换了 workspace，接着刚才没做完的事。旧 workspace 里的后台进程（监控脚本、后台命令）"
                   "都已经停了：先查一下进度，需要的重新挂上。")


def _read_stopped() -> dict:
    """清单：完整会话编号 → {account, id, name, cwd, state, stopped_at}。读不到当空。"""
    try:
        data = json.loads(stopped_bg_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_stopped(data: dict) -> None:
    path = stopped_bg_file()
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _record_stopped(name: str, sessions: list) -> None:
    """把停掉的会话并进清单（按完整编号去重；多次 --all 不会互相覆盖）。"""
    if not sessions:
        return
    data = _read_stopped()
    for s in sessions:
        full = str(s.get("full") or "")
        if len(full) < len("00000000-0000-0000-0000-000000000000"):
            path = session_transcript(profile_dir(name), str(s.get("id") or ""))
            full = path.stem if path is not None else full
        if not full:
            continue
        data[full] = {"account": name, "id": full[:8], "name": str(s.get("name") or ""),
                      "cwd": str(s.get("cwd") or ""), "state": str(s.get("state") or ""),
                      "stopped_at": time.time()}
    _write_stopped(data)


def cmd_resume_bg(args: list) -> int:
    """照 `stop-bg --all` 记下的清单，在原号把会话续上。

    停之前在跑的：续上后发一句接着做；挂着的（blocked/done/idle）：只重新登记，不跑模型、不烧额度。
    续上一条就从清单里划掉一条；失败的留着，再跑一次接着续。
    """
    rest, flags = parse_flags(args, bool_flags=("--dry-run",), usage=RESUME_BG_USAGE)
    if rest:
        die(f"多余参数 {' '.join(rest)}；用法: {RESUME_BG_USAGE}")
    data = _read_stopped()
    if not data:
        say("清单是空的：只有 cca stop-bg --all 停掉的会话会记下来")
        return 0
    items = sorted(data.items(), key=lambda kv: (kv[1].get("account", ""), kv[1].get("name", "")))
    for _full, e in items:
        mark = "接着做" if is_running(e.get("state", "")) else "只挂回"
        say(f"{e.get('account', '?'):8} {e.get('id', '')}  {mark}  {e.get('name') or '(无标题)'}  [{e.get('state')}]")
    if flags.get("--dry-run"):
        say(f"共 {len(items)} 条（--dry-run：没动手）")
        return 0
    from cca.session.takeover import deliver_after_resume, registry_entry, resume_quietly  # noqa: PLC0415

    failed = 0
    for full, e in items:
        account = str(e.get("account") or "")
        if registry_entry(account, full):
            say(f"{account} {full[:8]} 已经挂着，跳过")
        else:
            error = resume_quietly(account, full, str(e.get("cwd") or ""))
            if not error and is_running(e.get("state", "")):
                error = deliver_after_resume(account, full, RESUME_BG_NUDGE, sender={"name": "cca resume-bg"})
            if error:
                failed += 1
                warn(f"{account} {full[:8]} 没续上：{error}")
                continue
            say(f"{account} {full[:8]} 已续上")
        data.pop(full, None)
        _write_stopped(data)
    say(f"续上 {len(items) - failed}/{len(items)} 条" + ("；没续上的留在清单里，再跑一次 cca resume-bg" if failed else ""))
    return 1 if failed else 0

def last_transcript_entry(path: Path, *, skip_types=frozenset(), need_ts: bool = False,
                          limit: int = 262144):
    """transcript 最后一条能解析的记录（只读尾部 `limit` 字节）。读不到返回 None。

    `skip_types` 跳过"只记账、不代表会话在干活"的记录（见 `WAIT_BOOKKEEPING_TYPES`）；
    `need_ts=True` 只认带 `timestamp` 的记录 —— 记账类记录大多没有时间戳，这一步能把
    `relocated` 这类还没列进表的也一起滤掉。
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue                      # 尾部可能被截断成半行，跳过
        if not isinstance(entry, dict) or entry.get("type") in skip_types:
            continue
        if need_ts and _parse_ts(entry.get("timestamp")) is None:
            continue
        return entry
    return None

def entry_is_limit_error(entry) -> bool:
    """这条记录是不是额度/限流错误。"""
    if not isinstance(entry, dict) or not entry:
        return False
    text = json.dumps(entry, ensure_ascii=False).lower()
    return any(marker in text for marker in WAIT_LIMIT_MARKERS)
