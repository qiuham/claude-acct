"""测试共用：把 sideagent 目录换到临时目录，按新格式（一个 sideagent 一个文件夹 + 主会话名单）摆数据。

SIDEAGENT_DIR 换成 <临时目录>/sideagents，名单目录跟着是 <临时目录>/sessions：
绝不碰真实的 ~/.cca/sideagents 和 ~/.cca/sessions。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from cca.session import roster, sideagent


def use_temp_root(case) -> Path:
    """给一个 TestCase 换上临时的 sideagents 目录（自动清理）；返回这个目录。"""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name) / "sideagents"
    root.mkdir()
    patcher = mock.patch.object(sideagent, "SIDEAGENT_DIR", root)
    patcher.start()
    case.addCleanup(patcher.stop)
    # 迁移做没做过按目录记在进程里；临时目录每次都是新的，不用清
    return root


def write_state(name: str, **fields) -> Path:
    """sideagents/<名字>/state.json。"""
    sideagent._write_state(name, fields)  # noqa: SLF001
    return sideagent._state_path(name)  # noqa: SLF001


def write_roster(session: str, agents: dict, **top) -> Path:
    """sessions/<会话>.json：agents 是 {名字: 种类} 或 {名字: 条目}。"""
    path = roster.roster_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {name: ({"kind": value, "since": 1.0} if isinstance(value, str) else value)
            for name, value in agents.items()}
    path.write_text(json.dumps({"session": session, **top, "agents": body}, ensure_ascii=False),
                    encoding="utf-8")
    return path
