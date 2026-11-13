"""账号生命周期：new / add / rm / handoff / model / sync 等命令。"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from cca.account.model import discover_profiles, read_identity, require_profile, valid_name
from cca.account.store import (
    _settings,
    copy_trust,
    ensure_api_config,
    ensure_onboarding_flag,
    key_env_name,
    mask_key,
    profile_kind,
    read_account_proxy,
    read_marker,
    seed_trust_from_default,
    write_account_proxy,
    write_api_key,
    write_marker,
    write_settings_env,
)
from cca.api.client import (
    api_display,
    api_launch_args,
    endpoint_models,
    follow_plan,
    load_model_catalog,
    normalize_proxy,
    preset_for_url,
    proxy_settings,
)
from cca.cli import merge_space_value_flags, parse_flags
from cca.config import (
    ADD_USAGE,
    ADD_VALUE_FLAGS,
    EFFORT_LEVELS,
    HANDOFF_CANCELLED,
    HANDOFF_STOP_POLL_SECONDS,
    HANDOFF_STOP_WAIT_SECONDS,
    HANDOFF_USAGE,
    MANAGED_ENV_KEYS,
    MODEL_ALIAS_FLAGS,
    MODEL_FOLLOW_KEYS,
    MODEL_USAGE,
    MODELS_USAGE,
    NO_BROWSER,
    PROVIDERS,
    PROXY_ENV_VAR,
    RM_USAGE,
    SELF_MOVE_NUDGE,
    SET_USAGE,
    SHARED_FILES,
    SHARED_LINKS,
    USAGE_LIVE_TTL,
)
from cca.env import LaunchBlocked, build_env, catalog_window_for_slug, is_openrouter, tui_mode
from cca.errors import die, note, say, warn
from cca.paths import (
    shared_dir,
    accounts_root,
    default_settings_file,
    key_file,
    launcher_argv,
    marker_file,
    profile_dir,
)
from cca.report import _plain, _table, make_console, state_text
from cca.session.push import (
    _run_claude,
    _scan_transcript,
    active_workers,
    handoff_candidates,
    is_job,
    live_session_ids,
    session_transcript,
    shares_sessions,
    stop_sessions,
    strip_tool_search_records,
    tool_search_records,
)

# 需要成对参数的子命令（handoff / import）与体积换算。
PAIR_ARGS = 2
BYTES_PER_KB = 1024


def _link_shared_items(d: Path, src: Path, link_items) -> None:
    """把共享项做成软链。

    共享源不存在就先把它建出来（新机器上 `~/.claude` 可能还没生成）：
    否则新号建完没有软链，会话和技能就共享不到。
    """
    for item in link_items:
        target = src / item
        if not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if SHARED_FILES and item in SHARED_FILES:
                    target.touch()
                else:
                    target.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
        link = d / item
        if link.is_symlink() or link.exists():
            continue
        link.symlink_to(target)

def _default_settings_source() -> Path | None:
    """播种源：`~/.cca/defaults/settings.json`，跟任何账号都解耦；没有就不播种。"""
    dest = default_settings_file()
    return dest if dest.is_file() else None


def _seed_settings(d: Path) -> bool:
    """建号时复制默认 settings；没有默认文件就不建（和以前一致）。"""
    source = _default_settings_source()
    if source is None:
        return False
    shutil.copy2(source, d / "settings.json")
    return True

def cmd_new(args: list, *, next_step: str | None = None) -> int:
    """建号。`next_step` 用来改写/抑制结尾的"下一步"：API 号已经配好了，不该再说 cca login。"""
    if not args:
        die("用法: cca new <profile>")
    name = args[0]
    isolated = "--isolated" in args[1:]
    if not valid_name(name):
        die(f"非法的 profile 名: {name!r}（这个名字被保留：与子命令或已移除命令同名）")
    d = profile_dir(name)
    if d.exists() and any(d.iterdir()):
        die(f"{d} 已存在且非空，拒绝覆盖")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    d.chmod(0o700)

    link_items = () if isolated else SHARED_LINKS
    _link_shared_items(d, shared_dir(), link_items)   # 共享资产留在 ~/.claude
    seeded = _seed_settings(d)

    if isolated:
        (d / ".cca-isolated.json").write_text(json.dumps({"isolated": True}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    say(f"已创建 profile {name}: {d}")
    say("  共享软链: " + (", ".join(i for i in link_items if (d / i).is_symlink()) or "(无)"))
    say("  独立文件: " + ("settings.json" if seeded else "(无)"))
    # None = 默认说法（订阅号路径）；空串 = 不打印（API 号自己会给"启动: cca <name>"）。
    if next_step is None:
        next_step = f"cca login {name}"
    if next_step:
        say("")
        say(f"下一步: {next_step}")
    return 0

def parse_key_flags(args: list, name: str) -> dict:
    """解析换 key 用的参数（`--key V` / `--key=V` / `--key-stdin`），其余一律拒绝。"""
    api = {}
    index = 0
    while index < len(args):
        arg = args[index]
        head, sep, value = arg.partition("=")
        if head == "--key":
            if not sep:
                index += 1
                if index >= len(args):
                    die("--key 需要跟一个值；用法: cca login <profile> --key <key>")
                value = args[index]
            api["--key"] = value
        elif arg == "--key-stdin":
            api["--key-stdin"] = True
        else:
            die(f"{name}: API 号只支持 --key / --key-stdin（收到 {arg}）；"
                f"订阅号才用 --email/--console/--sso")
        index += 1
    return api

def no_browser_env(name: str) -> dict:
    """登录/启动时"不要自动开浏览器"的 env；**显式设过就尊重**（shell 或该号 settings 里的 `BROWSER`）。"""
    if build_env(name).get("BROWSER"):
        return {}
    return {"BROWSER": NO_BROWSER}

def cmd_login(args: list) -> int:
    if not args:
        die("用法: cca login <profile> [--email X|--console|--sso]")
    name = args[0]
    if not profile_dir(name).is_dir():
        die(
            f"{name} 还不存在（{profile_dir(name)}）—— login 只负责登录，不会顺手建号。\n"
            f"  建号 + 登录一步到位: cca add {name}\n"
            f"  或者分开两步: cca new {name} → cca login {name}"
        )
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
        secret = resolve_add_key(name, marker, parse_key_flags(args[1:], name))
        ensure_api_config(name)
        _install_mcp(name)
        write_api_key(name, secret)
        write_settings_env(name, marker)
        say(f"{name}: 已更新 API key（{mask_key(secret)}）")
        return 0
    if any(a == "--key" or a.startswith("--key=") or a == "--key-stdin" for a in args[1:]):
        die(
            f"{name} 是订阅号：API key 只对 API 号有意义。\n"
            f"  建一个 API 号: cca add <name> --provider deepseek --key <key>\n"
            f"  订阅号登录用: cca login {name}"
        )
    code = _run_claude(["auth", "login", *args[1:]], name,
                       env_extra=no_browser_env(name)).returncode
    if code == 0:
        # 登录后配置目录才存在全局配置：顺手把首次引导标记与其它号的目录信任带过去。
        if ensure_onboarding_flag(name):
            say(f"{name}: 已补 hasCompletedOnboarding（首次交互启动不再走引导）")
        _install_mcp(name)
        added = seed_trust_from_default(name)
        if added:
            say(f"{name}: 已从其它号继承 {added} 条目录信任（不再逐个目录询问）")
    return code

def parse_add_flags(args: list) -> tuple:
    """拆 `cca add` 的参数：返回 (profile, api_flags, passthrough)。

    只认 API 号的参数，其余**按原顺序**留着透传给 `claude auth login`（订阅号路径不变 ——
    重排过一次 `--email x`，被回归用例当场抓住）。
    API 参数同时支持 `--key V` 与 `--key=V` 两种写法 —— 使用者要的是"直接填"。
    """
    name, api, passthrough = "", {}, []
    index = 0
    while index < len(args):
        arg = args[index]
        head, sep, value = arg.partition("=")
        if head in ADD_VALUE_FLAGS:
            if not sep:
                if index + 1 >= len(args):
                    die(f"{head} 需要跟一个值；用法: cca add <name> --provider deepseek --key <key>")
                index += 1
                value = args[index]
            if head == "--env":
                api.setdefault("--env", []).append(value)
            else:
                api[head] = value
        elif arg == "--key-stdin":
            api["--key-stdin"] = True
        elif arg == "--isolated":
            api["--isolated"] = True
        elif not name and not arg.startswith("-"):
            name = arg
        else:
            passthrough.append(arg)
        index += 1
    return name, api, passthrough

def endpoint_url_from_flags(api: dict) -> str:
    """`--url` / `--base-url` 取一个（同义词，给不同值报错）。都没给返回空串。"""
    given = str(api.get("--url") or "").strip()
    alt = str(api.get("--base-url") or "").strip()
    if given and alt and given != alt:
        die(f"--url 与 --base-url 是同一个参数，给了两个不同的值：{given} / {alt}")
    return given or alt

def model_env_vars(model: str) -> dict:
    """`--model M` 的语义：这个号就用这一个模型 —— 主模型 + 三个档位映射 + 子 agent 一起设。"""
    return {
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
    }

def resolve_add_key(name: str, marker: dict, api: dict) -> str:
    """取密钥：`--key` → `--key-stdin` → `$<PROVIDER>_API_KEY`；都没有就报错并给三种给法。"""
    if api.get("--key") and api.get("--key-stdin"):
        die("--key 与 --key-stdin 只能给一个")
    if api.get("--key"):
        return str(api["--key"]).strip()
    if api.get("--key-stdin"):
        value = sys.stdin.read().strip()
        if not value:
            die(f"--key-stdin 读到了空内容；用法: echo $KEY | cca add {name} --provider "
                f"{marker['provider']} --key-stdin")
        return value
    env_name = key_env_name(marker)
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    die(
        f"{name}: 没有拿到 API key。三种给法：\n"
        f"  cca add {name} --provider {marker['provider']} --key <key>        "
        "# 最方便，但会进 shell history\n"
        f"  echo $KEY | cca add {name} --provider {marker['provider']} --key-stdin\n"
        f"  {env_name}=<key> cca add {name} --provider {marker['provider']}"
    )
    return ""            # die() 不会返回；这一行只为让静态检查看到完整路径

def api_marker_from_flags(name: str, api: dict) -> dict:
    """把命令行参数合成清单：预设打底 → `--url/--base-url/--model/--env/--proxy` 覆盖。

    `--provider` 与 `--url` **严格二选一**：前者是"预设"（端点/模型变量/密钥环境变量名都内置），
    后者是"任意 Anthropic 兼容端点"。两者同时给 = 预设的模型变量 + 自己的端点（中转/镜像）。
    """
    provider = str(api.get("--provider") or "").strip()
    if provider == "generic":          # 显式说"没有预设"，与省略等价
        provider = ""
    if provider and provider not in PROVIDERS:
        known = "、".join(sorted(PROVIDERS))
        die(f"未知 provider: {provider}（已知: {known}、generic）；任意兼容端点请用 --url（--base-url 同义）")
    url = endpoint_url_from_flags(api)
    if provider and url:
        # 端点二选一：预设自带端点，或你给 URL。混着给会让人以为"预设还能换端点"，把话讲直白。
        die(
            f"--provider 与 --url 只能给一个：端点要么用 {provider} 预设，要么由你指定。\n"
            f"  想要「{provider} 的模型变量 + 自己的端点」：--url <你的端点> --model "
            f"{PROVIDERS[provider]['env'].get('ANTHROPIC_MODEL', '<模型名>')}（必要时再 --env K=V）"
        )
    if not provider and url:
        # 只给 URL 时按主机认预设（会明说），省掉用户手抄模型变量。
        inferred = preset_for_url(url)
        if inferred:
            provider = inferred
            note(f"识别到 {url} 属于 {inferred} 预设：已套用官方模型变量（可用 --model/--env 覆盖）")
    preset = PROVIDERS.get(provider, {})
    base_url = str(url or preset.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        die(f"{name}: 缺少端点 —— 给 --provider <预设>（如 deepseek）或 --url <https://…>")
    if url and not provider and not api.get("--model"):
        # 不强制（中转/网关就该用 Claude 原生模型名），但要说清"这个号会发什么模型名"。
        note(
            "没给 --model —— 这个号会用 Claude 自带的模型名（claude-sonnet-4-5 等）。\n"
            "  中转/网关通常正是这样；若端点只认自家模型名（如 deepseek-flash），"
            f"现在补 --model <名>，或事后改：cca add {name} --model <名>"
        )
    env = dict(preset.get("env") or {})
    model = str(api.get("--model") or "").strip()
    if model:
        env.update(model_env_vars(model))
    env.update(env_flags_to_vars(api))
    proxy = normalize_proxy(api.get("--proxy") or preset.get("proxy"))
    return {
        "kind": "api",
        "provider": provider or "generic",
        "base_url": base_url,
        "env": env,
        "proxy": proxy,
        "created_at": int(time.time()),
    }

def _apply_provider_update(marker: dict, api: dict) -> None:
    """`--provider` / `--url` 改写清单的预设与端点（两者只能给一个）。"""
    url = endpoint_url_from_flags(api)
    provider = str(api.get("--provider") or "").strip()
    if provider == "generic":
        provider = ""
    if provider and url:
        die(
            f"--provider 与 --url 只能给一个：端点要么用 {provider} 预设，要么由你指定。\n"
            f"  想要「{provider} 的模型变量 + 自己的端点」：--url <你的端点> --model "
            f"{PROVIDERS.get(provider, {}).get('env', {}).get('ANTHROPIC_MODEL', '<模型名>')}"
            "（必要时再 --env K=V）"
        )
    if provider and provider not in PROVIDERS:
        die(f"未知 provider: {provider}（已知: {'、'.join(sorted(PROVIDERS))}、generic）；"
            "任意兼容端点请用 --url（--base-url 同义）")
    if provider:
        marker["provider"] = provider
        marker["env"].update(PROVIDERS[provider]["env"])
        marker["base_url"] = PROVIDERS[provider]["base_url"]
    elif url:
        inferred = preset_for_url(url)
        if inferred:
            note(f"识别到 {url} 属于 {inferred} 预设：已套用官方模型变量（可用 --model/--env 覆盖）")
            marker["provider"] = inferred
            marker["env"].update(PROVIDERS[inferred]["env"])
        else:
            marker["provider"] = "generic"
        marker["base_url"] = url.rstrip("/")

def _apply_marker_values(marker: dict, api: dict) -> None:
    """`--model` / `--env` / `--proxy` 改写清单的 env 与代理。"""
    model = str(api.get("--model") or "").strip()
    if model:
        marker["env"].update(model_env_vars(model))
    for key, value in env_flags_to_vars(api).items():
        if value is None:
            marker["env"].pop(key, None)
        else:
            marker["env"][key] = value
    if api.get("--proxy"):
        marker["proxy"] = normalize_proxy(api["--proxy"])

def _profile_changes(before: dict, marker: dict) -> list:
    """列出这次改了哪些字段（人话，逐条）。"""
    changes = []
    if before["base_url"] != marker["base_url"]:
        changes.append(f"端点 {before['base_url']} → {marker['base_url']}")
    if before["provider"] != marker["provider"]:
        changes.append(f"预设 {before['provider']} → {marker['provider']}")
    added = {k: v for k, v in marker["env"].items() if before["env"].get(k) != v}
    removed = [k for k in before["env"] if k not in marker["env"]]
    for key, value in added.items():
        was = before["env"].get(key)
        changes.append(f"{key} {was if was is not None else '(未设)'} → {value}")
    changes.extend(f"{key} {before['env'][key]} → (删除)" for key in removed)
    if before["proxy"] != marker["proxy"]:
        changes.append(f"代理 {before['proxy']} → {marker['proxy']}")
    return changes

def _rotate_api_key(name: str, marker: dict, api: dict, changes: list) -> None:
    """`--key` / `--key-stdin` 换密钥，并提醒 --key 会进 shell history。"""
    if api.get("--key") or api.get("--key-stdin"):
        secret = resolve_add_key(name, marker, api)
        write_api_key(name, secret)
        changes.append(f"密钥 → {mask_key(secret)}")
    if api.get("--key"):
        note(f"--key 会留在 shell history；下次建议用 --key-stdin 或 ${key_env_name(marker)}")

def _apply_openrouter_window(marker: dict) -> None:
    """OpenRouter 号按主模型写号级默认窗口（拿不到目录就跳过，不阻塞）。"""
    if is_openrouter(marker) and "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in (marker.get("env") or {}):
        window = catalog_window_for_slug(str((marker.get("env") or {}).get("ANTHROPIC_MODEL") or ""))
        if window:
            marker.setdefault("env", {})["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)

def update_api_profile(name: str, api: dict) -> int:
    """`cca add <已存在的 API 号> …`：**只改这次给的字段**，其余原样保留，并列出改了什么。

    解决"事后改不了"：改模型/加变量/换端点都不必 `rm` 重建。`--env K=`（空值）表示删掉该变量。
    """
    marker, error = read_marker(name)
    if marker is None:
        die(error)
    before = json.loads(json.dumps(marker))          # 深拷贝，用于算 diff
    _apply_provider_update(marker, api)
    _apply_marker_values(marker, api)
    changes = _profile_changes(before, marker)
    _rotate_api_key(name, marker, api, changes)

    if not changes:
        say(f"{name}: 没有要改的（这次给的参数与现状一致）")
        return 0
    _apply_openrouter_window(marker)
    marker_file(name).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    marker_file(name).chmod(0o600)
    ensure_api_config(name)
    write_settings_env(name, marker)
    say(f"已更新 {name}（{api_display(marker['provider'], marker['base_url'])}）：")
    for item in changes:
        say(f"   {item}")
    return 0

def _add_oauth(name: str, passthrough: list) -> int:
    """`cca add <号>`（没有 API 参数）：等价于 new + login。"""
    if not valid_name(name):
        die(f"非法的 profile 名: {name!r}（不得与子命令同名）")
    if profile_dir(name).is_dir() and profile_kind(name) == "api":
        # 别把"给已存在的 API 号加东西"误当成 OAuth 登录（会去要 key/跑 auth login）。
        _m = read_marker(name)[0] or {}
        die(
            f"{name} 已存在（{api_display(_m.get('provider', ''), _m.get('base_url', ''))}）；"
            "要改配置就给具体参数（--model / --env K=V / --url / --proxy / --key），"
            f"或用 cca rm {name} 删掉重建"
        )
    if profile_dir(name).is_dir():
        say(f"profile {name} 已存在，跳过建号")
    else:
        code = cmd_new([name])
        if code:
            return code
    return cmd_login([name, *passthrough])

def _add_api(name: str, api: dict, passthrough: list) -> int:
    """`cca add <号> --provider/--url …`：写清单、密钥与 settings。"""
    if not valid_name(name):
        die(f"非法的 profile 名: {name!r}（不得与子命令同名）")
    if passthrough:
        die(f"API 号不接受这些参数: {' '.join(passthrough)}（--email/--console/--sso 是 OAuth 专用的）")
    if profile_dir(name).is_dir() and any(profile_dir(name).iterdir()):
        if profile_kind(name) == "api":
            return update_api_profile(name, api)
        die(f"profile {name} 已存在且是订阅号；换个名字，或先 cca rm {name}")
    isolated = bool(api.pop("--isolated", False))
    marker = api_marker_from_flags(name, api)
    _apply_openrouter_window(marker)
    secret = resolve_add_key(name, marker, api)
    if not profile_dir(name).is_dir():
        # API 号的收尾自己会说"启动: cca <name>"，别再说"下一步: cca login"（那是订阅号的路径）。
        code = cmd_new([name, *(["--isolated"] if isolated else [])], next_step="")
        if code:
            return code
    if api.get("--key"):
        note(f"--key 会留在 shell history；下次建议用 --key-stdin 或 ${key_env_name(marker)}")
    marker_file(name).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    marker_file(name).chmod(0o600)
    write_api_key(name, secret)
    write_settings_env(name, marker)
    if ensure_api_config(name):
        say(f"{name}: 已建好 .claude.json（含 hasCompletedOnboarding，首次交互启动不再走引导）")
    added = seed_trust_from_default(name)
    if added:
        say(f"{name}: 已从其它号继承 {added} 条目录信任")
    elif not read_identity(name).get("email"):
        note(f"{name}: 没有可继承的目录信任；首次在某目录用 cca {name} --bg 前，先在那里跑一次交互式 cca {name} 接受信任")
    say("")
    say(f"→ {name}: API 号 {api_display(marker['provider'], marker['base_url'])}"
        + (f"（预设 {marker['provider']}）" if marker["provider"] != "generic" else ""))
    say(f"   端点: {marker['base_url']}")
    say(f"   密钥: {mask_key(secret)}（{key_file(name)}，0600）")
    say(f"   模型: {marker['env'].get('ANTHROPIC_MODEL', '-')}")
    say(f"   启动: cca {name}")
    return 0

def _install_panel(name: str) -> None:
    """建号后顺手把面板装上；装不上只说一声，不挡建号。"""
    if os.environ.get("CCA_AUTO_PLUGIN", "1") == "0":
        return
    from cca.plugin import install  # noqa: PLC0415 - 只有建号要

    try:
        install(name)
    except Exception:  # noqa: BLE001 - 装插件失败不该让建号失败
        note(f"{name}: 面板没装上，之后可以手动 cca plugin install --profile {name}")


def _install_mcp(name: str) -> None:
    """建号后顺手注册 sideagents MCP；失败只说一声，不挡建号。"""
    if os.environ.get("CCA_AUTO_MCP", "1") == "0":
        return
    from cca.mcp_sideagent import register  # noqa: PLC0415 - 只有建号要

    try:
        ok = register(name) is not None
    except Exception:  # noqa: BLE001 - 注册失败不该让建号失败
        ok = False
    if not ok:
        note(f"{name}: sideagents MCP 没注册上，之后可以手动 cca mcp install --profile {name}")


def cmd_add(args: list) -> int:
    """建号 + 配好凭据：给 `--provider`/`--url` 走 API key 路径，否则照旧 OAuth 登录。

    API 号在 `new` 之后写清单、写密钥（0600）、渲染 settings（env + apiKeyHelper），
    并顺带补首次引导标记与目录信任 —— 与订阅号登录后的收尾一致。
    """
    if not args:
        say(ADD_USAGE)
        return 0
    name, api, passthrough = parse_add_flags(args)
    code = _add_oauth(name, passthrough) if not api else _add_api(name, api, passthrough)
    if code == 0:
        _install_mcp(name)
        _install_panel(name)
    return code

def cmd_logout(args: list) -> int:
    if not args:
        die("用法: claude-acct logout <profile>")
    name = args[0]
    if profile_kind(name) == "api":
        if key_file(name).is_file():
            key_file(name).unlink()
            say(f"{name}: 已删除 API key（对话与配置保留；要恢复跑 cca login {name} --key …）")
        else:
            say(f"{name}: 本来就没有 API key")
        return 0
    return _run_claude(["auth", "logout", *args[1:]], name).returncode

def dir_usage(path: Path) -> tuple:
    """目录的 (文件数, 字节数)。软链只算链接本身，不跟进去 —— 共享池不是这个号的体积。"""
    files = total = 0
    stack = [path]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            files += 1
            try:
                # lstat：软链只算链接自身；也不跟进目录软链（共享池不是这个号的体积）。
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return files, total

def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < BYTES_PER_KB or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= BYTES_PER_KB
    return f"{size:.1f} GB"

def real_shared_entries(name: str) -> list:
    """共享项里**不是软链**的那些。

    正常的号里 `projects`/`skills`/`plugins`/`CLAUDE.md` 都是指向 ~/.claude 的软链；一旦被 CLI
    换成真目录（实测发生过：软链被重写成实体目录），删号就会连共享资产一起删 —— 拒绝执行。
    """
    bad = []
    for item in SHARED_LINKS:
        path = profile_dir(name) / item
        if path.exists() and not path.is_symlink():
            bad.append(item)
    return bad

def _rm_target(args: list) -> tuple:
    """解析并校验要删的号；返回 (name, dir, flags)。任何一道闸不过都直接退出。"""
    rest, flags = parse_flags(args, bool_flags=("--yes", "--force"), usage=RM_USAGE)
    if not rest:
        die(f"用法: {RM_USAGE}")
    if rest[1:]:
        die(f"多余参数 {' '.join(rest[1:])}；用法: {RM_USAGE}")
    name = rest[0]
    if not valid_name(name):
        die(f"非法的 profile 名: {name!r}")
    d = profile_dir(name)
    if not d.is_dir():
        die(f"profile {name} 不存在（{d}）")
    root = accounts_root().resolve()
    if root not in d.resolve().parents:
        die(f"{d} 不在 {root} 之下，拒绝删除")
    bad = real_shared_entries(name)
    if bad:
        die(
            f"{name} 的 {'、'.join(bad)} 是实体目录而不是共享软链（CLI 可能重写过），"
            "删它会连共享资产一起删；先核对再手工处理"
        )
    return name, d, flags

def _rm_stop_sessions(name: str, sessions: list, *, safe: bool) -> None:
    """删除前停掉后台会话；有交互会话时除非 --force 否则拒绝。"""
    interactive = [s for s in sessions if not is_job(s)]
    if interactive and not safe:
        listed = "、".join(str(s.get("id")) for s in interactive)
        die(
            f"{name} 还有交互会话在用（{listed}）—— 删配置目录会影响正在跑的会话；"
            "先在那个终端退出，确认是残留登记就加 --force"
        )
    if not sessions:
        return
    targets, skipped, not_jobs, failures = stop_sessions(name, force=safe)
    for f in failures:
        warn(f"停止 {name} 的会话失败 {f}")
    if skipped:
        die(
            f"{name} 还有 {len(skipped)} 个正在跑的会话（停它会掐掉进行中的任务）；"
            f"确认要一起停就加 --force，或先 cca stop-bg {name}"
        )
    if targets:
        say(f"已停掉 {name} 的 {len(targets)} 个后台会话（对话保留在共享池）")
    if not_jobs:
        note(f"{name}: {len(not_jobs)} 个交互会话的登记无处可停，随目录一起清掉")

def _rm_confirm(name: str, d: Path, flags: dict) -> bool:
    """打印删除范围并确认；返回是否继续。"""
    files, size = dir_usage(d)
    say(f"将删除: {d}")
    extra = "与 API key" if profile_kind(name) == "api" else ""
    say(f"  内容: {files} 个文件 / {human_size(size)}（含凭据文件{extra}，删掉无法恢复）")
    say("  保留: 对话记录（共享池）、服务端账号与订阅账单")
    if flags.get("--yes"):
        return True
    try:
        answer = input(f"输入 {name} 确认删除（其它任何输入都取消）: ").strip()
    except EOFError:
        die("没有读到确认输入；确认无误后加 --yes 再跑")
    if answer != name:
        say("已取消，什么都没删")
        return False
    return True

RENAME_ARGS = 2          # cca rename 要两个位置参数

def _rename_targets(args: list) -> tuple:
    """解析并校验 `cca rename <旧> <新>`；不合法直接 die。"""
    rest = [a for a in args if not a.startswith("-")]
    if len(rest) != RENAME_ARGS:
        die("用法: cca mv <旧号> <新号>（例: cca mv codex gpt-ds；rename 是它的别名）")
    old, new = rest[0], rest[1]
    if old == new:
        die("新旧名字一样，不用改")
    for name in (old, new):
        if not valid_name(name):
            die(f"非法的 profile 名: {name!r}（不能与子命令同名）")
    if not profile_dir(old).is_dir():
        die(f"{old} 不存在（cca ls 看看有哪些号）")
    if profile_dir(new).exists():
        die(f"{new} 已存在，换个名字或先 cca rm {new}")
    return old, new


def cmd_rename(args: list) -> int:
    """`cca mv <旧> <新>`：给号改名，目录 / 密钥通道 / sideagent 状态一起迁。

    名为"改名"实则三步：停掉这个号的进程（名字与路径都在里面）、`mv` 目录、把
    **写死了旧路径**的东西重写一遍（`apiKeyHelper`、sideagent 的状态文件与其中的
    `account` 字段）。会话与对话不受影响：transcript 在共享池（`projects` 软链）里，
    名字只影响本地这份配置目录。
    """
    old, new = _rename_targets(args)
    src, dst = profile_dir(old), profile_dir(new)

    sessions = active_workers(old)
    if sessions:
        _rm_stop_sessions(old, sessions, safe=True)
    daemon_dir = src / "daemon"
    if daemon_dir.is_dir():
        _kill_profile_processes(old)
    src.rename(dst)

    marker, error = read_marker(new)
    if marker is not None:
        if error:
            die(f"{new} 的清单读不了：{error}")
        write_marker(new, marker)          # 重渲染：apiKeyHelper 里的旧路径跟着换掉
    moved = _rename_sideagent_state(old, new)
    say(f"已改名：{old} → {new}")
    say(f"  目录: {dst}")
    if moved:
        say(f"  sideagent 状态迁移 {moved} 个文件")
    say(f"  会话与对话不受影响（共享池）；用 cca {new} 启动，旧名不再存在")
    return 0


def _kill_profile_processes(name: str) -> None:
    """把这个号的 daemon / 后台进程收掉（改名前必须先停，否则会往旧路径写）。"""
    marker = f"CLAUDE_CONFIG_DIR={profile_dir(name)}"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["pkill", "-f", marker], check=False, timeout=20)  # noqa: S603, S607
        time.sleep(1)


def _rename_sideagent_state(old: str, new: str) -> int:
    """把 `~/.cca/sideagents/<旧号>`、`<旧号>#…` 文件夹改成新名字，校正里面的 account 字段，
    并把各主会话名单（~/.cca/sessions/*.json）里指向它们的条目改过来。"""
    from cca.session import roster, sideagent  # noqa: PLC0415 - 只在改号名时用

    root = sideagent.SIDEAGENT_DIR
    if not root.is_dir():
        return 0
    renamed: dict = {}
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not (path.name == old or path.name.startswith(old + "#")):
            continue
        target = root / (new + path.name[len(old):])
        if target.exists():
            continue
        path.rename(target)
        renamed[path.name] = target.name
        for inner in ("state.json", "result.json"):
            _fix_account_fields(target / inner, old, new)
    if renamed:
        for path in sorted(roster.sessions_dir().glob("*.json")):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                data = json.loads(path.read_text(encoding="utf-8"))
                agents = data.get("agents") if isinstance(data, dict) else None
                if isinstance(agents, dict) and set(agents) & set(renamed):
                    data["agents"] = {renamed.get(name, name): entry for name, entry in agents.items()}
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(renamed)


def _fix_account_fields(target: Path, old: str, new: str) -> None:
    """state.json / result.json 里以旧号名开头的 account、owner_account 改成新号名。"""
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        for key in ("account", "owner_account"):
            if isinstance(data.get(key), str) and data[key].startswith(old):
                data[key] = new + data[key][len(old):]
        for job in data.get("jobs") or []:
            if isinstance(job, dict) and isinstance(job.get("account"), str) \
                    and job["account"].startswith(old):
                job["account"] = new + job["account"][len(old):]
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_rm(args: list) -> int:
    """删掉某个号的**本地配置目录**：凭证 / 会话登记 / daemon / jobs / 本号历史一起走。

    删的是"这个号在这台机器上的东西"，不是"这个号的历史"：transcript 在共享池（`projects`
    软链），删完照样能 `cca sessions` 看到、用别的号 `cca <号> --resume <完整id>` 续上。也删不掉服务端账号
    状态与订阅账单 —— 那两样与本地目录无关，提示里会写清楚。

    几道闸：共享项被换成实体目录时
    拒绝（防止误删共享池）；还有在跑的会话时拒绝（先 `cca stop-bg`，或加 `--force` 连在跑
    的一起停）；删除前打印体积并要求**输入 profile 名确认**（`--yes` 供脚本/无 tty 使用）。
    """
    name, d, flags = _rm_target(args)
    ident = read_identity(name)
    _rm_stop_sessions(name, active_workers(name), safe=bool(flags.get("--force")))
    if not _rm_confirm(name, d, flags):
        return 0

    had_daemon = (d / "daemon").is_dir()
    shutil.rmtree(d)
    say(f"已删除 {name} 的本地配置（{d}）")
    if had_daemon:
        note(
            f"{name}: 这个号用过 daemon —— 刚停下的进程可能还会写几秒 telemetry；"
            f"若之后又冒出同名空目录，再跑一次 cca rm {name} --yes 即可（无清单的目录也能删）"
        )

    email = ident.get("email") or "<该号邮箱>"
    say("对话留在共享池里：cca sessions 能看到，任何号都能 cca <号> --resume <完整id> 续上")
    say(f"想加回来: cca add {name} --email {email}")
    return 0

def handoff_plan(console, items: list, target: str, selected: set) -> None:
    """打印接管计划表（迁移前让你看清楚会动哪些）。"""
    table = _table(console, min_width=120)
    table.add_column("#", width=3, justify="right", no_wrap=True)
    table.add_column("SESSION", min_width=8, no_wrap=True)
    table.add_column("状态", min_width=8, no_wrap=True)
    table.add_column("号", width=4, no_wrap=True)
    table.add_column("目录", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("标题", min_width=10, ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("最后提问", min_width=18, ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("接管后", min_width=12, no_wrap=True)
    for index, item in enumerate(items, start=1):
        if not item.get("movable", True):
            action = "搬不了"
            style = "dim"
        elif index in selected:
            action = "会继续跑" if item["running"] else "等待（不烧额度）"
            style = "yellow" if item["running"] else "green"
        else:
            action = "不动（在跑）" if item["running"] else "不动"
            style = "dim"
        table.add_row(
            _plain(index),
            _plain(item["id"]),
            state_text(item["state"]),
            _plain(item["profile"]),
            _plain(item["cwd"] or "(目录未知)"),
            _plain(item["name"] or "(无标题)"),
            _plain(_last_prompt(item)),
            _plain(action, style=style),
        )
    console.print(_plain(f"接管计划 → {target}", style="bold"))
    console.print(table)

def _last_prompt(item: dict) -> str:
    transcript = session_transcript(profile_dir(item["profile"]), item["id"])
    if transcript is None:
        return ""
    return _scan_transcript(transcript)["prompt"].replace("\n", " ")

def work_dir() -> Path:
    """开会话的工作目录（默认 ~/llm，环境变量 CCA_WORK_DIR 可改）。"""
    return Path(os.environ.get("CCA_WORK_DIR") or Path.home() / "llm").expanduser()


def launch_dir(cwd: str) -> str:
    """在目标号上接着跑时用哪个目录。

    家目录 Claude Code 不让永久信任（“the home directory is trusted one session at a time”），
    后台 `--bg --resume` 在家目录起不来；实测从工作目录起同一个会话能接着跑，只是之后的
    命令在工作目录里执行。所以起始目录是家目录的会话，改到工作目录（存在时）去接。
    """
    if cwd and Path(cwd).resolve() == Path.home().resolve() and work_dir().is_dir():
        note(f"会话起始目录是家目录（{cwd}）：家目录不能长期信任，改在 {work_dir()} 里接着跑")
        return str(work_dir())
    return cwd


# handoff 的锁文件放这里（一段对话一个，只用来防重复搬）
HANDOFF_LOCK_DIR = Path("~/.cca/locks")


@contextlib.contextmanager
def _handoff_lock(full: str):
    """同一段对话同时只许一个 handoff：面板连按 y、命令行连敲两次，第二个直接拒绝。"""
    folder = HANDOFF_LOCK_DIR.expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"handoff-{full}.lock").open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _wait_stopped(profile: str, full: str) -> bool:
    """stop 以后等源号上这段对话的进程真的退出。"""
    deadline = time.monotonic() + HANDOFF_STOP_WAIT_SECONDS
    while full in live_session_ids(profile):
        if time.monotonic() >= deadline:
            return False
        time.sleep(HANDOFF_STOP_POLL_SECONDS)
    return True


HANDOFF_BUSY = "这段对话正在搬，别重复搬"
ALREADY_RUNNING_TAIL = "上跑着了"      # _move_one 的失败原因 "它已经在 X 上跑着了" 的固定尾巴（move_session / watchdog 认它）
MOVE_ALREADY_THERE = 2                 # move_session 的返回码：目标号上已经有一份在跑（重复会话，不是搬失败）
RM_TIMEOUT_SECONDS = 60
# 后台会话的 job state.json 里和 worktree 有关的字段（claude 2.1.322 的 job 记录用它们）；真实 jobs 里目前一条都没有
WORKTREE_KEYS = ("worktreePath", "worktreeBranch", "worktree", "inheritedWorktreePath")


def job_worktree(account: str, job: str) -> str:
    """这个号上这条后台 job 带的 worktree（state.json 里的字段）；没有返回空串。读不了 state.json 也返回非空（不敢删）。"""
    path = profile_dir(account) / "jobs" / job / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError):
        return "state.json 读不了，无法确认有没有 worktree"
    if not isinstance(data, dict):
        return "state.json 格式不对，无法确认有没有 worktree"
    for key in WORKTREE_KEYS:
        if data.get(key):
            return f"{key}={data[key]}"
    origin = data.get("originCwd")
    if origin and origin != data.get("cwd"):
        return f"originCwd={origin}"
    return ""


def drop_entry(account: str, job: str, full: str, *, why: str) -> str:
    """把一条会话从这个号的面板和 jobs 里删掉（`claude rm <短编号>`；共享池里的对话记录不受影响）。返回一句话结果，供写日志。

    搬走（或判为重复）以后调：旧号面板里留着一条 stopped 的它，人在那里按回车就会在旧号上再起一份。
    不删的情况：进程还活着（那是 stop 的事）、job 带 worktree（rm 会把续跑还要用的 worktree 一起删掉）。
    失败只返回原因，不抛，不影响“搬成功”的结论。
    """
    if full in live_session_ids(account):
        return f"没清 {account} 面板里的 {job}：进程还活着"
    tree = job_worktree(account, job)
    if tree:
        return f"没清 {account} 面板里的 {job}：这条 job 有 worktree（{tree}），rm 会把续跑还要用的 worktree 一起删掉"
    try:
        r = _run_claude(["rm", job], account, capture=True, timeout=RM_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"清 {account} 面板里的 {job} 失败（{why}）：{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:160]
        return f"清 {account} 面板里的 {job} 失败（{why}）：{detail}"
    return f"已清 {account} 面板里的 {job}（{why}）"


def _move_one(item: dict, target: str, prompt: str = "") -> str:
    """搬一条：加锁 → 源号还活着就先停、等它退出 → 目标号 `--bg --resume`。返回失败原因，成功返回空。

    要不要先停看进程在不在（daemon/roster.json），不看状态字：会话说完一轮是 done，
    进程却还在；这时直接 resume，CLI 只会复制一份新编号的副本（面板里搬自己时踩到）。
    """
    full = item["full"]
    with _handoff_lock(full) as locked:
        if not locked:
            return HANDOFF_BUSY
        if full in live_session_ids(target):
            return f"它已经在 {target} {ALREADY_RUNNING_TAIL}"
        # 没结束的（blocked/idle…）照旧先停；结束了但进程还活着的（说完一轮的 done）也要停
        if not item.get("completed") or full in live_session_ids(item["profile"]):
            r = _run_claude(["stop", item["id"]], item["profile"], capture=True)
            if r.returncode != 0:
                return "停不掉源号上的这一条：" + (r.stderr or r.stdout or "").strip()[:120]
            if not _wait_stopped(item["profile"], full):
                return f"停了 {HANDOFF_STOP_WAIT_SECONDS} 秒进程还在，没有搬（搬了会复制一份）"
        if copy_trust(item["profile"], target, item["cwd"]):
            say(f"{target}: 已补目录信任 {item['cwd']}")
        from cca.session.transcript import strip_session, target_needs_strip  # noqa: PLC0415

        if target_needs_strip(target):
            stripped = strip_session(full)
            if stripped:
                say(f"{item['id']}: 清掉了 {stripped} 处 tool_use.signature（Gemini 历史）")
        # prompt：续上后先说的一句话（自己换号时让它接着做，不然它会停在等输入）。
        # 有原名：启动提示是 /rename <原名>（保住名字），prompt 等目标号登记好再以消息送到。
        from cca.session.takeover import deliver_after_resume, resume_launch  # noqa: PLC0415

        launch, pending = resume_launch(target, full, prompt)
        problem = _resume_on(item, target, launch)
        if problem:
            return problem
        error = deliver_after_resume(target, full, pending)
        if error:
            _redeliver_by_prompt(item, target, full, pending, error)
        record_move(full, item["profile"], target)
        _drop_moved_source(item, target)
    return ""


def _stale_registrations(account: str, full: str) -> list:
    """这个号 sessions/ 里这条会话的登记文件中，进程已经不在的那些。

    CC 判断「是不是还在跑」时，碰到验证不了的旧登记（例如容器重启前留下的、pid 命名空间不同）
    会当它还在跑，于是续跑另起副本："session X is already running in the background, so this
    started a copy"（11-11 train-h004 搬到 001 时连续 4 次被复制）。cca 这边看 pid 不在了就删掉。
    """
    out = []
    for path in (profile_dir(account) / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("sessionId") != full:
            continue
        pid = int(data.get("pid") or 0)
        if pid > 0:
            try:
                os.kill(pid, 0)
                continue
            except OSError:
                pass
        out.append(path)
    return out


COPY_RE = re.compile(r"started a copy as\s+([0-9a-fA-F][0-9a-fA-F-]{5,})")


def _resume_on(item: dict, target: str, launch: list) -> str:
    """在目标号上 `--bg --resume <完整id> <launch…>`，保证续的是同一编号。返回失败原因，成功返回空。

    目标号上留着这条会话以前的旧 job（stopped、存着当时的选项）时，CC 会另起一份新编号的副本
    （"started a copy as …"）。11-11 b002 → 001 的 train-improve 就这样被复制了两份。所以：
    续之前先清掉目标号上的旧登记（只删面板条目，对话记录不动；带 worktree 的不删）；
    续完要是还复制了，把副本停掉删掉，按失败算（源号已停的由调用方回源号续上）。
    """
    full = item["full"]
    if (profile_dir(target) / "jobs" / item["id"]).is_dir() and full not in live_session_ids(target):
        say(f"{item['id']}: {drop_entry(target, item['id'], full, why='目标号上的旧登记，留着续跑会另起一份副本')}")
    for stale in _stale_registrations(target, full):
        stale.unlink(missing_ok=True)
        say(f"{item['id']}: 删掉了 {target} 上进程已不在的旧登记 {stale.name}（留着 CC 会当它还在跑、另起一份副本）")
    try:
        r = _run_claude(api_launch_args(target, ["--bg", "--resume", full, *launch]),
                        target, capture=True, cwd=item["cwd"], session=True)
    except LaunchBlocked as exc:
        return str(exc)
    output = f"{r.stdout or ''}\n{r.stderr or ''}"
    if r.returncode != 0:
        return output.strip()[:160]
    copy = COPY_RE.search(output)
    if copy:
        copy_id = copy.group(1)
        short = copy_id[:8]
        _run_claude(["stop", short], target, capture=True)
        _wait_stopped(target, copy_id)
        say(f"{item['id']}: {drop_entry(target, short, copy_id, why='续跑被 CC 复制成了副本')}")
        return f"在 {target} 上续成了副本 {short}（已停掉删掉），没有搬"
    return ""


MOVES_KEEP_LINES = 2000   # 换号记录只留最近这么多条


def record_move(full: str, source: str, target: str) -> None:
    """记一条换号（watchdog 搬和手动 handoff 都走 _move_one）。源号面板里的旧条目随后会被清掉，
    事后只靠登记表已经看不出它从哪来，所以单独记下。写失败不影响搬迁。"""
    from cca.paths import moves_file  # noqa: PLC0415

    path = moves_file()
    line = json.dumps({"at": int(time.time()), "session": full, "from": source, "to": target}, ensure_ascii=False)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as out:
            out.write(line + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > MOVES_KEEP_LINES * 2:
            path.write_text("\n".join(lines[-MOVES_KEEP_LINES:]) + "\n", encoding="utf-8")


def _drop_moved_source(item: dict, target: str) -> None:
    """搬成功了：清掉源号面板里的旧条目（watchdog 搬和手动 handoff 都走 _move_one），免得人在旧号面板里点出副本。"""
    if item["profile"] != target:
        say(f"{item['id']}: {drop_entry(item['profile'], item['id'], item['full'], why=f'已搬到 {target}')}")


def _redeliver_by_prompt(item: dict, target: str, full: str, pending: str, error: str) -> None:
    """跨会话消息没送到时的兜底，不让会话停在等输入。

    目标号上已经没有它的进程（续上就退出了）：再用原提示当启动提示续一次（名字可能被 CC 按提示改掉，
    总比停着强）。进程还在只是套接字不通：续不得（会复制一份），只能喊出来让人手动说一句。
    """
    from cca.session.takeover import registry_entry  # noqa: PLC0415

    manual = f"它会停在等输入，需要手动说一句（话是：{pending}）"
    if registry_entry(target, full):
        warn(f"{item['id']}: 已在 {target} 续上，但这句话没送到：{error}；{manual}")
        return
    problem = _resume_on(item, target, [pending])
    if problem:
        warn(f"{item['id']}: 在 {target} 续上后进程没了，这句话没送到（{error}），重续失败：{problem}；{manual}")
        return
    warn(f"{item['id']}: 在 {target} 的消息投递失败（{error}），已用原提示重新续上（会话名可能被改，需要再 /rename）")


def run_handoff(target: str, items: list, selected: set, *, strip_tool_search: bool = False,
                prompt: str = "") -> tuple:
    """执行接管：源号 stop → 目标号补信任 → 目标号 `--bg --resume`。返回 (成功, 失败提示)。

    已结束（done/stopped…）的会话跳过 stop，直接续；搬不了的（movable=False）一条都不动。
    会话是本地 JSONL，与 provider 无关，所以订阅号 ↔ API 号之间搬运也成立；但类型不同意味着
    "接着这份上下文的是另一个供应商"，值得明说一行（不阻止，人工动作本来就是显式的）。

    搬到 **API 号**（第三方端点）时还要看 transcript 里有没有 tool search 的延迟工具记录：
    有的话对方会 422（`unknown variant tool_definition`）。`strip_tool_search=True` 就地剥掉
    （先备份）；不开这个开关只提醒一句，绝不偷偷改文件。
    """
    taken, failures = [], []
    if not shares_sessions(target):
        # 隔离号的对话目录是自己一份，看不到别的号的对话：resume 只会报“找不到对话”
        return [], [f"{target} 是隔离号（对话目录不共用），搬不过去"]
    target_api = profile_kind(target) == "api"
    for index in sorted(selected):
        item = dict(items[index - 1])
        if not item.get("movable", True):
            # 家目录没建工作目录 / 交互会话：先停源号会得到"停了却叫不醒"，一步都不动。
            failures.append(f"{item['id']}: {item.get('reason') or '这条会话搬不了'}")
            continue
        item["cwd"] = launch_dir(item["cwd"])
        if not item["cwd"] or not Path(item["cwd"]).is_dir():
            where = item["cwd"] or "<会话起始目录未知>"
            failures.append(
                f"{item['id']}: 目录不可用（{where}）；手动接管：cd <目录> && cca {target} --bg --resume {item['full']}"
            )
            continue
        if target_api != (profile_kind(item["profile"]) == "api"):
            note(
                f"{item['id']}: 跨账号类型搬运（{item['profile']} → {target}）——"
                "接着这份上下文的是另一个供应商，内容会发给它"
            )
        if target_api:
            transcript = session_transcript(profile_dir(item["profile"]), item["id"])
            count = tool_search_records(transcript) if transcript is not None else 0
            if count:
                if strip_tool_search:
                    dropped, backups = strip_tool_search_records(transcript)
                    say(f"{item['id']}: 剥掉 {dropped} 条 tool search 记录"
                        f"（{len(backups)} 个文件，备份在 {accounts_root() / '_session-backups'}）")
                else:
                    warn(
                        f"{item['id']}: transcript（含子代理）里有 {count} 条 tool search 延迟工具记录，"
                        f"搬到第三方端点（{target}）会 422 unknown variant tool_definition；"
                        "加 --strip-tool-search 会先备份再剥掉"
                    )
        problem = _move_one(item, target, prompt)
        if problem:
            failures.append(f"{item['id']}: {problem}")
            continue
        taken.append(item)
    return taken, failures

def _matches_only(item: dict, term: str) -> bool:
    """`--only=<前缀|标题>` 能不能点名这一条（编号前缀、短编号、完整标题）。"""
    key = term.strip().lower()
    if not key:
        return False
    return (item["full"].lower().startswith(key)
            or item["id"].lower().startswith(key)
            or (item["name"] or "").lower() == key)


def _handoff_only_selection(items: list, only: list, source: str) -> set:
    """按 `--only=<前缀|标题>` 选中会话；每个词必须恰好命中一条。"""
    selected = set()
    for term in only:
        hits = [i for i, item in enumerate(items, start=1) if _matches_only(item, term)]
        if len(hits) != 1:
            die(
                f"--only={term} 匹配到 {len(hits)} 条会话；"
                f"写更长的前缀，或先 cca {source} agents 看清单"
            )
        selected.add(hits[0])
    return selected

def _handoff_selection(console, items: list, target: str, opts: tuple):
    """决定接管哪些会话；返回 1 基编号集合，人取消时是空集合。opts = (only, take_all, dry_run, source)。

    --all / --only / --dry-run 先打印计划表就返回；否则弹选择框，选完打印计划表再确认。
    """
    only, take_all, dry_run, source = opts
    if take_all:
        selected = set(range(1, len(items) + 1))
        handoff_plan(console, items, target, selected)
        return selected
    if only:
        selected = _handoff_only_selection(items, only, source)
        handoff_plan(console, items, target, selected)
        return selected
    # 默认一个都不勾：回车就搬走"所有能搬的"太容易误伤，交给人挑；
    # 选择框第一行可以一键全选"等输入/挂起的"。
    default: list = []
    if dry_run:
        handoff_plan(console, items, target, set(default))
        return set(default)
    return _ask_and_confirm(console, items, (source, target), default)

def _load_handoff_picker():
    """交互选择框要 questionary；没装时说清怎么装，而不是抛 traceback。"""
    import importlib  # noqa: PLC0415 - 只在交互时才需要

    try:
        return importlib.import_module("cca.account.handoff_picker")
    except ModuleNotFoundError as exc:
        if exc.name not in ("questionary", "prompt_toolkit"):
            raise
        die(
            f"cca handoff 的交互选择框需要 {exc.name}（pyproject 里钉的是 "
            "questionary==2.1.0、prompt_toolkit==3.0.51）。装上再试：\n"
            "  python3 -m pip install 'questionary==2.1.0' 'prompt_toolkit==3.0.51'\n"
            "或者不用交互：cca handoff <源号> <目标号> --only=<编号前缀>"
        )


def _ask_and_confirm(console, items: list, pair: tuple, default: list):
    """弹选择框 → 打印计划表 → 确认；返回 1 基编号集合；取消、选空、没确认都返回空集合（并说一句）。"""
    picker = _load_handoff_picker()
    confirm_move, pick_sessions = picker.confirm_move, picker.pick_sessions

    source, target = pair
    picked = pick_sessions(items, default)
    if picked is None:
        say(HANDOFF_CANCELLED)
        return set()
    if not picked:
        say("没有选中任何会话，什么都没动")
        return set()
    handoff_plan(console, items, target, set(picked))
    if not confirm_move(len(picked), source, target):
        say(HANDOFF_CANCELLED)
        return set()
    return set(picked)

def _attach_session(target: str, short_id: str) -> None:
    """以目标号的身份接管这条会话的终端：等价 `cca <号> attach <短id>`。"""
    from cca.cli import cmd_run  # noqa: PLC0415 - cli 顶部导入本模块，延迟破环

    say(f"进入 {target} 的会话 {short_id}（Ctrl+B 两次退出）")
    cmd_run(target, ["attach", short_id])

def _current_profile() -> str:
    """当前会话属于哪个号：看 CLAUDE_CONFIG_DIR 的最后一段。"""
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    name = Path(folder).name if folder else ""
    if not name:
        die("认不出当前号：这个会话没有 CLAUDE_CONFIG_DIR；改用 cca handoff <源号> <目标号>")
    return name


def _current_session_prefix(length: int = 8) -> str:
    """当前会话的短 id（handoff 的 --only 认前缀）。"""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not session:
        die("认不出当前会话：--current 要在 Claude Code 会话里用（缺 CLAUDE_CODE_SESSION_ID）")
    return session[:length]

def _report_handoff(source: str, target: str, taken: list, failures: list) -> int:
    """打印接管结果与失败原因。"""
    say(f"已从 {source} 搬到 {target}：{len(taken)} 条（源号已停，对话保留）")
    for item in taken:
        say(f"   {item['id']}  {item['name'] or '(无标题)'}   → cca {target} attach {item['id']}")
    if any(not item.get("running") for item in taken):
        # 实测（ds/daemon.log）："等输入"的后台登记只在 CLI daemon 存活期间存在 —— 退出 claude 后
        # daemon 空闲退出会把它们 settled(killed)，面板就空了；对话本身不受影响。
        note(
            "接管的会话是「等输入」状态：它只在 CLI daemon 存活期间出现在面板里"
            "（退出 claude 后 daemon 空闲退出会收掉这类登记，对话不受影响）。"
            f"要让它重新出现: cca {target} --bg --resume <完整id>"
        )
    for f in failures:
        warn(f"接管失败 {f}")
    return 0 if not failures else 1

def _pick_handoff_pair():
    """弹框选源号和目标号；额度数据用 cca ls --json 背后的同一个函数。取消返回空列表。"""
    pick_pair = _load_handoff_picker().pick_pair
    from cca.report import list_rows  # noqa: PLC0415

    names = discover_profiles()
    if not names:
        say("没有发现任何 profile")
        return []
    # 不等实时额度（每号起一次 claude /usage，要 5 秒左右）：先用本地快照马上弹框，每个号标出数据是多久以前的；
    # 同时在后台刷一遍，下次打开就是新的
    rows = list_rows(names, cached_only=True)
    _refresh_usage_in_background(rows)
    return list(pick_pair(rows, shares_sessions) or [])


def _refresh_usage_in_background(rows: list) -> None:
    """有号的额度快照超过 USAGE_LIVE_TTL 就起一个脱离本进程的 `cca ls --json` 去刷（它自己按缓存规则决定刷哪些）。"""
    now = time.time()
    stale = [row for row in rows if row.get("kind") != "api"
             and now - float((row.get("usage") or {}).get("fetched_at") or 0) > USAGE_LIVE_TTL]
    if not stale:
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(  # noqa: S603 - 参数是固定的本程序命令
            [*launcher_argv(), "ls", "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)


def _handoff_pair(rest: list, flags: dict) -> tuple:
    """认出源号、目标号；--current 时源号和会话来自当前会话，返回 (源号, 目标号, only 或 None)。"""
    if flags.get("--current"):
        # 只搬“我现在这条”：源号从当前会话的环境里认，会话 id 从 CLAUDE_CODE_SESSION_ID 取
        # 不写目标号：交给 watchdog 按额度挑（余量充足、周额度先到期、分散）
        if len(rest) > 1:
            die("--current 时最多写一个目标号。用法: cca handoff [<目标号>] --current")
        return _current_profile(), (rest[0] if rest else ""), [_current_session_prefix()]
    if len(rest) != PAIR_ARGS:
        die(f"要写清源号和目标号。用法: {HANDOFF_USAGE}")
    return rest[0], rest[1], None


def _request_self_move(source: str, target: str) -> int:
    """在要搬的会话里敲 `cca handoff <目标号> --current`：要停掉的正是自己。

    当场 stop 的话，这条命令会跟着会话一起被杀（实测 claude stop 杀掉会话的整棵进程树），
    所以这里只登记意图，交给常驻的 watchdog：它在这条会话这一轮结束时停源号、在目标号续跑，
    并让会话接着做。重复敲只是覆盖同一条登记。
    交互会话不是后台任务，`--bg --resume` 只会复制一份，不搬。
    """
    from cca.session import watchdog  # noqa: PLC0415
    from cca.session.push import shares_sessions  # noqa: PLC0415
    from cca.session.takeover import registry_entry  # noqa: PLC0415

    full = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if target:
        if source == target:
            die(f"这条会话已经在 {target} 上了")
        require_profile(target, usage=HANDOFF_USAGE)
        if not shares_sessions(target):
            die(f"{target} 是隔离号（对话目录不共用），搬不过去")
    entry = registry_entry(source, full)
    if not entry:
        die(f"在 {source} 的登记表里找不到这条会话（{full[:8]}）：它是不是不在 {source} 上跑？")
    if entry.get("kind") != "bg":
        where = target or "<号>（cca quota-hint 看先用哪个）"
        die(f"这是交互会话，不能自己换号。退出后用 cca {where} --resume {full} 接着聊")
    watchdog.request_move(source, target, full)
    dest = target or "有余量的号（cca 按额度挑）"
    say(f"已安排：这一轮结束后，cca 把本会话从 {source} 换到 {dest}（同一编号、同一名字），"
        "并收到一句「接着做刚才没做完的事」。还在跑的后台任务会被停掉，换过去后会收到通知，需要就重起。")
    say("现在把进度说清楚，然后结束这一轮，不要再调用工具。")
    return 0


def _request_stay() -> int:
    """`cca handoff --stay`：这条会话留在本号——这个号这一段（同一个重置时刻）内 cca 不再为额度提醒搬它。

    只免「到提醒点」的搬迁；真撞墙了照样搬。
    """
    from cca.session import watchdog  # noqa: PLC0415
    from cca.session.takeover import registry_entry  # noqa: PLC0415

    source = _current_profile()
    full = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
    if not full:
        die("认不出当前会话：--stay 要在 Claude Code 会话里用（缺 CLAUDE_CODE_SESSION_ID）")
    entry = registry_entry(source, full)
    if entry and entry.get("kind") != "bg":
        say("这是交互会话，cca 本来就不会自动搬它，不用 --stay")
        return 0
    windows = watchdog.request_stay(source, full)
    if not windows:
        say(f"已记下：留在 {source}（读不到 {source} 的额度窗口，这条记录不会生效，请稍后再试）")
        return 0
    say(f"已记下：本会话留在 {source}，这一段额度（到各窗口重置前）cca 不会因为提醒点搬走它；撞墙了仍会搬。")
    return 0


def move_session(source: str, target: str, full: str, *, prechecked: bool = False, tell: bool = True) -> int:
    """把一条会话从源号搬到目标号并让它接着做（由常驻的 watchdog 在合适的时刻调用）。

    原则：任何一步不成，都不让会话就这么停着。
    预检目标号（地区 / 额度；watchdog 已经查过的传 prechecked）→ 停源号、目标号续跑并让它接着做。
    预检不过或停之前就失败：会话还活着，发一句话告诉它没换成、接着在源号做；
    停了以后目标号续不上：回源号续上，同样说清楚。会话已经被人停掉或搬走了：什么都不做。
    """
    problem = "" if prechecked else target_problem(target)
    items: list = []
    # 预检要几秒：动手前再确认它还在源号上（这几秒里被人停掉的，不能在别的号上复活）
    if not problem and full not in live_session_ids(source):
        say(f"{full[:8]}: 预检期间会话已经不在 {source} 上跑了，不换号")
        return 1
    if not problem:
        items = [item for item in handoff_candidates(source) if item["full"] == full]
        if not items:
            problem = f"在 {source} 的会话清单里找不到它"
        else:
            taken, failures = run_handoff(target, items, {1},
                                          prompt=SELF_MOVE_NUDGE.format(source=source, target=target))
            if taken:
                return _report_handoff(source, target, taken, failures)
            if any(failure.endswith(ALREADY_RUNNING_TAIL) for failure in failures):
                # 目标号上已经有一份在跑：这是重复会话，不是搬失败；留给 watchdog 的重复处理（它会停掉多余的那份）
                say(f"{full[:8]}: {failures[0]}（重复会话，不算搬失败）")
                return MOVE_ALREADY_THERE
            if any(HANDOFF_BUSY in failure for failure in failures):
                # 另一个 handoff 正拿着锁在搬它：那边会负责到底，这里再回源号续或发话都会添乱
                say(f"{full[:8]}: 另一个 handoff 正在搬它，这个帮手不动")
                return 1
            problem = (failures or ["原因不明"])[0]
    say(f"{full[:8]}: 没换到 {target}：{problem}")
    # tell：会话自己要求换号（--current）时它在等结果，要告诉它没换成；watchdog 自己发起的，
    # 会话本来停在这一轮结束、什么都不知道，发一句「接着做」反而凭空开出一轮——不发。
    back = f"（cca）没换到 {target}：{problem}。会话还在 {source} 上，接着做刚才没做完的事。" if tell else ""
    running_on = [name for name in discover_profiles() if full in live_session_ids(name)]
    if source in running_on:
        if tell:
            _tell_session(source, full, back)
    elif running_on:
        # 它已经在别的号上跑着（目标号其实起来了、或者另有人在搬）：再在源号续一份就是副本
        say(f"{full[:8]}: 它已经在 {'、'.join(running_on)} 上跑着，不再回 {source} 续")
    else:
        _resume_back(source, full, back, cwd=items[0]["cwd"] if items else "")
    return 1


def target_problem(target: str) -> str:
    """换号前先看目标号起得起来、有没有额度；没问题返回空串。停源号之前就要知道。"""
    from cca.env import session_env  # noqa: PLC0415
    from cca.session.watchdog import fresh_room  # noqa: PLC0415

    try:
        session_env(target)              # 起会话前的那道闸：地区、代理、时区
    except LaunchBlocked as exc:
        return str(exc)
    if profile_kind(target) != "api" and not fresh_room(target):
        return "目标号额度已经用满"
    return ""


def _tell_session(account: str, full: str, text: str) -> None:
    """给一条还活着的会话说一句（原生跨会话消息），让它接着干。"""
    from cca.session.takeover import deliver, registry_entry  # noqa: PLC0415

    error = deliver(registry_entry(account, full), text, {"name": "cca"})
    if error:
        warn(f"{full[:8]}: 没能告诉它：{error}")


def _resume_back(account: str, full: str, text: str, *, cwd: str = "") -> None:
    """停了以后没在目标号续上：回原号续上，并说清楚。cwd 没给就从对话记录里认。"""
    if not cwd:
        path = session_transcript(profile_dir(account), full[:8])
        cwd = _scan_transcript(path).get("cwd", "") if path is not None else ""
    cwd = launch_dir(cwd) if cwd else ""
    from cca.session.takeover import deliver_after_resume, resume_launch  # noqa: PLC0415

    launch, pending = resume_launch(account, full, text)
    try:
        r = _run_claude(api_launch_args(account, ["--bg", "--resume", full, *launch]), account,
                        capture=True, cwd=cwd or None, session=True)
    except LaunchBlocked as exc:
        warn(f"{full[:8]}: 也没能在 {account} 续上：{exc}；手动：cca {account} --bg --resume {full}")
        return
    if r.returncode != 0:
        warn(f"{full[:8]}: 也没能在 {account} 续上：{(r.stderr or r.stdout or '').strip()[:160]}；"
             f"手动：cca {account} --bg --resume {full}")
        return
    say(f"{full[:8]}: 已回到 {account} 续上")
    error = deliver_after_resume(account, full, pending)
    if error:
        warn(f"{full[:8]}: 回到 {account} 续上了，但这句话没送到：{error}；它会停在等输入（话是：{text}）")


def cmd_handoff(args: list) -> int:  # noqa: C901, PLR0911, PLR0912 - 每个出口都是一种「不搬」的说明
    """把 <源号> 的会话搬到 <目标号>：源号 stop → 目标号 `--bg --resume`（对话不复制、不丢）。

    默认只搬"没在跑的"（等输入 / 已结束）：接管会继续跑，烧的是目标号的额度。要连在跑的一起
    搬用 `--all`；非 tty 必须给 `--only` 或 `--all`，否则报错退出，不留"猜你想搬哪条"的余地。
    """
    if not args and sys.stdin.isatty() and sys.stdout.isatty():
        # 只敲 cca handoff：先弹框选源号和目标号
        args = _pick_handoff_pair()
        if not args:
            say(HANDOFF_CANCELLED)
            return 0
    rest, flags = parse_flags(
        args,
        bool_flags=("--all", "--dry-run", "--strip-tool-search", "--attach", "--current", "--stay"),
        value_flags=("--session",),
        multi_flags=("--only",),
        usage=HANDOFF_USAGE,
    )
    if flags.get("--session"):
        # 面板插件的 /cca handoff 从 Claude Code 里调用：会话编号显式传进来（插件的子进程不一定带这个环境变量）
        if not (flags.get("--current") or flags.get("--stay")):
            die("--session 只配合 --current / --stay 用")
        os.environ["CLAUDE_CODE_SESSION_ID"] = str(flags.pop("--session"))
    if flags.get("--stay"):
        if rest or len(flags) > 1:
            die("--stay 只能单独用，不带号。用法: cca handoff --stay")
        return _request_stay()
    source, target, only = _handoff_pair(rest, flags)
    if flags.get("--current") and not flags.get("--dry-run"):
        return _request_self_move(source, target)
    if source == target:
        die("源号和目标号是同一个；handoff 的用途是换个号接着跑")
    require_profile(source, usage=HANDOFF_USAGE)
    require_profile(target, usage=HANDOFF_USAGE)

    dry_run = bool(flags.get("--dry-run"))
    take_all = bool(flags.get("--all"))
    if only is None:
        only = [t for t in (flags.get("--only") or []) if t.strip()]
    if not only and not take_all and not dry_run and not sys.stdin.isatty():
        die(
            f"非交互环境下要写清搬哪几条。用法: {HANDOFF_USAGE}"
            f"\n  例: cca handoff {source} {target} --only=<前缀>"
        )

    console = make_console()
    found = handoff_candidates(source)
    # 停掉的会话不列：人主动停的、或者早就收掉的，换号面板也不列它。
    # 显式 --only 点名的那条保留，确实要搬一条停掉的时还有路。
    items = [item for item in found if item.get("state") != "stopped"
             or any(_matches_only(item, term) for term in only)]
    if found and not items:
        say(f"{source} 只剩停掉的会话，不列；要搬其中一条，用 --only=<编号前缀> 点名，"
            f"或在目标号 cca {target} --bg --resume <完整编号>")
        return 0
    if not items:
        say(f"{source} 没有可搬的后台会话（交互会话不搬：--bg --resume 只会 fork 一份副本）")
        return 0

    selected = _handoff_selection(console, items, target, (only, take_all, dry_run, source))
    if dry_run:
        say("--dry-run：以上是计划，未停任何会话、未接管")
        return 0
    if not selected:
        return 0

    taken, failures = run_handoff(target, items, selected,
                                strip_tool_search=bool(flags.get("--strip-tool-search")))
    code = _report_handoff(source, target, taken, failures)
    # --attach：搬完直接进那条会话，省一步手打 attach
    if flags.get("--attach") and taken:
        _attach_session(target, taken[0]["id"])
    return code

def cmd_import(args: list) -> int:
    """单向复制会话：cca import <destination> <source>。"""
    if len(args) != PAIR_ARGS:
        die("用法: cca import <目标 profile> <源 profile>")
    dst, src = args
    if dst == src or not profile_dir(dst).is_dir() or not profile_dir(src).is_dir():
        die("目标和源 profile 必须存在且不能相同")
    marker = profile_dir(dst) / ".cca-isolated.json"
    if not marker.is_file():
        die(f"目标 profile {dst} 不是隔离 profile（创建时使用 cca new {dst} --isolated）")
    source = profile_dir(src) / "projects"
    target = profile_dir(dst) / "projects"
    if not source.is_dir():
        say(f"{src}: 没有可复制的会话")
        return 0
    copied = 0
    for item in source.rglob("*"):
        rel = item.relative_to(source)
        out = target / rel
        if item.is_dir():
            out.mkdir(parents=True, exist_ok=True)
        elif not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)
            copied += 1
    say(f"已从 {src} 单向复制到 {dst}: {copied} 个文件")
    return 0

def _sync_api_settings(name: str, marker: dict, src: Path, dst: Path) -> None:
    """API 号：保留它自己的 env 与 apiKeyHelper，其余从默认 settings 播种，再按清单重渲染。"""
    merged = _settings(name)
    try:
        seeded = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        seeded = {}
    if not isinstance(seeded, dict):
        seeded = {}
    # env 取并集：默认设置里的公共开关要传下来，该号自己的端点/模型优先（随后再按清单重渲染）。
    seeded["env"] = {**(seeded.get("env") or {}), **(merged.get("env") or {})}
    if merged.get("apiKeyHelper"):
        seeded["apiKeyHelper"] = merged["apiKeyHelper"]
    dst.write_text(json.dumps(seeded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_settings_env(name, marker)
    say(f"已从 {src} 播种 {dst}（保留 API 号的 env 与 apiKeyHelper，并按清单重渲染）")

def cmd_sync(args: list) -> int:
    if not args:
        die("用法: claude-acct sync <profile>")
    name = args[0]
    if not profile_dir(name).is_dir():
        die(f"profile {name} 不存在")
    marker = None
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
    src = _default_settings_source()
    if src is None:
        die(f"没有可播种的默认 settings：{default_settings_file()} 不存在（把想用的 settings.json 放到那里）")
    dst = profile_dir(name) / "settings.json"
    if marker is None:
        shutil.copy2(src, dst)
        say(f"已用 {src} 覆盖 {dst}")
    else:
        _sync_api_settings(name, marker, src, dst)
    if profile_kind(name) == "api" and ensure_api_config(name):
        say(f"{name}: 已建好 .claude.json（含 hasCompletedOnboarding）")
    elif ensure_onboarding_flag(name):
        say(f"{name}: 已补 hasCompletedOnboarding")
    added = seed_trust_from_default(name)
    if added:
        say(f"{name}: 已补 {added} 条目录信任记录")
    return 0

def cmd_which(args: list) -> int:
    """一个号是谁：目录 / 类型 / 端点。一行，脚本友好；详情看 cca status <profile>。"""
    rest = [a for a in args if not a.startswith("-")]
    if rest[1:]:
        die(f"多余参数 {' '.join(rest[1:])}；用法: cca which <profile>")
    name = require_profile(rest[0] if rest else None, usage="cca which <profile>")
    kind = "api" if profile_kind(name) == "api" else "oauth"
    if kind == "api":
        marker = read_marker(name)[0] or {}
        display = api_display(marker.get("provider", ""), marker.get("base_url", ""))
        say(f"{name}  {kind}  {display}  {profile_dir(name)}")
    else:
        say(f"{name}  {kind}  {profile_dir(name)}")
    return 0

def model_policy(name: str) -> dict:
    """该号当前的选型策略：主模型 / 子代理 / 四档别名 / 主模型推理档 + 主模型来自哪儿。

    **主模型的真相按号类型分**：API 号看清单的 `env.ANTHROPIC_MODEL`，订阅号看 `settings.model`。
    不能反过来 —— 新号的 settings.json 是从默认 settings 整份播种的，模板里的 `model`
    会跟着进每个新号（实测：ds/or 的 settings.model 都曾是 `opus`），照它报会把主模型和
    `--effort` 一起带错。
    """
    marker = read_marker(name)[0] if profile_kind(name) == "api" else None
    env = dict((marker or {}).get("env") or {})
    settings = _settings(name)
    if marker is None:
        env.update({str(k): str(v) for k, v in (settings.get("env") or {}).items()})
    if marker is not None:
        main = str(env.get("ANTHROPIC_MODEL") or settings.get("model") or "")
        source = "marker" if env.get("ANTHROPIC_MODEL") else "settings"
    else:
        main = str(settings.get("model") or env.get("ANTHROPIC_MODEL") or "")
        source = "settings" if settings.get("model") else "env"
    effort = ""
    for model, conf in (settings.get("modelSettings") or {}).items():
        if model == main and isinstance(conf, dict):
            effort = str(conf.get("effortLevel") or "")
    return {
        "profile": name,
        "main": main,
        "sub": env.get("CLAUDE_CODE_SUBAGENT_MODEL", ""),
        "opus": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
        "sonnet": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
        "haiku": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
        "fable": env.get("ANTHROPIC_DEFAULT_FABLE_MODEL", ""),
        "effort": effort,
        "source": source,
    }

def _model_env_changes(changes: dict) -> dict:
    """把 `main/sub/opus…` 翻成要写的环境变量。"""
    env_changes = {}
    pairs = [("sub", "CLAUDE_CODE_SUBAGENT_MODEL")] + [
        (flag.lstrip("-"), var) for flag, var in MODEL_ALIAS_FLAGS.items()
    ]
    for key, env_name in pairs:
        if changes.get(key):
            env_changes[env_name] = changes[key]
    if changes.get("main"):
        env_changes["ANTHROPIC_MODEL"] = changes["main"]
    return env_changes

def _write_api_model_policy(name: str, marker: dict, env_changes: dict, changes: dict) -> None:
    """API 号：清单是唯一真相；窗口跟着主模型一起算（spec §4：号级默认窗口）。"""
    env = dict(marker.get("env") or {})
    env.update(env_changes)
    if changes.get("main"):
        window = catalog_window_for_slug(changes["main"])
        if window:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(window)
        else:
            env.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
    marker["env"] = env
    write_marker(name, marker)          # 写清单 + chmod + 重渲染 settings（含 model）

def _write_effort(name: str, marker, changes: dict) -> list:
    """写推理档到 settings.json；返回做过的项。"""
    if not changes.get("effort"):
        return []
    data = _settings(name)
    main = (changes.get("main")
            or (marker or {}).get("env", {}).get("ANTHROPIC_MODEL")
            or data.get("model") or "")
    if not main:
        die("要设 --effort 得先有主模型：cca model <号> --main <slug> --effort <level>")
    settings_map = dict(data.get("modelSettings") or {})
    settings_map[main] = {**(settings_map.get(main) or {}), "effortLevel": changes["effort"]}
    data["modelSettings"] = settings_map
    profile_dir(name).joinpath("settings.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # API 号：预设里写着 `CLAUDE_CODE_EFFORT_LEVEL`（环境变量），它可能压过 modelSettings ——
    # 使用者显式改了档位，就把清单 env 一起改掉并重渲染，免得"改了没生效"。
    # 注意顺序：先写上面那份 `data`（含 modelSettings），再 `write_marker` 按清单重渲染 env ——
    # 反过来会把刚渲染好的 env 又用旧 data 盖回去（真机踩过：marker 是 high、settings 还是 max）。
    if marker is not None:
        env = dict(marker.get("env") or {})
        env["CLAUDE_CODE_EFFORT_LEVEL"] = str(changes["effort"])
        marker["env"] = env
        write_marker(name, marker)
    return ["effort"]

def _write_oauth_model_policy(name: str, env_changes: dict, changes: dict) -> None:
    """订阅号：settings.json 就是真相，主模型/别名/子代理都写它。"""
    data = _settings(name)
    env = dict(data.get("env") or {})
    env.update(env_changes)
    if env:
        data["env"] = env
    if changes.get("main"):
        data["model"] = changes["main"]
    profile_dir(name).joinpath("settings.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

def write_model_policy(name: str, changes: dict) -> dict:
    """把选型写进该号：API 号同步清单后重渲染，订阅号直接写 settings.json。

    `changes` 的键是 `main` / `sub` / `opus` / `sonnet` / `haiku` / `fable` / `effort`，
    值为空串表示"这项不动"（命令行没给就不在 changes 里）。
    """
    marker, error = read_marker(name) if profile_kind(name) == "api" else (None, "")
    if marker is not None and error:
        die(f"{name} 的清单读不了：{error}")
    env_changes = _model_env_changes(changes)
    if marker is not None:
        _write_api_model_policy(name, marker, env_changes, changes)
    done = _write_effort(name, marker, changes)
    if marker is None:
        _write_oauth_model_policy(name, env_changes, changes)
    done += sorted({k.lstrip("-") for k in changes if k != "--effort"})
    return {"profile": name, "changes": {key: changes[key] for key in sorted(set(done)) if key in changes},
            "policy": model_policy(name)}

def format_price(pricing: dict) -> str:
    """把 OpenRouter 的"每 token 美元"换算成人话：`FREE` 或 `$入/$出 每 M`。"""
    def per_million(key):
        try:
            return float((pricing or {}).get(key))
        except (TypeError, ValueError):
            return None

    prompt, completion = per_million("prompt"), per_million("completion")
    if prompt is None and completion is None:
        return "-"
    if prompt == 0 and completion == 0:
        return "FREE"

    def fmt(value):
        if value is None:
            return "?"
        return f"{value * 1_000_000:.2f}".rstrip("0").rstrip(".") or "0"

    return f"${fmt(prompt)}/${fmt(completion)} 每 M"

def filter_catalog(catalog: list, *, free_only: bool = False, search: str = "", sort: str = "") -> list:
    """目录的只读视图：免费过滤、关键词搜索、排序（price = 输入价升序，ctx = 窗口降序）。"""
    rows = list(catalog)
    if free_only:
        rows = [m for m in rows if str(m.get("id", "")).endswith(":free") or m.get("id") == "openrouter/free"]
    needle = str(search or "").strip().lower()
    if needle:
        rows = [m for m in rows
                if needle in str(m.get("id", "")).lower() or needle in str(m.get("name", "")).lower()]
    if sort == "price":
        def prompt_price(model):
            try:
                return float((model.get("pricing") or {}).get("prompt"))
            except (TypeError, ValueError):
                return float("inf")
        rows.sort(key=lambda m: (prompt_price(m), str(m.get("id", ""))))
    elif sort == "ctx":
        rows.sort(key=lambda m: (-(m.get("context_length") or 0), str(m.get("id", ""))))
    else:
        rows.sort(key=lambda m: str(m.get("id", "")))
    return rows

def list_models(rest: list, flags: dict) -> int:
    """`cca model --list`：只读 OpenRouter 目录，不碰任何号。"""
    if rest[1:]:
        die(f"多余参数 {' '.join(rest[1:])}；用法: cca model [<号>] --list [--free] [--search <词>]")
    sort = str(flags.get("--sort") or "").strip()
    if sort and sort not in ("price", "ctx"):
        die(f"--sort 只认 price 或 ctx（收到 {sort!r}）")
    catalog = load_model_catalog(refresh=bool(flags.get("--refresh")))
    if not catalog:
        die("拿不到 OpenRouter 目录（网络不通且没有可用缓存）；稍后再试，或用 cca doctor --online 看出口")
    rows = filter_catalog(
        catalog,
        free_only=bool(flags.get("--free")),
        search=str(flags.get("--search") or ""),
        sort=sort,
    )
    if flags.get("--json"):
        print(json.dumps(rows, ensure_ascii=False, indent=2))  # noqa: T201 - CLI JSON 输出边界
        return 0
    if not rows:
        say("目录里没有符合条件的模型")
        return 0

    from rich.table import Table  # noqa: PLC0415 - Rich 只在真正渲染时导入
    table = Table(box=None, collapse_padding=True, pad_edge=False, header_style="bold")
    table.add_column("ID", no_wrap=True, overflow="ellipsis")
    table.add_column("名称", overflow="ellipsis", no_wrap=True)
    table.add_column("ctx", justify="right", no_wrap=True)
    table.add_column("价格", justify="right", no_wrap=True)
    for model in rows:
        table.add_row(
            _plain(model["id"]),
            _plain(str(model.get("name") or "")),
            _plain(f"{model.get('context_length') or '-'}"),
            _plain(format_price(model.get("pricing") or {})),
        )
    make_console().print(table)
    say(f"共 {len(rows)} 个模型（目录来自 OpenRouter /api/v1/models，缓存 1 小时；--refresh 强制刷新）")
    return 0

def _model_follow(rest: list, flags: dict) -> int:
    """`--follow`：把同档位的新版本升上去（一次只跟一个号）。"""
    if flags.get("--all"):
        die("--follow 一次只跟一个号：cca model <号> --follow")
    if rest and len(rest) > 1:
        die(f"多余参数 {' '.join(rest[1:])}；用法: {MODELS_USAGE}")
    return run_model_follow(require_profile(rest[0] if rest else None, usage=MODELS_USAGE),
                            dry_run=bool(flags.get("--dry-run")))

def _write_models(names: list, changes: dict, flags: dict) -> int:
    """把选型写到这些号，并打印结果。"""
    results = [write_model_policy(n, changes) for n in names]
    if flags.get("--json"):
        print(json.dumps(results, ensure_ascii=False, indent=2))  # noqa: T201 - CLI JSON 输出边界
        return 0
    for item in results:
        policy = item["policy"]
        say(f"{policy['profile']}: 主模型 → {policy['main'] or '-'}"
            + (f"，子代理 → {policy['sub']}" if policy["sub"] else "")
            + (f"，effort → {policy['effort']}" if policy["effort"] else ""))
    say("改的是号级默认（新会话生效）；只想改一次就用 cca <号> --model <slug>")
    return 0

def cmd_model(args: list) -> int:
    """看或改"这个号用哪些模型"。

    不带号 = 只读汇总（每个号一行）；写入必须写号，批量要显式 `--all`。
    `--list` 不在号上动手，直接看 OpenRouter 目录（见 cca model --help）。
    """
    value_flags = ("--main", "--sub", "--effort", "--search", "--sort", *MODEL_ALIAS_FLAGS)
    rest, flags = parse_flags(
        merge_space_value_flags(args, value_flags),
        bool_flags=("--all", "--json", "--list", "--free", "--refresh", "--follow", "--dry-run"),
        value_flags=value_flags,
        usage=MODEL_USAGE,
    )
    if flags.get("--list") or flags.get("--free") or flags.get("--search"):
        return list_models(rest, flags)
    if flags.get("--follow"):
        return _model_follow(rest, flags)
    if flags.get("--effort") and flags["--effort"] not in EFFORT_LEVELS:
        die(f"--effort 只认 {'/'.join(EFFORT_LEVELS)}（收到 {flags['--effort']!r}）")

    changes = {key: str(flags[key]).strip() for key in
               ("--main", "--sub", "--effort", *MODEL_ALIAS_FLAGS) if flags.get(key)}
    changes = {key.lstrip("-"): value for key, value in changes.items()}
    writes = bool(changes)

    if not rest and not writes:
        return print_model_summary(discover_profiles(), as_json=bool(flags.get("--json")))
    if rest and len(rest) > 1:
        die(f"多余参数 {' '.join(rest[1:])}；用法: {MODEL_USAGE}")

    if flags.get("--all"):
        if rest:
            die("--all 与写号不能同时给；要么 cca model <号> …，要么 cca model --all …")
        names = discover_profiles()
    else:
        name = require_profile(rest[0] if rest else None, usage=MODEL_USAGE)
        names = [name]
        if not writes:
            return print_model_summary(names, as_json=bool(flags.get("--json")))
    return _write_models(names, changes, flags)

def print_model_summary(names: list, *, as_json: bool = False) -> int:
    """每个号一行策略（只读）。"""
    policies = [model_policy(n) for n in names]
    if as_json:
        print(json.dumps(policies, ensure_ascii=False, indent=2))  # noqa: T201 - CLI JSON 输出边界
        return 0
    if not policies:
        say("还没有任何号；先 cca add <name> …")
        return 0
    from rich.table import Table  # noqa: PLC0415 - Rich 只在真正渲染时导入
    table = Table(box=None, collapse_padding=True, pad_edge=False, header_style="bold")
    for column in ("PROFILE", "主模型", "子代理", "opus", "sonnet", "haiku", "fable", "effort"):
        table.add_column(column, overflow="ellipsis", no_wrap=True)
    for p in policies:
        table.add_row(*(_plain(p[key] or "-") for key in
                        ("profile", "main", "sub", "opus", "sonnet", "haiku", "fable", "effort")))
    make_console().print(table)
    return 0

def configured_models(name: str) -> dict:
    """该号现在配着的 `{键: slug}`（只收有值的）。"""
    env = _settings(name).get("env") or {}
    return {key: str(env[env_name]).strip() for key, env_name in MODEL_FOLLOW_KEYS
            if str(env.get(env_name) or "").strip()}

def cmd_models(args: list) -> int:
    if not args:
        die(MODELS_USAGE)
    as_json = "--json" in args
    rest = [a for a in args if a != "--json"]
    name = require_profile(rest[0] if rest else None, usage=MODELS_USAGE)
    ids, error = endpoint_models(name)
    if error:
        die(f"{name}: {error}")
    current = set(configured_models(name).values())
    if as_json:
        print(  # noqa: T201 - CLI JSON 输出边界
            json.dumps({"profile": name, "models": ids,
                        "configured": sorted(current)}, ensure_ascii=False, indent=2))
        return 0
    console = make_console()
    for model_id in ids:
        mark = "  ← 该号在用" if model_id in current else ""
        console.print(_plain(model_id + mark, style="bold" if mark else ""))
    console.print(_plain(""))
    console.print(_plain(f"共 {len(ids)} 个；带 `← 该号在用` 的是它现在配的。"
                         f"升级同档位的新版本: cca model {name} --follow", style="dim"))
    return 0

def run_model_follow(name: str, *, dry_run: bool) -> int:
    """`--follow` 的实现：探端点 → 算跟随计划 → 打印 → （非 dry-run 时）写下去。"""
    ids, error = endpoint_models(name)
    if error:
        die(f"{name}: {error}")
    configured = configured_models(name)
    if not configured:
        die(f"{name}: settings 里没有任何模型配置，没什么可跟随的")
    plan = follow_plan(configured, ids)
    console = make_console()
    if not plan:
        console.print(_plain(f"{name}: 已是最新（{len(configured)} 项都跟得上端点清单）"))
        return 0
    table = _table(console)
    for title in ("项", "现在", "跟随"):
        table.add_column(title, no_wrap=True)
    for key, old, new in plan:
        table.add_row(key, old, new)
    console.print(table)
    if dry_run:
        console.print(_plain("--dry-run：没有写入。去掉它就照上面这版改。", style="dim"))
        return 0
    results = write_model_policy(name, {key: new for key, _old, new in plan})
    console.print(_plain(f"{name}: 已写入 {len(plan)} 项（重启会话才生效）", style="bold"))
    for key, old, new in plan:
        console.print(_plain(f"  {key}: {old} → {new}", style="dim"))
    if isinstance(results, dict) and results.get("error"):
        die(str(results["error"]))
    return 0

def env_flags_to_vars(api: dict) -> dict:
    """`--env K=V` 列表 → 变量字典；**空值（`K=`）表示删除该键**，值用 None 标记。"""
    out = {}
    for item in api.get("--env", []):
        key, sep, value = str(item).partition("=")
        key = key.strip()
        if not key or not sep:
            die(f"--env 只接受 KEY=VALUE（收到 {item!r}）；删变量写 KEY=")
        if key in MANAGED_ENV_KEYS:
            die(
                f"--env 不能设置 {key}（工具管理的键：端点用 --url、密钥用 --key/--key-stdin、"
                "代理用 --proxy、配置目录由 profile 决定）"
            )
        out[key] = value or None
    return out

PROXY_BLOCK_START = "# >>> claude-acct proxy >>>"
PROXY_BLOCK_END = "# <<< claude-acct proxy <<<"


def _proxy_label(value: str) -> str:
    """把代理取值翻成人话。"""
    low = str(value or "").strip().lower()
    if low in ("none", "off", "-"):
        return "直连"
    if low == "inherit":
        return "不注入（继承 shell 原值）"
    if low in ("", "builtin"):
        return "未配置"
    return str(value).strip()


def _shell_rc() -> Path:
    """当前 shell 的启动文件；认不出的 shell 按 bash 处理。"""
    shell = Path(os.environ.get("SHELL") or "").name
    if shell == "zsh":
        return Path.home() / ".zshrc"
    if shell == "fish":
        return Path.home() / ".config" / "fish" / "config.fish"
    return Path.home() / ".bashrc"


def _write_global_proxy(value: str) -> Path:
    """把 export CCA_PROXY=… 写进 shell 启动文件里带标记的一段，重复执行只改这段。"""
    rc = _shell_rc()
    if rc.name == "config.fish":
        line = f"set -gx CCA_PROXY {shlex.quote(value)}"
    else:
        line = f"export CCA_PROXY={shlex.quote(value)}"
    block = f"{PROXY_BLOCK_START}\n{line}\n{PROXY_BLOCK_END}\n"
    text = rc.read_text(encoding="utf-8") if rc.is_file() else ""
    pattern = re.compile(
        rf"{re.escape(PROXY_BLOCK_START)}.*?{re.escape(PROXY_BLOCK_END)}\n?", re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += block
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text(text, encoding="utf-8")
    return rc


def _show_account_proxy(name: str) -> None:
    marker = read_marker(name)[0] if profile_kind(name) == "api" else None
    stored = read_account_proxy(name)
    proxy, unset = proxy_settings(marker, stored)
    own = bool(stored) or bool(marker and marker.get("proxy") not in ("", "builtin"))
    source = "单号" if own else "全局"
    shown = "未配置" if proxy is None and unset else _proxy_label(proxy or "none")
    say(f"{name}: {shown}（来源：{source}）")


def _set_tui(args: list) -> int:
    """`cca set tui fullscreen|default`：所有号默认用哪种渲染模式（鼠标点击只在 fullscreen 生效）。"""
    path = accounts_root() / "tui"
    if not args:
        say(f"渲染模式: {tui_mode()}"
            "（fullscreen = 全屏渲染，鼠标能点；default = 经典渲染）")
        return 0
    mode = args[0]
    if mode not in ("fullscreen", "default"):
        die("取值只有 fullscreen 或 default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mode + "\n", encoding="utf-8")
    say(f"渲染模式已设为 {mode}，新启动的会话生效"
        + ("（面板里鼠标点击只在 fullscreen 下有效）" if mode == "default" else ""))
    return 0

def _set_summary(args: list) -> int:
    """`cca set summary <号> [模型|off|default]`：sideagent 横条进度短语用这个号的哪个模型。"""
    from cca.session import summary  # noqa: PLC0415 - 只有这个子命令要

    if not args:
        say("用法: cca set summary <号> [模型|off|default]")
        return 0
    name = require_profile(args[0], usage="cca set summary <号> [模型|off|default]")
    path = summary.model_file(profile_dir(name))
    if len(args) == 1:
        model = summary.configured_model(profile_dir(name))
        say(f"{name}: 进度短语用 {model or 'off（不写，横条显示工具的活动描述）'}"
            + ("（默认）" if not path.exists() else ""))
        return 0
    value = args[1].strip()
    if value == "default":
        path.unlink(missing_ok=True)
        say(f"{name}: 进度短语用默认模型 {summary.DEFAULT_MODEL}（这个号自己的小模型）")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    say(f"{name}: 进度短语用 {value}" if value.lower() not in ("off", "none", "-")
        else f"{name}: 不写进度短语，横条显示工具的活动描述")
    note("新起的 sideagent 生效；已经在跑的下次写短语时就会读到")
    return 0


def cmd_set(argv: list) -> int:
    """`cca set proxy …` 代理；`cca set tui` 渲染模式。"""
    handlers = {"tui": _set_tui, "summary": _set_summary}
    if argv[:1] and argv[0] in handlers:
        return handlers[argv[0]](argv[1:])
    if not argv or argv[0] != "proxy":
        say(SET_USAGE)
        return 0
    rest = [item for item in argv[1:] if item != "--global"]
    global_scope = "--global" in argv[1:]
    max_args = 2
    if len(rest) > max_args:
        die("参数太多。用法: cca set proxy <profile> <url|none> / cca set proxy --global <url|none>")
    if global_scope:
        if not rest:
            raw = os.environ.get(PROXY_ENV_VAR)
            say(f"全局: {_proxy_label(raw) if raw is not None else '未配置'}（文件：{_shell_rc()}）")
            return 0
        value = normalize_proxy(rest[0])
        if not value:
            die("全局代理要给 none / inherit / <http(s)://…>")
        rc = _write_global_proxy(value)
        say(f"全局代理已写入 {rc}: {_proxy_label(value)}")
        note(f"新开终端生效；当前终端先执行 source {rc} 或 export CCA_PROXY={value}")
        return 0

    name = require_profile(rest[0] if rest else "", usage="cca set proxy <profile> <url|none>")
    if len(rest) == 1:
        _show_account_proxy(name)
        return 0
    value = normalize_proxy(rest[1])
    if not value:
        die("代理取值: none / inherit / <http(s)://…>")
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
        marker["proxy"] = value
        write_marker(name, marker)
    else:
        write_account_proxy(name, value)
    say(f"{name}: 代理 {_proxy_label(value)}（单号，优先于全局）")
    return 0
