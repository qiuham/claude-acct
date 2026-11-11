# 账号显式化 实施计划

> **给执行者：** 每个任务都是一次可提交的小步，按顺序做；每步都先写/改用例、看它失败、再实现。
> 步骤用 `- [ ]` 勾选跟踪。关联 spec：`docs/specs/explicit-profile.md`。

**目标：** 删掉 cca 的隐式选号（默认号 / workspace 绑定 / `$CLAUDE_ACCT`），把 `carry` 提升为独立命令。

**做法：** 先加新能力（`carry` 独立、按号命令必须写号、只读命令汇总所有号），再删旧命令
（`switch` / `bind` / `unbind` / `resume`），让每个任务结束时 `make test` 全绿；文档与版本最后收口。

**技术栈：** 单文件 Python 3.12（只用 stdlib）+ `rich`（懒加载）；测试是 stdlib `unittest` 加一个假
`claude` 桩，入口 `make test` / `make lint`（`COLUMNS=200`、临时 `HOME`、`CCA_PROXY=http://localhost:7897`）。

---

## 任务 1：`parse_flags` 支持可重复的取值参数

`cca carry --only=<前缀>` 需要能写多次，而现在的 `parse_flags` 用字典存取值参数，重复会被覆盖。

**文件：** 改 `bin/claude-acct`（`parse_flags`，约 2447 行）

- [ ] **步骤 1：先写用例**

加到 `tests/test_claude_acct.py` 的 `TestCli` 类里（挨着其它 `_run_python` 用例）：

```python
    def test_parse_flags_collects_repeatable_values(self):
        out = self._run_python(
            """
            import importlib.machinery, importlib.util, sys
            loader = importlib.machinery.SourceFileLoader("cca_mod", sys.argv[1])
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cca_mod", loader))
            loader.exec_module(module)
            rest, flags = module.parse_flags(
                ["a", "b", "--only=x", "--only=y", "--all"],
                bool_flags=("--all",), multi_flags=("--only",), usage="t",
            )
            print(rest, flags)
            """
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("['a', 'b']", out.stdout)
        self.assertIn("'--only': ['x', 'y']", out.stdout)
        self.assertIn("'--all': True", out.stdout)
```

- [ ] **步骤 2：跑一遍，确认失败**

```bash
python3 -m unittest tests.test_claude_acct.TestCli.test_parse_flags_collects_repeatable_values 2>&1 | tail -3
```

预期：`TypeError: parse_flags() got an unexpected keyword argument 'multi_flags'`。

- [ ] **步骤 3：实现**

把 `bin/claude-acct` 的 `parse_flags` 换成（只加 `multi_flags` 一项，其余不动）：

```python
def parse_flags(args: list, bool_flags: tuple, value_flags: tuple = (), usage: str = "",
                multi_flags: tuple = ()) -> tuple:
    """拆 `--flag` / `--flag=值` 两种写法，返回 (位置参数, {flag: True 或 值})。

    **未知 flag 一律报错**，不静默忽略 —— 静默忽略是最坏的失败方式：`cca switch 002 --cary`
    看着像成功了，其实什么都没接管。只有 `=` 形式带值，所以 `--carry` 裸写仍是它自己的含义
    （进交互选择），不会把后面的位置参数吃掉。

    `multi_flags` 里的参数可重复，值按出现顺序收进列表（`--only=a --only=b` → `["a", "b"]`）。
    """
    rest, flags = [], {}
    for arg in args:
        head, _, value = arg.partition("=")
        if head in multi_flags:
            flags.setdefault(head, []).append(value)
        elif head in value_flags:
            flags[head] = value
        elif arg in bool_flags:
            flags[arg] = True
        elif arg.startswith("-") and arg != "-":
            hint = did_you_mean(arg, tuple(bool_flags) + tuple(value_flags) + tuple(multi_flags))
            die(
                f"未知参数 {arg}"
                + (f"；是想用 {hint} 吗？" if hint else "")
                + (f"\n用法: {usage}" if usage else "")
            )
        else:
            rest.append(arg)
    return rest, flags
```

- [ ] **步骤 4：跑测试**

