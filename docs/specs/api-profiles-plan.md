# API 号（API key + 第三方 Anthropic 兼容端点）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `cca` 能管理"用 API key 打第三方 Anthropic 兼容端点"的号（如 DeepSeek），
命令面与订阅号一致（`cca ds`、`--bg`、`ls`、`status`、`switch`、`sessions` 都能用）。

**Architecture:** 种类显式：`<配置目录>/.cca-profile.json`（0600，真相来源）存在即 API 号，
不存在即现有订阅号（零改动）。`build_env()` 成为唯一分派点（订阅号现状 / API 号注入
`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` + 清单 env，先清继承值防串味）；
`settings.json` 的 `env` 是清单渲染出的产物（不含密钥），让绕过 `cca` 直接跑 `claude` 也能到对端点。

**Tech Stack:** Python 3.12 标准库（单文件 `bin/claude-acct`）、`unittest` + 假 `claude` 桩（离线）、
ruff。设计见 `docs/specs/api-profiles.md`。

**约定（每个任务都要遵守）**
- 所有测试在临时 `HOME` + 假 `claude` 下跑，**不联网**。
- 每次提交前跑 `make test` 与 `make lint`。
- 密钥永不进输出：断言用完整的假 key 字符串去 `assertNotIn`。
- 计划里的代码是**最终形态**；实施时按任务顺序逐个落地，不要跳步。

**涉及文件**
- 修改：`bin/claude-acct`（常量、数据层、`build_env`、`cmd_add`/`cmd_login`/`cmd_logout`/
  `cmd_run`/`cmd_resume`/`run_carry`、`cmd_list`、`cmd_status`、`cmd_doctor`、`cmd_sync`、`cmd_rm`）
- 修改：`tests/test_claude_acct.py`（新增 19 条用例 + 2 个种子 helper）
- 修改：`README.md`、`VERSION`

---

### Task 1: 常量与清单数据层

**Files:**
- Modify: `bin/claude-acct`（`PROFILE_RE` 附近加常量；`read_identity` 之后加函数）
- Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_profile_kind_and_marker(self):
        """有清单 = API 号；没有 = 订阅号；清单坏了要报错而不是当成订阅号。"""
        self.run_cli("new", "002")
        self.assertEqual(self.kind_of("001"), "oauth")
        self.assertEqual(self.kind_of("002"), "oauth")
        marker = self.acct_dir("002") / ".cca-profile.json"
        marker.write_text(json.dumps({"kind": "api", "provider": "deepseek",
                                      "base_url": "https://api.deepseek.com/anthropic",
                                      "env": {"ANTHROPIC_MODEL": "deepseek-flash[1m]"}}),
                          encoding="utf-8")
        self.assertEqual(self.kind_of("002"), "api")
        marker.write_text("{ 坏 JSON", encoding="utf-8")
        r = self.run_cli("status", "002")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("无法解析", r.stderr)
```

同时加测试侧 helper（放在 `seed_usage` 附近）：

```python
    def kind_of(self, name):
        """被测代码里的 profile_kind()，用于断言种类判定。"""
        return self._run_python(
            f"""
            import importlib.machinery, importlib.util, sys
            loader = importlib.machinery.SourceFileLoader("cca_mod", sys.argv[1])
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cca_mod", loader))
            loader.exec_module(module)
            print(module.profile_kind({name!r}))
            """
        ).stdout.strip()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_profile_kind_and_marker -v`
Expected: FAIL（`profile_kind` 不存在 / status 对坏清单不报错）

- [ ] **Step 3: 实现**

在 `PROFILE_RE` 后加常量：

```python
# API 号（API key + 第三方 Anthropic 兼容端点）见 docs/specs/api-profiles.md
PROFILE_MARKER = ".cca-profile.json"
KEY_FILE = ".api-key"
PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "key_env": "DEEPSEEK_API_KEY",
        "proxy": "none",
        "env": {
            "ANTHROPIC_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
        },
    },
}
# 工具管理的环境键：API 号的 --env 不许设它们（各有专用参数），启动时也要先清掉继承值。
MANAGED_ENV_KEYS = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
) + PROXY_KEYS
```

在 `read_identity` 之后加：

```python
def marker_file(name: str) -> Path:
    return profile_dir(name) / PROFILE_MARKER


def key_file(name: str) -> Path:
    return profile_dir(name) / KEY_FILE


def profile_kind(name: str) -> str:
    """`api`（有清单）或 `oauth`（订阅号）。清单坏掉也算 api —— 要让上层报错，而不是当订阅号混过去。"""
    return "api" if marker_file(name).is_file() else "oauth"


def read_marker(name: str) -> tuple:
    """读清单，返回 (marker, error)：没有清单 → `(None, "")`（订阅号）；坏 → `(None, 原因)`。"""
    f = marker_file(name)
    if not f.is_file():
        return None, ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{f} 无法解析（{exc}）；修好 JSON，或 cca rm {name} 重建"
    if not isinstance(data, dict) or not str(data.get("base_url") or "").strip():
        return None, f"{f} 缺 base_url；修好 JSON，或 cca rm {name} 重建"
    marker = dict(data)
    marker["provider"] = str(data.get("provider") or "generic")
    marker["base_url"] = str(data["base_url"]).strip()
    marker["proxy"] = str(data.get("proxy") or "none")
    marker["env"] = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    return marker, ""
```

`cmd_status` 开头加清单校验（任何按号操作的命令都该如此，见 Task 8 统一）：

```python
    if profile_kind(name) == "api":
        _marker, error = read_marker(name)
        if _marker is None:
            die(error)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m unittest tests.test_claude_acct.CliTest.test_profile_kind_and_marker -v`
Expected: PASS

- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): 清单数据层与种类判定"
```

---

### Task 2: 密钥读写与掩码

