"""输出与退出：say / warn / note / die。"""

from __future__ import annotations

import sys
from typing import NoReturn


def say(msg: str = "") -> None:
    print(msg)  # noqa: T201 - 这就是 CLI 输出边界

def warn(msg: str) -> None:
    print(f"警告: {msg}", file=sys.stderr)  # noqa: T201 - 这就是 CLI 输出边界

def note(msg: str) -> None:
    """非警告级提醒：只描述状态，不要求行动。"""
    print(f"提示: {msg}", file=sys.stderr)  # noqa: T201 - 这就是 CLI 输出边界

def die(msg: str, code: int = 1) -> NoReturn:
    print(f"错误: {msg}", file=sys.stderr)  # noqa: T201 - 这就是 CLI 输出边界
    raise SystemExit(code)
