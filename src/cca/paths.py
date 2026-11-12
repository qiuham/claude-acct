"""路径规则：配置目录、凭据文件、全局配置、各类缓存与状态文件。"""

from __future__ import annotations

import sys
from pathlib import Path

from cca.config import (
    ACCOUNT_PROXY_FILE,
    KEY_FILE,
    LAUNCHER_ARGV,
    PROFILE_MARKER,
)


def repo_root() -> Path:
    """仓库根目录（VERSION 在这里）：src/cca/paths.py 往上三层。"""
    return Path(__file__).resolve().parents[2]


def launcher_argv() -> list[str]:
    """重新调起自己的命令前缀。启动器（bin/claude-acct / python -m cca）会填好。"""
    if LAUNCHER_ARGV:
        return list(LAUNCHER_ARGV)
    return [str(Path(sys.argv[0]).resolve())]


def home() -> Path:
    return Path("~").expanduser()

def shared_dir() -> Path:
    """共享资产目录：projects / skills / plugins / CLAUDE.md。

    它不属于任何号；每个号的这几个条目都软链到这里。
    """
    return home() / ".claude"

def state_dir() -> Path:
    """cca 自己的状态目录：sideagent 名单、wake、默认设置。"""
    return home() / ".cca"

def default_settings_file() -> Path:
    """新号播种用的默认 settings.json：不属于任何账号，可以直接编辑。"""
    return state_dir() / "defaults" / "settings.json"

def moves_file() -> Path:
    """换号记录（一行一条 {at, session, from, to}）：cca agents 用它标「← 旧号」。"""
    return state_dir() / "moves.jsonl"

def stopped_bg_file() -> Path:
    """`cca stop-bg --all` 记下的会话清单，`cca resume-bg` 照它续（换 workspace 用）。"""
    return state_dir() / "stopped-bg.json"

def accounts_root() -> Path:
    return home() / ".claude-accounts"

def profile_dir(name: str) -> Path:
    """号的配置目录。所有号一视同仁，都住在 ~/.claude-accounts 下。"""
    return accounts_root() / name

def global_config_file(name: str) -> Path:
    """这个号的全局 .claude.json。"""
    return profile_dir(name) / ".claude.json"

def credentials_file(name: str) -> Path:
    return profile_dir(name) / ".credentials.json"

def marker_file(name: str) -> Path:
    """API 号的清单文件（工具自有的真相来源）。订阅号没有这个文件。"""
    return profile_dir(name) / PROFILE_MARKER

def key_file(name: str) -> Path:
    """API 号的密钥文件（0600，一行裸 key）。"""
    return profile_dir(name) / KEY_FILE

def account_proxy_file(name: str) -> Path:
    """订阅号的代理文件（0600，一行地址）；API 号存在清单里。"""
    return profile_dir(name) / ACCOUNT_PROXY_FILE

def usage_cache_file() -> Path:
    return accounts_root() / "_usage-cache.json"

def subscription_cache_file() -> Path:
    return accounts_root() / "_subscription-cache.json"

def waiting_file() -> Path:
    return accounts_root() / "waiting.json"

def transcript_cache_file() -> Path:
    """会话记录扫描结果的缓存（标题、最后提问、起始目录、最后用的模型），按文件大小和修改时间失效。"""
    return accounts_root() / ".cache" / "transcripts.json"

def catalog_cache_file() -> Path:
    return accounts_root() / ".cache" / "openrouter-models.json"

def shared_pool_root() -> Path:
    """共享会话池：所有号的 `projects/` 都指向它。"""
    return shared_dir() / "projects"
