"""订阅与用量缓存：live_subscriptions / live_usage。"""

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from cca.account.model import read_identity
from cca.account.store import profile_kind, read_marker
from cca.config import (
    SNAPSHOT_PREFER_SECONDS,
    SUBSCRIPTION_LIVE_TTL,
    USAGE_CLAUDE_ENV,
    USAGE_CLAUDE_FLAGS,
    USAGE_LIVE_TIMEOUT,
    USAGE_LIVE_TTL,
    USAGE_WINDOWS,
)
from cca.paths import (
    credentials_file,
    global_config_file,
    subscription_cache_file,
    usage_cache_file,
)


def _parse_ts(value):
    """ISO 时间串 → epoch 秒；缺失或解析不了返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None

def usage_windows(utilization) -> dict:
    """把 `{"five_hour": {"utilization":…, "resets_at":…}}` 规整成渲染用的窗口表。

    快照（`.claude.json` 的 `cachedUsageUtilization.utilization`）与实时接口
    （`/api/oauth/usage`）返回的是**同一种形状**，所以两条路共用这一份解析。
    """
    now = time.time()
    windows = {}
    for key, label in USAGE_WINDOWS:
        w = (utilization or {}).get(key) or {}
        percent = w.get("utilization")
        if percent is None:
            continue
        resets_at = _parse_ts(w.get("resets_at"))
        windows[key] = {
            "label": label,
            "percent": int(percent),
            "resets_at": resets_at,
            "reset_passed": resets_at is not None and resets_at <= now,
        }
    return windows

def read_cache(f: Path) -> dict:
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

def merge_cache(f: Path, updates: dict) -> None:
    """把这次刷新到的号并进缓存文件；别的号原样保留。

    `cca ls`（可能好几个终端同时开）和 watchdog 都在刷这两份缓存：整份读、整份写会让
    后写的人拿自己开头读到的旧条目盖掉别人刚刷的新条目。所以写之前加锁重读，每个号只在
    更新时才替换，再用临时文件 + rename 落盘，读的人不会读到写了一半的文件。
    """
    if not updates:
        return
    try:
        f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with f.with_name(f.name + ".lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = read_cache(f)
            for name, entry in updates.items():
                old = data.get(name)
                if not isinstance(old, dict) \
                        or float(entry.get("fetched_at") or 0) >= float(old.get("fetched_at") or 0):
                    data[name] = entry
            tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.chmod(0o600)
            tmp.replace(f)
    except OSError:
        pass                      # 缓存写不了不影响显示

def read_usage_cache() -> dict:
    return read_cache(usage_cache_file())

def _oauth_block(name: str) -> dict:
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

def local_subscription(name: str) -> dict:
    """不联网能拿到的订阅信息：只有凭据里的套餐类型。

    计费方式和首订日来自平台的非公开接口；为了不自己发请求，这两项去掉了。
    """
    return {
        "type": str(_oauth_block(name).get("subscriptionType") or ""),
        "status": "",
        "source": "local",
    }

def _has_subscription_data(sub: dict) -> bool:
    """响应里有没有可用的订阅信息。

    **不能只看 status**：老 fixture 里 `subscription_status` 可能是 null，只看它会把整条响应丢掉。
    """
    return any(str((sub or {}).get(k) or "") for k in ("type", "status"))

def _subscription_from_profile(body) -> dict:
    """fixture 兼容：还接受旧的 profile 形状，只取套餐类型和状态。"""
    org = (body or {}).get("organization") if isinstance(body, dict) else None
    org = org if isinstance(org, dict) else {}
    return {
        "type": str(org.get("organization_type") or ""),
        "status": str(org.get("subscription_status") or ""),
        "source": "live",
    }

def _subscription_from_fixture(override: str, name: str) -> tuple:
    """从测试/离线文件取一条订阅；读不了或没有该号都带原因返回。"""
    try:
        payload = json.loads(Path(override).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"读不了 {override}：{exc}"
    data = payload.get(name) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}, f"{override} 里没有 {name}"
    sub = _subscription_from_profile(data)
    return (sub, "") if _has_subscription_data(sub) else (sub, "fixture 里没有订阅字段")

def _run_claude(name: str, args: list, *, timeout: float = 20.0, extra_env: dict | None = None):
    """用这个号的配置目录和代理跑一次 claude；返回 (CompletedProcess 或 None, 原因)。"""
    from cca.env import build_env  # noqa: PLC0415 - 避免顶层循环

    exe = shutil.which("claude")
    if not exe:
        return None, "找不到 claude"
    try:
        env = {**build_env(name), **(extra_env or {})}
        return subprocess.run([exe, *args], capture_output=True, text=True,  # noqa: S603
                              timeout=timeout, env=env, check=False), ""
    except subprocess.TimeoutExpired:
        return None, "claude 超时"
    except OSError as exc:
        return None, str(exc)


def fetch_subscription_live(name: str) -> tuple:
    """订阅信息：跑 `claude auth status --json`，不自己请求接口。"""
    override = os.environ.get("CCA_SUBSCRIPTION_LIVE_FILE")   # 测试/离线：直接读文件
    if override:
        return _subscription_from_fixture(override, name)
    result, reason = _run_claude(name, ["auth", "status", "--json"], timeout=15)
    if result is None:
        return {}, reason
    # 没登录（或登录失效）时 claude 退出码是 1，但照样打出 {"loggedIn": false, ...}：先看输出，
    # 不能一见非 0 就当「取不到」——那样会一直沿用几天前缓存里的「有效」（11-12 002/004 就是这样）。
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {}, f"退出码 {result.returncode}" if result.returncode else "输出不是 JSON"
    if not isinstance(data, dict):
        return {}, "输出不是对象"
    sub = {
        "type": str(data.get("subscriptionType") or ""),
        "status": "active" if data.get("loggedIn") else "logged_out",
        "source": "live",
    }
    return (sub, "") if _has_subscription_data(sub) else (sub, "没有订阅字段")


def read_subscription_cache() -> dict:
    return read_cache(subscription_cache_file())

def live_subscriptions(names: list, *, force: bool) -> dict:
    """取订阅信息，和 `live_usage` 同一套：TTL 内用缓存，过期的并行刷新。"""
    cache = read_subscription_cache()
    now = time.time()
    result, need, updates = {}, [], {}
    for name in names:
        entry = cache.get(name)
        fresh = isinstance(entry, dict) and _has_subscription_data(entry) \
            and now - float(entry.get("fetched_at") or 0) < SUBSCRIPTION_LIVE_TTL
        if fresh and not force:
            result[name] = {**entry, "source": "cache"}
        else:
            need.append(name)
    if need:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(need)) as pool:
            futures = {name: pool.submit(fetch_subscription_live, name) for name in need}
            for name, future in futures.items():
                try:
                    sub, _why = future.result()
                except Exception:  # noqa: BLE001 - 线程里出什么事都不许拖垮面板
                    sub = {}
                if _has_subscription_data(sub):
                    result[name] = updates[name] = {**sub, "fetched_at": now}
        merge_cache(subscription_cache_file(), updates)
    return result

def profile_subscription(name: str) -> dict:
    """单个号的订阅：本地打底，实时覆盖；任何失败都退回本地。"""
    sub = local_subscription(name)
    try:
        got = live_subscriptions([name], force=False).get(name)
    except Exception:  # noqa: BLE001 - 订阅只是装饰信息，失败就只用本地数据
        got = None
    if got:
        sub.update(got)
    return sub

def _usage_from_fixture(override: str, name: str) -> tuple:
    """从测试/离线文件取该号的额度窗口；读不了或没有数据都带原因返回。"""
    try:
        payload = json.loads(Path(override).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"读不了 {override}：{exc}"
    data = payload.get(name) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}, f"{override} 里没有 {name}"
    windows = usage_windows(data)
    return (windows, "") if windows else ({}, f"{override} 里没有窗口数据")

_SESSION_USED = re.compile(r"Current session:\s*(\d+)%\s*used")
_WEEK_USED = re.compile(r"Current week \(all models\):\s*(\d+)%\s*used")


def _usage_from_report(text: str, name: str) -> dict:
    """从 `claude -p /usage` 的输出正文里读百分比。

    这比 `.claude.json` 的 cachedUsageUtilization 可靠：实测那次 CLI 跑完快照
    **没有**被重写（fetchedAtMs 还是十几分钟前的），只看快照等于没刷新。
    重置时刻沿用快照里同一个窗口的（同一段窗口不会变）。
    """
    five, week = _SESSION_USED.search(text), _WEEK_USED.search(text)
    if not (five or week):
        return {}
    old = (usage_snapshot(name) or {}).get("windows") or {}
    out = {}
    for key, match in (("five_hour", five), ("seven_day", week)):
        base = dict(old.get(key) or {})
        if match:
            base["percent"] = int(match.group(1))
        if base:
            out[key] = base
    return out


def fetch_usage_live(name: str) -> tuple:
    """实时额度：跑一次 `claude -p /usage` 刷新缓存，再读 `.claude.json` 的结构化数据。

    这条路不自己请求 Anthropic：请求头、版本都由 Claude Code 自己维护。
    """
    override = os.environ.get("CCA_USAGE_LIVE_FILE")   # 测试/离线：直接读文件
    if override:
        return _usage_from_fixture(override, name)
    marker = read_marker(name)[0] if profile_kind(name) == "api" else None
    if marker is not None:
        return {}, "API 号不查额度"
    started = time.time()
    result, reason = _run_claude(name, ["-p", "/usage", "--output-format", "json", *USAGE_CLAUDE_FLAGS],
                                 timeout=USAGE_LIVE_TIMEOUT + 10, extra_env=USAGE_CLAUDE_ENV)
    if result is None:
        return {}, reason
    if result.returncode != 0:
        return {}, f"退出码 {result.returncode}"
    try:
        report = str(json.loads(result.stdout or "{}").get("result") or "")
    except ValueError:
        report = ""
    windows = _usage_from_report(report, name)
    if windows:
        return windows, ""
    # 正文里有时没有额度那几行（CLI 先打印、额度请求还在路上）。这时只认这次调用写进去的快照：
    # 旧快照不能冒充实时值（实测：同号还开着别的会话时，它会把内存里的旧值写回 .claude.json）。
    snapshot = usage_snapshot(name)
    if float(snapshot.get("fetched_at") or 0) >= started and snapshot.get("windows"):
        return snapshot["windows"], ""
    return {}, "CLI 没有返回额度"


def live_usage(names: list, *, force: bool) -> tuple:
    """给这些号取额度：`({号: {"windows", "fetched_at", "source"}}, {号: 失败原因})`。

    缓存 TTL 内直接用缓存（`cca ls` 平时因此仍是瞬时的）；过期的并行刷新一轮，
    失败的号不写缓存 —— 但**把原因带出去**，否则面板会静默端着旧数字（真机踩过：
    005 的实时接口 403，面板却一直显示 4 分钟前的缓存值，和 CLI 里看到的 100% 对不上）。
    """
    cache = read_usage_cache()
    now = time.time()
    result, need, failures, updates = {}, [], {}, {}
    for name in names:
        entry = cache.get(name)
        fresh = isinstance(entry, dict) and isinstance(entry.get("windows"), dict) \
            and now - float(entry.get("fetched_at") or 0) < USAGE_LIVE_TTL
        # 本地快照（claude 自己在会话启动/结束时刷新）可能比缓存还新：这种情况直接用它，
        # 不起 `claude -p /usage`（那个在慢账号上要十几秒，首刷还容易超时）。
        snapshot_at = float((usage_snapshot(name) or {}).get("fetched_at") or 0)
        if not force and snapshot_at > float((entry or {}).get("fetched_at") or 0) \
                and now - snapshot_at < SNAPSHOT_PREFER_SECONDS:
            continue
        if fresh and not force:
            result[name] = {"windows": entry["windows"], "fetched_at": entry["fetched_at"],
                            "source": "cache"}
        else:
            need.append(name)
    if need:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(need)) as pool:
            futures = {name: pool.submit(fetch_usage_live, name) for name in need}
            for name, future in futures.items():
                try:
                    windows, why = future.result()
                except Exception as exc:  # noqa: BLE001 - 线程里出什么事都不许拖垮面板
                    windows, why = {}, f"{type(exc).__name__}"
                if windows:
                    result[name] = {"windows": windows, "fetched_at": now, "source": "live"}
                    updates[name] = {"windows": windows, "fetched_at": now}
                elif why:
                    failures[name] = why
    merge_cache(usage_cache_file(), updates)
    return result, failures

def usage_snapshot(name: str) -> dict:
    """读 `.claude.json` 里缓存的额度快照（`cachedUsageUtilization`）。

    这是 Claude Code 自己刷新的缓存，本工具**不联网、不读凭据**：额度只在 claude 启动或
    结束会话时更新，所以每条都带 `fetched_at`，过期的缓存要能看出来。`resets_at` 已经过去
    的窗口单独标 `reset_passed` —— 那时的百分比是重置前的旧值（实测踩过：缓存里写着
    5 小时 98%，其实窗口刚重置，实际是满血）。
    """
    f = global_config_file(name)
    data = {}
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    cached = data.get("cachedUsageUtilization") or {}
    windows = usage_windows(cached.get("utilization"))
    fetched = cached.get("fetchedAtMs")
    return {
        "profile": name,
        "email": read_identity(name).get("email", ""),
        "fetched_at": (fetched / 1000) if isinstance(fetched, (int, float)) else None,
        "windows": windows,
        "extra_disabled_reason": str(data.get("cachedExtraUsageDisabledReason") or ""),
    }

def weekly_reset_key(windows: dict, now: float | None = None) -> float:
    """在几个都能用的订阅号里挑哪个：**周额度越早重置越先用**。

    快重置的那个号，剩下的周额度到点就作废；晚重置的号留着，额度不会跑。
    周窗口已经过了重置时刻（缓存是旧值，新的一周从下次使用才开始计时）或没有数据的排最后。
    """
    week = (windows or {}).get("seven_day") or {}
    resets = week.get("resets_at")
    now = time.time() if now is None else now
    if not resets or week.get("reset_passed") or resets <= now:
        return float("inf")
    return float(resets)


def usage_roominess(snap: dict):
    """该号还剩多少余量：取最紧的窗口（数值越大越紧），重置已过的窗口按 0 算。

    "最空闲"必须按这个比，不能直接比缓存里的百分比 —— 见 `usage_snapshot` 里的例子。
    没有窗口数据（从没刷新过）返回 None，不参与比较。
    """
    if not snap["windows"]:
        return None
    return max(0 if w["reset_passed"] else w["percent"] for w in snap["windows"].values())
