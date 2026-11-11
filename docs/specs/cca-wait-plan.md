# `cca wait` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一条命令 `cca wait <号> --detach`：等到该号额度窗口重置，把它那时卡在额度上/等输入的会话自动续上。

**Architecture:** 纯 stdlib、单文件扩展。四块：`waiting.json` 状态（幂等 + 补跑）、触发时刻（读 `cca ls --json` 的窗口 `resets_at`）、判定（按 transcript 最后一条记录的事实，不看面板状态名）、执行（`stop` → 会话自己的 cwd → `-p --resume`，逐条串行 + 退避）。`--detach` 只是把 `--run` 丢进 `nohup setsid`。

**Tech Stack:** Python 3.12（stdlib：`json`/`subprocess`/`time`/`os`）+ 现有 `rich` 懒加载；测试 stdlib `unittest` + 假 `claude` 桩，`make test` / `make lint`。

**Spec:** `docs/specs/cca-wait.md`

## Global Constraints

* 状态文件 `<accounts_root>/waiting.json`，权限 **0600**，**临时文件 + `os.replace` 原子写**。
* 触发时刻只来自 `cca ls --json` 的 `usage.windows.<five_hour|seven_day>.resets_at`（epoch 秒）；
  `--window auto` = 5 小时还卡着就用它，否则 7 天，两个都没卡 → 立刻可跑并说明。
* 判定只看两侧事实：transcript **最后一条非空记录**（是额度错误 → 推；不是额度错误且 `claude logs`
  已退出 → 推；不是额度错误且日志在流动 → 跳过、不 stop；拿不到 transcript → 跳过）。
  **面板状态名（`blocked`/`working`/…）不参与判定**（实测全不可信）。
* 执行原语：`cca <号> stop <id>` → `cd <会话自己的 cwd>` → `cca <号> -p "<提示>" --resume <id>`；
  逐条串行；命中 `running as a background session` 记 skipped；命中额度字样按 **5 分钟**退避、
  同一条最多重试到 `not_before + 3600`。
* 不做周期、不做常驻守护、不在别的 cca 命令里偷跑（`cca ls`/`cca doctor` 只显示一行"N 条等待中"）。
* 仓库约定：单文件 stdlib、`make test` + `make lint` 全绿才提交、VERSION 跟批、文档同步、主题分支 + MR。

## Review Focus

1. **拿不到额度快照**（新号没跑过 claude、`.claude.json` 坏了）→ `--window auto` 不能崩，应"立刻可跑"并说明。
2. **老会话的 transcript 里有历史额度字样、但最后一条是正常输出** → 不能被误判为"卡在额度上"
   （判据是**最后一条记录**，不是尾部 contains）。
3. **会话真的在跑**（`-p --resume` 报 `running as a background session`）→ 记 skipped 且不进死循环。
4. **触发时一条候选都没有**（`sessions: null` 且该号没有任何非 `done` 会话）→ 记录收尾成 done 并说明，
   不能永远挂着。
5. **`--at` 的跨天与已过**（23:00 传 `--at 01:40` → 次日 01:40；传已过的时刻 → 次日同一时刻；
   恰好等于现在 → 取下一个）。

---

### Task 1: 状态文件

**Files:** Modify `bin/claude-acct`（新增等待相关常量与函数，放在 `catalog_cache_file()` 那一带）；Test `tests/test_claude_acct.py`

**Interfaces:**
- Produces: `waiting_file() -> Path`；`load_waiting() -> list[dict]`（坏文件当空）；`save_waiting(entries: list) -> None`（0600 + 原子写）；
  `new_waiting_entry(profile, sessions, prompt, window) -> dict`（字段见 spec §4，`state="waiting"`，`results={}`）。

- [ ] **Step 1: 写失败用例**（`test_waiting_roundtrip_and_atomic_write`：保存两条 → 读回一致；文件 0600；目录里不留 `.tmp` 残留；坏 JSON → `load_waiting() == []`）
- [ ] **Step 2: 跑，确认失败** `python3 -m unittest tests.test_claude_acct.CliTest.test_waiting_roundtrip_and_atomic_write` → FAIL（函数不存在）
- [ ] **Step 3: 实现**（`tmp = path.with_suffix(".json.tmp")` → 写 → `os.chmod(tmp, 0o600)` → `os.replace(tmp, path)`）
- [ ] **Step 4: 跑，确认通过**
- [ ] **Step 5: 提交** `feat: cca wait 的状态文件（0600 + 原子写）`

---

### Task 2: 触发时刻

**Files:** Modify `bin/claude-acct`；Test `tests/test_claude_acct.py`

**Interfaces:**
- Consumes: Task 1 无；读 `cca ls --json` 的输出（测试用 `CCA_WAIT_SNAPSHOT_FILE` 注入 fixture）。
- Produces: `wait_not_before(profile: str, window: str = "auto", at: str = "") -> tuple[float, str]`
  返回 `(epoch, 说明)`；`at` 形如 `01:40` 时取"最近的那个未来时刻"（已过或恰好现在 → 次日）。

- [ ] **Step 1: 写失败用例**（三个窗口场景 + `--at` 跨天/已过/恰好现在）
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**（`auto`：`five_hour.reset_passed is False and percent >= 100` 之类判"卡着"；
  快照缺失 → `(time.time(), "没有额度快照，立刻可跑")`）
- [ ] **Step 4: 跑，确认通过**
- [ ] **Step 5: 提交** `feat: cca wait 的触发时刻（窗口重置 / --at）`

---

### Task 3: 判定哪些会话该推