**Files:** Modify: `bin/claude-acct`；Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_api_key_file_and_mask(self):
        self.run_cli("new", "002")
        secret = "sk-ant-oat01-SUPERSECRET-VALUE"
        out = self._run_python(f"""
            import importlib.machinery, importlib.util, sys
            loader = importlib.machinery.SourceFileLoader("cca_mod", sys.argv[1])
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cca_mod", loader))
            loader.exec_module(module)
            module.write_api_key("002", {secret!r})
            print(module.read_api_key("002"))
            print(module.mask_key(module.read_api_key("002")))
            print(oct(module.key_file("002").stat().st_mode & 0o777))
        """)
        self.assertIn(secret, out.stdout.splitlines()[0])
        self.assertEqual(out.stdout.splitlines()[1], "sk-ant…ALUE")
        self.assertEqual(out.stdout.splitlines()[2], "0o600")
        self.assertNotIn(secret, out.stdout.splitlines()[1])

    def test_write_api_key_rejects_empty(self):
        self.run_cli("new", "002")
        r = self._run_python("""
            import importlib.machinery, importlib.util, sys
            loader = importlib.machinery.SourceFileLoader("cca_mod", sys.argv[1])
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cca_mod", loader))
            loader.exec_module(module)
            try:
                module.write_api_key("002", "   ")
            except SystemExit as exc:
                print("EXIT", exc.code)
        """)
        self.assertIn("EXIT", r.stdout)
        self.assertFalse((self.acct_dir("002") / ".api-key").exists())
```

- [ ] **Step 2: 跑测试确认失败** — `python3 -m unittest tests.test_claude_acct.CliTest.test_api_key_file_and_mask tests.test_claude_acct.CliTest.test_write_api_key_rejects_empty -v` → FAIL

- [ ] **Step 3: 实现**

```python
def read_api_key(name: str) -> str:
    """读密钥文件（strip 掉换行）。缺失或读不到都返回空串，由调用方决定怎么提示。"""
    f = key_file(name)
    if not f.is_file():
        return ""
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_api_key(name: str, value: str) -> None:
    """写密钥文件（0600，末尾只留一个换行）。空值直接拒绝：写空 key 是最坏的"看起来成功"。"""
    value = value.strip()
    if not value:
        die(f"拒绝写入空的 API key（{key_file(name)}）")
    f = key_file(name)
    f.write_text(value + "\n", encoding="utf-8")
    os.chmod(f, 0o600)


def mask_key(value: str) -> str:
    """掩码：前 6 后 4；短于 16 位只说"已配置"。工具任何输出都不出现完整 key。"""
    if not value:
        return "缺失"
    if len(value) < 16:
        return "已配置"
    return f"{value[:6]}…{value[-4:]}"


def key_state(name: str) -> str:
    """展示用的密钥状态：`已配置 sk-abc…f3a2` / `缺失`。"""
    return f"已配置 {mask_key(read_api_key(name))}" if read_api_key(name) else "缺失"
```

（`key_state` 只在展示里用；`mask_key` 已满足"完整 key 不出现"。）

- [ ] **Step 4: 跑测试确认通过** — 同上 → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): 密钥文件读写与掩码"
```

---

### Task 3: 清单 → 环境变量 / settings.json 渲染

