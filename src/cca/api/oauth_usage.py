"""订阅号直接请求 `/api/oauth/usage`：额度窗口 + 重置卡（cedar_ember）。

请求和 Claude Code 自己的 /usage 一样：同一个地址、同一组请求头、同一个 UA（版本读本机 claude）。
令牌只在本进程内存里用：不写缓存、不打印、不进失败原因。**这里不刷新令牌**：刷新会换掉刷新令牌，
把这个号上正在跑的会话踢下线。令牌过期就算这次没取到，等这个号上开一次 Claude Code 会话、令牌续上。
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from cca.account.store import _cli_version, read_account_proxy
from cca.api.client import proxy_settings
from cca.api.identity import cli_version
from cca.config import (
    OAUTH_BETA,
    OAUTH_PROFILE_SCOPE,
    OAUTH_RESETS_QUERY,
    OAUTH_USAGE_QUERY,
    OAUTH_USAGE_TIMEOUT,
    OAUTH_USAGE_URL,
)
from cca.paths import credentials_file

# 这两种状态码 = 令牌过期或失效：这次算没取到，令牌由 Claude Code 自己刷新
AUTH_FAILED = (401, 403)
RATE_LIMITED = 429
_GRANT_ID = re.compile(r"^[a-z0-9_-]{1,40}$")
# 缓存里只留这些字段（不留卡的编号）
CARD_FIELDS = ("label", "type", "resets_left", "resets_total", "starts_at", "ends_at", "clears")


def oauth_block(name: str) -> dict:
    """读凭据里的 `claudeAiOauth` 块。只在进程内用：不打印、不落盘。"""
    f = credentials_file(name)
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else {}


def has_profile_scope(oauth: dict) -> bool:
    """令牌带不带 `user:profile`：没有就不查重置卡。"""
    scopes = oauth.get("scopes")
    return isinstance(scopes, list) and OAUTH_PROFILE_SCOPE in scopes


_VERSION_LOCK = threading.Lock()


@functools.lru_cache(maxsize=1)
def read_claude_version() -> str:
    return cli_version() or _cli_version()


def claude_version() -> str:
    """本机 claude 的版本号，整个进程只取一次（先看软链目录名，取不到再跑一次 `claude --version`）。

    各号并发查额度：加锁，免得几个线程同时各起一次 `claude --version`。
    """
    with _VERSION_LOCK:
        return read_claude_version()


def request_headers(token: str, version: str) -> dict:
    """和 Claude Code 请求 /api/oauth/usage 时一样的请求头。"""
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-app": "cli",
        "User-Agent": f"claude-cli/{version} (external, cli)",
    }


def _from_fixture(path: str, name: str) -> dict:
    """测试/离线：`CCA_OAUTH_USAGE_FILE` 是 `{号: 返回体}`；返回体写 `{"_http_status": 401}` 表示请求失败。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"body": None, "status": None, "reason": "网络失败（桩文件读不了）"}
    body = payload.get(name) if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        return {"body": None, "status": None, "reason": "网络失败（桩文件里没有这个号）"}
    status = body.get("_http_status")
    if status:
        return {"body": None, "status": int(status), "reason": _status_reason(int(status))}
    return {"body": body, "status": 200, "reason": ""}


