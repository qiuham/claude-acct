"""测试隔离：先于任何 cca 模块导入，把 HOME 指到临时目录。

cca 的状态都落在 HOME 下：`~/.claude-accounts`（各号目录、watchdog.json、wake.json、jobs）、`~/.cca`（锁、
sideagent 记录）、`~/.claude`（共享对话池）。测试一旦漏了某处 mock，就会写进真实状态：例如往真实
watchdog.json 记一条「会话要求换号」，常驻的 watchdog 会当真，下一次到提醒点就把那条会话搬走。
所以每个 test_*.py 的第一个 cca 导入前先 `import isolate`：整个测试进程的 HOME 是一次性的临时目录，
退出时删掉；同时清掉会把测试带到真实会话上的环境变量，并禁止测试里自动拉起 watchdog。
需要特定目录结构的测试照旧自己建临时目录再 patch（如 AccountsCase）。
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# 这些变量会让代码认出「当前是哪个真实会话 / 哪个真实号」；测试要用时自己 patch.dict
LEAKY_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_JOB_DIR")
# 有些测试（sideagent）要求「在 cca 起的会话里」，需要一个 CLAUDE_CONFIG_DIR：给它临时 HOME 里的一个号，
# 而不是继承真实环境里的那个（以前测试因此往真实 ~/.claude-accounts/ds/sessions 写过假登记）
TEST_PROFILE = "ds"


def _install() -> Path:
    existing = os.environ.get("CCA_TEST_HOME")
    if existing and Path(existing).is_dir():                  # 同一进程里多个测试模块共用一个
        return Path(existing)
    home = Path(tempfile.mkdtemp(prefix="cca-test-home-"))
    os.environ["CCA_TEST_HOME"] = str(home)
    os.environ["HOME"] = str(home)
    for key in LEAKY_ENV:
        os.environ.pop(key, None)
    profile = home / ".claude-accounts" / TEST_PROFILE
    profile.mkdir(parents=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(profile)
    os.environ.setdefault("CCA_WATCHDOG_AUTOSTART", "0")
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    return home


TEST_HOME = _install()