**Files:** Modify `bin/claude-acct`；Test `tests/test_claude_acct.py`

**Interfaces:**
- Produces: `WAIT_LIMIT_MARKERS = ("429", "usage limit", "rate limit", "You've hit your")`；
  `last_transcript_entry(path) -> dict | None`；
  `wait_verdict(profile: str, sid: str) -> tuple[str, str]`（`("push"|"skip", 一行原因)`）；
  `wait_targets(profile: str, only=(), force_all=False) -> list[tuple[str, str, str]]`（sid, verdict, reason）。

- [ ] **Step 1: 写失败用例**（四条分支各一条：最后一条是额度错误 → push；最后一条正常且日志显示已退出 → push；
  最后一条正常且日志在流动 → skip；transcript 缺失 → skip。外加 Review Focus 2 的反例：
  历史里有额度字样、最后一条是正常输出 → **不能** push）
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**（transcript 尾部 256KB 取最后一条可解析的非空行；`claude logs <id>` 复用既有
  `STALE_LOG_MARKERS` 判"已退出"）
- [ ] **Step 4: 跑，确认通过**
- [ ] **Step 5: 提交** `feat: cca wait 的判定（按 transcript 最后一条，不看面板状态）`

---

### Task 4: 执行一条记录

**Files:** Modify `bin/claude-acct`；Test `tests/test_claude_acct.py`

**Interfaces:**
- Consumes: Task 2 的 `wait_not_before()`、Task 3 的 `wait_targets()`。
- Produces: `run_wait_entry(entry: dict, *, dry_run: bool = False, now: float | None = None) -> dict`
  —— 到点才跑；逐条按 Global Constraints 的三步执行；写回 `results[sid] = {"state": "sent"|"skipped"|"failed", "at": …, "note": …}`；
  额度错误写 `retry_after`；`sessions: null` 且零候选 → `state="done"` + note。

- [ ] **Step 1: 写失败用例**（成功 → sent 且第二次 `--run` 不再调用桩（幂等）；额度错误 → `retry_after` 为 5 分钟后；
  `running as a background session` → skipped；`not_before` 已过 → 立刻执行（补跑）；零候选 → done；
  `--dry-run` 不调用桩）
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**（`runner` 通过参数注入，默认 `subprocess.run(["cca", …])`，测试传桩）
- [ ] **Step 4: 跑，确认通过**
- [ ] **Step 5: 提交** `feat: cca wait 的执行与重试（幂等 / 补跑 / 退避）`

---

### Task 5: 命令面

**Files:** Modify `bin/claude-acct`（`SUBCOMMANDS`/dispatch/`usage()`/`COMMAND_USAGE["wait"]`/`cmd_list`+`cmd_doctor` 的"N 条等待中"）；Test `tests/test_claude_acct.py`

**Interfaces:**
- Produces: `cmd_wait(args) -> int`；`wait_loop(*, once: bool, runner=None) -> int`；`detach_wait(entry) -> int`。

- [ ] **Step 1: 写失败用例**（`cca wait 001 --dry-run` 列出会推哪几条与依据；`cca wait --list` 一行一条；
  `--cancel <id>` 之后 `--run` 不再碰它；`--run --once` 跑完退出；`--detach` 断言拼出的命令与日志路径
  （桩住 `subprocess.Popen`）；`cca ls` 输出里有"N 条等待中"）
- [ ] **Step 2: 跑，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑，确认通过**
- [ ] **Step 5: 全量回归** `make test && make lint`
- [ ] **Step 6: 提交** `feat: cca wait 命令面（登记 / --run / --list / --cancel / --detach）`

---

### Task 6: 文档、版本与真机验收

**Files:** `README.md`（"选号与搬运"后新增一节）、`bin/claude-acct`（`--help` 文案）、`docs/specs/cca-wait.md`（§10 验收记录）、`VERSION`

- [ ] **Step 1: README 一节**（一条命令 + `--dry-run` 先看 + 判定四条 + 补跑语义 + 与"预防式：撞墙前用 CC 自带会话定时"的区别）
- [ ] **Step 2: VERSION 0.20.0 + `make test && make lint`**
- [ ] **Step 3: 真机验收（使用者手动触发）**：`cca wait 001 --dry-run` 看它挑出的会话与依据 →
  `cca wait 001 --detach` → 到点看 `waiting.json` 与 `~/.accounts/wait/*.log`；结果写进 spec §10
- [ ] **Step 4: 提交并开 MR**

---

## 自检

**Spec 覆盖**：§3 判定 → 任务 3；§4 状态 → 任务 1；§5 命令面 → 任务 2/5；§6 执行 → 任务 4；§7 非目标 →
全部任务遵守（无周期、无守护、不偷跑）；§8 验证 → 任务 1-5 的用例 + 任务 6 真机；§9 关系 → 任务 6 文档
（旧脚本标注由本功能取代）。

**Review Focus 归属**：1 → 任务 2（快照缺失用例）；2 → 任务 3（历史字样+最后正常输出的反例）；
3 → 任务 4（`running as a background session` 用例）；4 → 任务 4（零候选 → done）；5 → 任务 2（`--at` 三个边界）。

**命名一致性**：`waiting_file` / `load_waiting` / `save_waiting` / `new_waiting_entry` / `wait_not_before` /
`last_transcript_entry` / `wait_verdict` / `wait_targets` / `run_wait_entry` / `wait_loop` / `detach_wait` /
`cmd_wait` 在各任务里同名同签名；测试注入点 `CCA_WAIT_SNAPSHOT_FILE` 只在任务 2 定义、任务 5 复用。
