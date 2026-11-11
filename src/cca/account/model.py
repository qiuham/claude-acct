"""账号数据：名字规则、发现、身份元数据。"""

from __future__ import annotations

import json
import re

from cca.config import PROFILE_RE, RESERVED_NAMES
from cca.errors import die
from cca.paths import accounts_root, credentials_file, global_config_file, profile_dir


def valid_name(name: str) -> bool:
    return bool(re.match(PROFILE_RE, name)) and name not in RESERVED_NAMES

def discover_profiles() -> list:
    """~/.claude-accounts 下每个合法目录就是一个号，没有内置号。"""
    root = accounts_root()
    if not root.is_dir():
        return []
    return [p.name for p in sorted(root.iterdir()) if p.is_dir() and valid_name(p.name)]

def require_profile(name, *, usage: str) -> str:
    """要落到某个号的命令都从这里取号：没写号直接报错，不做隐式选择。"""
    if not name:
        die(f"要写清哪个号。用法: {usage}")
    name = str(name)
    if not profile_dir(name).is_dir():
        die(f"profile {name} 不存在（{profile_dir(name)}）；先运行 cca add {name} 或 cca new {name}")
    return name

def read_identity(name: str) -> dict:
    """从 .claude.json 读账号元数据；文件缺失或损坏时返回空 dict。"""
    f = global_config_file(name)
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    acct = data.get("oauthAccount") or {}
    return {
        "email": acct.get("emailAddress", ""),
        "org_type": acct.get("organizationType", ""),
        # 席位套餐（Team / Enterprise）的席位档：决定 Pro 的多少倍，见 report.TEAM_SEAT_LABELS。
        "seat_tier": acct.get("seatTier", ""),
        # 先取 userRateLimitTier：席位（Team/Enterprise）账号的 organizationRateLimitTier
        # 可能是登录瞬间的旧代号（如 default_raven），而 userRateLimitTier 与上游
        # /api/oauth/profile 返回的 rate_limit_tier 一致；个人号两者或只填后者。
        "rate_tier": acct.get("userRateLimitTier") or acct.get("organizationRateLimitTier") or "",
        # 计费渠道（apple_subscription / stripe_subscription / prepaid）：本地就有，不用联网。
        "billing_type": acct.get("billingType", ""),
        # 首次订阅时间：claude 自己写在 .claude.json 里，取不到接口也能显示。
        "subscription_created_at": acct.get("subscriptionCreatedAt", ""),
        "extra_usage": acct.get("hasExtraUsageEnabled"),
        "display_name": acct.get("displayName", ""),
    }


def has_subscription_login(name: str) -> bool:
    """凭据文件里有没有订阅登录的 accessToken。

    Claude Code 也往同一个 `.credentials.json` 里写 `mcpOAuth`（MCP 服务器授权），
    那种文件只有几十字节、没有 `claudeAiOauth`。只看文件在不在会把它误当成登录。
    """
    f = credentials_file(name)
    if not f.is_file():
        return False
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return False
    return bool(str(oauth.get("accessToken") or "").strip())
