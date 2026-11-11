"""启动环境：build_env、代理注入、API 号环境变量、会话窗口覆盖。"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cca.account.store import (
    api_env_vars,
    profile_kind,
    read_account_proxy,
    read_marker,
)
from cca.api import egress, regions
from cca.api.client import api_no_proxy, find_model, load_model_catalog, proxy_settings
from cca.config import (
    COMPACT_WINDOW_RATIO,
    GATEWAY_FIRST_BYTE_KEY,
    MANAGED_ENV_KEYS,
    NO_PROXY_VALUE,
    PROXY_KEYS,
    WINDOW_ENV_KEYS,
)
from cca.errors import die, note
from cca.paths import accounts_root, profile_dir

# 传给 claude 的时区：默认跟出口 IP 走；探测失败时用这个兜底。
CLAUDE_TZ = "Asia/Singapore"
# cca 自己在终端里显示时刻用的时区：固定上海。
DISPLAY_TZ = "Asia/Shanghai"
# shell 里的 TZ 先存下来，供 CCA_TZ=auto 用。cca 自己不改进程的 TZ：
# 终端显示在 report.format_clock 里按 DISPLAY_TZ 换算，子进程拿到的就是原样的值。
SHELL_TZ = (os.environ.get("TZ") or "").strip()


def local_tz() -> str:
    """本机时区：shell 里写的 IANA 名优先，其次 /etc/timezone，最后退回 CLAUDE_TZ。"""
    if SHELL_TZ and "/" in SHELL_TZ:
        return SHELL_TZ
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    return name or CLAUDE_TZ


def expected_session_tz(name: str) -> str | None:
    """这个号起会话会用的时区；出口探测失败返回 None。

    返回 None 时调用方不要据此做判断（比如强杀会话）：探测失败不等于时区不对。
    """
    choice = (os.environ.get("CCA_TZ") or "").strip().lower()
    if choice and choice != "egress":
        return launch_tz(None)
    marker = None
    if profile_kind(name) == "api":
        marker, _error = read_marker(name)
    proxy, _proxy_unset = proxy_settings(marker, read_account_proxy(name))
    info = egress.probe_cached(proxy)
    return launch_tz(info) if info else None


def probe_tz(name: str) -> str:
    """给"起一个会话"的路径用：经这个号的代理探测出口，按出口时区设 TZ（进程内缓存）。

    `CCA_TZ` 已经写死（不是 egress）时不必探测，省掉最多 3 秒的网络等待。
    探测失败时退回 CLAUDE_TZ，和以前的行为一致。
    """
    return expected_session_tz(name) or launch_tz(None)


def launch_tz(info: dict | None) -> str:
    """传给 claude 的 TZ。CCA_TZ=auto 跟本机、=egress 跟出口（默认）、或直接写 IANA 名。"""
    choice = (os.environ.get("CCA_TZ") or "").strip()
    lowered = choice.lower()
    if lowered == "auto":
        return local_tz()
    if choice and lowered != "egress":
        return choice
    return (info or {}).get("timezone") or CLAUDE_TZ


def is_openrouter(marker: dict) -> bool:
    """这个号是不是走 OpenRouter —— 按目录写会话窗口的逻辑只对它生效。

    别的中转（codex 那类）模型名不在 OpenRouter 目录里，套这套逻辑只会刷无用的提示；
    它们要窗口就自己 `--env CLAUDE_CODE_AUTO_COMPACT_WINDOW=…`。
    """
    if marker is None:
        return False
    if str(marker.get("provider") or "").lower() == "openrouter":
        return True
    host = (urllib.parse.urlsplit(str(marker.get("base_url") or "")).hostname or "").lower()
    return host == "openrouter.ai"

def model_flag_value(claude_args: list) -> str:
    """取本次启动 `--model X` / `--model=X` 的值；没写返回空串。"""
    args = list(claude_args)
    for index, arg in enumerate(args):
        if arg == "--model" and index + 1 < len(args):
            return str(args[index + 1])
        if str(arg).startswith("--model="):
            return str(arg).split("=", 1)[1]
    return ""

def compact_window_for(slug: str, catalog: list):
    """这次启动要不要写 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`：整数或 None。

    三分支（顺序即优先级）：
    1. 目录里查不到这个 slug → None（不猜）；
    2. slug 去掉 `vendor/` 前缀后是 `claude-…` → None（CC 自己知道该模型的窗口）；
    3. 其余按 `context_length × 系数` **向下取整到千**（系数默认 0.9，见
       `config.COMPACT_WINDOW_RATIO`；`CCA_COMPACT_WINDOW_RATIO` 可覆盖）——
       1M 窗口 → 900000，200k → 180000。
    """
    model = find_model(catalog, slug)
    if model is None:
        return None
    tail = str(slug or "").strip().split("/")[-1].lower()
    if tail.startswith("claude-"):
        return None
    context = model.get("context_length")
    if not isinstance(context, int) or context <= 0:
        return None
    return int(context * _compact_ratio() // 1000 * 1000)


def _compact_ratio() -> float:
    """压缩窗口系数：`CCA_COMPACT_WINDOW_RATIO` 覆盖，夹在 0.5 ~ 0.98。"""
    raw = os.environ.get("CCA_COMPACT_WINDOW_RATIO")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = COMPACT_WINDOW_RATIO
    else:
        value = COMPACT_WINDOW_RATIO
    return min(0.98, max(0.5, value))

def catalog_window_for_slug(slug: str):
    """按目录给某个 slug 算号级默认窗口（拿不到目录就 None —— 不猜）。"""
    catalog = load_model_catalog()
    if not catalog:
        return None
    return compact_window_for(slug, catalog)

def session_window_env(name: str, claude_args: list) -> dict:
    """本次会话的窗口覆盖（只有 OpenRouter 号 + 写了 `--model` 时才有内容）。

    号级 `settings.json` 一个字节都不动：`--model` 的语义就是"只作用于这次启动"。
    """
    slug = model_flag_value(claude_args)
    if not slug:
        return {}
    marker, _error = read_marker(name)
    if not is_openrouter(marker):
        return {}
    catalog = load_model_catalog()
    if not catalog:
        note(
            "拿不到 OpenRouter 目录（网络不通且没有可用缓存）：这次不设置上下文窗口。"
            "模型窗口拿不准时，可在会话里用 /autocompact 调，或设 CLAUDE_CODE_MAX_CONTEXT_TOKENS"
        )
        return {}
    if find_model(catalog, slug) is None:
        note(
            f"{slug} 不在 OpenRouter 目录里：这次不设置上下文窗口。"
            "模型窗口拿不准时，可在会话里用 /autocompact 调，或设 CLAUDE_CODE_MAX_CONTEXT_TOKENS"
        )
        return {}
    window = compact_window_for(slug, catalog)
    return {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(window)} if window else {}

def _is_claude_process(proc: Path) -> bool:
    """这个进程是不是 Claude Code 本体。

    只按 `CLAUDE_CONFIG_DIR` 认进程会把别的号的后台脚本（`sleep`、看门狗 shell）当成
    会话：这些脚本从父 shell 继承了配置目录，还常自带 `TZ=Asia/Shanghai`，于是合法
    会话被误判成"时区不对"，handoff / takeover 全被拦住。真会话的 argv 是
    `claude …`；npm 安装的老版本是 `node …/claude-code/cli.js`。
    """
    try:
        parts = (proc / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    argv = [part.decode(errors="replace") for part in parts if part]
    if not argv:
        return False
    if Path(argv[0]).name.lower().startswith("claude"):
        return True
    runtime = Path(argv[0]).name.lower()
    if runtime in ("node", "nodejs", "bun") and len(argv) > 1:
        script = argv[1].lower()
        return "claude-code" in script or Path(script).name == "claude"
    return False


def running_session_tz(name: str, proc_root: Path | None = None) -> list:
    """这个号正在跑的 claude 进程：`[(pid, TZ)]`。读不到的进程跳过。

    `proc_root` 只给测试用；默认扫 `/proc`。
    """
    wanted = f"CLAUDE_CONFIG_DIR={profile_dir(name)}".encode()
    root = Path("/proc") if proc_root is None else proc_root
    found = []
    for proc in root.iterdir():
        if not proc.name.isdigit() or not _is_claude_process(proc):
            continue
        try:
            raw = (proc / "environ").read_bytes()
        except OSError:
            continue
        entries = raw.split(b"\0")
        if wanted not in entries:
            continue
        tz = ""
        for item in entries:
            if item.startswith(b"TZ="):
                tz = item[3:].decode(errors="replace")
                break
        found.append((int(proc.name), tz or "(未设置)"))
    return sorted(found)


class LaunchBlocked(RuntimeError):
    """现在不能起会话（出口地区不支持、没配代理）。消息可以直接给用户看。"""


def session_env(name: str) -> dict:
    """起 claude 会话用的完整环境：代理、地区检查、语言、时区、遥测。

    **所有起会话的路径都必须走这里。** 只走 build_env() 的路径既不拦不支持的地区、
    也不设出口时区：handoff / takeover / wake / watchdog 以前就是这么漏掉的。

    探测按代理在进程内缓存，一个进程里同一个出口只查一次。
    """
    marker = None
    if profile_kind(name) == "api":
        marker, _error = read_marker(name)
    proxy, proxy_unset = proxy_settings(marker, read_account_proxy(name))
    if proxy is None and proxy_unset and marker is None:
        raise LaunchBlocked(
            "没有配置代理：出口不明。先给这个号配代理："
            f"cca set proxy {name} http://host:port；或设全局：cca set proxy --global http://host:port。"
            "确认要走本机直连：cca set proxy <号> none。")
    info = egress.probe_cached(proxy)
    if info and marker is None and info.get("country") and not regions.supported(info["country"]):
        raise LaunchBlocked(f"出口在 {info['country']}（{info.get('ip') or '?'}），"
                            "Claude 不支持这个地区，已阻止启动。")
    env = build_env(name)
    # 语言固定 en_US，不依赖出口探测是否成功
    env["LANG"] = egress.default_locale()
    env.pop("LC_ALL", None)
    # 时区默认跟出口 IP 走；CCA_TZ=auto 跟本机、写 IANA 名则写死（见 launch_tz）
    env["TZ"] = launch_tz(info)
    _apply_first_party(env)
    _apply_telemetry(env)
    # 有会话正在用别的时区跑，说明后台守护进程记着旧值：这时起新会话会继承旧时区。
    # 不静默起一个错的，直接拦住并说清怎么处理。
    wrong = [(pid, tz) for pid, tz in running_session_tz(name)
             if pid != os.getpid() and tz != env["TZ"]]
    if wrong:
        detail = "、".join(f"pid {pid}（{tz}）" for pid, tz in wrong[:3])
        raise LaunchBlocked(
            f"{name} 有正在跑的会话时区不对：{detail}，出口是 {env['TZ']}。"
            "先运行 cca doctor 处理（它会列出这些会话并问是否强杀），再启动。")
    return env


def launch_env(name: str, claude_args: list) -> dict:
    """启动该号要用的环境：`session_env()` 的结果 + 本次会话的窗口覆盖。"""
    try:
        env = session_env(name)
    except LaunchBlocked as exc:
        die(str(exc))
    env.update(session_window_env(name, claude_args))
    return env


def _apply_first_party(env: dict) -> None:
    """强制把接口当成 Anthropic 官方：CLI 不再按第三方标记处理。

    变量名开头有下划线，是 CLI 自己的隐藏开关，不是笔误。
    """
    env["_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"] = "1"


def _apply_telemetry(env: dict) -> None:
    """默认关遥测和错误上报；不碰非必要流量开关，自动更新照常。"""
    for key in ("DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING"):
        env.setdefault(key, "1")

# 父会话留给子进程的标记。从 claude 会话里启动新号时要清掉。
NESTED_ENV_KEYS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_ENTRYPOINT",
    # sideagent 归属：子进程要是自己也派活，得算它自己的会话
    "CCA_OWNER_SESSION",
    "CCA_OWNER_ACCOUNT",
)


def _scrub_nested(env: dict) -> None:
    """从 claude 会话里启动时，把父会话留下的标记清掉。

    父会话的环境里有“我在 Claude 里”“我是子会话”“消息 socket 在哪”这些变量。
    原样带走的话，新起的 claude 会以为自己也在父会话里，行为跟着变。
    只在 `CLAUDECODE` 存在时才清：普通终端启动没有它，完全不受影响。
    """
    if not env.get("CLAUDECODE"):
        return
    for key in NESTED_ENV_KEYS:
        env.pop(key, None)


def tui_mode() -> str:
    """渲染模式：`fullscreen`（默认）或 `default`（经典渲染）。

    fullscreen 用 alt-screen 渲染，面板里的鼠标点击只在它下面有效；
    想回到经典渲染：`cca set tui default`。
    """
    path = accounts_root() / "tui"
    try:
        return (path.read_text(encoding="utf-8").strip() or "fullscreen")
    except OSError:
        return "fullscreen"


def build_env(name: str) -> dict:
    """构造运行 claude 所需的环境：代理按 proxy_settings()，配置目录按号设置。

    API 号（有清单）先清掉本号管理的环境键再注入清单值 —— 继承来的 `ANTHROPIC_*` 会造成
    "拿 A 家 key 打 B 家端点"这类最难查的故障。**密钥不进环境**：它走 settings.json 的
    `apiKeyHelper`（见 docs/specs/api-profiles.md §3.3），这样 agent 自己跑的 Bash 也读不到。
    """
    env = dict(os.environ)
    _scrub_nested(env)
    # 渲染模式：fullscreen 就开 alt-screen 渲染（等价 claude --tui fullscreen）；
    # default 时清掉，免得从别的 fullscreen 会话里继承下来。
    if tui_mode() == "fullscreen":
        env["CLAUDE_CODE_NO_FLICKER"] = "1"
    else:
        env.pop("CLAUDE_CODE_NO_FLICKER", None)
    # 父进程（另一个号的会话、sideagent 的守护进程）留下的接口和模型变量一律清掉：
    # 订阅号带着别人的密钥去请求是 401，带着别人的模型名是“模型不存在”。
    for key in [name for name in env if name.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))]:
        env.pop(key, None)
    # 窗口开关同理：父会话（另一个号）的值会把这个号的 settings 顶掉，
    # 表现成"压缩比配置里写的早"。清掉，让各号用自己的 settings / 清单。
    for key in WINDOW_ENV_KEYS:
        env.pop(key, None)
    marker = None
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
        for key in set(MANAGED_ENV_KEYS) | set(marker["env"]):
            env.pop(key, None)
    proxy, _proxy_unset = proxy_settings(marker, read_account_proxy(name))
    if proxy is not None:
        for key in PROXY_KEYS:
            env[key] = proxy
        no_proxy = api_no_proxy(marker) if marker is not None else NO_PROXY_VALUE
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    # proxy is None（CCA_PROXY=none）：不注入内建代理，也不动继承来的代理变量 ——
    # 语义是"代理这事别管"，而不是"禁用代理"，所以 shell 里已有的设置原样保留。
    if marker is not None:
        # 使用者在 shell 里自己设的首字节上限优先于 cca 的网关默认值；清单 env 里写了则以清单为准
        user_first_byte = env.get(GATEWAY_FIRST_BYTE_KEY)
        env.update(api_env_vars(marker))
        if user_first_byte and GATEWAY_FIRST_BYTE_KEY not in marker["env"]:
            env[GATEWAY_FIRST_BYTE_KEY] = user_first_byte
    env["CLAUDE_CONFIG_DIR"] = str(profile_dir(name))
    return env