```bash
python3 -m unittest tests.test_claude_acct.TestCli.test_parse_flags_collects_repeatable_values 2>&1 | tail -3
```

预期：`OK`。

- [ ] **步骤 5：提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: parse_flags 支持可重复取值参数（cca carry --only 用）"
```

---

## 任务 2：`cca carry <源号> <目标号>` 独立成命令

把 `cmd_switch` 里的"接管"逻辑抽出来，改成"从指定源号搬到指定目标号"。
`carry_candidates` 现在扫"除目标号以外的所有号"，要改成只扫源号。

**文件：** 改 `bin/claude-acct`（`carry_candidates` 约 1267 行；新 `cmd_carry` 放在 `cmd_switch` 旁边；
`SUBCOMMANDS`、`COMMAND_USAGE`、`main()` 的 dispatch）、`tests/test_claude_acct.py`

- [ ] **步骤 1：改 `carry_candidates` 只认源号**

```python
def carry_candidates(source: str) -> list:
    """源号的后台会话，附上完整 id 与起始目录，按"建议接管"排序。

    交互会话（`kind: interactive`）不算候选：那是别人终端里正开着的会话，`--bg --resume`
    只会 fork 出一份副本（双份上下文、双份额度），没有任何收益。
    """
    items = []
    for s in active_workers(source):
        if s.get("kind") == "interactive":
            continue
        cwd = ""
        transcript = session_transcript(profile_dir(source), s["id"])
        if transcript is not None:
            cwd = _scan_transcript(transcript)["cwd"]
        items.append(
            {
                "profile": source,
                "id": s.get("id", ""),
                # 执行时用完整 UUID：`--bg --resume` 只认它。
                "full": s.get("full") or s.get("id", ""),
                "state": s.get("state", "unknown"),
                "name": s.get("name", ""),
                "cwd": cwd,
                "running": is_running(s.get("state", "unknown")),
            }
        )
    items.sort(key=lambda x: (x["running"], x["name"] or x["id"]))
    return items
```

- [ ] **步骤 2：写新命令**

```python
CARRY_USAGE = "cca carry <源号> <目标号> [--only=<前缀|标题>]… [--all] [--dry-run]"


def cmd_carry(args: list) -> int:
    """把 <源号> 的会话搬到 <目标号>：源号 stop → 目标号 `--bg --resume`（对话不复制、不丢）。

    默认只搬"没在跑的"（等输入 / 已结束）：接管会继续跑，烧的是目标号的额度。要连在跑的一起
    搬用 `--all`；非 tty 必须给 `--only` 或 `--all`，否则报错退出，不留"猜你想搬哪条"的余地。
    """
    rest, flags = parse_flags(
        args,
        bool_flags=("--all", "--dry-run"),
        multi_flags=("--only",),
        usage=CARRY_USAGE,
    )
    if len(rest) != 2:
        die(f"要写清源号和目标号。用法: {CARRY_USAGE}")
    source, target = rest
    if source == target:
        die("源号和目标号是同一个；carry 的用途是换个号接着跑")
    require_profile(source, usage=CARRY_USAGE)
    require_profile(target, usage=CARRY_USAGE)

    dry_run = bool(flags.get("--dry-run"))
    take_all = bool(flags.get("--all"))
    only = flags.get("--only") or []
    if not only and not take_all and not sys.stdin.isatty() and not dry_run:
        die(f"非交互环境下要写清搬哪几条：{CARRY_USAGE}\n  例: cca carry {source} {target} --only=<前缀>")

    console = make_console()
    items = carry_candidates(source)
    if not items:
        say(f"{source} 没有可搬的后台会话（交互会话不搬：--bg --resume 只会 fork 一份副本）")
        return 0

    selected = set()
    if take_all:
        selected = set(range(1, len(items) + 1))
        carry_plan(console, items, target, selected)
    elif only:
        for term in [t.strip() for t in only if t.strip()]:
            key = term.lower()
            hits = [
                i
                for i, item in enumerate(items, start=1)
                if item["full"].lower().startswith(key)
                or item["id"].lower().startswith(key)
                or (item["name"] or "").lower() == key
            ]
            if len(hits) != 1:
                die(f"--only={term} 匹配到 {len(hits)} 条会话；写更长的前缀，或先 cca {source} agents 看清单")
            selected.add(hits[0])
        carry_plan(console, items, target, selected)
    else:
        default = [i for i, item in enumerate(items, start=1) if not item["running"]]
        carry_plan(console, items, target, set(default))
        if dry_run:
            selected = set(default)
        else:
            selected = set(ask_carry_selection(console, items, default))

    if dry_run:
        say("--dry-run：以上是计划，未停任何会话、未接管")
        return 0
    if not selected:
        say("没有选中任何会话，什么都没动")
        return 0

    taken, failures = run_carry(target, items, selected)
    say(f"已从 {source} 搬到 {target}：{len(taken)} 条（源号已停，对话保留）")
    for item in taken:
        say(f"   {item['id']}  {item['name'] or '(无标题)'}   → cca {target} attach {item['id']}")
    if any(not item.get("running") for item in taken):
        note(
            "接管的会话是「等输入」状态：它只在 CLI daemon 存活期间出现在面板里"
            "（退出 claude 后 daemon 空闲退出会收掉这类登记，对话不受影响）。"
            f"要让它重新出现: cca {target} --bg --resume <完整id>"
        )
    for f in failures:
        warn(f"接管失败 {f}")
    return 0 if not failures else 1
