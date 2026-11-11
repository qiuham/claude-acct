#!/usr/bin/env python3
"""把旧机器上的 001 从 ~/.claude 搬成普通号。

改造后所有号都住在 ~/.claude-accounts/<名字>，`~/.claude` 只放共享资产
（projects / skills / plugins / CLAUDE.md）。这个脚本负责旧机器的一次性搬迁：

    python3 scripts/migrate-001.py --dry-run   # 只看计划
    python3 scripts/migrate-001.py             # 问一次再搬
    python3 scripts/migrate-001.py --yes       # 不问

搬什么：~/.claude 里的账号资料（凭据、settings、daemon、jobs…）和 ~/.claude.json。
留什么：共享资产留在 ~/.claude，新目录里给它们建软链。
顺带：~/.cca/defaults/settings.json 还不存在时，用 001 的 settings.json 生成一份
（新号播种都读它）。
搬之前把要移动的东西复制一份备份到 ~/.claude-accounts/_backup-001-<时间>/。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

HOME = Path.home()
SHARED = HOME / ".claude"
DST = HOME / ".claude-accounts" / "001"
DEFAULTS = HOME / ".cca" / "defaults" / "settings.json"

# ~/.claude 里属于"这个号自己的"条目
MOVES = (".credentials.json", "settings.json", "daemon", "jobs", "session-env",
         "shell-snapshots", "todos", "file-history", "history.jsonl", "statsig")
# 旧机器上不在 ~/.claude 里、但也属于 001 的文件
EXTRA = ("~/.claude.json", "~/.claude.json.backup")
# 留在 ~/.claude 共享的条目
SHARED_LINKS = ("projects", "skills", "plugins", "CLAUDE.md")


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    assume_yes = "--yes" in argv
    if DST.exists():
        print(f"已经搬过了：{DST} 存在")
        return 0
    if not SHARED.is_dir():
        print(f"没有 {SHARED}，这台机器不需要迁移")
        return 0

    moves = [SHARED / item for item in MOVES if (SHARED / item).exists()]
    extras = [Path(p).expanduser() for p in EXTRA if Path(p).expanduser().exists()]
    shared = [SHARED / item for item in SHARED_LINKS if (SHARED / item).exists()]

    print(f"从 {SHARED} 搬到 {DST}")
    print("  搬走: " + (", ".join(str(p) for p in moves + extras) or "(无)"))
    print("  留下(共享): " + (", ".join(str(p) for p in shared) or "(无)"))
    seed_defaults = not DEFAULTS.exists() and (SHARED / "settings.json").is_file()
    if seed_defaults:
        print(f"  生成默认 settings: {DEFAULTS}（从 001 的 settings.json 复制）")
    if dry_run:
        print("--dry-run：以上是计划，什么都没动")
        return 0
    if not assume_yes:
        answer = input("确认搬吗？输入 y 或 yes 继续: ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = HOME / ".claude-accounts" / f"_backup-001-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    for source in moves + extras:
        target = backup / source.name
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)

    if seed_defaults:
        DEFAULTS.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SHARED / "settings.json", DEFAULTS)
        DEFAULTS.chmod(0o600)

    DST.mkdir(parents=True, mode=0o700, exist_ok=True)
    DST.chmod(0o700)
    for source in moves + extras:
        shutil.move(str(source), str(DST / source.name))
    for source in shared:
        link = DST / source.name
        if not link.exists():
            link.symlink_to(source)

    print(f"已搬完：001 现在是普通号（{DST}）")
    print(f"  备份留在 {backup}")
    print(f"  共享资产还在 {SHARED}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