def _status_reason(code: int) -> str:
    if code in AUTH_FAILED:
        return f"令牌过期或失效（HTTP {code}）"
    return f"HTTP {code} 限流" if code == RATE_LIMITED else f"HTTP {code}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 3xx：urllib 跟跳转时会把 Authorization 原样带到新主机，令牌就漏给第三方了。

    返回 None = 不跳，3xx 变成 HTTPError，按失败处理（这次没取到）。
    """

    def redirect_request(self, *_args, **_kwargs):
        return None


def _open(request, proxy):
    """真的发请求（测试里替换掉这一个函数）。代理规则同 env.build_env：None = 沿用环境里的代理变量。"""
    handlers = [_NoRedirect()]
    if proxy is not None:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(request, timeout=OAUTH_USAGE_TIMEOUT) as response:
        return response.read()


def fetch_oauth_usage(name: str, *, with_resets: bool) -> dict:  # noqa: PLR0911 - 每种失败各回一句原因
    """请求一次 /api/oauth/usage：`{"body": dict 或 None, "status": 状态码或 None, "reason": 失败原因}`。

    `with_resets` 为真时带 cedar_ember=1，返回体里多一个重置卡对象。
    令牌已经过期就不发请求，直接按 401 返回（这次算没取到）。
    """
    oauth = oauth_block(name)
    token = str(oauth.get("accessToken") or "")
    if not token:
        return {"body": None, "status": None, "reason": "凭据里没有令牌"}
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 <= time.time():
        return {"body": None, "status": 401, "reason": "令牌过期"}
    override = os.environ.get("CCA_OAUTH_USAGE_FILE")   # 测试/离线：不联网，读文件
    if override:
        return _from_fixture(override, name)
    # 订阅号没配代理（也没明说直连）就不发请求：不能带着令牌从本机出口直连（同 env.session_env 的规则）
    proxy, unset = proxy_settings(None, read_account_proxy(name))
    if proxy is None and unset:
        return {"body": None, "status": None, "reason": "没有配置代理"}
    version = claude_version()
    if not version:
        return {"body": None, "status": None, "reason": "取不到本机 claude 的版本号"}
    query = OAUTH_RESETS_QUERY if with_resets else OAUTH_USAGE_QUERY
    request = urllib.request.Request(  # noqa: S310 - 地址是常量 https
        f"{OAUTH_USAGE_URL}?{query}", headers=request_headers(token, version))
    try:
        raw = _open(request, proxy)
    except urllib.error.HTTPError as exc:
        return {"body": None, "status": exc.code, "reason": _status_reason(exc.code)}
    except (OSError, ValueError) as exc:      # 只报类型名：异常文本里可能带请求细节
        return {"body": None, "status": None, "reason": f"网络失败（{type(exc).__name__}）"}
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return {"body": None, "status": 200, "reason": "返回体解析不了"}
    return {"body": body, "status": 200, "reason": ""}


def _ts(value):
    """ISO 时间串或 epoch（秒/毫秒）→ epoch 秒；没有或解析不了返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000 if value > 1e12 else float(value)  # noqa: PLR2004 - 毫秒/秒分界
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usable_grant(grant) -> bool:
    """不看时间的检查：编号像样、清哪些窗口写了、还剩张数、没停用。时间在显示时再判（缓存会放 1 小时）。"""
    return isinstance(grant, dict) and bool(_GRANT_ID.match(str(grant.get("id") or ""))) \
        and isinstance(grant.get("clears"), list) and bool(grant["clears"]) \
        and _int(grant.get("resets_left")) > 0 and not grant.get("paused")


def parse_resets(body: dict, fetched_at: float) -> dict:
    """返回体 → 缓存条目：`{available, eligible, ineligible_reason, cards, fetched_at}`。

    `cedar_ember` 缺失或为 null = 这个号没有重置卡（available 为假）。只留缓存要的字段，不留卡的编号。
    """
    cedar = body.get("cedar_ember") if isinstance(body, dict) else None
    if not isinstance(cedar, dict):
        return {"available": False, "eligible": None, "ineligible_reason": "", "cards": [],
                "fetched_at": fetched_at}
    cards = []
    for grant in cedar.get("grants") or []:
        if not _usable_grant(grant):
            continue
        cards.append({
            "label": str(grant.get("label") or ""),
            "type": str(grant.get("type") or ""),
            "resets_left": _int(grant.get("resets_left")),
            "resets_total": _int(grant.get("resets_total")),
            "starts_at": _ts(grant.get("starts_at")),
            "ends_at": _ts(grant.get("ends_at")),
            "clears": [str(c) for c in grant["clears"]],
        })
    eligible = cedar.get("eligible")
    return {
        "available": True,
        "eligible": bool(eligible) if eligible is not None else None,
        "ineligible_reason": str(cedar.get("ineligible_reason") or ""),
        "cards": cards,
        "fetched_at": fetched_at,
    }


def card_active(card: dict, now: float) -> bool:
    """这张卡现在能不能用：已经生效（没写生效时间算生效）、还没到期（没写到期时间算不过期）。"""
    starts, ends = card.get("starts_at"), card.get("ends_at")
    return (starts is None or now >= float(starts)) and (ends is None or now < float(ends)) \
        and _int(card.get("resets_left")) > 0


def active_cards(entry: dict, now: float | None = None) -> list:
    now = time.time() if now is None else now
    return [c for c in (entry or {}).get("cards") or [] if isinstance(c, dict) and card_active(c, now)]
