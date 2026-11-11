"""把面板插件装进每个号，省得用户自己敲两条 claude 命令。

插件的安装状态是**按号**的（每个号一个配置目录），所以 `cca plugin install --all`
逐个号装。装的是这个仓库自带的 marketplace（`.claude-plugin/marketplace.json`）。

    cca plugin install              # 当前号
    cca plugin install --all        # 所有号
    cca plugin install --profile 002
    cca plugin list                 # 看哪些号装上了
"""

from __future__ import annotations

import subprocess

from cca.config import (
    LEGACY_MARKETPLACES,
    LEGACY_PLUGINS,
    PLUGIN_MARKETPLACE,
    PLUGIN_NAME,
)
from cca.errors import die, say
from cca.paths import profile_dir, repo_root


def _claude() -> str:
    import shutil  # noqa: PLC0415

    exe = shutil.which("claude")
    if not exe:
        die("找不到 claude 可执行文件，装不了插件")
    return exe


def _run(name: str, args: list, cwd: str | None = None) -> tuple[bool, str]:
    """在这个号的配置目录里跑一条 claude 命令；cwd 给项目范围的命令用。"""
    import os  # noqa: PLC0415

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(profile_dir(name))
    result = subprocess.run(  # noqa: S603 - 参数由本模块拼好
        [_claude(), *args], env=env, capture_output=True, text=True, check=False, cwd=cwd,
    )
    ok = result.returncode == 0
    message = (result.stdout or result.stderr or "").strip().splitlines()
    return ok, (message[-1] if message else "")


def _drop_legacy_settings(name: str) -> bool:
    """从这个号的 settings.json 里直接删掉旧名字的插件和 marketplace 键。

    各号的 plugins 目录都链到同一个 ~/.claude/plugins：`claude plugin uninstall`
    只在第一个号上成功，后面的号报 not found，settings.json 里的旧键就一直留着。
    所以照 `cca mcp install` 清旧 MCP 键的做法，直接改文件。返回有没有改。
    """
    import json  # noqa: PLC0415

    path = profile_dir(name) / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    changed = False
    enabled = data.get("enabledPlugins")
    if isinstance(enabled, dict):
        for key in list(enabled):
            plugin, _, market = key.partition("@")
            if plugin in LEGACY_PLUGINS or market in LEGACY_MARKETPLACES:
                del enabled[key]
                changed = True
    markets = data.get("extraKnownMarketplaces")
    if isinstance(markets, dict):
        for market in LEGACY_MARKETPLACES:
            if market in markets:
                del markets[market]
                changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def _drop_legacy(name: str) -> None:
    """旧名字（cca- 前缀）装过的话卸掉，免得同一份插件加载两遍。"""
    for old in LEGACY_PLUGINS:
        _run(name, ["plugin", "uninstall", f"{old}@{PLUGIN_MARKETPLACE}", "--keep-data"])
        for market in LEGACY_MARKETPLACES:
            _run(name, ["plugin", "uninstall", f"{old}@{market}", "--keep-data"])
    for market in LEGACY_MARKETPLACES:
        _run(name, ["plugin", "marketplace", "remove", market])
    # 上面的命令在共用插件目录的号上会报 not found，留下旧键：直接从文件里删
    _drop_legacy_settings(name)


def _installed_scopes(name: str, package: str) -> list[tuple[str, str | None]]:
    """这个号把插件装在了哪些范围：[(范围, 项目目录或 None)]；读不到就只更新 user。"""
    import json  # noqa: PLC0415

    path = profile_dir(name) / "plugins" / "installed_plugins.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("plugins", {}).get(package, [])
    except (OSError, ValueError, AttributeError):
        entries = []
    scopes = [(str(item.get("scope") or "user"), item.get("projectPath")) for item in entries
              if isinstance(item, dict)]
    return scopes or [("user", None)]


def install(name: str) -> bool:
    """给这个号装上 marketplace 和面板插件；已经装过也重跑一次（幂等）。"""
    repo = str(repo_root())
    # 先把旧名字卸掉：同一个仓库路径的旧 marketplace 留着，会挡住新名字的安装
    _drop_legacy(name)
    ok, message = _run(name, ["plugin", "marketplace", "add", repo])
    if not ok:
        say(f"{name}: 加 marketplace 失败 —— {message}")
        return False
    package = f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE}"
    ok, message = _run(name, ["plugin", "install", package])
    if not ok:
        say(f"{name}: 装插件失败 —— {message}")
        return False
    # 已经装过的号，install 不会重拷文件；再跑一次 update，把仓库里的新版拿进来。
    # 每个装过的范围都要更新：只更新 user 范围时，项目范围里的旧版会盖住新版
    for scope, project in _installed_scopes(name, package):
        _run(name, ["plugin", "update", "--scope", scope, package], cwd=project)
    say(f"{name}: 面板已装（{package}）")
    return True


def profiles(args: list) -> list[str]:
    """要装哪些号：--all、--profile=<号>，默认当前号。"""
    import os  # noqa: PLC0415

    if "--all" in args:
        # 和 cca ls 同一套列号方式：不把 .cache、wait 这类不是号的目录算进来
        from cca.account.model import discover_profiles  # noqa: PLC0415

        return discover_profiles()
    for index, item in enumerate(args):
        if item.startswith("--profile="):
            return [item.split("=", 1)[1]]
        if item == "--profile" and index + 1 < len(args):
            return [args[index + 1]]
    folder = os.environ.get("CLAUDE_CONFIG_DIR") or ""
    name = profile_dir_name(folder)
    if not name:
        die("不知道是哪个号：用 --profile <号>，或在某个号的会话里运行")
    return [name]


def profile_dir_name(folder: str) -> str:
    from pathlib import Path  # noqa: PLC0415

    return Path(folder).name if folder else ""


def cmd_plugin(args: list) -> int:
    action = args[0] if args else "install"
    if action in ("-h", "--help"):
        say(__doc__ or "cca plugin install [--all] [--profile 号]")
        return 0
    if action == "install":
        failed = [name for name in profiles(args[1:]) if not install(name)]
        return 1 if failed else 0
    if action == "list":
        import json  # noqa: PLC0415

        for name in profiles(args[1:] or ["--all"]):
            config = profile_dir(name) / "settings.json"
            try:
                data = json.loads(config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                say(f"{name}: 读不到配置")
                continue
            plugins = (data.get("enabledPlugins") or {})
            flag = "已装" if plugins.get(f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE}") else "没装"
            say(f"{name}: {flag}")
        return 0
    return die(f"不认识的用法: {action}（可用 install / list）")
