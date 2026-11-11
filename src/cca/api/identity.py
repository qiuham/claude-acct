"""请求头：让 cca 调用 Anthropic 接口时的身份和本机 Claude Code 一致。

版本号从 claude 可执行文件读：~/.local/bin/claude 一般软链到
~/.local/share/claude/versions/<版本>，取那个目录名。

"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def cli_version() -> str:
    """本机 Claude Code 的版本号；读不到返回空串。"""
    path = shutil.which("claude")
    if not path:
        return ""
    try:
        name = Path(path).resolve().name
    except OSError:
        return ""
    return name if VERSION_RE.match(name) else ""


def anthropic_headers(token: str = "") -> dict:
    """api.anthropic.com 的请求头。"""
    headers = {
        "accept": "application/json",
        "anthropic-version": "2023-06-01",
        "x-app": "cli",
    }
    version = cli_version()
    if version:
        headers["user-agent"] = f"claude-cli/{version} (external, sdk-cli)"
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers
