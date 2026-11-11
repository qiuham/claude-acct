# OpenRouter 号 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `cca` 支持 OpenRouter 号：一个 key，按会话选模型（`--model`），选型可查（目录只读），并让每条会话用的模型在面板上可见。

**Architecture:** 沿用现有 API 号机制——新增 `openrouter` 预设（端点/密钥环境变量名/模型变量都进清单），
再把"目录查询 → 按会话写窗口 → 启动时注入 env"做成一条窄链：`load_model_catalog()` 拉并缓存
OpenRouter 的 `/api/v1/models`，`compact_window_for()` 决定这次启动要不要写
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`，`launch_env()` 把它并进 `build_env()` 的结果。选择器侧只多一个
`cca model` 子命令（只读汇总 + 写默认 + 读目录）。

**Tech Stack:** 单文件 Python 3.12（只用 stdlib；HTTP 走 `urllib.request`，与 `probe_endpoint()` 同款）+
`rich`（懒加载）；测试是 stdlib `unittest` + 假 `claude` 桩，入口 `make test` / `make lint`。

**Spec:** `docs/specs/openrouter.md`

## Global Constraints

* 端点 `base_url` = `https://openrouter.ai/api`；密钥环境变量名 = `OPENROUTER_API_KEY`；
  **凭据仍走 `<配置目录>/.api-key`（0600）+ `apiKeyHelper`**，密钥不进 env、argv、日志。
* 预设模型变量：`ANTHROPIC_MODEL=openrouter/free`、`ANTHROPIC_DEFAULT_OPUS_MODEL=~anthropic/claude-opus-latest`、
  `..._SONNET_...=~anthropic/claude-sonnet-latest`、`..._HAIKU_...=~anthropic/claude-haiku-latest`、
  `ANTHROPIC_DEFAULT_FABLE_MODEL=~anthropic/claude-fable-latest`。
* 预设额外 env 只有三个：`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`、
  `CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT=1`、`CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1`。
  **不许出现** `ENABLE_TOOL_SEARCH`、`CLAUDE_CODE_SUBAGENT_MODEL`；`proxy` = `builtin`。
* 目录源 `GET https://openrouter.ai/api/v1/models`（公开免 key）；缓存
  `~/.claude-accounts/.cache/openrouter-models.json`（0600，TTL 3600 秒）；目录不可用时用旧缓存并打一行
  提示，**永不阻塞**建号与启动。测试用 `CCA_MODEL_CATALOG_FILE` 指向 fixture，**不许联网**。
* 窗口规则：`context_length × 0.75` **向下取整到千**；slug 去掉可选 `vendor/` 前缀后形如
  `claude-…` 的一律**不写**；目录里查不到的一律**不写**并打一行提示。
* 仓库约定：单文件 stdlib、测试 hermetic（`make test` + `make lint` 全绿才能提交）、
  每批改 `VERSION`、文档随行为一起改、主题分支 + MR（main 受保护）。

## Review Focus

以下五类是 spec 暗示但任何单个任务的测试都可能漏掉的输入，按最容易咬人的顺序排列；
每条都在"归属任务"里加了对应测试。

1. **目录请求失败/超时/返回坏 JSON** → 建号与启动都不能被拖住，只能降级（用旧缓存，或干脆不写窗口 + 一行提示）。
2. **`--model` 传的是 CC 认得的 Claude slug**（`anthropic/claude-opus-4-8`）→ 绝不能写窗口，
   写了会用错误的值盖掉模型真实窗口。
3. **`--model` 传的是目录里没有的 slug**（新模型、拼错）→ 不写窗口 + 一行提示，不许崩、不许猜。
4. **同一把 key 两个会话不同 `--model`** → 必须互不污染：`--model` 只进本次会话的 env，
   **绝不能写进号级 `settings.json`**。
5. **缓存文件损坏 / 权限不对 / 目录不存在** → 当作没缓存重拉；重拉失败也不抛异常。

---

### Task 1: `openrouter` 预设与建号

**Files:**
- Modify: `bin/claude-acct`（`PROVIDERS` 表、`ADD_USAGE` 的 provider 列表）
- Test: `tests/test_claude_acct.py`

**Interfaces:**
- Consumes: 现有 `cmd_add` / `api_marker_from_flags` / `api_env_vars(marker)` / `key_env_name(marker)`。
- Produces: `PROVIDERS["openrouter"]` = `{"base_url": str, "key_env": str, "proxy": PROXY_BUILTIN, "env": {…}}`，
  供后续任务与 `cca add --provider openrouter` 使用。

- [ ] **Step 1: 写失败用例**

```python
    def test_add_openrouter_preset_renders_gateway_env(self):
        r = self.run_cli("add", "or", "--provider", "openrouter", "--key", "sk-or-v1-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        marker = json.loads((self.acct_dir("or") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["base_url"], "https://openrouter.ai/api")
        env = marker["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "openrouter/free")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "~anthropic/claude-opus-latest")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "~anthropic/claude-sonnet-latest")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "~anthropic/claude-haiku-latest")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "~anthropic/claude-fable-latest")
        self.assertEqual(env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
        self.assertEqual(env["CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT"], "1")
        self.assertEqual(env["CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK"], "1")
        self.assertNotIn("ENABLE_TOOL_SEARCH", env)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        self.assertEqual(marker["proxy"], "builtin")
        self.assertNotIn("sk-or-v1-test", json.dumps(marker))     # 密钥只进 .api-key
        self.assertEqual(oct((self.acct_dir("or") / ".api-key").stat().st_mode)[-3:], "600")
```

- [ ] **Step 2: 跑一遍确认失败**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_add_openrouter_preset_renders_gateway_env`
Expected: FAIL（`未知 provider: openrouter`）

- [ ] **Step 3: 实现预设**

在 `PROVIDERS` 里加一项，键与值与 Global Constraints 完全一致；`ADD_USAGE` 里把已知预设列表从
`deepseek` 改成 `deepseek、openrouter`（用 `"、".join(sorted(PROVIDERS))` 的现有写法，不要手写死）。

- [ ] **Step 4: 跑用例确认通过**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_add_openrouter_preset_renders_gateway_env`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: openrouter 预设（端点/密钥变量名/四档别名 + 网关推荐 env）"
```

---

### Task 2: 模型目录：拉取、缓存、离线降级

**Files:**
- Modify: `bin/claude-acct`（新增目录相关常量与函数，放在 `preset_for_url()` 附近）
- Test: `tests/test_claude_acct.py`（新增 fixture 文件写入 tmp `HOME`）

**Interfaces:**
- Produces（后续任务与用户都用这套名字）:
  * `CATALOG_URL = "https://openrouter.ai/api/v1/models"`、`CATALOG_TTL = 3600`
  * `catalog_cache_file() -> Path`（`accounts_root()/".cache"/"openrouter-models.json"`）
  * `load_model_catalog(*, refresh: bool = False) -> list[dict]`：读缓存；过期或 `refresh` 时联网重拉；
    失败就用旧缓存；两者都没有则返回 `[]`。**不抛异常、不阻塞**。
  * `find_model(catalog: list, slug: str) -> dict | None`
  * 环境变量 `CCA_MODEL_CATALOG_FILE`：设置了就直接读该文件（测试与离线用），**不联网**。
- Consumes: `accounts_root()`、`probe_endpoint()` 里的 `urllib.request.build_opener(ProxyHandler(...))` 写法。

- [ ] **Step 1: 写失败用例（覆盖 Review Focus 1 与 5）**

```python
    def write_catalog_fixture(self, models=None):
        fixture = self.home / "or-models.json"
        fixture.write_text(json.dumps({"data": models or [
            {"id": "openrouter/free", "name": "Free", "context_length": 200000,
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "qwen/qwen3.8-27b:free", "name": "Qwen Free", "context_length": 262144,
             "pricing": {"prompt": "0", "completion": "0"}},
        ]}), encoding="utf-8")
        return fixture

    def test_catalog_reads_fixture_without_network(self):
        fixture = self.write_catalog_fixture()
        out = self._run_python(CATALOG_PROBE, env_extra={"CCA_MODEL_CATALOG_FILE": str(fixture)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("openrouter/free", out.stdout)

    def test_catalog_survives_broken_cache_and_network_failure(self):
        cache = self.home / ".claude-accounts" / ".cache" / "openrouter-models.json"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("{ 这不是 JSON", encoding="utf-8")
        out = self._run_python(CATALOG_PROBE, env_extra={"CCA_CATALOG_URL": "http://127.0.0.1:9/nope"})
        self.assertEqual(out.returncode, 0, out.stderr)     # 不许抛异常
        self.assertIn("[]", out.stdout)                     # 没有可用目录就是空列表
```

其中 `CATALOG_PROBE` 是测试文件顶部的模块级字符串常量（与既有 `BUILD_ENV_CODE` 同款写法）：
用 `importlib` 载入被测文件，调 `load_model_catalog()`，把 `[m["id"] for m in catalog]` 打成一行。
本计划后续出现的 `WINDOW_PROBE` / `LAUNCH_ENV_PROBE` / `LAST_MODEL_PROBE` 同样放在那里。

- [ ] **Step 2: 跑一遍确认失败**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_catalog_reads_fixture_without_network tests.test_claude_acct.CliTest.test_catalog_survives_broken_cache_and_network_failure`
Expected: FAIL（`load_model_catalog` 不存在）

- [ ] **Step 3: 实现目录读取**

`load_model_catalog()` 的顺序固定：`CCA_MODEL_CATALOG_FILE` → 缓存（未过期直接用）→ 联网拉
（超时 8 秒，用该号无关的默认代理设置）→ 成功写缓存（`mkdir(mode=0o700)` + 文件 0600）→
失败回退旧缓存 → 都没有就 `[]`。URL 用 `CCA_CATALOG_URL` 可覆盖（测试用）。解析时只保留
`id`/`name`/`context_length`/`pricing`/`supported_parameters` 这几个字段，其它丢掉。

- [ ] **Step 4: 跑用例确认通过**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_catalog_reads_fixture_without_network tests.test_claude_acct.CliTest.test_catalog_survives_broken_cache_and_network_failure`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: 模型目录（拉取/缓存/离线降级，测试走 fixture 不联网）"
```

---

### Task 3: 按会话写窗口，并在启动时注入

**Files:**
- Modify: `bin/claude-acct`（新增 `compact_window_for()` / `launch_env()`；`cmd_run()` 改用 `launch_env()`）
- Test: `tests/test_claude_acct.py`

**Interfaces:**
- Consumes: Task 2 的 `load_model_catalog()` / `find_model()`。
- Produces:
  * `compact_window_for(slug: str, catalog: list) -> int | None` —— 返回该写进
    `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 的整数（`context_length × 0.75` 向下取整到千），
    不该写时返回 `None`。
  * `model_flag_value(claude_args: list) -> str` —— 从 `--model X` / `--model=X` 里取出值，没有则 `""`。
  * `launch_env(name: str, claude_args: list) -> dict` —— `build_env(name)` 的结果再并入本次会话的
    窗口覆盖；`cmd_run()` 用它替换原来的 `build_env(name)`。

- [ ] **Step 1: 写失败用例（覆盖 Review Focus 2、3、4）**

```python
    def test_compact_window_three_branches(self):
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        catalog = [
            {"id": "openrouter/free", "context_length": 200000, "pricing": {}, "name": "Free"},
            {"id": "qwen/qwen3.8-27b:free", "context_length": 262144, "pricing": {}, "name": "Qwen"},
            {"id": "anthropic/claude-opus-4-8", "context_length": 1000000, "pricing": {}, "name": "Opus"},
        ]
        out = self._run_python(WINDOW_PROBE, json.dumps(catalog))
        self.assertIn("openrouter/free=150000", out.stdout)          # 200000×0.75
        self.assertIn("qwen/qwen3.8-27b:free=196000", out.stdout)    # 262144×0.75 向下取整到千
        self.assertIn("anthropic/claude-opus-4-8=None", out.stdout)  # CC 认得的 claude-* 不写
        self.assertIn("nobody/unknown-model=None", out.stdout)       # 目录里没有 → 不写

    def test_session_model_does_not_touch_profile_settings(self):
        """--model 只进本次会话 env：号级 settings.json 一个字节都不许改（Review Focus 4）。"""
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        fixture = self.write_catalog_fixture()
        before = (self.acct_dir("or") / "settings.json").read_bytes()
        out = self._run_python(LAUNCH_ENV_PROBE, "or", "--model", "qwen/qwen3.8-27b:free",
                               env_extra={"CCA_MODEL_CATALOG_FILE": str(fixture)})
        self.assertIn("qwen/qwen3.8-27b:free", out.stdout)            # --model 原样透传
        self.assertEqual((self.acct_dir("or") / "settings.json").read_bytes(), before)
```

`LAUNCH_ENV_PROBE` 载入被测文件后调 `launch_env(sys.argv[2], sys.argv[3:])`，打印
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 与 `ANTHROPIC_MODEL` 两个键的值；它跑之前要把
`CCA_MODEL_CATALOG_FILE` 指到 fixture（`env_extra`）。

- [ ] **Step 2: 跑一遍确认失败**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_compact_window_three_branches tests.test_claude_acct.CliTest.test_session_model_does_not_touch_profile_settings`
Expected: FAIL（`compact_window_for` / `launch_env` 不存在）

- [ ] **Step 3: 实现三个函数并接到 `cmd_run()`**

`compact_window_for` 的判定顺序：目录里查不到 → `None`；slug 去掉首段 `vendor/` 后以 `claude-` 开头
→ `None`；否则 `int(ctx * 0.75 // 1000 * 1000)`。`launch_env` 里：`ANTHROPIC_MODEL` 已由清单
env 给定时**只有** `--model` 存在才覆盖窗口；`--model` 的值不写进号级配置，只影响本次 env。
`cmd_run()` 里把 `os.execvpe(exe, [exe, *claude_args], build_env(name))` 换成 `launch_env(name, claude_args)`。

- [ ] **Step 4: 跑用例确认通过**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_compact_window_three_branches tests.test_claude_acct.CliTest.test_session_model_does_not_touch_profile_settings`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: 按 --model 给本次会话写压缩窗口（启动时注入，不动号级配置）"
```

---

### Task 4: `cca model`：只读汇总与写入默认

**Files:**
- Modify: `bin/claude-acct`（`SUBCOMMANDS`、`main()` 的 dispatch、`COMMAND_USAGE["model"]`、`usage()`；
  新函数 `cmd_model`、`model_policy(name) -> dict`、`write_model_policy(name, changes: dict) -> dict`）
- Test: `tests/test_claude_acct.py`

**Interfaces:**
- Consumes: `require_profile(name, usage=…)`、`_settings(name)`、`write_settings_env`、
  `profile_kind(name)`、`read_marker`、Task 3 的 `compact_window_for`。
- Produces: `cca model [<号>] [--main <slug>] [--sub <slug>] [--opus <slug>] [--sonnet <slug>]
  [--haiku <slug>] [--fable <slug>] [--effort <level>] [--all] [--json]`；
  `model_policy(name)` 返回 `{"main": str, "sub": str, "opus": str, …, "effort": str, "source": str}`。

- [ ] **Step 1: 写失败用例**

```python
    def test_model_summary_without_profile_is_read_only(self):
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        before = (self.acct_dir("or") / "settings.json").read_bytes()
        r = self.run_cli("model")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("or", r.stdout)
        self.assertEqual((self.acct_dir("or") / "settings.json").read_bytes(), before)

    def test_model_write_requires_profile(self):
        r = self.run_cli("model", "--main", "openrouter/free")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("要写清哪个号", r.stderr)

    def test_model_sets_main_alias_and_effort(self):
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        r = self.run_cli("model", "or", "--main", "qwen/qwen3.8-27b:free",
                         "--opus", "~anthropic/claude-opus-latest", "--effort", "high")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = json.loads((self.acct_dir("or") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(st["model"], "qwen/qwen3.8-27b:free")
        self.assertEqual(st["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], "~anthropic/claude-opus-latest")
        self.assertEqual(st["modelSettings"]["qwen/qwen3.8-27b:free"]["effortLevel"], "high")

    def test_model_all_writes_every_profile(self):
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        self.seed_api_profile("or2", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        r = self.run_cli("model", "--all", "--sub", "openrouter/free")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for name in ("or", "or2"):
            st = json.loads((self.acct_dir(name) / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(st["env"]["CLAUDE_CODE_SUBAGENT_MODEL"], "openrouter/free")
```

- [ ] **Step 2: 跑一遍确认失败**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_model_summary_without_profile_is_read_only tests.test_claude_acct.CliTest.test_model_write_requires_profile tests.test_claude_acct.CliTest.test_model_sets_main_alias_and_effort tests.test_claude_acct.CliTest.test_model_all_writes_every_profile`
Expected: FAIL（未知子命令 `model`）

- [ ] **Step 3: 实现**

只读路径：遍历 `discover_profiles()`，每行打 `号 / 主模型 / 四档别名 / 子代理 / effort`；
别名与子代理取自 `settings.json` 的 `env`（缺就显示 `-`，并在有清单时回落到清单 env）。
写入路径：`--main` 写 `settings.json` 的 `model`；`--sub` 写 `env.CLAUDE_CODE_SUBAGENT_MODEL`；
四档写 `env.ANTHROPIC_DEFAULT_*_MODEL`；`--effort` 写 `modelSettings[<当前主模型>].effortLevel`；
写完调 `write_settings_env()` 保证与清单一致（API 号），并 `cca doctor` 不报漂移。
不带号且没有任何写入参数 = 只读；带写入参数但既没写号也没 `--all` → `die`。

- [ ] **Step 4: 跑用例确认通过**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_model_summary_without_profile_is_read_only tests.test_claude_acct.CliTest.test_model_write_requires_profile tests.test_claude_acct.CliTest.test_model_sets_main_alias_and_effort tests.test_claude_acct.CliTest.test_model_all_writes_every_profile`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: cca model —— 只读汇总 + 写默认/档位/effort（写入必须写号）"
```

---

### Task 5: `cca model --list`：目录只读视图

**Files:**
- Modify: `bin/claude-acct`（`cmd_model` 增加 `--list/--free/--search/--sort/--refresh`）
- Test: `tests/test_claude_acct.py`

**Interfaces:**
- Consumes: Task 2 的 `load_model_catalog(refresh=)`、Task 4 的 `cmd_model`。
- Produces: 过滤/排序/格式化逻辑 `filter_catalog(catalog, *, free_only, search, sort) -> list[dict]`
  与 `format_price(pricing: dict) -> str`（`"FREE"` 或 `"$2/$10 每 M"`）。

- [ ] **Step 1: 写失败用例**

```python
    def test_model_list_free_filter_and_sort(self):
        self.seed_api_profile("or", provider="openrouter", base_url="https://openrouter.ai/api",
                              env={"ANTHROPIC_MODEL": "openrouter/free"}, render=True)
        fixture = self.write_catalog_fixture([
            {"id": "openrouter/free", "name": "Free router", "context_length": 200000,
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "qwen/qwen3.8-27b:free", "name": "Qwen", "context_length": 262144,
             "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "anthropic/claude-opus-4-8", "name": "Opus", "context_length": 1000000,
             "pricing": {"prompt": "0.000004", "completion": "0.00002"}},
        ])
        env = {"CCA_MODEL_CATALOG_FILE": str(fixture)}
        r = self.run_cli("model", "--list", "--free", **env)
        self.assertIn("openrouter/free", r.stdout)
        self.assertIn("qwen/qwen3.8-27b:free", r.stdout)
        self.assertNotIn("claude-opus-4-8", r.stdout)
        self.assertIn("FREE", r.stdout)
        r = self.run_cli("model", "--list", "--sort", "ctx", **env)
        self.assertLess(r.stdout.index("claude-opus-4-8"), r.stdout.index("qwen/qwen3.8-27b:free"))
        r = self.run_cli("model", "--list", "--search", "qwen", **env)
        self.assertIn("qwen/qwen3.8-27b:free", r.stdout)
        self.assertNotIn("claude-opus-4-8", r.stdout)
        r = self.run_cli("model", "--list", **env)
        self.assertIn("$4/$20 每 M", r.stdout)          # 0.000004/0.00002 换算成每 M 美元
```

- [ ] **Step 2: 跑一遍确认失败**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_model_list_free_filter_and_sort`
Expected: FAIL（`model` 不认识 `--list`）

- [ ] **Step 3: 实现**

`--list` 不需要号（只读目录）。表格列：`ID / 名称 / ctx / 价格`，行数多时按 `--sort` 排；
`--free` 只留 `id` 以 `:free` 结尾或等于 `openrouter/free` 的行；`--search` 对 `id` 与 `name`
做大小写不敏感子串匹配；`--refresh` 传给 `load_model_catalog(refresh=True)`。
价格换算：`pricing["prompt"]`/`["completion"]` 是"每 token 美元"，乘 1e6 后按需保留两位小数。

- [ ] **Step 4: 跑用例确认通过**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_model_list_free_filter_and_sort`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: cca model --list/--free/--search/--sort（目录只读，带价格与窗口）"
```

---

### Task 6: 会话面板显示"用的哪个模型"

**Files:**
- Modify: `bin/claude-acct`（新增 `last_session_model(path: Path) -> str`；
  `resumable_sessions()` 与 `active_workers()` 的展示路径补这一列；
  `cmd_sessions` 与 `_background_sessions_text` 加列）
- Test: `tests/test_claude_acct.py`

**Interfaces:**
- Produces: `last_session_model(path: Path) -> str` —— 只读文件尾部（最后 256 KB），
  取最后一条 `"model":"…"`，忽略 `"<synthetic>"`；读不到返回 `""`。

- [ ] **Step 1: 写失败用例**

```python
    def test_last_session_model_ignores_synthetic(self):
        f = self.seed_transcript("001", "aaaa1111-0000-0000-0000-000000000000", title="甲")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"model": "qwen/qwen3.8-27b:free"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + "\n")
        out = self._run_python(LAST_MODEL_PROBE, str(f))
        self.assertIn("qwen/qwen3.8-27b:free", out.stdout)

    def test_sessions_shows_model_column(self):
        f = self.seed_transcript("001", "bbbb2222-0000-0000-0000-000000000000", title="乙")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"model": "openrouter/free"}}) + "\n")
        r = self.run_cli("sessions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("openrouter/free", r.stdout)
```

- [ ] **Step 2: 跑一遍确认失败**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_last_session_model_ignores_synthetic tests.test_claude_acct.CliTest.test_sessions_shows_model_column`
Expected: FAIL（`last_session_model` 不存在 / 输出里没有模型）

- [ ] **Step 3: 实现**

`last_session_model()` 用 `open("rb")` + `seek(-262144, 2)` 读尾部，正则 `b'"model":"([^"]+)"'`
取最后一个；`cmd_sessions` 的表格与会话 JSON 加 `model` 字段；`_background_sessions_text()`
在每行尾部追加 `[模型]`（读不到就不追加，保持既有输出）。

- [ ] **Step 4: 跑用例确认通过**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_last_session_model_ignores_synthetic tests.test_claude_acct.CliTest.test_sessions_shows_model_column`
Expected: PASS

- [ ] **Step 5: 全量回归（新增列会动到既有断言）**

Run: `make test && make lint`
Expected: 全绿；若有既有用例因新增列失败，改断言而不是删列。

- [ ] **Step 6: 提交**

```bash
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat: cca sessions / cca status 显示每条会话最后用的模型"
```

---

### Task 7: 文档、帮助、版本与真机验收

**Files:**
- Modify: `README.md`（新增「OpenRouter 号」一节）、`bin/claude-acct`（`ADD_USAGE` 与 `COMMAND_USAGE["model"]` 文案）、
  `docs/specs/openrouter.md`（补 §11 验收记录）、`VERSION`

**Interfaces:**
- Consumes: 前六个任务的全部命令面。

- [ ] **Step 1: 写 README 一节**

包含：一句定位（一个 key、多模型、按会话选）、建号命令、四个日常命令
（`cca or --model <slug>`、`cca or --bg --model <slug>`、`cca model or --list --free`、
`cca sessions` 看模型列）、窗口规则三分支与 `/model` 的两个边界（按 `s` 只改本次会话；
切大窗口模型后用 `/autocompact`）。

- [ ] **Step 2: 帮助文案**

`cca add --help` 的 provider 列表加上 openrouter（带一行例子）；`cca model --help` 覆盖
只读汇总/写入/`--list` 三组用法与"写入必须写号、批量要 `--all`"。

- [ ] **Step 3: 版本与全量验证**

```bash
echo "0.19.0" > VERSION
make test && make lint
```

- [ ] **Step 4: 真机验收（需要使用者提供 OpenRouter key，人工步骤）**

```bash
cca add or --provider openrouter --key sk-or-…
cca or -p "只回两个字：收到"                            # 默认 openrouter/free
cca or --model ~anthropic/claude-haiku-latest -p "…"    # 指定模型
cca or --bg --model openrouter/free '说一句 hello'       # 后台
cca model or --list --free                             # 免费清单（真目录）
cca sessions                                           # 模型列
```

每一步的输出要点写进 `docs/specs/openrouter.md` 的 §11 验收记录（照 `api-profiles.md` §9.1 的写法）。

- [ ] **Step 5: 提交并开 MR**

```bash
git add README.md bin/claude-acct docs/specs/openrouter.md VERSION
git commit -m "docs: OpenRouter 号用法 + VERSION 0.19.0 + 验收记录"
env -u GIT_SSH_COMMAND git push -u origin feat/openrouter
env -u GIT_SSH_COMMAND glab mr create --title "feat: OpenRouter 号（一个 key、按会话选模型、目录只读）" \
  --description "见 docs/specs/openrouter.md 与 docs/specs/openrouter-plan.md"
```

---

## 自检

**Spec 覆盖**：§3 接线 → 任务 1；§4 按会话选模型（含窗口三分支）→ 任务 3；§5 命令面 → 任务 4/5；
§6 目录与缓存 → 任务 2；§7 可见性 → 任务 6；§8 非目标 → 全部任务遵守（特别是不写 `ENABLE_TOOL_SEARCH`、
不设 `CLAUDE_CODE_SUBAGENT_MODEL`、不做能力推断、不做 `--pick`）；§9 验证 → 任务 2/3/4/5/6 的用例
+ 任务 7 的真机验收；§10 风险 → 任务 3（窗口）、任务 4（写入边界）、任务 7（文档写明）。

**Review Focus 归属**：1 → 任务 2 Step 1 第二个用例；2 → 任务 3 Step 1 的
`anthropic/claude-opus-4-8=None` 断言；3 → 任务 3 的 `nobody/unknown-model=None` 断言；
4 → 任务 3 的 `test_session_model_does_not_touch_profile_settings`；5 → 任务 2 的坏缓存用例。

**命名一致性**：`load_model_catalog` / `find_model` / `compact_window_for` / `model_flag_value` /
`launch_env` / `model_policy` / `write_model_policy` / `filter_catalog` / `format_price` /
`last_session_model` 在各任务里同名同签名；`CCA_MODEL_CATALOG_FILE` 与 `CCA_CATALOG_URL`
两个测试注入点只在任务 2 定义、任务 3/5 使用。

**未决（执行时才会知道的事实）**：OpenRouter 真实目录的字段是否与 spec §6 列的一致、
`openrouter/free` 的真实 `context_length` 是否仍为 200000 —— 任务 7 的真机验收会证实，
不符就更新 spec §6 与 §3 的默认值（窗口按实测值重算）。
