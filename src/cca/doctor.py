"""体检：cca doctor。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cca.account.lifecycle import configured_models
from cca.account.model import discover_profiles, has_subscription_login, read_identity
from cca.account.store import (
    _settings,
    _settings_env,
    api_env_vars,
    profile_kind,
    read_account_proxy,
    read_api_key,
    read_marker,
)
from cca.api.client import (
    api_display,
    endpoint_models,
    follow_plan,
    probe_endpoint,
    proxy_settings,
    proxy_unresolvable,
)
from cca.config import (
    PROXY_ENV_VAR,
    PROXY_KEYS,
    SHARED_LINKS,
    SUPPORTED_COUNTRIES_URL,
)
from cca.paths import global_config_file, key_file, profile_dir, shared_dir
from cca.report import _plain, _table, make_console, state_text
from cca.session.push import (
    _run_claude,
    background_summary,
    format_running_detail,
    format_state_counts,
    stale_sessions,
)

# 密钥文件只给当前用户读写。
PRIVATE_FILE_MODE = 0o600


def _check_symlinks(name: str) -> list:
    """返回该 profile 的软链问题列表。"""
    problems = []
    src = shared_dir()
    for item in SHARED_LINKS:
        want = src / item
        got = profile_dir(name) / item
        if not want.exists():
            continue
        if got.is_symlink():
            try:
                if got.resolve() != want.resolve():
                    problems.append(f"{item} 软链指向了 {got.resolve()}，期望 {want}")
            except OSError:
                problems.append(f"{item} 软链已损坏")
        elif got.exists():
            problems.append(f"{item} 是普通文件/目录，已不再共享（可能被 CLI 重写替换）")
        else:
            problems.append(f"{item} 缺失")
    return problems

def _egress():
    proxy, proxy_unset = proxy_settings()
    if proxy is None and proxy_unset:
        return 1, "未配置代理，跳过出网检查（避免暴露本机 IP）"
    curl = shutil.which("curl")
    if not curl:
        return 1, "找不到 curl"
    try:
        r = subprocess.run(  # noqa: S603 - curl 来自 shutil.which("curl")，参数是固定常量
            [curl, "-sS", "-m", "10", "https://ipinfo.io/json"],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return 1, str(exc)
    if r.returncode != 0:
        return 1, (r.stderr or "curl 失败").strip()
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return 1, "返回内容不是 JSON"
    return 0, f"{data.get('ip')} {data.get('city')}, {data.get('country')} ({data.get('org')})"

class _Report:
    """一次体检的失败项与警告项；函数之间只传这一个对象，避免参数越堆越多。"""
    def __init__(self) -> None:
        self.fails: list = []
        self.warns: list = []

def _doctor_options(args: list) -> tuple:
    """解析 doctor 的三个开关；--probe-key 隐含 --online。"""
    probe_key = "--probe-key" in args
    return ("--online" in args or probe_key), probe_key, "--json" in args

def _proxy_label(warns: list) -> tuple:
    """算出代理展示文字，顺带记下解析不了的代理主机。"""
    proxy, proxy_unset = proxy_settings()
    if proxy is None and proxy_unset:
        label = "未配置（启动 claude 前必须配置）"
        warns.append("没有配置代理：cca set proxy --global http://host:port；或给单个号 cca set proxy <号> <url>")
    elif proxy is None:
        inherited = [key for key in PROXY_KEYS if os.environ.get(key)]
        suffix = f"；继承 {'、'.join(sorted(inherited))}" if inherited else ""
        label = f"直连（{PROXY_ENV_VAR}=none{suffix}）"
    else:
        label = proxy
    if proxy is not None:
        host = proxy_unresolvable(proxy)
        if host:
            warns.append(
                f"代理主机 {host} 解析不了；不需要代理用 {PROXY_ENV_VAR}=none，换地址用 {PROXY_ENV_VAR}=<url>"
            )
    return label, proxy, proxy_unset

def _check_api_credentials(n: str, marker: dict, report: _Report) -> str:
    """检查 API 号的密钥文件、apiKeyHelper、凭据冲突与 env 漂移；返回密钥明文。"""
    helper = str(_settings(n).get("apiKeyHelper") or "")
    key_path = helper.rsplit(maxsplit=1)[-1] if helper else ""
    secret = read_api_key(n)
    if not secret:
        report.fails.append(f"{n}: 缺 API key，运行 cca login {n} --key <key>")
    elif not key_path or not Path(key_path).is_file():
        report.fails.append(
            f"{n}: settings.json 里的 apiKeyHelper 不可用（{helper or '未渲染'}）；"
            f"运行 cca sync {n} 重新渲染"
        )
    else:
        mode = key_file(n).stat().st_mode & 0o777
        if mode != PRIVATE_FILE_MODE:
            report.warns.append(f"{n}: {key_file(n)} 权限 {oct(mode)}，运行 chmod 600 {key_file(n)}")
    if has_subscription_login(n):
        report.warns.append(
            f"{n}: 同时存在订阅登录（.credentials.json）与网关凭据（apiKeyHelper）——"
            "冲突状态，建议二选一"
        )
    drift = {k: v for k, v in api_env_vars(marker).items() if _settings_env(n).get(k) != v}
    if drift:
        report.warns.append(
            f"{n}: settings.json 的 env 与清单不一致（{', '.join(sorted(drift))}），"
            f"运行 cca sync {n}"
        )
    return secret

def _probe_api_endpoint(n: str, marker: dict, opts: tuple, console, report: _Report) -> None:
    """--online 时探一次端点，并把密钥结论记进 report。"""
    _online, probe_key = opts
    ok, detail, key_state = probe_endpoint(marker, n, with_key=probe_key)
    if key_state == "missing":
        # 缺 key 上面已经 FAIL 报过，这里不重复、也不谎报"端点连不上"。
        if console:
            console.print(_plain(f"  {n}: 跳过密钥验证（{detail}）", style="yellow"))
    elif not ok:
        report.warns.append(f"{n}: 端点 {marker['base_url']} 连不上（{detail}）")
    elif key_state == "rejected":
        report.fails.append(f"{n}: 密钥被端点拒绝（{detail}）→ cca login {n} --key <key>")
    elif key_state == "endpoint_error":
        report.fails.append(f"{n}: 端点/上游报错（{detail}）—— 这不是 key 的问题；"
                     "看网关日志或稍后重试")
    elif key_state == "unknown":
        report.warns.append(f"{n}: 端点可达但请求被拒（{detail}）—— 检查端点路径与模型名"
                     f"（当前模型: {marker.get('env', {}).get('ANTHROPIC_MODEL') or '默认'}）")
    else:
        label = f"端点可达（{detail}）" if not probe_key else f"端点可达且密钥有效（{detail}）"
        if console:
            console.print(_plain(f"  {n}: {label}", style="dim"))
        # 顺带看一眼"端点上的同档位模型是不是有更新的"（只提示，不静默改）。
        ids, _why = endpoint_models(n)
        if ids:
            stale = follow_plan(configured_models(n), ids)
            if stale:
                pairs = "、".join(f"{old} → {new}" for _key, old, new in stale[:3])
                report.warns.append(f"{n}: 端点上已有更新的同档模型（{pairs}）→ cca model {n} --follow")

def _check_api_network(n: str, marker: dict, opts: tuple, console, report: _Report):
    """检查端点 DNS 与代理设置；--online 时再探活。返回该号生效的代理。"""
    online, _probe_key = opts
    host = urllib.parse.urlsplit(marker["base_url"]).hostname
    if host:
        try:
            socket.getaddrinfo(host, None)
        except OSError:
            report.warns.append(f"{n}: 端点主机 {host} 解析不了（DNS）")
    proxy, _builtin = proxy_settings(marker, read_account_proxy(n))
    if proxy is None and not any(os.environ.get(k) for k in PROXY_KEYS):
        current = marker["proxy"]
        advice = "cca " + "set proxy " + n + " <地址>"
        report.warns.append(
            f"{n}: 没有代理（清单里是 {current}，shell 里也没有）—— "
            "自动更新/遥测这类非端点流量会直连；实测 downloads.claude.ai 直连不通会报 "
            f"Auto-update failed。建议 {advice}（端点仍走 NO_PROXY 直连）"
        )
    if online:
        _probe_api_endpoint(n, marker, opts, console, report)
    return proxy

def _print_api_profile(n: str, marker: dict, secret: str, console) -> None:
    """一行展示 API 号身份（没有 Rich 时跳过）。"""
    if console:
        console.print(
            _plain(f"  {n}: ", style="dim")
            + _plain(f"{api_display(marker.get('provider', ''), marker.get('base_url', ''))} ", style="bold")
            + _plain(f"{marker['base_url']} ", style="dim")
            + _plain(f"key {'已配置' if secret else '缺失'}")
        )

def _check_oauth_files(n: str, report: _Report) -> None:
    """订阅号的本地检查：凭据文件、共享软链、账号邮箱。"""
    if has_subscription_login(n):
        report.warns.extend(f"{n}: {p}" for p in _check_symlinks(n))
    else:
        report.warns.append(f"{n}: 尚未登录（凭据里没有订阅 token），运行 cca login {n}")
    if not read_identity(n).get("email"):
        report.warns.append(f"{n}: 读不到账号邮箱（{global_config_file(n)} 缺失或无 oauthAccount）")

def _ancestor_pids() -> set:
    """当前进程和它的祖先。强杀要跳过它们，否则 doctor 会把这个会话自己杀掉。"""
    pids = set()
    pid = os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return pids


def _session_label(n: str, pid: int) -> str:
    """从登记表找这个 pid 对应的会话：短编号 + 工作目录。找不到就报 pid。"""
    folder = profile_dir(n) / "sessions"
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or int(data.get("pid") or 0) != pid:
                continue
            sid = str(data.get("sessionId") or "")[:8]
            cwd = str(data.get("cwd") or "")
            return f"{sid}（{cwd}）" if sid else f"pid {pid}（{cwd}）"
    return f"pid {pid}"


def _kill_pids(pids: list) -> None:
    """先 SIGTERM；3 秒后还在的 SIGKILL。"""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not any(Path(f"/proc/{pid}").exists() for pid in pids):
            return
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue


def _check_session_tz(names: list, report: _Report, *, interactive: bool = True) -> None:
    """时区不对的会话，问一句后强杀；下次启动就会用出口时区。

    claude 的后台守护进程把第一次拉起时的 TZ 记住，客户端后来传的 TZ 不采纳，
    所以光改代码不够，必须把进程杀掉重建。出口探测失败时不动手：探测失败不等于时区不对。
    """
    from cca.env import expected_session_tz, running_session_tz  # noqa: PLC0415 - 只在真有会话在跑时才探测出口

    skip = _ancestor_pids()
    planned = []                      # (号, pid, TZ, 出口时区)
    for n in names:
        processes = running_session_tz(n)
        if not processes:
            continue
        expected = expected_session_tz(n)
        if expected is None:
            report.warns.append(f"{n}: 有会话在跑，但出口探测失败，没法判断时区；先查代理")
            continue
        planned.extend(
            (n, pid, tz, expected) for pid, tz in processes
            if tz != expected and pid not in skip
        )
    if not planned:
        return

    # 先列出要杀的是哪几条：报告里能对上号，出问题也知道动了什么。
    for n, pid, tz, expected in planned:
        report.warns.append(
            f"{n}: 会话 {_session_label(n, pid)} 时区不对（TZ={tz}，出口 {expected}）")
    if not interactive or not sys.stdin.isatty():
        report.warns.append("跳过强杀：这里没法交互确认；在终端里跑 cca doctor 会询问")
        return
    answer = input(f"要强杀上面这些会话吗？（会中断它们）输入 y 或 yes 确认: ").strip().lower()
    if answer not in ("y", "yes"):
        report.warns.append("已取消，没有杀任何会话")
        return
    _kill_pids([pid for _n, pid, _tz, _expected in planned])
    report.warns.append(f"已强杀 {len(planned)} 个会话；重开会用出口时区")


def _collect_oauth_background(n: str, running: list, hanging: list, report: _Report) -> None:
    """把订阅号的后台会话归到"在跑"或"挂起"，并记下已退出的残留。"""
    summary = background_summary(n)
    if summary["running"]:
        running.append(format_running_detail(n, summary))
    elif summary["total"]:
        hanging.append(f"{n}({format_state_counts(summary['counts'])})")
    report.warns.extend(
        f"{n}: {s['id']} 已退出但仍在后台会话清单里（cca {n} rm {s['id']} 可清掉）"
        for s in stale_sessions(n)
    )

def _print_background_summary(running: list, api_running: list, hanging: list,
                              console, report: _Report) -> None:
    """汇总各号的后台会话状态。"""
    if len(running) > 1:
        report.warns.append("检测到多个号同时有后台会话在跑: " + ", ".join(running) + " —— 建议串行使用")
    elif running and console:
        console.print(_plain("后台会话: ", style="dim") + _plain(running[0]))
    if api_running and console:
        console.print(_plain("API 号在跑（不同供应商、按量计费，不参与串行约定）: "
                             + ", ".join(api_running), style="dim"))
    if hanging and console:
        console.print(_plain("挂起的后台会话（未在跑）: " + ", ".join(hanging), style="dim"))

def _print_online_section(names: list, console, report: _Report) -> None:
    """--online 的登录状态与出口检查。"""
    console.print(_plain(""))
    console.print(_plain("--online 检查:", style="bold"))
    for n in names:
        r = _run_claude(["auth", "status", "--json"], n, capture=True)
        try:
            ok = json.loads(r.stdout).get("loggedIn")
        except (json.JSONDecodeError, AttributeError):
            ok = None
        console.print(_plain("  ") + _plain(f"{n}: ", style="bold") + _plain(f"loggedIn={ok}"))
        if ok is False:
            report.warns.append(f"{n}: auth status 报告未登录")
    code, out = _egress()
    if code == 0:
        console.print(_plain("  出口: ", style="dim") + _plain(out))
        console.print(_plain("  请对照支持地区列表自查: ", style="dim") + _plain(SUPPORTED_COUNTRIES_URL))
    else:
        report.warns.append(f"出口检查失败: {out}")

def _print_doctor_json(names: list, proxy_info: tuple,
                       running: list, hanging: list, report: _Report) -> None:
    """--json 模式的结构化输出。"""
    proxy, proxy_unset = proxy_info
    print(  # noqa: T201 - doctor 的机器可读输出边界
        json.dumps(
            {
                "profiles": names,
                "proxy": {"url": proxy, "unset": proxy_unset},
                "running": running,
                "hanging": hanging,
                "warns": report.warns,
                "fails": report.fails,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

def _print_doctor_table(console, report: _Report) -> None:
    """人类可读的结果表。"""
    console.print(_plain(""))
    table = _table(console, min_width=100)
    table.add_column("级别", width=5, no_wrap=True)
    table.add_column("检查项", ratio=1, overflow="fold")
    for w in report.warns:
        table.add_row(state_text("WARN"), _plain(w))
    for f in report.fails:
        table.add_row(state_text("FAIL"), _plain(f))
    if not report.warns and not report.fails:
        table.add_row(state_text("OK"), _plain("未发现问题"))
    console.print(table)

def cmd_doctor(args: list) -> int:
    online, probe_key, as_json = _doctor_options(args)
    opts = (online, probe_key)
    report = _Report()
    names = discover_profiles()
    # JSON 模式不需要 Rich：doctor 是排障命令，应该在没有 Rich 的环境里也能出结构化结果。
    console = None if as_json else make_console()
    if console:
        console.print(_plain("发现 ", style="dim") + _plain(f"{len(names)} 个 profile: {', '.join(names) or '(无)'}"))

    proxy_label, proxy, proxy_unset = _proxy_label(report.warns)
    if console:
        console.print(_plain("代理: ", style="dim") + _plain(proxy_label))

    running, hanging, api_running = [], [], []
    for n in names:
        if profile_kind(n) == "api":
            marker, error = read_marker(n)
            if marker is None:
                report.fails.append(f"{n}: {error}")
                continue
            secret = _check_api_credentials(n, marker, report)
            proxy = _check_api_network(n, marker, opts, console, report)
            summary = background_summary(n)
            if summary["running"]:
                api_running.append(format_running_detail(n, summary))
            _print_api_profile(n, marker, secret, console)
            continue
        _check_oauth_files(n, report)
        _collect_oauth_background(n, running, hanging, report)

    _check_session_tz(names, report, interactive=not as_json)
    _print_background_summary(running, api_running, hanging, console, report)
    if online and console:
        _print_online_section(names, console, report)

    if as_json:
        _print_doctor_json(names, (proxy, proxy_unset), running, hanging, report)
        return 1 if report.fails else 0

    _print_doctor_table(console, report)
    return 1 if report.fails else 0
