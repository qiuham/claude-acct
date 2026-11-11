"""出口探测：每次启动查一次出口 IP、国家、时区。

用这个号的代理去问 ip-api，返回的是代理出口看到的信息。
查不到不拦启动，只记一条提示 —— 断网时不能把所有人挡在门外。

"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

API = "http://ip-api.com/json/?fields=status,query,countryCode,timezone"

# 语言固定美式英语；时区跟出口 IP 走，探测失败才回落新加坡（见 env.launch_env）。
_available: set[str] | None = None
_PROBE_CACHE: dict = {}      # 代理 → (探测时刻, 结果)
PROBE_TTL = 300.0            # 常驻进程（watchdog）活很久：失败的探测和旧的出口信息都不能永远沿用


def probe_cached(proxy: str | None) -> dict | None:
    """同一个进程里按代理缓存探测结果 PROBE_TTL 秒：搬会话要连着起好几个子进程，不必每次都查。"""
    now = time.monotonic()
    cached = _PROBE_CACHE.get(proxy)
    if cached is None or now - cached[0] >= PROBE_TTL:
        cached = _PROBE_CACHE[proxy] = (now, probe(proxy))
    return cached[1]


def probe(proxy: str | None, *, timeout_s: float = 3.0) -> dict | None:
    """经代理查一次出口信息；失败返回 None。CCA_OFFLINE=1 时不查（测试用）。"""
    if os.environ.get("CCA_OFFLINE") == "1":
        return None
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({} if proxy is None else {"http": proxy, "https": proxy})
    )
    try:
        with opener.open(API, timeout=timeout_s) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(data, dict) or data.get("status") != "success":
        return None
    return {
        "ip": str(data.get("query") or ""),
        "country": str(data.get("countryCode") or "").upper(),
        "timezone": str(data.get("timezone") or ""),
    }


def system_locales() -> set[str]:
    global _available  # noqa: PLW0603 - 进程内只查一次
    if _available is None:
        try:
            out = subprocess.run(["locale", "-a"], capture_output=True, text=True,  # noqa: S607
                                 check=False, timeout=5).stdout
            _available = {line.strip() for line in out.splitlines()}
        except (OSError, subprocess.SubprocessError):
            _available = set()
    return _available


def _norm_locale(name: str) -> str:
    """locale 名字大小写和编码写法不统一：en_US.UTF-8 / en_US.utf8 是同一个。"""
    return str(name or "").strip().lower().replace("utf-8", "utf8")


def default_locale() -> str:
    """固定美式英语：本机装了就用本机写法，没装也照样写标准名。

    用户要求语言不跟环境走；装不装语言包都不影响结果。
    """
    installed = {_norm_locale(name): name for name in system_locales()}
    return installed.get(_norm_locale("en_US.UTF-8"), "en_US.UTF-8")
