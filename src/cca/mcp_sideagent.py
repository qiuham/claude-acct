"""MCP 服务：让主对话像喊子代理一样指挥另一个号。

主对话加载这个服务后，直接调工具即可，不用记命令：

    sideagent_send      号名 = 派新活（新实例，干完退出）；实例全名 = 追问那一个；返回任务编号
    sideagent_result    取结果：最相关的一条，或按任务编号取那一条
    sideagent_interrupt 只打断手上这一轮，进程留着，接着能发
    sideagent_status    看sideagent在不在跑、在干什么、用了多少词元、是不是旧版本
    sideagent_stop      停掉sideagent

底层就是 `cca sideagent` 的常驻进程（claude 官方 stream-json 模式），MCP 只是
一个转接层：同一份会话、同一个上下文。

注册（写进当前号的配置，重启会话后生效；`cca new`/`cca add` 建号时会自动注册）：

    cca mcp install            # 当前号
    cca mcp install --all      # 所有号
    cca mcp install --profile 003

手动启动（claude 会替你拉起，一般不用手跑）：

    cca mcp serve
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
import uuid
from contextlib import redirect_stderr

from cca.errors import die, say
from cca.paths import global_config_file
from cca.session import backends, jobs, sideagent, takeover

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "sideagents"
SERVER_VERSION = "0.1.0"
SERVER_KEY = "sideagents"
# 订阅号某个窗口用到这么多就算满，派不了活
FULL_PERCENT = 100

TOOLS = [
    {
        "name": "sideagent_send",
        "description": ("派活、追问，或者给别的会话发话：\n"
                        "1. account 只写号名（如 ds）= 派新活：一律新开一个实例，上下文从零开始，"
                        "不会塞给同号正在跑的实例；干完自动退出（像 Agent）。\n"
                        "2. account 写实例全名（如 ds#ff2832，派活时返回的那个）= 追问那一个（像 SendMessage）："
                        "它还在跑就按 mode 插话或排队；已经退出就用它的会话记录接着开，上下文还在，"
                        "任务编号新给一个；记录找不到会报错。返回任务编号，用 sideagent_result 取结果。\n"
                        "3. session 写别的会话的编号前缀或标题 = 给它发一句话（不用写 account）："
                        "不管它在哪个号、在跑还是已经 done / stopped，都会找到它现在挂的号，没在跑就唤醒，"
                        "用原生跨会话消息送到，它回话会回到这里。不接管、不进面板。"
                        "原生 SendMessage 发不到（not reachable、ListAgents 里没有）的会话就用这个，"
                        "不要说它「下线了」、也不要等它「上线」。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string",
                            "description": "号名（如 ds、002）= 派新活；实例全名（如 ds#ff2832）= 追问那一个；"
                                           "和 session 一起给 = 只在这个号上找、在这个号上唤醒"},
                "session": {"type": "string",
                            "description": "给别的会话发话：它的编号前缀、完整编号或标题"},
                "name": {"type": "string",
                         "description": "可选：横条上显示的名字（像给子代理起的名字）；不给就显示号名"},
                "description": {"type": "string",
                                "description": "可选：3-5 个词说这件活是什么（像 Agent 的 description）；"
                                               "横条中间还没有进度短语时显示它，回报的摘要也用它"},
                "model": {"type": "string",
                          "description": "可选：这个实例用哪个模型（如 sonnet、opus、haiku，按那个号的配置解析）；"
                                         "只对派新活有用，追问时沿用原来的"},
                "message": {"type": "string", "description": "要它做的事"},
                "timeout_seconds": {"type": "number", "description": "background=false 时最多等多久，默认 600 秒；后台派活不限时"},
                "background": {"type": "boolean",
                               "description": "默认 true：立刻返回，干完自动回报；显式传 false 才在原地等结果"},
                "include_context": {"type": "boolean",
                                    "description": "把主会话最近的对话摘要带上，第一次派活时建议开"},
                "context": {"type": "string",
                            "description": "接着哪段会话：会话编号或 auto（当前会话），等于子代理拿不到的原对话"},
                "keep": {"type": "boolean",
                         "description": "显式选项：干完不退出，留着（最多闲置 30 分钟）；默认干完就退，像子代理"},
                "notify": {"type": "boolean",
                           "description": "后台任务干完要不要回报：默认 true；传 false 就只写结果文件，自己去查"},
                "mode": {"type": "string", "enum": ["interject", "queue"],
                         "description": ("追问正在忙的实例时怎么送，照 SendMessage："
                                         "interject（默认）在它下一个工具调用结束时送到；它要是先做完，"
                                         "就用这条消息接着开、做完再回报一次。"
                                         "queue 是显式选项：等这一轮干完再单独开一轮。它闲着时两种一样")},
            },
            "required": ["message"],
        },
    },
    {
        "name": "sideagent_result",
        "description": ("取某个号后台任务的结果：running 表示还在跑；finished 时带回复正文；"
                        "interrupted 被打断；stopped 被停掉；merged 是插话，回应在 merged_into 那条任务里。"
                        "给 job_id 就只取那一条；不给就挑最相关的（在跑的 > 有内容的已完成 > 其他）。"
                        "account 可以是号名，也可以是完整名字（如 ds#ab12cd）。"),
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string"},
                           "job_id": {"type": "string",
                                      "description": "sideagent_send 返回的任务编号"}},
            "required": ["account"],
        },
    },
    {
        "name": "sideagent_interrupt",
        "description": ("打断某个 sideagent 手上这一轮（像按 Esc），进程不停、上下文还在，"
                        "排队的下一条会接着跑。要整个停掉用 sideagent_stop。"),
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string",
                                       "description": "号名或完整名字（如 ds#ab12cd）"}},
            "required": ["account"],
        },
    },
    {
        "name": "sideagent_status",
        "description": ("看某个号的sideagent在不在跑、会话编号、闲置了多久。"
                        "只列本会话派的任务：写号名就把本会话在这个号下的任务（含 ds#编号）列出来，"
                        "多个列成数组；不给号 = 本会话派出去的全部。"
                        "别的号、已经结束的会话都不在这里——看不到不代表会话不在了；"
                        "要给它发话用 sideagent_send {session: <编号或标题>}，会自动找号、唤醒。"),
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string"}},
        },
    },
    {
        "name": "sideagent_takeover",
        "description": ("接管一个 Claude Code 原生会话（后台会话或别的会话），挂到本会话的 sideagent 面板上，"
                        "本号、跨号都行。不给 session = 列出本机各号能接管的会话（编号、号、目录、标题、状态）。"
                        "给 session（编号前缀、完整会话编号或名字）= 接管：没在跑就在它原来的号上用 "
                        "claude --bg --resume 接着跑（原来是后台会话的保留原编号和原设置；普通会话一律全部自动批准），"
                        "返回接上后的名字，之后它的进度和完成通知会一直送进本会话、显示在面板上。"
                        "只想给它发一句话不用接管：sideagent_send {session: ...} 会自动找号、唤醒、投递。"
                        "本会话退出时不会停它；从面板拿掉用 sideagent_release。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "会话编号前缀、完整编号或名字；不给就列出能接管的"},
                "account": {"type": "string", "description": "可选：只在这个号里找（默认所有号）"},
            },
        },
    },
    {
        "name": "sideagent_release",
        "description": "把 sideagent_takeover 接上的会话从本会话的面板上拿掉；会话本身不停，接着跑。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "sideagent_takeover 返回的名字"}},
            "required": ["name"],
        },
    },
    {
        "name": "sideagent_accounts",
        "description": ("列出本机能派活的号（不起进程）：号名、类型（claude 订阅号 / api 按量号）、"
                        "模型家族（Claude、DeepSeek、GPT 等）和模型名、订阅号 5 小时和 7 天已用百分比、"
                        "额度满没满、后台在跑几个。号名因人而异，派活前用它查，别照搬例子里的名字。"
                        "sideagent_send 的 account 就填这里的 name。"
                        "排序即推荐顺序：能用的订阅号在前，按周额度从早到晚重置排，priority 1 最先用"
                        "（快重置的那部分额度不用就作废）；api 号按量计费排在后面；用不了的在最后。"),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sideagent_stop",
        "description": "停掉某个号的sideagent进程。",
        "inputSchema": {
            "type": "object",
            "properties": {"account": {"type": "string"}},
            "required": ["account"],
        },
    },
]


def cmd_mcp(args: list) -> int:
    action = args[0] if args else "serve"
    if action in ("-h", "--help"):
        say(__doc__ or "cca mcp serve | install [--profile 号 | --all]")
        return 0
    if action == "serve":
        return serve()
    if action == "install":
        return _install(args[1:])
    return die(f"不认识的用法: {action}（可用 serve / install）")


def serve() -> int:
    """按 MCP 的 stdio 协议收请求、回响应。一行一条 JSON。

    **stdout 只留给协议**：cca 自己的提示（`say`）会 print 到 stdout，直接混进来会
    把 JSON-RPC 流弄坏，所以处理期间把 sys.stdout 指到 stderr。

    **工具调用各开一条线程**：派活常常要等sideagent跑几十秒，串行会让后面的调用
    干等着；同时发几个就同时跑几个，回包用一个锁排队写。
    """

    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    write_lock = threading.Lock()

    def reply(response) -> None:
        if response is None:
            return
        with write_lock:
            protocol_out.write(json.dumps(response, ensure_ascii=False) + "\n")
            protocol_out.flush()

    # 本会话接管的原生会话：后台线程定时同步进面板读的文件，面板就不用每次刷新起进程
    stop_sync = threading.Event()
    threading.Thread(target=backends.sync_loop, args=(stop_sync, jobs.my_session),
                     daemon=True, name="sideagent-sync").start()
    workers: list = []
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if request.get("method") == "tools/call":
            worker = threading.Thread(target=lambda req=request: reply(handle(req)),
                                      daemon=True)
            worker.start()
            workers.append(worker)
        else:
            reply(handle(request))
    for worker in workers:
        worker.join()
    stop_sync.set()
    return 0


def handle(request: dict) -> dict | None:
    method = str(request.get("method") or "")
    request_id = request.get("id")
    if request_id is None:
        return None                      # 通知，不回复
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}
        return _ok(request_id, result)
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _call(request_id, request.get("params") or {})
    return _error(request_id, -32601, f"不支持的方法: {method}")


def _call(request_id, params: dict) -> dict:
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    # cca 的 die() 把原因写到 stderr；收起来回给模型，别只说“详情见 stderr”
    complaints = io.StringIO()
    try:
        with redirect_stderr(complaints):
            text = TOOL_HANDLERS[name](_named_by_label(name, arguments))
    except KeyError:
        return _error(request_id, -32602, f"不存在的工具: {name}")
    except SystemExit:
        reason = complaints.getvalue().strip() or "cca 拒绝执行"
        return _content(request_id, reason, is_error=True)
    except sideagent.SideagentError as exc:
        return _content(request_id, str(exc), is_error=True)
    except Exception as exc:  # noqa: BLE001 - MCP 层兜住，别把会话带崩
        return _content(request_id, f"{type(exc).__name__}: {exc}", is_error=True)
    return _content(request_id, text)


# 这几个工具的 name / account 可以写面板上看到的名字（例如 ham-fa），不只是记录名（005#bg-13ca09b7）
_NAME_ARGS = {"sideagent_release": "name", "sideagent_stop": "account", "sideagent_status": "account",
              "sideagent_result": "account", "sideagent_interrupt": "account", "sideagent_send": "account"}


def _named_by_label(tool: str, arguments: dict) -> dict:
    """面板名字换成记录名：只在本会话名单里找，且只有一条对上才换；别的原样返回。"""
    key = _NAME_ARGS.get(tool)
    value = str(arguments.get(key) or "").strip() if key else ""
    if not value or "#" in value or value in sideagent.agent_names():
        return arguments
    mine = jobs.my_roster()
    hits = [name for name, entry in mine.items()
            if value in (str(entry.get("label") or ""), str(sideagent._read_state(name).get("label") or ""))]  # noqa: SLF001
    return {**arguments, key: hits[0]} if len(hits) == 1 else arguments


def _owned(name: str, mine: dict | None = None) -> bool:
    """这个 sideagent 是不是本会话用着的（派出的或接管的）；规则见 jobs.owns。"""
    return jobs.owns(name, mine)


def _instances(account: str) -> list:
    """本会话在这个号下还没停的 sideagent 名字：长驻的 <号>，加临时的 <号>#哈希。

    归属看本会话的名单（见 _owned）；号名只用来缩小范围。
    """
    if "#" in account:
        return [account] if _owned(account) else []
    mine = jobs.my_roster()
    return [name for name in sideagent.agent_names(include_finished=False)
            if (name == account or name.startswith(account + "#")) and _owned(name, mine)]


def _queue_note(info: dict) -> str:
    """它正忙时说明这条是排队等，不是打断当前这轮。"""
    if not info.get("busy"):
        return ""
    ahead = int(info.get("queued") or 0) + 1
    return f"（它正忙，前面还有 {ahead} 条，按顺序一条条跑）"


def _send_reply(name: str, job: str, info: dict, *, interject: bool, new: bool,  # noqa: PLR0913
                resumed: bool) -> str:
    """回话照子代理：派新活照 Agent 的后台回话；追问照 SendMessage 的回话。"""
    look = f'要现在看就用 sideagent_result {{account: "{name}", job_id: "{job}"}}。'
    if new:
        return (f"{name}: 已在后台开始，任务编号 {job}。干完会自动回报（task-notification），不用轮询；{look}\n"
                f'要接着问它，用 sideagent_send {{account: "{name}", message: ...}}。')
    if resumed:
        # 照 SendMessage 对已结束代理的回话（Resuming agent X）
        return (f"{name}: 正在用它的会话记录接着开（上下文还在），任务编号 {job}；做完会回报。{look}")
    # 插话照样发给它（11-01 以后的进程都认插话）；“旧版”只说明代码改过，不等于不认插话
    if info.get("busy") and interject:
        # 照 SendMessage 对正在跑的代理的回话
        return (f"{name}: 消息已排上，在它下一个工具调用结束时送到（任务编号 {job}）。"
                "它要是先做完，会用这条消息接着开、做完再回报一次；等那条回报，别重发。")
    if info.get("busy"):
        return (f"{name}: 它正忙，这条排在后面，这一轮干完再单独开一轮{_queue_note(info)}，"
                f"任务编号 {job}；做完会回报。{look}")
    return f"{name}: 它闲着，马上开始处理，任务编号 {job}；做完会回报。{look}"


def _interject_note(info: dict) -> str:
    """插话时说清楚：忙就并进这一轮，闲就当新的一轮。"""
    if info.get("busy"):
        return "（插话：送进它正在跑的这一轮，下一个工具调用结束就能看到）"
    return "（它闲着，插话就当成新的一轮）"


def _running_instances(account: str) -> list:
    return [name for name in _instances(account) if sideagent.running(name)]


def _route(account: str, context: str = "", label: str = "", *, description: str = "",  # noqa: PLR0913
           model: str = "", caller_agent: str = "") -> tuple:
    """这句话交给哪个 sideagent：返回（实例名，是不是新起的）。

    号名（ds）= 派新活：一律新开实例，上下文从零开始（像 Agent）。
    实例全名（ds#ff2832）= 追问：在跑就交给它；已经退出就用它的会话记录接着开（像 SendMessage）。
    """
    if "#" not in account:
        return sideagent.prepare(account, context=context, label=label, description=description, model=model,
                            caller_agent=caller_agent)
    if not _owned(account):
        message = f"{account} 不是本会话派的，看不到也发不了"
        raise sideagent.SideagentError(message)
    if sideagent.running(account):
        return account, False
    return account, _revive(account)


def _revive(name: str) -> bool:
    """已经退出的实例：用它的 claude 会话记录接着开，同一个名字、上下文还在。"""
    record = _read_job_exact(name)
    session = str(record.get("session_id") or "")
    if not session:
        msg = (f"{name}: 找不到它的会话记录，没法接着开（可能是老版本派的，或用 context 分叉、没存会话）。"
               "派新活请只写号名")
        raise sideagent.SideagentError(msg)
    if not sideagent.transcript_exists(name, session):
        msg = f"{name}: 会话记录 {session} 在 {sideagent.account_of(name)} 号里找不到了，没法接着开"
        raise sideagent.SideagentError(msg)
    return sideagent.ensure(name, cwd=str(record.get("cwd") or ""), resume=session,
                       label=str(record.get("label") or ""),
                       description=str(record.get("description") or ""),
                       model=str(record.get("model") or ""),
                       caller_agent=str(record.get("caller_agent") or ""))


def _status_of(name: str) -> dict:
    """一个 sideagent 的状态：按实例种类交给对应后端，两个工具口径一致。"""
    return backends.backend_for(name).status(name)


def _tool_send(arguments: dict) -> str:
    session = str(arguments.get("session") or "").strip()
    if session:
        return _send_to_session(session, str(arguments.get("message") or ""),
                                str(arguments.get("account") or ""))
    return _send_job(arguments)


def _send_job(arguments: dict) -> str:
    """派新活或追问本会话派的实例。"""
    account = str(arguments.get("account") or "")
    message = str(arguments.get("message") or "")
    if not account or not message:
        return "要给 account 和 message（给别的会话发话就写 session）"
    backend = backends.backend_for(account)
    if not backend.takes_jobs:
        return _send_direct(account, message, backend)
    timeout = float(arguments.get("timeout_seconds") or sideagent.SEND_TIMEOUT)
    if arguments.get("include_context"):
        context = _caller_context()
        if context:
            message = f"{context}\n\n现在要做的事：{message}"
    # 照 SendMessage：给实例发消息默认在它下一个工具调用结束时送到；queue 只作显式选项
    mode = str(arguments.get("mode") or "interject")
    if mode not in ("queue", "interject"):
        return "mode 只能是 queue 或 interject"
    was_running = "#" in account and sideagent.running(account)
    name, started = _route(account, context=str(arguments.get("context") or ""),
                            label=str(arguments.get("name") or ""),
                            description=str(arguments.get("description") or ""),
                            model=str(arguments.get("model") or ""),
                            # 面板插件按 tool.call 的 agentId 塞进来：派活的是哪个子代理（主对话派的没有）
                            caller_agent=str(arguments.get("caller_agent") or ""))
    keep = bool(arguments.get("keep"))
    interject = mode == "interject"
    info = sideagent.status(name)
    if arguments.get("background", True):
        job = _start_job(name, message, {
            "keep": keep,
            "notify": bool(arguments.get("notify", True)),
            "interject": interject,
        })
        return _send_reply(name, job, info, interject=interject,
                           new="#" not in account, resumed=started and not was_running)
    try:
        result = sideagent.ask(name, message, timeout, notify=bool(arguments.get("notify")),
                          interject=interject, keep=keep)
    finally:
        _remember_session(name)
        # 干完就退，像子代理：同一个实例后面还有活（插话、排队）时 stop_later 会等它闲下来
        if not keep:
            sideagent.stop_later(name)
    if result.get("merged"):
        into = result.get("merged_into") or "?"
        return f"{name}: 插话已送进它正在跑的这一轮（任务 {into}），回应会写在那条任务的结果里。"
    # 照子代理前台派活的回话：结果正文，接着说怎么追问，最后是用量
    usage = (f"<usage>total_tokens: {result.get('token_count') or 0}\n"
             f"tool_uses: {result.get('tool_uses') or 0}\n"
             f"duration_ms: {result.get('duration_ms') or 0}</usage>")
    return (f"{result.get('text') or ''}\n"
            f"实例 {name}（要接着问它，用 sideagent_send 发给这个名字）\n{usage}")


def _tool_status(arguments: dict) -> str:
    account = str(arguments.get("account") or "")
    if not account:
        return _all_status()
    names = _instances(account)
    if not names:
        return json.dumps({"account": account, "running": False, "state": "none"},
                          ensure_ascii=False)
    if len(names) == 1:
        return json.dumps(_status_of(names[0]), ensure_ascii=False)
    return json.dumps([_status_of(name) for name in names], ensure_ascii=False)


def _send_to_session(key: str, message: str, account: str) -> str:
    """给任意一条会话发一句话（不接管）：它挂在哪个号就发到哪，没在跑先唤醒。"""
    if not message:
        return "要给 message"
    entry = sideagent.caller_now()
    if not entry.get("messagingSocketPath"):
        return "找不到本会话的登记条目，对方回话没处送；重启会话后再试"
    sender = {**entry, "account": sideagent._owner_account()}  # noqa: SLF001
    result = takeover.message(key, message, sender, account=account, usable=_unusable_reason)
    if result.get("error"):
        return str(result["error"])
    where = f"{result['account']}/{result['session_id'][:8]}（{result['label'] or '无标题'}）"
    how = "没在跑，已唤醒并送到" if result["woke"] else "在跑，已送到"
    return (f"{where}{how}。它的回话会作为跨会话消息回到这里；没有接管，它的进度不会进本会话。"
            "还要说就再用同样的 session 发。")


def _unusable_reason(name: str) -> str:
    """这个号现在能不能起会话：能用返回空串，不能用返回原因（额度用满、没登录、没配 key）。"""
    from cca.report import _fetch_list_rows, _list_targets  # noqa: PLC0415 - 只在要唤醒时用

    rows = _fetch_list_rows([name], _list_targets([name], cached_only=False), refresh=False)
    if not rows:
        return "读不到这个号"
    entry = _account_entry(rows[0])
    return "" if entry.get("usable") else str(entry.get("why") or "用不了")


def _send_direct(name: str, message: str, backend: backends.Backend) -> str:
    """不走后台任务的实例（接管的原生会话）：直接发一句，结果看面板或 sideagent_result。"""
    if not _owned(name):
        return f"{name} 不是本会话接管的"
    error = backend.tell(name, message, interject=True)
    return error or (f"已发给 {name}（接管的原生后台会话），它在忙就在下一个工具调用结束时看到。"
                     "它闲下来时面板会通知；也可以用 sideagent_result 看它最后说的话。")


def _tool_takeover(arguments: dict) -> str:
    key = str(arguments.get("session") or "").strip()
    account = str(arguments.get("account") or "").strip()
    if not key:
        rows = takeover.candidates([account] if account else None)
        if not rows:
            return "本机没有能接管的会话"
        return json.dumps([{k: row[k] for k in ("account", "id", "name", "kind", "cwd", "state")}
                           for row in rows], ensure_ascii=False)
    entry = sideagent.caller_now()
    owner = ({**sideagent.owner_of(entry), "account": sideagent._owner_account(),  # noqa: SLF001
              "instance_socket": str(entry.get("messagingSocketPath") or "")} if entry else {})
    if not owner.get("session"):
        return "找不到本会话的登记条目，接不上（面板要按会话认归属）"
    result = takeover.takeover(key, owner, account=account)
    if result.get("error"):
        return str(result["error"])
    how = "已在原号接着跑" if result.get("woke") else "本来就在跑"
    return (f"已接管 {result['account']}/{result['session_id'][:8]}（{result['label'] or '无标题'}），{how}。"
            f"名字 {result['name']}：用 sideagent_send 发给这个名字；它闲下来时面板会通知。"
            + (f"\n\n{result['card']}" if result.get("card") else ""))


def _tool_release(arguments: dict) -> str:
    name = str(arguments.get("name") or "")
    if not backends.backend_for(name).outlives_owner or name not in jobs.my_roster():
        return f"{name} 不是本会话接管的会话"
    left = takeover.release(name, _my_session())
    others = f"；还有 {left} 个会话接管着它" if left else ""
    return f"{name} 已从本会话的面板拿掉；会话本身没停{others}"


def _tool_stop(arguments: dict) -> str:
    account = str(arguments.get("account") or "")
    if not account:
        return "要给 account"
    names = _instances(account) or [account]
    return "\n".join(backends.backend_for(name).stop(name) for name in names)


def _tool_interrupt(arguments: dict) -> str:
    account = str(arguments.get("account") or "")
    if not account:
        return "要给 account"
    backend = backends.backend_for(account)
    if not backend.takes_jobs:
        return backend.interrupt(account)
    alive = _running_instances(account)
    if not alive:
        return f"{account}: 本会话没有在跑的 sideagent"
    if len(alive) > 1:
        names = "、".join(alive)
        msg = f"{account} 有 {len(alive)} 个在跑：{names}。给完整名字（例如 {alive[0]}）再打断。"
        raise sideagent.SideagentError(msg)
    return backends.backend_for(alive[0]).interrupt(alive[0])


def _all_status() -> str:
    mine = jobs.my_roster()
    rows = []
    for name in sideagent.agent_names(include_finished=False):
        state = sideagent._read_state(name)  # noqa: SLF001
        # 不记在谁名下、也没记主人的（命令行直接起的）不算本会话的
        if name not in mine and not (state.get("owner_pid") or state.get("owner_session")):
            continue
        if not _owned(name, mine):
            continue
        rows.append(json.dumps(_status_of(name), ensure_ascii=False))
    return "\n".join(rows) if rows else "本会话没有sideagent在跑"


def _model_family(kind: str, model: str, provider: str) -> str:
    """号用的模型家族：订阅号一律 Claude；api 号看模型名，再看供应商。"""
    if kind != "api":
        return "Claude"
    text = f"{model} {provider}".lower()
    for word, family in (("deepseek", "DeepSeek"), ("gpt", "GPT"), ("claude", "Claude"),
                         ("gemini", "Gemini"), ("qwen", "Qwen"), ("kimi", "Kimi"), ("glm", "GLM")):
        if word in text:
            return family
    return "其他"


def _account_entry(row: dict) -> dict:
    """`cca ls` 的一行 → 给模型看的一条：只留挑号要用的字段。"""
    from cca.account.store import read_api_key, read_marker  # noqa: PLC0415 - 只在列号时用
    from cca.report import format_clock  # noqa: PLC0415

    name = row["profile"]
    kind = row["kind"]
    model = ""
    if kind == "api":
        model = str(((read_marker(name)[0] or {}).get("env") or {}).get("ANTHROPIC_MODEL") or "")
    entry = {"name": name, "kind": "api 按量" if kind == "api" else "claude 订阅",
             "family": _model_family(kind, model, str(row.get("provider") or ""))}
    if model:
        entry["model"] = model
    if kind == "api":
        entry["endpoint"] = row.get("api_label") or row.get("base_url") or ""
        entry["usable"] = bool(read_api_key(name))
        if not entry["usable"]:
            entry["why"] = "没配 key"
    else:
        windows = (row.get("usage") or {}).get("windows") or {}
        full = False
        for key, label in (("five_hour", "five_hour_used"), ("seven_day", "seven_day_used")):
            window = windows.get(key)
            if not window:
                continue
            percent = 0 if window.get("reset_passed") else int(window.get("percent") or 0)
            entry[label] = f"{percent}%"
            entry[label.replace("_used", "_resets")] = format_clock(window.get("resets_at"))
            full = full or percent >= FULL_PERCENT
        entry["usable"] = bool(row.get("logged_in")) and not full
        if not row.get("logged_in"):
            entry["why"] = "没登录"
        elif full:
            entry["why"] = "额度用满，等重置"
    background = row.get("background") or {}
    entry["running"] = int(background.get("running") or 0)
    return entry


def _tool_accounts(arguments: dict) -> str:  # noqa: ARG001 - 没有参数
    from cca.account.model import discover_profiles  # noqa: PLC0415
    from cca.report import _fetch_list_rows, _list_targets  # noqa: PLC0415

    names = discover_profiles()
    if not names:
        return "本机没有号：先用 cca add / cca login 加一个"
    # 和 cca ls 同一套数据：额度走它的 5 分钟缓存，取不到就用本地快照
    rows = _fetch_list_rows(names, _list_targets(names, cached_only=False), refresh=False)
    return json.dumps(_by_priority([(_account_entry(row), row) for row in rows]), ensure_ascii=False)


def _by_priority(pairs: list) -> list:
    """能用的订阅号排最前，按周额度从早到晚重置，并标 priority（1 最先用）；再是 api 号；用不了的最后。

    同样能用时先用周额度早重置的号：它剩下的额度到点就作废，晚重置的号留着不会跑。
    """
    from cca.api.usage import weekly_reset_key  # noqa: PLC0415

    def key(pair: tuple) -> tuple:
        entry, row = pair
        api = entry["kind"] != "claude 订阅"
        return (not entry.get("usable"), api,
                float("inf") if api else weekly_reset_key((row.get("usage") or {}).get("windows") or {}))

    ranked = [entry for entry, _ in sorted(pairs, key=key)]
    rank = 0
    for entry in ranked:
        if entry.get("usable") and entry["kind"] == "claude 订阅":
            rank += 1
            entry["priority"] = rank
    return ranked


def _tool_result(arguments: dict) -> str:
    account = str(arguments.get("account") or "")
    if not account:
        return "要给 account"
    job_id = str(arguments.get("job_id") or "")
    return json.dumps(backends.backend_for(account).result(account, job_id), ensure_ascii=False)


def _caller_context(max_chars: int = 4000) -> str:
    """主会话最近的对话摘要：给sideagent补上它看不到的背景。"""
    from pathlib import Path  # noqa: PLC0415

    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    session_id = _my_session() or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not folder or not session_id:
        return ""
    encoded = "-" + str(Path.cwd()).strip("/").replace("/", "-")
    path = Path(folder) / "projects" / encoded / f"{session_id}.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return ""
    picked = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant") or not isinstance(content, list):
            continue
        text = "".join(str(c.get("text") or "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text").strip()
        if text and not text.startswith("<"):
            picked.append(f"{role}: {text[:500]}")
    body = "\n".join(picked[-8:])
    if not body:
        return ""
    return "背景（主会话最近对话的摘要）：\n" + body[-max_chars:]


# 任务记录和归属挪到了 jobs 模块；这里留旧名字，MCP 内部和测试照旧能用
_fill_record = jobs.fill_record
_mark_stopped = jobs.mark_stopped
_mark_running = jobs.mark_running
_me = jobs.me
_my_session = jobs.my_session
_owner_record = jobs.owner_record
_is_mine = jobs.is_mine
_progress_tail = jobs.progress_tail
_job_path = jobs.job_path
_job_files = jobs.job_files
_read_job_exact = jobs.read_job_exact
_jobs_of = jobs.jobs_of
_rank = jobs.rank
_best = jobs.best
_pick_job = jobs.pick_job
_read_job = jobs.read_job
_read_job_by_id = jobs.read_job_by_id
_write_job = jobs.write_job
_remember_session = jobs.remember_session
_owner_fields = jobs.owner_fields


TOOL_HANDLERS = {
    "sideagent_send": _tool_send,
    "sideagent_status": _tool_status,
    "sideagent_accounts": _tool_accounts,
    "sideagent_takeover": _tool_takeover,
    "sideagent_release": _tool_release,
    "sideagent_stop": _tool_stop,
    "sideagent_result": _tool_result,
    "sideagent_interrupt": _tool_interrupt,
}


def _start_job(name: str, message: str, opts: dict) -> str:
    """后台跑一次 ask；完成（或失败）后把结果写进 <名字>/result.json。返回任务编号。"""

    keep = bool(opts.get("keep"))
    notify = bool(opts.get("notify", True))
    interject = bool(opts.get("interject"))

    base = {"id": uuid.uuid4().hex[:8], "account": name, "started": _now(),
            **_owner_fields()}
    _write_job(name, {**base, "state": "running", "message": message})
    _remember_session(name)
    def run() -> None:
        try:
            # 后台任务：做完默认要叫醒主会话，不然只能自己去查
            # 后台派活不限时：sideagent 结束时一定交结果或报错（卡住、进程退出时连接会断）
            result = sideagent.ask(name, message, None, notify=notify, job=base["id"],
                              interject=interject, keep=keep)
            if result.get("merged"):
                _write_job(name, {**base, "state": "merged",
                                  "merged_into": result.get("merged_into") or ""})
                return
            _write_job(name, {**base,
                              "state": "interrupted" if result.get("interrupted") else "finished",
                              "text": result.get("text") or "",
                              "turns": result.get("turns"),
                              "duration_ms": result.get("duration_ms"),
                              "tool_uses": result.get("tool_uses"),
                              "token_count": result.get("token_count")})
        except Exception as exc:  # noqa: BLE001 - 后台任务，错误也要落盘
            _write_job(name, {**base, "state": "failed",
                              "error": f"{type(exc).__name__}: {exc}"})
        finally:
            _remember_session(name)
            # 干完就退，像子代理；同一个实例后面还有活时 stop_later 会等它闲下来
            if not keep:
                sideagent.stop_later(name)
    threading.Thread(target=run, daemon=True).start()
    return base["id"]


def _now() -> float:
    import time  # noqa: PLC0415
    return time.time()


def _ok(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _content(request_id, text: str, *, is_error: bool = False) -> dict:
    return _ok(request_id, {"content": [{"type": "text", "text": text}], "isError": is_error})


# ---------------------------------------------------------------- 注册


def register(profile: str):
    """把 sideagents 写进这个号的全局配置（幂等、不打印）；返回写到的文件，找不到 cca 返回 None。"""
    command = shutil.which("cca")
    if not command:
        return None
    path = global_config_file(profile)
    data = _read_json(path)
    servers = data.setdefault("mcpServers", {})
    # 旧名字留下的注册清掉，别让一个号挂两个服务
    for legacy in ("cca-sideagents", "cca-peers"):
        servers.pop(legacy, None)
    servers[SERVER_KEY] = {"command": command, "args": ["mcp", "serve"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _install(args: list) -> int:
    profiles = _target_profiles(args)
    for profile in profiles:
        path = register(profile)
        if path is None:
            die("找不到 cca 可执行文件，没法写进 MCP 配置")
        say(f"{profile}: 已注册 {SERVER_KEY}（{path}）—— 重启该号的会话后生效")
    return 0


def _target_profiles(args: list) -> list:
    from pathlib import Path  # noqa: PLC0415

    if "--all" in args:
        # 和 cca ls 同一套列号方式：不把 .cache、wait 这类目录算进来，001 也不会算两遍
        from cca.account.model import discover_profiles  # noqa: PLC0415

        return discover_profiles()
    for index, item in enumerate(args):
        if item.startswith("--profile="):
            return [item.split("=", 1)[1]]
        if item == "--profile" and index + 1 < len(args):
            return [args[index + 1]]
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    if not folder:
        die("不知道是哪个号：用 --profile <号>，或在某个号的会话里运行")
    return [Path(folder).name]


def _read_json(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