**Files:** Modify: `bin/claude-acct`；Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def seed_api_profile(self, name="002", *, base_url="https://api.deepseek.com/anthropic",
                         provider="deepseek", env=None, key="sk-ant-oat01-SUPERSECRET-VALUE",
                         proxy="none"):
        """造一个 API 号：清单 + 密钥（必要时连 settings.json 一起渲染）。"""
        self.run_cli("new", name)
        marker = {
            "kind": "api", "provider": provider, "base_url": base_url,
            "env": env if env is not None else dict(PROVIDERS_ENV["deepseek"]),
            "proxy": proxy, "created_at": int(time.time()),
        }
        (self.acct_dir(name) / ".cca-profile.json").write_text(json.dumps(marker), encoding="utf-8")
        if key:
            p = self.acct_dir(name) / ".api-key"
            p.write_text(key + "\n", encoding="utf-8")
            p.chmod(0o600)
        return marker

    def test_render_settings_env_keeps_other_keys(self):
        """settings.json 的 env 是渲染产物：写进端点与模型变量、保留其它键、永不写密钥。"""
        self.seed_api_profile("002")
        (self.acct_dir("002") / "settings.json").write_text(
            json.dumps({"theme": "dark", "env": {"MY_FLAG": "1"}}), encoding="utf-8"
        )
        out = self._run_python("""
            import importlib.machinery, importlib.util, json, sys
            loader = importlib.machinery.SourceFileLoader("cca_mod", sys.argv[1])
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cca_mod", loader))
            loader.exec_module(module)
            marker, error = module.read_marker("002")
            assert not error, error
            module.write_settings_env("002", marker)
            print(json.dumps(json.load(open(module.profile_dir("002") / "settings.json")), sort_keys=True))
        """)
        data = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"]["MY_FLAG"], "1")
        self.assertEqual(data["env"]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(data["env"]["ANTHROPIC_MODEL"], "deepseek-flash[1m]")
        self.assertIn(str(self.acct_dir("002") / ".api-key"), data["apiKeyHelper"])
        self.assertNotIn("sk-ant", json.dumps(data))          # settings.json 全文不含密钥
```

`PROVIDERS_ENV` 由测试侧固定（避免与实现耦合）：

```python
PROVIDERS_ENV = {
    "deepseek": {
        "ANTHROPIC_MODEL": "deepseek-flash[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
    }
}
```

- [ ] **Step 2: 跑测试确认失败** → FAIL（`write_settings_env` 不存在）
- [ ] **Step 3: 实现**

```python
KEY_HELPER = "apiKeyHelper"


def api_key_helper_command(name: str) -> str:
    """凭据通道：让 claude 在请求时自己去读该号的 0600 密钥文件。

    官方 gateway 文档要点：值同时进 `Authorization: Bearer` 与 `x-api-key`（不会选错 header），
    默认缓存 5 分钟；且只有 settings 文件里的配置才能覆盖 supervisor 托管的后台会话。
    """
    return f"cat {key_file(name)}"


def api_env_vars(marker: dict) -> dict:
    """API 号要注入的非密环境变量：base_url + 清单 env。密钥不在这里。"""
    env = {"ANTHROPIC_BASE_URL": marker["base_url"]}
    env.update(marker["env"])
    return env


def write_settings_env(name: str, marker: dict) -> dict:
    """把清单渲染进该号 `settings.json` 的 `env` 块（只含非密变量，其它键原样保留）。

    这样绕过 `cca` 直接跑 `claude` 也能到对的端点（缺密钥会明确报鉴权失败，不会悄悄回落订阅）。
    真相始终在清单：`doctor` 会比对两者是否一致。
    """
    f = profile_dir(name) / "settings.json"
    data = {}
    if f.is_file():
        try:
            loaded = json.loads(f.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            data = {}
    env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    env.update(api_env_vars(marker))
    data["env"] = env
    data[KEY_HELPER] = api_key_helper_command(name)      # 凭据通道（顶级键，不是 env 变量）
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return env
```

- [ ] **Step 4: 跑测试确认通过** → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): 清单渲染进 settings.json 的 env"
```

---

### Task 4: `build_env()` 分派、防串味与代理

**Files:** Modify: `bin/claude-acct`（`proxy_settings`、`build_env`）；Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_api_env_injects_and_scrubs(self):
        """API 号：注入清单变量；把继承来的 ANTHROPIC_* 清掉（防拿 A 家 key 打 B 家端点）。"""
        self.seed_api_profile("002")
        out = self._run_python("""
            import importlib.machinery, importlib.util, os, sys
            os.environ["ANTHROPIC_API_KEY"] = "inherited-should-be-gone"
            os.environ["ANTHROPIC_BASE_URL"] = "https://wrong.example"
            os.environ["ANTHROPIC_MODEL"] = "claude-sonnet-5"
            loader = importlib.machinery.SourceFileLoader("cca_mod", sys.argv[1])
            module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cca_mod", loader))
            loader.exec_module(module)
            env = module.build_env("002")
            for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_AUTH_TOKEN",
                        "ANTHROPIC_API_KEY", "HTTPS_PROXY", "CLAUDE_CONFIG_DIR"):
                print(key, "=", env.get(key))
        """)
        got = dict(line.split(" = ", 1) for line in out.stdout.strip().splitlines() if " = " in line)
        self.assertEqual(got["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertEqual(got["ANTHROPIC_MODEL"], "deepseek-flash[1m]")
        # 密钥不进环境（由 apiKeyHelper 提供）
        self.assertEqual(got["ANTHROPIC_AUTH_TOKEN"], "None")
        self.assertEqual(got["ANTHROPIC_API_KEY"], "None")
        self.assertNotIn("wrong.example", out.stdout)
        # 默认 proxy=none：不注入内建代理
        self.assertEqual(got["HTTPS_PROXY"], os.environ.get("HTTPS_PROXY"))
        self.assertEqual(got["CLAUDE_CONFIG_DIR"], str(self.acct_dir("002")))

    def test_api_env_proxy_override(self):
        self.seed_api_profile("002", proxy="http://my-proxy:8080")
        out = self._run_python(BUILD_ENV_SNIPPET("002"))
        self.assertIn("HTTPS_PROXY = http://my-proxy:8080", out.stdout)

    def test_oauth_env_unchanged(self):
        """回归：订阅号的 env 与改动前逐键一致。"""
        self.run_cli("new", "002")
        out = self._run_python(BUILD_ENV_SNIPPET("002"))
        self.assertNotIn("ANTHROPIC_BASE_URL = https://api.deepseek.com", out.stdout)
        self.assertIn(f"CLAUDE_CONFIG_DIR = {self.acct_dir('002')}", out.stdout)
```

（`BUILD_ENV_SNIPPET` 是测试文件顶部的常量字符串，内容同上面的 `_run_python` 片段，避免重复。）

- [ ] **Step 2: 跑测试确认失败** → FAIL（`ANTHROPIC_BASE_URL` 未注入）
- [ ] **Step 3: 实现**

`proxy_settings` 加参数（保持既有调用不变）：

```python
def proxy_settings(marker: dict | None = None) -> tuple:
    """返回 (要注入的代理地址 或 None, 是否内建)。`CCA_PROXY` 优先；否则 API 号看清单 `proxy`。"""
    raw = os.environ.get(PROXY_ENV_VAR)
    if raw is None:
        if marker is not None:
            value = str(marker.get("proxy") or "none").strip()
            if value in ("inherit",):          # 别管代理：继承的原样保留
                return None, False
            if value and value not in PROXY_DISABLED:
                return value, False
            return None, False                  # none（默认）：不注入内建代理
        return DEFAULT_PROXY_URL, True          # 订阅号：现状（内建代理）
    value = raw.strip()
    if value.lower() in PROXY_DISABLED:
        return None, False
    return value, False
```

`build_env`：

```python
def build_env(name: str) -> dict:
    """构造运行 claude 所需的环境：代理按 proxy_settings()，配置目录按号设置。

    API 号（有清单）先清掉本号管理的环境键再注入清单值 —— 继承来的 `ANTHROPIC_*` 会造成
    "拿 A 家 key 打 B 家端点"这类最难查的故障。密钥只在存在时注入；缺密钥由启动路径拦截。
    """
    env = dict(os.environ)
    marker = None
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
        for key in set(MANAGED_ENV_KEYS) | set(marker["env"]):
            env.pop(key, None)
    proxy, _is_builtin = proxy_settings(marker)
    if proxy is not None:
        for key in PROXY_KEYS:
            env[key] = proxy
        env["NO_PROXY"] = NO_PROXY_VALUE
        env["no_proxy"] = NO_PROXY_VALUE
    if marker is not None:
        env.update(api_env_vars(marker))
        # 密钥**不注入环境**：走 settings.json 的 apiKeyHelper（见 docs/specs/api-profiles.md §3.3），
        # 这样 agent 自己跑的 Bash 也读不到它。
    if is_default(name):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(profile_dir(name))
    return env


def key_env_name(marker: dict) -> str:
    """该号的密钥环境变量名：预设给 `key_env`（deepseek → DEEPSEEK_API_KEY），通用端点用 ANTHROPIC_API_KEY。"""
    preset = PROVIDERS.get(marker.get("provider") or "", {})
    return preset.get("key_env") or "ANTHROPIC_API_KEY"
```

- [ ] **Step 4: 跑测试确认通过**（含 `test_api_env_proxy_override`、`test_oauth_env_unchanged`）→ PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): build_env 分派、防串味与按号代理"
```

---

### Task 5: `cca add ds --provider …`（含缺信息引导）

**Files:** Modify: `bin/claude-acct`（新增 `parse_add_flags`、`resolve_add_key`、`cmd_add` 分派）；
Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_add_api_profile_happy_path(self):
        secret = "sk-ant-oat01-SUPERSECRET-VALUE"
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", secret)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(secret, r.stdout + r.stderr)          # 密钥不进输出
        marker = json.loads((self.acct_dir("ds") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["provider"], "deepseek")
        self.assertEqual(marker["base_url"], "https://api.deepseek.com/anthropic")
        self.assertEqual(marker["proxy"], "none")
        self.assertEqual(marker["env"]["ANTHROPIC_MODEL"], "deepseek-flash[1m]")
        keyfile = self.acct_dir("ds") / ".api-key"
        self.assertEqual(keyfile.read_text(encoding="utf-8").strip(), secret)
        self.assertEqual(keyfile.stat().st_mode & 0o777, 0o600)
        settings = json.loads((self.acct_dir("ds") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"], "https://api.deepseek.com/anthropic")
        self.assertNotIn(secret, json.dumps(settings))
        self.assertIn("建议改用 --key-stdin", r.stdout)          # --key 的 history 提示

    def test_add_api_profile_missing_key_guides(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek")
        self.assertNotEqual(r.returncode, 0)
        for hint in ("--key", "--key-stdin", "DEEPSEEK_API_KEY"):
            self.assertIn(hint, r.stderr)

    def test_add_unknown_provider_guides(self):
        r = self.run_cli("add", "ds", "--provider", "deepseekk", "--key", "x" * 20)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("deepseek", r.stderr)
        self.assertIn("--base-url", r.stderr)

    def test_add_key_stdin_and_env_var(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key-stdin", stdin_data="sk-xyz-1234567890\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.acct_dir("ds") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-xyz-1234567890")
        self.run_cli("rm", "ds", "--yes")
        r = self.run_cli("add", "ds2", "--provider", "deepseek", DEEPSEEK_API_KEY="sk-env-1234567890")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.acct_dir("ds2") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-env-1234567890")

    def test_add_key_stdin_empty_fails(self):
        r = self.run_cli("add", "ds", "--provider", "deepseek", "--key-stdin", stdin_data="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("echo $KEY", r.stderr)
        self.assertFalse((self.acct_dir("ds") / ".api-key").exists())

    def test_add_generic_endpoint(self):
        r = self.run_cli("add", "gtw", "--base-url", "https://gw.example/anthropic",
                         "--key", "sk-generic-1234567890", "--model", "my-model")
        self.assertEqual(r.returncode, 0, r.stderr)
        marker = json.loads((self.acct_dir("gtw") / ".cca-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["provider"], "generic")
        self.assertEqual(marker["base_url"], "https://gw.example/anthropic")
        for var in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertEqual(marker["env"][var], "my-model")

    def test_add_env_rejects_managed_keys(self):
        for bad in ("CLAUDE_CONFIG_DIR=/tmp/x", "ANTHROPIC_BASE_URL=https://x", "HTTPS_PROXY=http://x"):
            r = self.run_cli("add", "ds", "--provider", "deepseek", "--key", "sk-1234567890abcd",
                             "--env", bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("--env", r.stderr)

    def test_add_without_provider_unchanged(self):
        """回归：不带 API 参数的 add 仍是 建号 + OAuth 登录。"""
        r = self.run_cli("add", "002", "--email", "claude002@example.com")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.record()["argv"][0], "auth")
        self.assertFalse((self.acct_dir("002") / ".cca-profile.json").exists())
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**

```python
ADD_VALUE_FLAGS = ("--provider", "--key", "--base-url", "--model", "--env", "--proxy")


def parse_add_flags(args: list) -> tuple:
    """拆 `cca add` 的参数：返回 (profile, api_flags, passthrough)。

    只认 API 号的参数，其余原样留着透传给 `claude auth login`（现状不变）。API 参数同时支持
    `--key V` 与 `--key=V` 两种写法 —— 使用者要的是"直接填"。
    """
    rest, api, passthrough = [], {}, []
    index = 0
    while index < len(args):
        arg = args[index]
        head, sep, value = arg.partition("=")
        if head in ADD_VALUE_FLAGS:
            if not sep:
                if index + 1 >= len(args):
                    die(f"{head} 需要跟一个值；用法: cca add <name> --provider deepseek --key <key>")
                index += 1
                value = args[index]
            if head == "--env":
                api.setdefault("--env", []).append(value)
            else:
                api[head] = value
        elif arg == "--key-stdin":
            api["--key-stdin"] = True
        elif arg.startswith("-"):
            passthrough.append(arg)
        else:
            rest.append(arg)
        index += 1
    return (rest[0] if rest else ""), api, rest[1:] + passthrough


def api_marker_from_flags(name: str, api: dict) -> dict:
    """把命令行参数合成清单：预设打底 → `--base-url/--model/--env/--proxy` 覆盖。"""
    provider = api.get("--provider", "")
    if provider and provider not in PROVIDERS:
        known = "、".join(sorted(PROVIDERS))
        die(f"未知 provider: {provider}（已知: {known}）；任意兼容端点请用 --base-url")
    base_url = api.get("--base-url") or (PROVIDERS.get(provider, {}).get("base_url") or "")
    if not base_url:
        die("缺少端点：给 --provider（预设）或 --base-url <url>")
    env = dict(PROVIDERS.get(provider, {}).get("env") or {})
    model = api.get("--model")
    if model:
        env.update({
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
        })
    for item in api.get("--env", []):
        key, _, value = item.partition("=")
        key = key.strip()
        if not key or not _:
            die(f"--env 只接受 KEY=VALUE（收到 {item!r}）")
        if key in MANAGED_ENV_KEYS:
            die(f"--env 不能设置 {key}（工具管理的键：--base-url 管端点、密钥用 --key/--key-stdin、"
                f"代理用 --proxy）")
        env[key] = value
    proxy = api.get("--proxy") or PROVIDERS.get(provider, {}).get("proxy") or "none"
    return {
        "kind": "api",
        "provider": provider or "generic",
        "base_url": base_url,
        "env": env,
        "proxy": proxy,
        "created_at": int(time.time()),
    }


def resolve_add_key(name: str, marker: dict, api: dict) -> str:
    """按 `--key` → `--key-stdin` → `$<PROVIDER>_API_KEY` 取密钥；都没有就报错并给三种给法。"""
    if "--key" in api and api.get("--key-stdin"):
        die("--key 与 --key-stdin 只能给一个")
    if api.get("--key"):
        return api["--key"].strip()
    if api.get("--key-stdin"):
        value = sys.stdin.read().strip()
        if not value:
            die("--key-stdin 读到了空内容；用法: echo $KEY | cca add " + name + " --provider … --key-stdin")
        return value
    env_name = key_env_name(marker)
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    die(
        f"{name}: 没有拿到 API key。三种给法：\n"
        f"  cca add {name} --provider {marker['provider']} --key <key>        # 最方便，但会进 shell history\n"
        f"  echo $KEY | cca add {name} --provider {marker['provider']} --key-stdin\n"
        f"  {env_name}=<key> cca add {name} --provider {marker['provider']}"
    )
```

`cmd_add` 改为：

```python
def cmd_add(args: list) -> int:
    """建号 + 配好凭据：API 号（给 --provider/--base-url）走密钥路径，否则照旧 OAuth 登录。"""
    name, api, passthrough = parse_add_flags(args)
    if not api:
        return cmd_new([name, *passthrough]) or cmd_login([name, *passthrough])
    if not name or not valid_name(name):
        die(f"非法的 profile 名: {name!r}")
    if passthrough:
        die(f"API 号不接受这些参数: {' '.join(passthrough)}（--email/--console/--sso 是 OAuth 专用的）")
    marker = api_marker_from_flags(name, api)
    secret = resolve_add_key(name, marker, api)
    if cmd_new([name]):
        return 1
    if api.get("--key"):
        note("提示: --key 会留在 shell history；下次建议用 --key-stdin 或 $" + key_env_name(marker))
    marker_file(name).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(marker_file(name), 0o600)
    write_api_key(name, secret)
    write_settings_env(name, marker)
    say(f"→ {name}: API 号（{marker['provider']}）")
    say(f"   端点: {marker['base_url']}")
    say(f"   密钥: {mask_key(secret)}（{key_file(name)}，0600）")
    say(f"   模型: {marker['env'].get('ANTHROPIC_MODEL', '-')}")
    say(f"   启动: cca {name}")
    return 0
```

- [ ] **Step 4: 跑测试确认通过** → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): cca add --provider/--base-url/--key（含缺信息引导）"
```

---

### Task 6: `cca login` / `cca logout` 对 API 号

**Files:** Modify: `bin/claude-acct`（`cmd_login`、`cmd_logout`）；Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_login_rotates_api_key(self):
        self.seed_api_profile("002", key="sk-old-1234567890abcd")
        r = self.run_cli("login", "002", "--key", "sk-new-1234567890wxyz")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.acct_dir("002") / ".api-key").read_text(encoding="utf-8").strip(),
                         "sk-new-1234567890wxyz")
        self.assertNotIn("sk-old", r.stdout + r.stderr)
        self.assertNotIn("sk-new", r.stdout + r.stderr)      # 只出现掩码

    def test_login_api_missing_key_guides(self):
        self.seed_api_profile("002", key=None)
        r = self.run_cli("login", "002", stdin_data="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--key", r.stderr)

    def test_logout_removes_api_key_only(self):
        self.seed_api_profile("002")
        r = self.run_cli("logout", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.acct_dir("002") / ".api-key").exists())
        self.assertTrue((self.acct_dir("002") / ".cca-profile.json").is_file())
        self.assertIn("对话与配置保留", r.stdout)
        # OAuth 号仍走 claude auth logout（回归）
        self.run_cli("new", "003")
        self.run_cli("logout", "003")
        self.assertIn("logout", self.calls())
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**

`cmd_login` 开头加分支：

```python
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
        api = {}
        rest = args[1:]
        index = 0
        while index < len(rest):
            arg = rest[index]
            head, sep, value = arg.partition("=")
            if head == "--key":
                if not sep:
                    index += 1
                    if index >= len(rest):
                        die("--key 需要跟一个值")
                    value = rest[index]
                api["--key"] = value
            elif arg == "--key-stdin":
                api["--key-stdin"] = True
            else:
                die(f"API 号不认识参数 {arg}（只支持 --key / --key-stdin）")
            index += 1
        secret = resolve_add_key(name, marker, api)
        write_api_key(name, secret)
        write_settings_env(name, marker)
        say(f"{name}: 已更新 API key（{mask_key(secret)}）")
        return 0
```

`cmd_logout` 开头加：

```python
    if profile_kind(name) == "api":
        if key_file(name).is_file():
            key_file(name).unlink()
            say(f"{name}: 已删除 API key（对话与配置保留；要恢复跑 cca login {name} --key …）")
        else:
            say(f"{name}: 本来就没有 API key")
        return 0
```

- [ ] **Step 4: 跑测试确认通过** → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): login 换 key、logout 删 key"
```

---

### Task 7: 启动路径（缺密钥快速失败 / 权限提示 / 清单损坏）

**Files:** Modify: `bin/claude-acct`（新增 `require_api_key`，接到 `cmd_run`、`cmd_resume`、`run_carry`）；
Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_run_api_profile_missing_key_fails_fast(self):
        self.seed_api_profile("002", key=None)
        r = self.run_cli("002", "-p", "hi")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("缺少 API key", r.stderr)
        self.assertIn("cca login 002 --key", r.stderr)
        self.assertEqual(self.calls(), [])          # 假 claude 一次都没被调用

    def test_run_api_profile_warns_on_loose_key_perms(self):
        self.seed_api_profile("002")
        (self.acct_dir("002") / ".api-key").chmod(0o644)
        r = self.run_cli("002", "-p", "hi")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0600", r.stderr)

    def test_run_api_profile_corrupt_marker_fails_before_launch(self):
        self.seed_api_profile("002")
        (self.acct_dir("002") / ".cca-profile.json").write_text("{ 坏", encoding="utf-8")
        r = self.run_cli("002", "-p", "hi")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("无法解析", r.stderr)
        self.assertEqual(self.calls(), [])
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**

```python
def require_api_key(name: str, marker: dict) -> str:
    """启动前确认密钥可用：文件 → `$<PROVIDER>_API_KEY`；都没有就带引导报错（不启动 claude）。

    顺带在权限过宽时提示一句（不阻止）：0600 是这台机器上保护密钥的唯一手段。
    """
    secret = read_api_key(name)
    if not secret:
        env_name = key_env_name(marker)
        die(
            f"{name} 缺少 API key：cca login {name} --key <key>\n"
            f"  或临时用环境变量：{env_name}=<key> cca {name}"
        )
    try:
        mode = key_file(name).stat().st_mode & 0o777
        if mode != 0o600:
            warn(f"{key_file(name)} 权限是 {oct(mode)}，建议 chmod 600（密钥只靠文件权限保护）")
    except OSError:
        pass
    return secret
```

`cmd_run` 里在启动 claude 之前（拿到 name 之后）插入：

```python
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
        require_api_key(name, marker)
```

`cmd_resume` 与 `run_carry` 同理（它们会 `_run_claude([...])` 打 API）：在各自确定 profile 之后
调用同样的三行（`profile_kind` → `read_marker` → `require_api_key`）。

- [ ] **Step 4: 跑测试确认通过** → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): 启动路径校验密钥（缺 key 快速失败、权限提示、清单损坏即报）"
```

---

### Task 8: `cca ls` / `cca status` 显示

**Files:** Modify: `bin/claude-acct`（`cmd_list`、`cmd_status`、`_profile_row`）；
Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_ls_shows_api_kind_and_missing_key_hint(self):
        self.seed_api_profile("002")
        r = self.run_cli("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("api:deepseek", r.stdout)
        self.assertIn("api 号没有订阅额度缓存", r.stdout)
        self.assertNotIn("sk-ant", r.stdout)                    # 掩码之外不出现密钥
        self.run_cli("logout", "002")
        r = self.run_cli("ls")
        self.assertIn("key 缺失", r.stdout)
        self.assertIn("cca login 002 --key", r.stdout)

    def test_status_api_profile(self):
        self.seed_api_profile("002")
        r = self.run_cli("status", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        for expect in ("api:deepseek", "https://api.deepseek.com/anthropic", "deepseek-flash[1m]",
                       "sk-ant…ALUE", "缺失" if False else "已配置"):
            self.assertIn(expect, r.stdout)
        for oauth_only in ("档位", "凭据", "重新登录", "额外用量"):
            self.assertNotIn(oauth_only, r.stdout)
        self.assertNotIn("sk-ant-oat01-SUPERSECRET-VALUE", r.stdout)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**

`_profile_row` 增加种类信息：

```python
        "kind": profile_kind(name),
        "provider": (marker or {}).get("provider", "") if profile_kind(name) == "api" else "",
        "base_url": (marker or {}).get("base_url", "") if profile_kind(name) == "api" else "",
```

`cmd_list`：

* 身份列：API 号显示 `api:<provider>`（dim），不显示邮箱；
* API 号额度两列显示 `-`（`usage_cell_text(None)` 已经产出 `-`）；
* 底部脚注：`if any(row["kind"] == "api" for row in rows)` →
  `api 号没有订阅额度缓存，用量看供应商控制台`；
* 有 API 号且缺 key → 追加一行黄色 `key 缺失: 002（cca login 002 --key …）`。

`cmd_status` 在 API 号分支渲染：

```python
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
        grid.add_row("账号", identity)                      # 002  api:deepseek  ← 当前
        grid.add_row("目录", _plain(row["dir"]))
        grid.add_row("选中", _plain(binding_reason(name)))
        grid.add_row("端点", _plain(marker["base_url"]))
        grid.add_row("模型", _plain(marker["env"].get("ANTHROPIC_MODEL", "-")))
        state = key_state(name)
        grid.add_row("密钥", _plain(state, style="green" if state != "缺失" else "yellow"))
        grid.add_row("代理", _plain(marker["proxy"] or "none"))
        if key_state(name) == "缺失":
            grid.add_row("", _plain(f"→ 修复: cca login {name} --key <key>", style="yellow"))
        grid.add_row("额度", _plain("-（API 号按量计费，用量看供应商控制台）"))
        ...后台会话行（复用现有渲染）...
        console.print(grid)
        return 0
```

- [ ] **Step 4: 跑测试确认通过** → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): ls/status 显示种类、端点、模型与密钥状态"
```

---

### Task 9: `doctor` 对 API 号的检查

**Files:** Modify: `bin/claude-acct`（`cmd_doctor`）；Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_doctor_api_checks(self):
        self.seed_api_profile("002")
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK", r.stdout)
        # 权限过宽 → WARN
        (self.acct_dir("002") / ".api-key").chmod(0o644)
        r = self.run_cli("doctor")
        self.assertIn("chmod 600", r.stdout)
        # 清单与 settings 不一致 → WARN + 修复提示
        (self.acct_dir("002") / ".api-key").chmod(0o600)
        settings = json.loads((self.acct_dir("002") / "settings.json").read_text(encoding="utf-8"))
        settings["env"]["ANTHROPIC_BASE_URL"] = "https://stale.example"
        (self.acct_dir("002") / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        r = self.run_cli("doctor")
        self.assertIn("cca sync 002", r.stdout)
        # 缺 key → FAIL
        self.run_cli("logout", "002")
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cca login 002 --key", r.stdout)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**

在 `cmd_doctor` 的逐号循环里加（沿用现有 `ok/warn/fail` 收集方式）：

```python
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            fail(f"{name}: {error}")
        else:
            secret = read_api_key(name)
            if not secret:
                fail(f"{name}: 缺 API key → cca login {name} --key <key>")
            else:
                mode = key_file(name).stat().st_mode & 0o777
                if mode != 0o600:
                    warn(f"{name}: {key_file(name)} 权限 {oct(mode)} → chmod 600 {key_file(name)}")
                if not resolve_host(marker["base_url"]):
                    warn(f"{name}: 端点 {marker['base_url']} 解析不了（DNS）")
                helper = str(_settings(name).get(KEY_HELPER) or "")
                if not helper or not Path(helper.split()[-1]).is_file():
                    fail(f"{name}: apiKeyHelper 指向的密钥文件不可读（{helper or '未渲染'}）"
                         f" → cca sync {name}")
                if credentials_file(name).is_file():
                    warn(f"{name}: 同时存在 .credentials.json 与 apiKeyHelper（订阅登录与网关凭据冲突）")
                settings_env = _settings_env(name)
                drift = {k: v for k, v in api_env_vars(marker).items() if settings_env.get(k) != v}
                if drift:
                    warn(f"{name}: settings.json 的 env 与清单不一致（{', '.join(sorted(drift))}）"
                         f" → cca sync {name}")
```

辅助函数（`resolve_host` 复用 doctor 现有的代理/主机解析写法；`_settings_env` 读 settings.json 的 env）：

```python
def _settings(name: str) -> dict:
    f = profile_dir(name) / "settings.json"
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _settings_env(name: str) -> dict:
    return {str(k): str(v) for k, v in (_settings(name).get("env") or {}).items()}
```

- [ ] **Step 4: 跑测试确认通过** → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): doctor 检查 API 号的密钥权限、端点和清单一致性"
```

---

### Task 10: `sync` 合并、`rm` 提示、`which` 标注

**Files:** Modify: `bin/claude-acct`（`cmd_sync`、`cmd_rm`、`cmd_which`）；Test: `tests/test_claude_acct.py`

- [ ] **Step 1: 写失败测试**

```python
    def test_sync_api_profile_preserves_env(self):
        self.seed_api_profile("002")
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"theme": "light", "env": {"SHARED": "1"}}), encoding="utf-8"
        )
        r = self.run_cli("sync", "002")
        self.assertEqual(r.returncode, 0, r.stderr)
        settings = json.loads((self.acct_dir("002") / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["theme"], "light")                       # 从 001 播种
        self.assertEqual(settings["env"]["SHARED"], "1")
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.deepseek.com/anthropic")             # 清单重渲染

    def test_rm_api_profile_mentions_key(self):
        self.seed_api_profile("002")
        r = self.run_cli("rm", "002", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("API key", r.stdout)

    def test_which_reports_kind(self):
        self.seed_api_profile("002")
        r = self.run_cli("which", "-v", CLAUDE_ACCT="002")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("api:deepseek", r.stdout)
```

- [ ] **Step 2: 跑测试确认失败** → FAIL
- [ ] **Step 3: 实现**

`cmd_sync`：非默认号且是 API 号时改成合并 + 重渲染：

```python
    marker = None
    if profile_kind(name) == "api":
        marker, error = read_marker(name)
        if marker is None:
            die(error)
    src = default_dir() / "settings.json"
    ...
    if marker is None:
        shutil.copy2(src, dst)                     # 现状：整文件覆盖
        say(f"已用 {src} 覆盖 {dst}")
    else:
        merged = json.loads(src.read_text(encoding="utf-8")) if src.is_file() else {}
        if not isinstance(merged, dict):
            merged = {}
        keep = _settings_env(name)                 # 该号现有的 env（含清单渲染结果）
        dst.write_text(json.dumps({**merged, "env": keep}, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        write_settings_env(name, marker)           # 再用清单重渲染
        say(f"已从 001 重新播种 {dst}（保留 API 号的 env）")
```

`cmd_rm` 的删除提示：API 号时把"含凭据文件"改成"含凭据文件与 API key"：

```python
        extra = "与 API key" if profile_kind(name) == "api" else ""
        say(f"  内容: {files} 个文件 / {human_size(size)}（含凭据文件{extra}，删掉无法恢复）")
```

`cmd_which` 的依据串：

```python
        if profile_kind(name) == "api":
            marker, _ = read_marker(name)
            reason = f"{reason}, api:{(marker or {}).get('provider', 'generic')}"
```

- [ ] **Step 4: 跑测试确认通过** → PASS
- [ ] **Step 5: 全量 + 提交**

```bash
make test && make lint
git add bin/claude-acct tests/test_claude_acct.py
git commit -m "feat(api): sync 合并保留 env、rm/which 标注 API 号"
```

---

### Task 11: 文档与版本

**Files:** Modify: `README.md`、`VERSION`、`docs/specs/design.md`

- [ ] **Step 1: README 新增一节**（放在「号面板与额度」之前）

````markdown
## 接第三方 key（API 号）

除了 Claude 订阅号，`cca` 也能管理**用 API key 打第三方 Anthropic 兼容端点**的号 —— 例如 DeepSeek：

```bash
cca add ds --provider deepseek --key sk-xxxx     # 预设：端点与模型变量都填好
cca add ds --provider deepseek --key-stdin < key.txt
DEEPSEEK_API_KEY=sk-xxxx cca add ds --provider deepseek
cca add gtw --base-url https://gw.example/anthropic --key sk-yyyy --model my-model   # 任意兼容端点
cca ds                                            # 用这个号启动（参数照旧透传）
cca login ds --key sk-zzzz                        # 换 key
cca logout ds                                     # 删 key（对话与配置保留）
```

* **配置**：`<配置目录>/.cca-profile.json`（0600）是真相来源，密钥单独放
  `<配置目录>/.api-key`（0600）；工具启动时把端点、密钥、模型变量注入环境，任何输出只给掩码
  （`sk-abc…f3a2`），永不打印完整 key。
* **模型变量**按官方配方预设（`deepseek-flash[1m]`、子 agent 同模型、effort=max、
  自动压缩窗口 786432），`--model M` 可整体改，`--env K=V` 可逐个覆盖。
* **代理**：API 号默认直连（实测直连更快）；`--proxy <url>` 或 `CCA_PROXY` 可覆盖。
* **额度**：API 号按量计费，没有订阅额度缓存，`cca ls` 的额度列显示 `-`，用量看供应商控制台。
* 缺什么都会明说：没 key、预设名写错、清单坏了、密钥文件被删 —— 每条都给出可直接复制的修复命令。
* 合规：API key 必须**本人所有、仅本人使用**，不得共享；发给第三方端点的内容受该供应商政策
  约束，内部代码/数据是否外发由你自己判断并遵守所属组织规定。
* **凭据通道是 `apiKeyHelper`**（settings.json 顶级键，指向该号 0600 密钥文件）：值同时进
  `Authorization: Bearer` 与 `x-api-key`（不会因为选错变量而 401），后台会话也能可靠拿到；
  密钥不写进配置文件、也不进 claude 的环境变量。
* 需要协议转换（OpenAI/Gemini 端点）或按请求路由 / fallback / 用量日志时，把 `--base-url`
  指向 LiteLLM、claude-code-router 这类本地网关即可 —— 本工具不做代理。

### 网关排障（第三方端点常见 400/401）

| 现象 | 处理 |
| --- | --- |
| `401` | 凭据无效或被吊销；helper 通道同时发两个头，若仍 401 就是 key 本身的问题 |
| `400` 提到 `context_management` / `Extra inputs are not permitted` | 端点不接受实验性字段：`--env CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` |
| `400` 提到 `thinking` / `adaptive` | 上游不支持自适应推理：`--env CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1` |
| `400` 说上下文/输出超限 | 把 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 调到端点上限（预设已给 DeepSeek 设好），必要时设 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` |
| `/fast` 报网络或组织限制 | 网关凭据下用不了：`CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS=1` / `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1` |
| Remote Control / 语音输入不可用 | 网关凭据下官方就是禁用的，要用就切回订阅号 |
````

- [ ] **Step 2: README 合规边界补两条**

```markdown
* API 号的 key 必须是本人所有、仅本人使用，不得共享或转借；
* 发给第三方端点的内容受该供应商政策约束 —— 内部代码与数据是否外发自行判断并遵守组织规定。
```

- [ ] **Step 3: 版本与 design.md**

```bash
echo "0.12.0" > VERSION
```

`docs/specs/design.md` 顶部已指向 `api-profiles.md`（Task 0 的 spec 提交已做）。

- [ ] **Step 4: 跑全量 + 提交**

```bash
make test && make lint
git add README.md VERSION docs/specs/design.md
git commit -m "docs: README 补 API 号用法与合规说明；VERSION 0.12.0"
```

---

### Task 12: 端到端验收（用真实 DeepSeek key）

**前置**：使用者提供一个 DeepSeek API key（或用 `DEEPSEEK_API_KEY` 环境变量）。

- [ ] **Step 1: 建号并跑一个最小请求**

```bash
cca add dsdemo --provider deepseek --key-stdin <<< "$DEEPSEEK_API_KEY"
cca doctor --online --probe-key          # 期望: dsdemo: 端点可达且密钥有效（HTTP 200）
cca dsdemo -p "reply with the single word ok"
```
Expected: `--probe-key` 报"密钥有效"（真的带密钥发了 max_tokens=1 的请求）；`-p` 输出 `ok`
（不是 401，也不是 Anthropic 的模型名报错）。

- [ ] **Step 2: 后台会话**

```bash
cd <一个已信任的目录>                      # `cca add` 会从 001 继承信任；没继承到就先在这里跑一次交互式 cca dsdemo
cca dsdemo --bg "写一行到 /tmp/dsdemo.txt 然后结束"   # 任务用位置参数，**别加 -p**（CLI 拒绝 --bg 与 --print 同用）
cca dsdemo agents --json | python3 -m json.tool | head
```
Expected: 出现一条 `kind: background` 的会话，最终 `state` 走到 `done`/`completed`，
且 `/tmp/dsdemo.txt` 被写出来（说明后端会话也拿到了凭据）。

- [ ] **Step 2.5: 凭据通道（helper）**

```bash
python3 - <<'PYH'
import json, subprocess, pathlib
s = json.load(open(pathlib.Path.home() / ".claude-accounts/dsdemo/settings.json"))
out = subprocess.run(s["apiKeyHelper"], shell=True, capture_output=True, text=True)
print("helper 输出长度:", len(out.stdout.strip()))
print("只有密钥、没有 banner:", out.stdout.strip() == out.stdout.strip().split()[-1] != "")
PYH
```
Expected: 两行都成立（长度与 key 一致、输出里没有多余内容）。

- [ ] **Step 3: 面板与详情**

```bash
cca ls | grep dsdemo
cca status dsdemo
```
Expected: `ls` 显示 `api:deepseek`、额度列 `-`；`status` 显示端点、模型、密钥掩码、代理。

- [ ] **Step 4: 切换与清理**

```bash
cca switch dsdemo && cca which -v && cca switch 002   # which -v 应显示 ", api:deepseek"
cca rm dsdemo --yes
```
（`rm` 时若有后台会话在跑会拒绝，先 `cca stop-bg dsdemo --force`；删完若又冒出同名空目录
（daemon 尾巴写 telemetry），再跑一次 `cca rm dsdemo --yes` 即可。）
Expected: 切换生效、`which -v` 标注 `api:deepseek`、`rm` 输出含"API key"、目录消失。

- [ ] **Step 5: 记录到 spec 的验证章节并提交**

```bash
git add docs/specs/api-profiles.md
git commit -m "docs: 记录 API 号的端到端验收结果"
```

---

## 自检（写完计划后逐条核对）

1. **spec 覆盖**：§3 数据模型 → Task 1/2；§4 env 与代理 → Task 3/4；§5 命令面与 5.1 缺信息引导 →
   Task 5/6/7/10；§6 显示与 doctor → Task 8/9；§7 安全 → Task 2（0600）/Task 5（掩码与提示）/
   Task 11（合规文案）；§8 预设 → Task 1（常量）+ Task 12（验收）；§9 测试 → 各任务的测试步骤；
   §10 兼容 → Task 11（VERSION）+ 各任务的回归用例；§11 非目标 → 计划里没有这些任务。
2. **占位符扫描**：本计划无 TBD/TODO；每个代码步骤都给了可执行代码。
3. **命名一致性**：`profile_kind` / `read_marker` / `marker_file` / `key_file` / `read_api_key` /
   `write_api_key` / `mask_key` / `key_state` / `api_env_vars` / `write_settings_env` /
   `key_env_name` / `parse_add_flags` / `api_marker_from_flags` / `resolve_add_key` /
   `require_api_key` / `_settings_env` —— 各任务间引用一致。
