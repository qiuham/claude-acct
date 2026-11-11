"""python -m cca 的入口。"""

from __future__ import annotations

import sys

from . import config
from .cli import main

config.LAUNCHER_ARGV = [sys.executable, "-m", "cca"]

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
