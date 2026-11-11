"""账号文件读写：.claude.json / .credentials.json / settings / 清单。"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
from urllib.parse import urlsplit

from cca.account.model import discover_profiles
from cca.config import (
    AGENTS_TIMEOUT,
    API_TOOLSET_ENV,
    GATEWAY_FIRST_BYTE_KEY,
    GATEWAY_FIRST_BYTE_MS,
    GATEWAY_HOST_SUFFIXES,
    PROVIDERS,
)
from cca.errors import die
from cca.paths import (
    account_proxy_file,
    credentials_file,
    global_config_file,
    key_file,
    marker_file,
    profile_dir,
)

# 掩码至少这么长才露出前 6 后 4；更短的一律只说"已配置"。
MIN_MASK_KEY_LENGTH = 16


def profile_kind(name: str) -> str:
    """`api`（有清单）或 `oauth`（订阅号）。清单坏掉也算 api —— 要让上层报错，而不是当订阅号混过去。"""
    return "api" if marker_file(name).is_file() else "oauth"

def read_marker(name: str) -> tuple:
    """读清单，返回 (marker, error)：没有清单 → `(None, "")`（订阅号）；坏 → `(None, 原因)`。"""
    f = marker_file(name)
    if not f.is_file():
        return None, ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{f} 无法解析（{exc}）；修好 JSON，或 cca rm {name} 重建"
    if not isinstance(data, dict) or not str(data.get("base_url") or "").strip():
        return None, f"{f} 缺 base_url；修好 JSON，或 cca rm {name} 重建"
    marker = dict(data)
    marker["provider"] = str(data.get("provider") or "generic")
    marker["base_url"] = str(data["base_url"]).strip().rstrip("/")   # 尾斜杠会让 CLI 拼出 //v1/messages
    # 没写 proxy = 没配置（用全局默认），不等于明确直连
    marker["proxy"] = str(data.get("proxy") or "")
    marker["env"] = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    return marker, ""

def read_account_proxy(name: str) -> str:
    """读订阅号的代理；没有文件或读不到返回空串（= 用全局默认）。"""
    f = account_proxy_file(name)
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_account_proxy(name: str, value: str) -> None:
    """写订阅号的代理（0600，一行）。"""
    f = account_proxy_file(name)
    f.write_text(value.strip() + "\n", encoding="utf-8")
    f.chmod(0o600)


def read_api_key(name: str) -> str:
    """读密钥文件（strip 掉换行）。缺失或读不到都返回空串，由调用方决定怎么提示。"""
    f = key_file(name)
    if not f.is_file():
        return ""
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

def write_api_key(name: str, value: str) -> None:
    """写密钥文件（0600，末尾只留一个换行）。空值直接拒绝：写空 key 是最坏的"看起来成功"。"""
    value = value.strip()
    if not value:
        die(f"拒绝写入空的 API key（{key_file(name)}）")
    f = key_file(name)
    f.write_text(value + "\n", encoding="utf-8")
    f.chmod(0o600)

def mask_key(value: str) -> str:
    """掩码：前 6 后 4；短于 16 位只说"已配置"。工具任何输出都不出现完整 key。"""
    if not value:
        return "缺失"
    if len(value) < MIN_MASK_KEY_LENGTH:
        return "已配置"
    return f"{value[:6]}…{value[-4:]}"

def key_state(name: str) -> str:
    """展示用的密钥状态：`已配置 sk-abc…f3a2` / `缺失`。"""
    secret = read_api_key(name)
    return f"已配置 {mask_key(secret)}" if secret else "缺失"

def write_marker(name: str, marker: dict) -> None:
    """写回清单（0600），并按清单重渲染该号的 settings.json。"""
    marker_file(name).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    marker_file(name).chmod(0o600)
    write_settings_env(name, marker)

def key_env_name(marker: dict) -> str:
    """该号的密钥环境变量名：预设给 `key_env`（deepseek → DEEPSEEK_API_KEY），通用端点用 ANTHROPIC_API_KEY。"""
    preset = PROVIDERS.get(str(marker.get("provider") or ""), {})
    return str(preset.get("key_env") or "ANTHROPIC_API_KEY")

def api_key_helper_command(name: str) -> str:
    """凭据通道：让 claude 在请求时自己去读该号的 0600 密钥文件。

    官方 gateway 文档的要点：helper 的值**同时**放进 `Authorization: Bearer` 与 `x-api-key`
    （不会因为选错变量而 401），默认缓存 5 分钟（`CLAUDE_CODE_API_KEY_HELPER_TTL_MS` 可调），
    而且只有 settings 文件里的配置才能覆盖 supervisor 托管的后台会话。
    """
    return f"cat {key_file(name)}"

def is_gateway_marker(marker: dict) -> bool:
    """这个 API 号是不是走内网自建网关（codex 那类中转：gemini、gpt、cc 等）。

    两条都要满足：provider 不是 PROVIDERS 里的预设（deepseek、openrouter 直连官方 API），
    并且端点主机在内网域（config.GATEWAY_HOST_SUFFIXES）。外部的 `--url` 自定义端点宕机时
    不该被多等 330 秒，所以不算。订阅号没有清单，不会走到这里。
    """
    if str(marker.get("provider") or "generic") in PROVIDERS:
        return False
    host = (urlsplit(str(marker.get("base_url") or "")).hostname or "").lower()
    return any(host.endswith(suffix) for suffix in GATEWAY_HOST_SUFFIXES)

def api_env_vars(marker: dict) -> dict:
    """API 号要注入的非密环境变量：base_url + 工具兼容开关 + 清单 env。密钥不在这里（走 apiKeyHelper）。

    网关号默认再加首字节等待上限（见 config.GATEWAY_FIRST_BYTE_MS）；清单 env 里写了同名键
    就以清单为准。清单是唯一真相：这个值和其它清单键一样会被同步进 settings.json，
    改默认值后 doctor / report 会报漂移，下次写入时跟上。build_env（cca 起会话、sideagent
    起实例）和 write_settings_env（daemon respawn、直接跑 claude 读的 settings）共用这个出口。
    """
    env = {"ANTHROPIC_BASE_URL": marker["base_url"], **API_TOOLSET_ENV}
    if is_gateway_marker(marker):
        env[GATEWAY_FIRST_BYTE_KEY] = GATEWAY_FIRST_BYTE_MS
    env.update(marker["env"])
    return env

def write_settings_env(name: str, marker: dict) -> dict:
    """把清单渲染进该号 `settings.json`：`env` 块 + 顶级 `apiKeyHelper`（永不含密钥本身）。

    这样绕过 `cca` 直接跑 `claude` 也能到对的端点、也能拿到凭据（helper 去读 0600 文件）。
    真相始终在清单：`doctor` 比对两者是否一致，不一致提示 `cca sync`。
    """
    f = profile_dir(name) / "settings.json"
    data = {}
    if f.is_file():
        try:
            loaded = json.loads(f.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            data = {}
    env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    env.update(api_env_vars(marker))
    data["env"] = env
    if env.get("ANTHROPIC_MODEL"):
        # 顶级 model 与清单对齐：否则从默认 settings 播种来的 `model` 会在这儿变成第二个真相。
        data["model"] = env["ANTHROPIC_MODEL"]
    data["apiKeyHelper"] = api_key_helper_command(name)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return env

def ensure_onboarding_flag(name: str) -> bool:
    """补上 `hasCompletedOnboarding`，返回是否写了文件。

    建号（`cca new`）与 headless 登录（`claude auth login`）都不会写这个全局标记，
    于是新号第一次交互启动会把"其实已经登录好"的配置当成全新安装：从选主题开始走引导，
    甚至在引导里再要一次登录。启动前补上它，只动这一个布尔，不碰 oauthAccount。
    """
    f = global_config_file(name)
    if not f.is_file():
        return False
    try:
        raw = f.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict) or data.get("hasCompletedOnboarding") is True:
        return False
    data["hasCompletedOnboarding"] = True
    version = _cli_version()
    if version:
        data["lastOnboardingVersion"] = version
    try:
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2 if "\n" in raw else None), encoding="utf-8")
    except OSError:
        return False
    return True

def _trust_entries(name: str) -> dict:
    """该号全局配置里"已信任"的目录：只取路径，值固定为 True。"""
    f = global_config_file(name)
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return {}
    return {
        path: True
        for path, entry in projects.items()
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True
    }

def ensure_api_config(name: str) -> bool:
    """给 API 号建好 `.claude.json`（订阅号是 `claude auth login` 建它的，API 号没有那一步）。

    没有这个文件，两件事会静默落空：① `hasCompletedOnboarding` 没处写 → 首次**交互**启动会走
    引导向导（`-p` 模式看不出来，实测在 mock 端到端里才暴露）；② 001 的目录信任没处落盘 →
    `--bg` 会因为"目录未信任"起不来。已存在则只补引导标记。
    """
    f = global_config_file(name)
    if f.is_file():
        return ensure_onboarding_flag(name)
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    f.write_text(json.dumps({"hasCompletedOnboarding": True}, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    with contextlib.suppress(OSError):
        f.chmod(0o600)
    return True

def _load_global_config(f) -> tuple:
    """读全局配置，返回 (data, raw)：坏文件/非字典都返回 (None, "")，让调用方安静退出。"""
    if not f.is_file():
        return None, ""
    try:
        raw = f.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, ""
    return (data if isinstance(data, dict) else None), raw

def _merge_trust(data: dict, paths) -> int:
    """把 paths 逐个标记成已信任（只加不减），返回新加了几条。"""
    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects
    added = 0
    for path in paths:
        entry = projects.get(path)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            continue
        projects[path] = {**(entry if isinstance(entry, dict) else {}), "hasTrustDialogAccepted": True}
        added += 1
    return added

def _write_global_config(f, data: dict, raw: str) -> bool:
    """按原文件的换行风格写回；写失败返回 False。"""
    try:
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2 if "\n" in raw else None),
                     encoding="utf-8")
    except OSError:
        return False
    return True

def seed_trust_from_default(name: str) -> int:
    """把**其它号已信任**的目录补进该号，返回补了几条。

    同一台机器、同一个人的第二个号，不该在每个目录重新问一遍"是否信任此文件夹"。
    只加不减：目标号自己的信任记录原样保留，也绝不触碰账号字段。
    """
    # 来源是**所有其它号**的并集：信任是逐目录攒出来的，新号在"别人已信任的目录"里也该直接能用 ——
    # 实测新号在 002/ds 已信任的目录里起 --bg 会被 "Workspace not trusted" 挡下。
    source = {}
    for other in discover_profiles():
        if other == name:
            continue
        source.update(_trust_entries(other))
    if not source:
        return 0
    f = global_config_file(name)
    data, raw = _load_global_config(f)
    if data is None:
        return 0
    added = _merge_trust(data, source)
    if not added:
        return 0
    if not _write_global_config(f, data, raw):
        return 0
    return added

def copy_trust(src: str, dst: str, directory: str) -> bool:
    """把某个目录的"已信任"标记从 src 号复制到 dst 号（只加不减）；返回是否写了文件。

    `--bg` 拒绝在未信任的目录里启动（后台会话没法回答信任提示），所以交接前必须先补这一位。
    """
    if src == dst or not directory:
        return False
    if directory not in _trust_entries(src):
        return False
    f = global_config_file(dst)
    data, raw = _load_global_config(f)
    if data is None:
        return False
    if not _merge_trust(data, [directory]):
        return False
    return _write_global_config(f, data, raw)

def _settings(name: str) -> dict:
    """读该号 settings.json（坏文件当空字典：doctor/sync 要能继续跑并报出来）。"""
    f = profile_dir(name) / "settings.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

def _settings_env(name: str) -> dict:
    return {str(k): str(v) for k, v in (_settings(name).get("env") or {}).items()}

def _cli_version() -> str:
    """`claude --version` 的版本号（如 2.1.280）；取不到返回空串。"""
    exe = shutil.which("claude")
    if not exe:
        return ""
    try:
        r = subprocess.run(  # noqa: S603 - exe 来自 shutil.which("claude")，不是外部输入
            [exe, "--version"], capture_output=True, text=True, timeout=AGENTS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip().split(" ")[0] if r.returncode == 0 else ""

def credential_expiry(name: str) -> dict:
    """凭据到期时间：`{"access": ts|None, "refresh": ts|None}`（epoch 秒）。

    这是本工具**唯一**读 `.credentials.json` 的场合，且刻意不 `json.loads` 整个文件：
    只用正则取两个数字字段，token 本身既不进内存也不进任何输出。取不到就是 None，
    不换别的读法（凭据文件是 claude 自己写的，格式稳定）。

    access 到期很短（小时级，claude 启动时会用刷新令牌自动续），真正决定"要不要重新登录"
    的是 refresh 到期（月级）—— 两个都给出来，别让短的那个吓人。
    """
    out = {"access": None, "refresh": None}
    f = credentials_file(name)
    if not f.is_file():
        return out
    try:
        raw = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for key, field in (("access", "expiresAt"), ("refresh", "refreshTokenExpiresAt")):
        hit = re.search(rf'"{field}"\s*:\s*(\d+)', raw)
        if hit:
            out[key] = int(hit.group(1)) / 1000
    return out
