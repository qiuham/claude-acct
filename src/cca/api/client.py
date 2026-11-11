"""HTTP / 代理 / 错误映射 / 端点与模型目录。"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cca.account.model import read_identity
from cca.account.store import (
    _settings,
    ensure_api_config,
    key_env_name,
    profile_kind,
    read_account_proxy,
    read_api_key,
    read_marker,
    write_settings_env,
)
from cca.api.identity import anthropic_headers
from cca.config import (
    API_DISALLOWED_TOOLS,
    CATALOG_TTL,
    CATALOG_URL,
    MODELS_TIMEOUT,
    NO_PROXY_VALUE,
    PROVIDERS,
    PROXY_BUILTIN,
    PROXY_DISABLED,
    PROXY_ENV_VAR,
)
from cca.errors import die, note, warn
from cca.paths import catalog_cache_file, credentials_file, key_file, profile_dir

# 密钥/缓存文件只给当前用户读写；HTTP 5xx 表示网关或上游自己出错。
PRIVATE_FILE_MODE = 0o600
HTTP_SERVER_ERROR_MIN = 500


def _parse_proxy(value: str) -> tuple:
    """把配置值翻成 ((代理或 None, 是否没配置), 是否明确表态)。

    明确的直连（none/off/-/inherit）算表态；空或 builtin 算没配置，继续往外找。
    """
    low = value.strip().lower()
    if low in PROXY_DISABLED or low == "inherit":
        return (None, False), True
    if low in ("", PROXY_BUILTIN.lower()):
        return (None, True), False
    return (value.strip(), False), True


def proxy_settings(marker: dict | None = None, stored: str | None = None) -> tuple:
    """当前生效的代理：返回 (代理 URL 或 None, 是否"没配置")。

    优先级：号里存的代理 → `CCA_PROXY`（全局默认）。
    单号配置优先，所以设了全局代理后，个别号仍然可以走自己的地址。
    返回的第二项为 True 表示"既没配也没显式说直连"——订阅号遇到这种要拒绝启动。
    显式写 none/off/- 表示直连，第二项为 False。

    `stored` 是订阅号的代理文件内容；`marker` 是 API 号的清单（proxy 字段）。
    """
    for value in (stored, marker.get("proxy") if marker is not None else None):
        if value is None:
            continue
        result, explicit = _parse_proxy(str(value))
        if explicit:
            return result
    raw = os.environ.get(PROXY_ENV_VAR)
    result, _explicit = _parse_proxy(raw if raw is not None else "")
    return result

def proxy_unresolvable(proxy: str):
    """内建代理解析不了时返回主机名，否则 None。"""
    host = urllib.parse.urlsplit(proxy).hostname
    if not host:
        return None
    try:
        socket.getaddrinfo(host, None)
    except OSError:
        return host
    return None

def normalize_proxy(value) -> str:
    """校验并规整代理取值：空 / `none` / `inherit` / `<http(s)://…>`。

    没有默认代理：传空就存空，用的时候会要求先配。
    Claude Code 只认 http 代理，socks 会明确拒绝，不让人配了个跑不起来的地址。
    """
    proxy = str(value or "").strip()
    if not proxy:
        return ""                    # 不配置：启动时再要求补
    low = proxy.lower()
    if low in PROXY_DISABLED:
        return "none"
    if low in ("inherit", PROXY_BUILTIN.lower()):
        return "inherit" if low == "inherit" else ""
    if "://" not in proxy:
        die(f"--proxy 取值: 空（不配置，启动时要求先配）/ none / inherit / <http(s)://…>"
            f"（收到 {value!r}）")
    if not low.startswith(("http://", "https://")):
        die(f"只支持 http(s) 代理，Claude Code 不认 socks（收到 {value!r}）")
    return proxy

def api_no_proxy(marker: dict) -> str:
    """API 号的 NO_PROXY：内建列表 + 该号端点主机。

    端点走直连（实测比经内建代理快 6 倍），其余流量（自动更新、遥测、模型目录等）照常走代理 ——
    这两件事必须分开：上一版给 API 号"完全不注入代理"，结果把自动更新也饿死了
    （`downloads.claude.ai` 直连超时 → 红字 `Auto-update failed`）。
    """
    host = (urllib.parse.urlsplit(str(marker.get("base_url") or "")).hostname or "").strip()
    entries = [item for item in NO_PROXY_VALUE.split(",") if item]
    if host and host not in entries:
        entries.append(host)
    return ",".join(entries)

def api_display(provider: str, base_url: str) -> str:
    """API 号在面板/详情里的称呼：有预设用预设名，没有就用端点主机名（一眼看出指向哪里）。"""
    provider = str(provider or "").strip()
    if provider and provider != "generic":
        return f"api:{provider}"
    host = (urllib.parse.urlsplit(str(base_url or "")).hostname or "").strip()
    return f"api:{host}" if host else "api:generic"

def parse_catalog(payload) -> list:
    """把 `/api/v1/models` 的响应裁成我们要的字段；形状不对就当空目录。"""
    if not isinstance(payload, dict):
        return []
    items = payload.get("data")
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        context = item.get("context_length")
        pricing = item.get("pricing")
        supported = item.get("supported_parameters")
        out.append({
            "id": str(item["id"]),
            "name": str(item.get("name") or item["id"]),
            "context_length": int(context) if isinstance(context, (int, float)) else None,
            "pricing": pricing if isinstance(pricing, dict) else {},
            "supported_parameters": supported if isinstance(supported, list) else [],
        })
    return out

def fetch_model_catalog(url: str) -> list:
    """拉一次目录。网络/超时/坏 JSON 一律返回 []（由调用方决定回退）。"""
    request = urllib.request.Request(url, headers={"accept": "application/json"})  # noqa: S310 - 地址来自配置或环境
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 网络/超时/坏 JSON 都按"这次没拉到"处理，由调用方回退
        return []
    return parse_catalog(payload)

def read_catalog_file(path: Path) -> list:
    """读一份目录 JSON（缓存或 fixture）。读了坏文件当空目录，不抛。"""
    try:
        return parse_catalog(json.loads(path.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 - 缓存/fixture 坏掉就当空目录，不让展示命令崩
        return []

def load_model_catalog(*, refresh: bool = False) -> list:
    """取目录：fixture 覆盖 → 未过期缓存 → 联网重拉 → 旧缓存 → 空。

    `CCA_MODEL_CATALOG_FILE` 指到文件时直接读它、**不联网**（测试与离线用）；
    `CCA_CATALOG_URL` 换地址（测试用）。任何一步失败都往后退，不抛异常。
    """
    fixture = os.environ.get("CCA_MODEL_CATALOG_FILE", "").strip()
    if fixture:
        return read_catalog_file(Path(fixture))

    cache = catalog_cache_file()
    try:
        age = time.time() - cache.stat().st_mtime
    except OSError:
        age = None
    if age is not None and age < CATALOG_TTL and not refresh:
        return read_catalog_file(cache)

    url = os.environ.get("CCA_CATALOG_URL", "").strip() or CATALOG_URL
    fetched = fetch_model_catalog(url)
    if fetched:
        try:
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cache.write_text(json.dumps({"data": fetched}, ensure_ascii=False), encoding="utf-8")
            cache.chmod(PRIVATE_FILE_MODE)
        except OSError:
            pass          # 缓存写不进去也要能用目录：只是下次还得重拉
        return fetched

    if age is not None:
        return read_catalog_file(cache)
    return []

def find_model(catalog: list, slug: str) -> dict | None:
    """按 slug 找一条目录记录（OpenRouter 的 id 全小写，比较时也归一）。"""
    target = str(slug or "").strip().lower()
    if not target:
        return None
    for item in catalog:
        if str(item.get("id", "")).lower() == target:
            return item
    return None

def preset_for_url(url: str) -> str:
    """端点主机命中某个预设就返回它的名字（否则空串）。

    只在**主机完全一致**时推断（`api.deepseek.com`），不猜镜像/反代域名 —— 那些端点的模型名
    往往由中转自己映射，乱套预设反而出错。
    """
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    if not host:
        return ""
    for name, preset in PROVIDERS.items():
        if (urllib.parse.urlsplit(str(preset.get("base_url") or "")).hostname or "").lower() == host:
            return name
    return ""

def parse_model_slug(slug: str):
    """`gpt-6-sol` → `("gpt-", (6,), "-sol")`；拆不出来返回 None（宁可不跟，也不猜）。

    版本段允许 `6` / `6.1` / `5.6` 这种点分数字；前缀与后缀原样保留 —— 跟随只允许
    **同前缀、同后缀**、版本严格变大，所以 `-sol` 永远不会被换成 `-astra`。
    """
    found = re.fullmatch(r"([A-Za-z][A-Za-z0-9._-]*?)(\d+(?:\.\d+)*)(-[A-Za-z0-9._-]+)?", str(slug or ""))
    if not found:
        return None
    version = tuple(int(part) for part in found.group(2).split("."))
    return found.group(1), version, found.group(3) or ""

def follow_candidates(slug: str, available) -> list:
    """同前缀同后缀、版本更高的候选（按版本升序）。拆不出来或没有就返回空。"""
    parsed = parse_model_slug(slug)
    if parsed is None:
        return []
    prefix, version, suffix = parsed
    out = []
    for candidate in available or []:
        other = parse_model_slug(candidate)
        if other is None or other[0] != prefix or other[2] != suffix:
            continue
        if other[1] > version:
            out.append((other[1], str(candidate)))
    return sorted(out)

def follow_plan(configured: dict, available) -> list:
    """`{键: slug}` → `[(键, 旧, 新), …]`（只列要改的）。有歧义就不动那一条。"""
    plan = []
    for key, old in configured.items():
        candidates = follow_candidates(old, available)
        if not candidates:
            continue
        best = candidates[-1][0]
        winners = [name for version, name in candidates if version == best]
        if len(winners) != 1:
            continue                      # 同版本多个候选：分不清，跳过
        plan.append((key, str(old), winners[0]))
    return plan

def extract_model_ids(data) -> list:
    """从 `/v1/models` 的响应里抠出 id（兼容 `{"data": [...]}` 与纯数组两种形状）。"""
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    ids = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif isinstance(item, str):
            ids.append(item)
    return sorted(set(ids))

def _fetch_endpoint_models(base: str, headers: dict, proxy) -> tuple:
    """发一次 GET /v1/models，返回 `(ids, 原因)`；网络错误不外抛。"""
    request = urllib.request.Request(f"{base}/v1/models", headers=headers)  # noqa: S310 - 地址来自该号清单
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({} if proxy is None else {"http": proxy, "https": proxy})
    )
    try:
        with opener.open(request, timeout=MODELS_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return [], f"{base}/v1/models 回了 HTTP {exc.code}（端点可能不提供模型清单）"
    except (OSError, ValueError) as exc:
        return [], f"{base}/v1/models 打不开：{type(exc).__name__}: {exc}"
    ids = extract_model_ids(data)
    if not ids:
        return [], f"{base}/v1/models 没返回模型列表"
    return ids, ""

def endpoint_models(name: str) -> tuple:
    """探该号端点的模型清单：`(ids, 原因)`（第二个非空表示没探到）。"""
    override = os.environ.get("CCA_MODELS_FILE")
    if override:                          # 测试/离线：直接读文件，别碰网络
        try:
            return extract_model_ids(json.loads(Path(override).read_text(encoding="utf-8"))), ""
        except (OSError, json.JSONDecodeError) as exc:
            return [], f"读不了 {override}：{exc}"
    marker, error = read_marker(name)
    if marker is None:
        return [], f"只有 API 号能跟随上游（{error or '订阅号没有自建端点'}）"
    base = str(marker.get("base_url") or "").rstrip("/")
    if not base:
        return [], "清单里没有 base_url"
    headers = {}
    secret = read_api_key(name) or os.environ.get(key_env_name(marker), "").strip()
    if secret:
        headers["authorization"] = f"Bearer {secret}"
        headers["x-api-key"] = secret
    proxy, _ = proxy_settings(marker, read_account_proxy(name))
    return _fetch_endpoint_models(base, headers, proxy)

def require_api_key(name: str, marker: dict) -> str:
    """启动前确认密钥可用：文件 → `$<PROVIDER>_API_KEY`；都没有就带引导报错（不启动 claude）。

    顺带在权限过宽时提示一句（不阻止）：0600 是这台机器上保护密钥的唯一手段。
    真正取用密钥的是 claude 自己（settings.json 的 apiKeyHelper），这里只是**预检**，
    免得让人撞上 `apiKeyHelper failed` 或 401 才回头查。
    """
    secret = read_api_key(name) or os.environ.get(key_env_name(marker), "").strip()
    if not secret:
        die(
            f"{name} 缺少 API key：cca login {name} --key <key>\n"
            f"  或临时用环境变量：{key_env_name(marker)}=<key> cca {name}"
        )
    try:
        mode = key_file(name).stat().st_mode & 0o777
        if mode != PRIVATE_FILE_MODE:
            warn(f"{key_file(name)} 权限是 {oct(mode)}，建议 chmod 600（密钥只靠文件权限保护）")
    except OSError:
        pass
    return secret

def _http_verdict(code: int, detail: str, secret: str) -> tuple:
    """把 HTTP 错误码翻成 `(可达, 说明, 密钥结论)`，避免主流程堆 return。"""
    if secret and code in (401, 403):
        return True, detail, "rejected"
    if code >= HTTP_SERVER_ERROR_MIN:
        # 网关/上游自己出错：跟 key 好不好无关，别报成"密钥有效"（实测 codex 网关偶发 502）。
        return True, detail, "endpoint_error"
    if secret:
        return True, detail, "unknown"      # 4xx（路径/模型/格式）——可达，但别声称密钥有效
    return True, detail, "-"

def probe_endpoint(marker: dict, name: str = "", *, with_key: bool = False) -> tuple:
    """探活端点，返回 `(是否可达, 说明, 密钥是否被接受)`。

    返回的第三项是结论：`"-"`（没带密钥）/ `"valid"`（2xx）/ `"rejected"`（401、403）/
    `"endpoint_error"`（5xx：网关或上游的问题）/ `"unknown"`（其它 4xx：路径/模型不对）/
    `"missing"`（要带密钥但没配）。

    默认**不带凭据**：任何 HTTP 响应都算"端点在"（200/400/401 都可能 —— 有的网关先校验模型
    再校验凭据），探活不需要密钥，也就不必把密钥读进内存。
    `with_key=True` 才真的带上该号的密钥发一次最小请求（max_tokens=1，几乎不花钱），
    用来回答"我这个 key 到底能不能用"：200 = 有效，401/403 = 被拒。代理按该号生效设置，
    与 claude 实际走的路径一致。
    """
    url = marker["base_url"].rstrip("/") + "/v1/messages"
    body = json.dumps({
        "model": str(marker.get("env", {}).get("ANTHROPIC_MODEL") or "probe"),
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if (urllib.parse.urlsplit(url).hostname or "").endswith("anthropic.com"):
        headers.update(anthropic_headers())
    secret = ""
    if with_key:
        secret = read_api_key(name) or os.environ.get(key_env_name(marker), "").strip()
        if not secret:
            return False, f"没有密钥可验证：cca login {name} --key <key>", "missing"
        # helper 走的是两个头都知道；验证请求按官方排障文档用 Bearer，另有 x-api-key 兜底。
        headers["authorization"] = f"Bearer {secret}"
        headers["x-api-key"] = secret
    request = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310 - 地址来自该号清单
    proxy, _ = proxy_settings(marker, read_account_proxy(name))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({} if proxy is None else {"http": proxy, "https": proxy})
    )
    try:
        with opener.open(request, timeout=8) as response:
            return True, f"HTTP {response.status}", "valid" if secret else "-"
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}"
        try:
            snippet = exc.read().decode("utf-8", "replace")[:120].replace("\n", " ")
            if snippet:
                detail += f" {snippet}"
        except OSError:
            pass
        return _http_verdict(exc.code, detail, secret)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)[:120], "-"

def check_api_profile(name: str) -> None:
    """按号操作前的统一预检：清单可解析 + 密钥可用 + 凭据通道已渲染。

    密钥是 claude 通过 settings.json 的 `apiKeyHelper` 自己去读的 —— helper 缺失或指向的文件
    不在，就等于这个号根本没有凭据。这种情况直接**重渲染**（和启动前补 hasCompletedOnboarding
    同一类机械修复），而不是让人撞 401；值不一致（漂移）不在这里处理，由 doctor 报。
    """
    if profile_kind(name) != "api":
        return
    marker, error = read_marker(name)
    if marker is None:
        die(error)
    require_api_key(name, marker)
    helper = str(_settings(name).get("apiKeyHelper") or "")
    target = helper.rsplit(maxsplit=1)[-1] if helper else ""
    ensure_api_config(name)
    if not helper or not Path(target).is_file():
        write_settings_env(name, marker)
        note(f"{name}: 已重渲染 settings.json（apiKeyHelper → {key_file(name)}）")

def models_banner(name: str) -> str:
    """启动横幅里补一句"这台会话实际用什么模型"。

    `/model` 从 2.1.317 起不再显示 "Custom Opus model → 具体是谁"，API 号映射到
    非 Claude 模型后就看不出真实模型了。这里把主/子两个关键槽位打出来，省得每次进
    会话还要 `/status` 或 `cca model <号>` 才能确认。
    """
    data = {}
    settings = profile_dir(name) / "settings.json"
    if settings.is_file():
        try:
            loaded = json.loads(settings.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            data = {}
    env = data.get("env") or {}
    main = str(env.get("ANTHROPIC_MODEL") or data.get("model") or "")
    sub = str(env.get("CLAUDE_CODE_SUBAGENT_MODEL") or "")
    parts = []
    if main:
        parts.append(f"主 {main}")
    if sub and sub != main:
        parts.append(f"子 {sub}")
    return (" · " + " · ".join(parts)) if parts else ""


def identity_banner(name: str) -> str:
    """启动横幅里的一行身份摘要。

    订阅号给邮箱；API 号没有邮箱，就给「api:<预设|主机> · key 状态」——别再拿邮箱当唯一身份，
    否则每次启动都印一句"[未登录]"（使用者实测就是这么被误导的）。
    """
    if profile_kind(name) != "api":
        return read_identity(name).get("email") or "未登录"
    marker = read_marker(name)[0] or {}
    label = api_display(marker.get("provider", ""), marker.get("base_url", ""))
    if read_api_key(name) or os.environ.get(key_env_name(marker), "").strip():
        return f"{label} · key 已配置"
    return f"{label} · key 缺失（cca login {name} --key <key>）"

def runnable_hint(name: str) -> str:
    """这个号现在能不能真跑起来；跑不了就返回一句带**可复制修复命令**的提示，否则空串。

    切号/看号时最烦的失败是"切过去了才发现跑不起来"（缺 key、没登录），所以这里提前说。
    """
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            return error
        if not (read_api_key(name) or os.environ.get(key_env_name(marker), "").strip()):
            return f"{name} 缺 API key，切过去也跑不了：cca login {name} --key <key>"
        return ""
    if not credentials_file(name).is_file():
        return f"{name} 还没登录，切过去也跑不了：cca login {name}"
    return ""

def api_launch_args(name: str, claude_args: list) -> list:
    """API 号启动前补的参数：把第三方端点不认的第一方工具排除掉。"""
    args = list(claude_args)
    if profile_kind(name) != "api":
        return args
    if any(a.split("=")[0] in ("--disallowedTools", "--disallowed-tools") for a in args):
        return args
    # 必须用 `--flag=值` 的**单 token** 形式：`--disallowedTools` 是变参，写成两个 token 会把后面的
    # 位置参数（`cca ds stop <id>` 的 `stop <id>`、`--bg '<任务>'` 的任务）当成工具名吞掉
    # ——实测 `Permission deny rule "stop" matches no known tool`（真机踩到）。
    # DeepSeek 的 Anthropic 端点不接受 Claude Code 的 deferred ToolSearch
    # 块（tool_definition）；只在 DeepSeek 账号禁用它。
    blocked = API_DISALLOWED_TOOLS
    marker, _ = read_marker(name)
    if marker and marker.get("provider") == "deepseek":
        blocked = (*blocked, "ToolSearch", "MCPSearch")
    return [f"--disallowedTools={','.join(blocked)}", *args]