```

- [ ] **步骤 3：接上命令面**

`SUBCOMMANDS` 加 `"carry"`；`main()` 的 `dispatch` 加 `"carry": cmd_carry`；`COMMAND_USAGE` 加：

```python
    "carry": """cca carry <源号> <目标号> [--only=<前缀|标题>]… [--all] [--dry-run] —— 把会话搬到另一个号

  做两步: 源号 stop（只停不删，对话保留）→ 目标号 `--bg --resume <完整id>` 接管同一条
  默认只搬"没在跑的"（接管会继续跑，烧目标号的额度）；--all 连在跑的一起
  非 tty 必须给 --only 或 --all（脚本里不留"猜你想搬哪条"）
  例: cca carry 001 002 --only=abcde123
      cca carry ds 002 --all --dry-run
      # 手工等价: cca ds stop-bg  然后  cca 002 --bg --resume <完整id>
""",
```

- [ ] **步骤 4：把 7 个 carry 用例改成新语法**

`tests/test_claude_acct.py` 里这些用例的调用形式（源号都是 001）：

| 用例 | 改成 |
| --- | --- |
| `test_switch_carry_moves_session_to_target` | `self.run_cli("carry", "001", "002", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)` |
| `test_switch_carry_interactive_needs_a_choice` | `self.run_cli("carry", "001", "002", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)`，并把断言改成 `assertIn("已从 001 搬到 002", r.stdout)`（不再依赖 stdin 空输入） |
| `test_switch_carry_interactive_default_takes_parked_only` | `self.run_cli("carry", "001", "002", FAKE_CLAUDE_AGENTS=payload, stdin_data="\n")` |
| `test_switch_carry_dry_run_touches_nothing` | `self.run_cli("carry", "001", "002", "--only=abcde123", "--dry-run", FAKE_CLAUDE_AGENTS=payload)` |
| `test_switch_carry_explicit_running_is_taken` | `self.run_cli("carry", "001", "002", "--only=abcde123", FAKE_CLAUDE_AGENTS=payload)` |
| `test_switch_carry_all_takes_running` | `self.run_cli("carry", "001", "002", "--all", FAKE_CLAUDE_AGENTS=payload)` |
| `test_switch_carry_unknown_prefix_fails` | `self.run_cli("carry", "001", "002", "--only=zzz", FAKE_CLAUDE_AGENTS=payload)`，断言改成 `assertIn("匹配到 0 条", r.stderr)` |

同时把用例名里的 `switch_carry` 改成 `carry`（`test_carry_moves_session_to_target` 等），
并删掉用例里现在无用的 `CODER_WORKSPACE_ID="ws-1"` 与 `self.run_cli("bind", …)` 调用
（`bind` 在任务 5 会被删；本任务先去掉对它的依赖）。

- [ ] **步骤 5：新增 3 个边界用例**

```python
    def test_carry_non_tty_without_only_fails(self):
        self.run_cli("new", "002")
        self.seed_carriable()
        payload = self.agents_payload("abcde123", "abcde123-0000-0000-0000-000000000000")
        r = self.run_cli("carry", "001", "002", FAKE_CLAUDE_AGENTS=payload, stdin_data="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("非交互环境", r.stderr)
        self.assertNotIn("--bg --resume", self.calls())

    def test_carry_rejects_same_profile(self):
        r = self.run_cli("carry", "001", "001")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("同一个", r.stderr)

    def test_carry_unknown_target_fails(self):
        r = self.run_cli("carry", "001", "009")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("不存在", r.stderr)
```

- [ ] **步骤 6：跑全套**

```bash
make test 2>&1 | tail -3
```

预期：`OK`（用例数比之前多 3 个）。

- [ ] **步骤 7：提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: carry 独立成命令（cca carry <源号> <目标号>）"
```

---

## 任务 3：按号命令必须写号

去掉 `$CLAUDE_ACCT` 与 workspace 绑定两层，裸跑 `cca` 与"参数写在号前"都改成报错。

**文件：** 改 `bin/claude-acct`（`resolve_profile`、`cmd_run`、`cmd_which`、`main()`）、
`tests/test_claude_acct.py`

- [ ] **步骤 1：加 `require_profile`，删掉两层隐式解析**

```python
def require_profile(name, *, usage: str) -> str:
    """要落到某个号的命令都从这里取号：没写号直接报错，不做隐式选择。"""
    if not name:
        die(f"要写清哪个号。用法: {usage}")
    name = str(name)
    if not profile_dir(name).is_dir():
        die(f"profile {name} 不存在（{profile_dir(name)}）；先运行 cca add {name} 或 cca new {name}")
    return name
```

删掉 `resolve_profile()`、`read_bind()` / `write_bind()` / `bind_file()`、`binding_reason()`。

- [ ] **步骤 2：改 `cmd_run` 与 `cmd_which`**

```python
def cmd_run(name: str, claude_args: list) -> int:
    name = require_profile(name, usage="cca <profile> [claude 参数…]")
    check_api_profile(name)
    background_guard(name)
    if sys.stderr.isatty():
        print(f"→ 账号 {name} [{identity_banner(name)}]", file=sys.stderr)
    if ensure_onboarding_flag(name):
        note(f"{name}: 已补上 hasCompletedOnboarding —— 该号首次交互启动不会再走引导")
    exe = shutil.which("claude")
    if not exe:
        die("找不到 claude 可执行文件，请确认已安装且在 PATH 中")
    os.execvpe(exe, [exe, *claude_args], build_env(name))
    return 0  # 不会到达


def cmd_which(args: list) -> int:
    """一个号是谁：目录 / 类型 / 端点。一行，脚本友好；详情看 cca status <profile>。"""
    rest = [a for a in args if not a.startswith("-")]
    if len(rest) != 1:
        die("用法: cca which <profile>")
    name = require_profile(rest[0], usage="cca which <profile>")
    if profile_kind(name) == "api":
        marker = read_marker(name)[0] or {}
        say(f"{name}  {api_display(marker.get('provider', ''), marker.get('base_url', ''))}  {profile_dir(name)}")
    else:
        say(f"{name}  oauth  {profile_dir(name)}")
    return 0
```

- [ ] **步骤 3：改 `main()` 的两个入口**

```python
    if not argv:
        die(
            "要写清哪个号：cca <profile> [claude 参数…]\n"
            "  总览: cca ls    可续会话: cca sessions    体检: cca doctor\n"
            "  例: cca 002  /  cca ds --bg '<任务>'"
        )
```

参数写在号前的分支：

```python
    if head.startswith("-"):
        tail = " ".join([head, *rest]).strip()
        die(f"参数 {head} 要跟在号名后面：cca <profile> {tail}\n  例: cca 002 {tail}")
```

- [ ] **步骤 4：改用例**

* 删 `test_which_default`、`test_which_env_override`、`test_which_uses_workspace_binding`、`test_bind_requires_workspace_env`；
* 新增：

```python
    def test_which_requires_profile(self):
        r = self.run_cli("which")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("要写清哪个号", r.stderr)

    def test_which_prints_one_line_for_profile(self):
        self.run_cli("new", "002")
        r = self.run_cli("which", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertIn("002", r.stdout)

    def test_bare_run_asks_for_a_profile(self):
        r = self.run_cli()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("要写清哪个号", r.stderr)
        self.assertFalse((self.home / "claude-call.json").exists())

    def test_flag_before_profile_points_at_correct_form(self):
        r = self.run_cli("-c")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cca <profile>", r.stderr)

    def test_claude_acct_env_no_longer_selects(self):
        self.run_cli("new", "002")
        r = self.run_cli("which", "002", CLAUDE_ACCT="001")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("002", r.stdout)
```

- [ ] **步骤 5：跑全套并提交**

```bash
make test 2>&1 | tail -3
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: 按号命令必须写号（删掉 \$CLAUDE_ACCT 与 workspace 绑定两层）"
```

预期：`OK`。（此时 `cmd_status` / `cmd_sessions` / `cmd_list` / `cmd_switch` 里还留着
`resolve_profile()` 的调用，下一步处理。）

---

## 任务 4：只读命令不带号 = 汇总所有号；sessions 去掉号参数

> 设计修正（执行中发现）：**会话不绑账号**。所有号的 `projects/` 指向同一份目录，`cca sessions`
> 列的就是那个共享池，所以号参数没有意义，直接删掉；"某号在跑什么"归 `cca status <号>`。
> 详见 spec §3.1。

**文件：** 改 `bin/claude-acct`（`cmd_status`、`cmd_sessions`、`resumable_sessions`、`cmd_list`、
`cmd_doctor` 里的 `CLAUDE_ACCT` 提示）、`tests/test_claude_acct.py`

- [ ] **步骤 1：`cmd_status` 拆成"渲染一个号"+"遍历"**

把现有 1864–1988 行的函数体（`name = …` 之后的全部渲染代码）整体搬进新函数，签名 `render_status(name: str, *, as_json: bool)`；
`name` 相关的局部变量改成入参。新 `cmd_status`：

```python
def cmd_status(args: list) -> int:
    """不带号 = 每个号一段摘要；带号 = 单号详情（身份 / 目录 / 额度 / 后台会话明细）。"""
    rest, flags = parse_flags(args, bool_flags=("--json",), usage=STATUS_USAGE)
    if rest[1:]:
        die(f"多余参数 {' '.join(rest[1:])}；用法: {STATUS_USAGE}")
    as_json = bool(flags.get("--json"))
    names = [require_profile(rest[0], usage=STATUS_USAGE)] if rest else discover_profiles()
    if not names:
        die("还没有任何号；先 cca add <name> …")
    if as_json:
        print(json.dumps([status_payload(n) for n in names], ensure_ascii=False, indent=2))
        return 0
    for index, name in enumerate(names):
        if index:
            say("")
        render_status(name)
    return 0
```

`status_payload(name)` 由现有 `--json` 分支的字典构造抽出（每项加 `"profile": name`）。

- [ ] **步骤 2：`cmd_sessions` 去掉号参数**

`resumable_sessions()` 不再收 profile（扫的就是内置号那份共享 projects），`cmd_sessions` 的
位置参数一律报"多余参数"。`--json` 的顶层键从 `profile` 改成 `pool: "shared"`。

```python
def cmd_sessions(args: list) -> int:
    """列共享池里的可续会话。不带号 = 每个号一段；带号 = 只列该号。"""
    show_all = "--all" in args
    as_json = "--json" in args
    wide = "--wide" in args
    rest = [a for a in args if a not in ("--all", "--json", "--wide")]
    if rest[1:]:
        die(f"多余参数 {' '.join(rest[1:])}；用法: {SESSIONS_USAGE}")
    days = 0 if show_all else SESSIONS_DAYS
    names = [require_profile(rest[0], usage=SESSIONS_USAGE)] if rest else discover_profiles()
    if not names:
        die("还没有任何号")
    console = make_console()
    for index, name in enumerate(names):
        if index:
            console.print(_plain(""))
        render_sessions(name, days=days, as_json=as_json, wide=wide)
    return 0
```

（多号时每段开头打一行 `[profile]`；JSON 模式则给每项加 `"profile"` 字段，整体仍是一个数组。）

- [ ] **步骤 3：用例**

```python
    def test_status_without_profile_summarizes_all(self):
        self.run_cli("new", "002")
        self.run_cli("new", "003")
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("001", "002", "003"):
            self.assertIn(name, r.stdout)

    def test_status_with_profile_still_single(self):
        self.run_cli("new", "002")
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("003", r.stdout)

    def test_sessions_rejects_profile_arg(self):
        r = self.run_cli("sessions", "002")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("多余参数", r.stderr)
```

- [ ] **步骤 4：顺手清掉 `cmd_list` 的 `*` 与 `resolve_profile`**

`cmd_list`（1989 行起）里的 `cur, _ = resolve_profile()`、`*` 标记与相应脚注一起删掉
（`--json` 输出里的 `"current"` 也去掉），脚注只留额度快照那句。

- [ ] **步骤 5：跑全套并提交**

```bash
make test 2>&1 | tail -3
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: status/sessions 不带号时汇总所有号；ls 去掉当前号标记"
```

---

## 任务 5：删掉旧命令与隐式选号层

**文件：** 改 `bin/claude-acct`（`SUBCOMMANDS`、`valid_name`、`cmd_switch` 2476–2650、`cmd_bind` 2303–2330、
`cmd_resume` 2857–2883、`read_last_profile` / `write_last_profile` / `last_profile_file`、
`cmd_rm` 里的解绑与切换记录清理、`main()`）、`tests/test_claude_acct.py`

- [ ] **步骤 1：加"已移除命令"出口**

```python
# 已移除的命令：命中就给出替代写法，别回落到"把命令名当号名"去看 profile 存不存在。
REMOVED_COMMANDS = {
    "switch": "默认号已取消：直接写号（cca ds / cca 002）；搬会话用 cca carry <源号> <目标号>",
    "bind": "workspace 绑定已取消：直接写号，如 cca ds",
    "unbind": "workspace 绑定已取消：直接写号，如 cca ds",
    "resume": "改用 claude 自己的参数：cca <号> --resume <完整id>（先 cca sessions 找 id）",
}
# 号名保留名单：子命令 + 已移除命令，两者都不许再被建成号。
RESERVED_NAMES = tuple(SUBCOMMANDS) + tuple(REMOVED_COMMANDS)
```

`valid_name()` 改成 `name not in RESERVED_NAMES`。`main()` 在 `head in dispatch` 之前插：

```python
    if head in REMOVED_COMMANDS:
        die(f"{head} 已移除 —— {REMOVED_COMMANDS[head]}")
```

- [ ] **步骤 2：删函数与常量**

删除：`cmd_switch`、`cmd_bind`、`cmd_resume`、`read_last_profile`、`write_last_profile`、
`last_profile_file`、`SWITCH_USAGE`、`COMMAND_USAGE` 里的 `switch` / `bind` / `resume` 条目、
`SUBCOMMANDS` 里的这四个名字、`main()` 里的 `"bind"` / `"switch"` dispatch 项与 `if head == "unbind"` 分支。

`cmd_rm` 里这段一并删掉（它引用了已删的函数）：

```python
    ws = os.environ.get("CODER_WORKSPACE_ID", "").strip()
    mapping = read_bind()
    if ws and mapping.get(ws) == name:
        ...
    if read_last_profile() == name:
        ...
```

替换成一句：

```python
    say("对话仍在共享池：cca sessions 能看到，任何号都能 cca <号> --resume <id> 续上")
```

- [ ] **步骤 3：删用例**

删掉这些（名字以 `switch` / `bind` / `resume` / `unbind` 为主题）：`test_switch_summary_for_api_profile`、
`test_switch_warns_when_target_cannot_run`、`test_switch_binds_workspace_and_prints_identity`、
`test_switch_requires_workspace_env`、`test_switch_unknown_profile_fails`、`test_switch_flags_claude_acct_override`、
`test_switch_without_carry_still_stops_and_does_not_resume`、`test_switch_stops_parked_others_by_default`、
`test_switch_skips_running_others_without_force`、`test_switch_reports_quota_of_new_profile`、
`test_switch_rejects_typo_flag`、`test_switch_rejects_extra_positional`、`test_switch_carry_skips_interactive_sessions`、
`test_switch_force_stops_running_others`、`test_switch_only_touches_other_profiles`、
`test_switch_without_args_stops_nothing`、`test_switch_dash_returns_to_previous`、
`test_switch_dash_without_history_fails`、`test_switch_dash_to_removed_profile_fails`、
`test_switch_without_args_lists_candidates_without_switching`、`test_rm_unbinds_workspace`、
`test_resume_by_prefix_execs_full_uuid`、`test_resume_by_title_prefers_custom_title`、
`test_resume_ambiguous_prefix_lists_candidates`、`test_resume_unknown_term_fails`、
`test_resume_requires_term`、`test_resume_is_reserved_profile_name`。

- [ ] **步骤 4：新增 3 个用例**

```python
    def test_removed_commands_explain_replacement(self):
        for cmd in ("switch", "bind", "unbind", "resume"):
            r = self.run_cli(cmd)
            self.assertNotEqual(r.returncode, 0, cmd)
            self.assertIn("已移除", r.stderr, cmd)

    def test_removed_names_stay_reserved(self):
        for name in ("switch", "bind", "unbind", "resume"):
            self.assertNotEqual(self.run_cli("new", name).returncode, 0, name)

    def test_legacy_binding_file_is_ignored(self):
        self.run_cli("new", "002")
        (self.home / ".claude-accounts" / "bind.tsv").write_text(
            "# workspace_uuid\tprofile\nws-1\t002\n", encoding="utf-8"
        )
        r = self.run_cli("which", "001", CODER_WORKSPACE_ID="ws-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("001", r.stdout)
```

（旧绑定文件是 `~/.claude-accounts/bind.tsv`、切换记录是 `~/.claude-accounts/.last-profile`；
两者都不再读，用例只断言"存在也不影响选号"。）

- [ ] **步骤 5：确认没有残留引用，跑全套并提交**

```bash
grep -n "resolve_profile\|read_bind\|write_bind\|binding_reason\|read_last_profile\|last_profile_file" bin/claude-acct
make test 2>&1 | tail -3
make lint 2>&1 | tail -1
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "refactor: 删除 switch/bind/unbind/resume 与隐式选号层"
```

预期：`grep` 无输出；`make test` 的 `OK`；`make lint` 的 `All checks passed!`。

---

## 任务 6：命令面文案收口

**文件：** 改 `bin/claude-acct`（`usage()` 1152–1185、`COMMAND_USAGE` 里的 `which` / `sessions` / `status` / `new` / `rm`）

- [ ] **步骤 1：改 `usage()`**

删掉"`cca`（裸跑用当前号）"、`cca bind` / `cca unbind` / `cca switch` / `cca resume` 四行，
加 `cca carry`、`cca which <profile>`，并把"解析顺序"那行换成：

```
解析顺序: 只有一种 —— 命令里写的号。没有默认号、没有 workspace 绑定、没有 $CLAUDE_ACCT。
```

- [ ] **步骤 2：改各条 `--help`**

`cca which --help` → `cca which <profile> —— 打印这个号的目录 / 类型 / 端点（一行）`；
`cca sessions --help` → 说明不带号 = 全部号；`cca status --help` → 同上；`cca rm --help` 里
"拒绝删 001（它是共享资产来源）"的措辞从"默认号"改成"内置号"。

- [ ] **步骤 3：用例**

```python
    def test_help_has_no_removed_commands(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for word in ("cca switch", "cca bind", "cca resume"):
            self.assertNotIn(word, r.stdout, word)
        self.assertIn("cca carry", r.stdout)
        self.assertIn("没有默认号", r.stdout)
```

- [ ] **步骤 4：跑全套并提交**

```bash
make test 2>&1 | tail -3
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "docs: 命令面文案去掉选号相关表述"
```

---

## 任务 7：文档改写

**文件：** `README.md`（"切号"、"把旧号的会话带过来"、"删号"章节）、`docs/specs/design.md`（解析顺序段）、
`docs/specs/api-profiles.md`（提到 `cca switch` 的句子）

- [ ] **步骤 1：README**

把 `## 切号` 与 `### 把旧号的会话"带"到新号（--carry）` 两节改写成一节 `## 选号与搬运`，内容包含：
"账号永远写在命令里"（`cca ds` / `cca 002 -c` / `cca ds --bg '…'`）；`cca ls` / `cca status` /
`cca sessions` 不带号 = 汇总所有号；`cca carry <源号> <目标号>` 的三个例子与"手工等价两步"；
一句"裸跑 `claude` 仍是 001，因为它的配置目录就是 `~/.claude`"；以及"订阅号之间仍建议串行
（共享额度窗口），API 号之间可以并行"。删掉所有 `cca switch` / `cca bind` / `cca resume` 的用法。

- [ ] **步骤 2：design.md 与 api-profiles.md**

`design.md` 里"解析顺序: 显式参数 → $CLAUDE_ACCT → workspace 绑定 → 001"那段改成上文的单行说法，
并指向 `explicit-profile.md`；`api-profiles.md` 里"`cca switch` 也像订阅号一样可用"这类句子改成
`cca carry` 或直接写号。

- [ ] **步骤 3：检查无残留**

```bash
grep -rn "cca switch\|cca bind\|cca unbind\|cca resume\|CLAUDE_ACCT\|CODER_WORKSPACE_ID" README.md docs/specs/*.md
```

预期：只剩 `explicit-profile.md`（它记录的就是"删掉这些"）与历史 spec 里明确标注"已废弃"的地方。

- [ ] **步骤 4：提交**

```bash
git add README.md docs/specs/design.md docs/specs/api-profiles.md
git commit -m "docs: README/design 改写为账号显式化（选号与搬运）"
```

---

## 任务 8：收口

- [ ] **步骤 1：版本与全量验证**

```bash
echo "0.17.0" > VERSION
make test 2>&1 | tail -3
make lint 2>&1 | tail -1
```

预期：`OK` + `All checks passed!`（大版本号进一位：删了四个命令、改了三个命令的语义）。

- [ ] **步骤 2：真实环境手工验收**

```bash
cca ls                       # 面板不再有 *；ds 与 codex 两个 API 号正常
cca status                   # 每个号一段摘要
cca sessions                 # 全部号的可续会话，带号名
cca ds --bg 'echo hi'        # 仍是显式写号的老用法
cca 002 --resume <某个完整id>  # 透传续会话
```

验收记录写进 `docs/specs/explicit-profile.md` 末尾新增的"§9 验收记录"（照 `api-profiles.md` §9.1 的写法，
写清命令与真实输出要点）。

- [ ] **步骤 3：提交并开 MR**

```bash
git add VERSION docs/specs/explicit-profile.md
git commit -m "chore: VERSION 0.17.0 并记录账号显式化的验收"
env -u GIT_SSH_COMMAND git push -u origin feat/explicit-profile
env -u GIT_SSH_COMMAND glab mr create --title "feat: 账号显式化（去掉默认号与 workspace 绑定，carry 独立）" \
  --description "见 docs/specs/explicit-profile.md 与 explicit-profile-plan.md"
```

拿到 MR 号后向使用者要合并批准（main 是保护分支）。

---

## 自检

**Spec 覆盖：** §1 决策 → 任务 3/5；§2 原则 → 任务 3/4；§3 命令面 → 任务 3/4/5/6；
§3.1 续会话 → 任务 3（`main` 报错文案）与任务 5（`resume` 移除提示）；§4 carry → 任务 2；
§5 001 的地位 → 任务 6（措辞）；§6 实现清单 → 任务 3/4/5；§7 非目标 → 全部任务遵守；
§8 的 12 条用例 → 任务 2（3 条）、3（5 条）、4（3 条）、5（3 条）覆盖，其中"`cca stop-bg` 不带号报错"
由任务 3 的 `require_profile` 顺带保证（现有用例已覆盖）。

**占位符扫描：** 无 TODO / TBD；每个代码步骤都给了完整代码。

**命名一致性：** `require_profile(name, *, usage=)`、`carry_candidates(source)`、`render_status(name)`、
`render_sessions(profile, *, days, as_json, wide)`、`REMOVED_COMMANDS` / `RESERVED_NAMES` 在各任务里同名。
