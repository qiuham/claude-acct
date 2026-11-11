# API 号（第三方 Anthropic 兼容端点）

关联：`design.md`（订阅号的数据模型、共享与隔离矩阵、串行守护）。本文只写"API 号"这块新子系统。

状态：设计已评审（2026-09-25），等待实施。

## 1. 背景与目标

现状：一个 profile = 一份 Claude 订阅（OAuth），凭据在 `<配置目录>/.credentials.json`，
身份与额度分别来自 `.claude.json` 的 `oauthAccount` / `cachedUsageUtilization`。

目标：让同一个工具也能管理**用 API key 打第三方 Anthropic 兼容端点**的号 —— 例如 DeepSeek
（`https://api.deepseek.com/anthropic`），并能接任意兼容端点（kimi / glm / 本地 vLLM…）。

做到：`cca add ds --provider deepseek --key sk-…` 之后，`cca ds`、`cca ds --bg …`、
`cca ls`、`cca carry`、`cca sessions` 都像订阅号一样可用。

非目标见 §11。

## 2. 术语

| 词 | 含义 |
| --- | --- |
| 订阅号 | 现有形态：OAuth 订阅凭据，`kind=oauth`（隐式，没有清单文件） |
| API 号 | 新形态：API key + 第三方兼容端点，`kind=api`，有清单文件 |
| 清单 | `<配置目录>/.cca-profile.json`，工具自有的号元数据（真相来源） |
| 预设 | 内置的 provider 参数表（当前只有 `deepseek`），可用命令行参数覆盖 |

## 3. 数据模型

### 3.1 种类判定

`profile_kind(name)`：读到清单就 `api`，否则 `oauth`。只此一处判断，其余命令都读它。
因此**现有 001/002 零改动**：没有清单文件 = 订阅号，行为与现在完全一致。

### 3.2 清单 `<配置目录>/.cca-profile.json`（0600）

```json
{
  "kind": "api",
  "provider": "deepseek",
  "base_url": "https://api.deepseek.com/anthropic",
  "env": {
    "ANTHROPIC_MODEL": "deepseek-flash[1m]",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash[1m]",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432"
  },
  "proxy": "none",
  "created_at": 1790255000
}
```

* `env` 是**所有非密变量的唯一出口**（预设 + `--env` 覆盖合并后的结果）。`base_url` 单列，
  因为它同时要渲染进 `settings.json`（§4.2）。
* `proxy`：空（默认，不配）| `none` | `inherit` | `<http(s)://url>`。**默认不配代理**；订阅号没配就拒绝启动，
  但把该号端点主机加进 `NO_PROXY`：端点直连（实测 `api.deepseek.com` 直连 70ms、经代理 459ms），
  其余流量（自动更新、遥测、模型目录）照常走代理。
  曾经默认 `none`（完全不注入），结果把**自动更新**也饿死了 —— `downloads.claude.ai` 直连超时，
  CLI 报红字 `Auto-update failed`（实测：直连 timeout / 经代理 200）。非端点流量与端点流量必须分开。
* 写入即 0600；父目录 0700。工具只读它来做展示与注入，不需要 claude 感知。

### 3.3 密钥 `<配置目录>/.api-key`（0600）+ `apiKeyHelper`

一行裸 key，无元数据。**交付通道用 `apiKeyHelper`**（官方 gateway 文档的正解）：

```json
{ "apiKeyHelper": "cat /home/<user>/.claude-accounts/ds/.api-key" }
```

理由（见 §12 的调研对比）：

* gateway 凭据**必须在 settings 文件里配**才能可靠覆盖 supervisor 托管的后台会话（shell export
  到不了那儿）—— 而 helper 让我们既能覆盖，又不把密钥写进配置文件；
* helper 的返回值**同时**放进 `Authorization: Bearer` 与 `x-api-key` 两个头，天然消灭
  "变量选错 → 401"这一整类问题（`ANTHROPIC_AUTH_TOKEN` 只走 Bearer、`ANTHROPIC_API_KEY` 只走
  x-api-key，选错就是 401）；
* 绕开 `ANTHROPIC_API_KEY` 在交互模式下的一次性授权弹窗；
* 换 key 后自动生效（CLI 默认缓存 helper 输出 5 分钟，`CLAUDE_CODE_API_KEY_HELPER_TTL_MS` 可调）。

规则：密钥不进 argv、不进日志、不进 `settings.json`；展示一律掩码；`rm` 时随目录删除
（输出写明"含 API key"）。工具启动路径仍会**预检**密钥（缺失/权限过宽/helper 不可执行都在
启动前说清楚），但**不把密钥注入 claude 的环境** —— 这样 agent 跑的 Bash 也看不到它。

## 4. 环境注入与代理

### 4.1 `build_env()` 分派

```
订阅号：CLAUDE_CONFIG_DIR + 代理（必须配 CCA_PROXY；没配就拒绝启动）
API 号 ：CLAUDE_CONFIG_DIR
        + ANTHROPIC_BASE_URL    ← 清单 base_url
        + 清单 env              ← 模型、effort、自动压缩窗口等
        + 代理按清单 proxy（默认 none；CCA_PROXY 仍可覆盖）
        （密钥不注入环境：走 settings.json 的 apiKeyHelper，见 §3.3）
```

`settings.json` 里同时渲染 `apiKeyHelper`（顶级键，不是 env 变量）—— 这是 API 号的凭据通道。

**防串味**：启动 API 号时，先从继承的环境里删掉本号管理的键
（`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY` 以及清单 `env` 里的每个键），
再注入清单的值。否则 shell 里残留的 `ANTHROPIC_API_KEY` 可能造成"拿 A 家 key 打 B 家端点"
这种最难查的故障。想额外加变量用 `--env`（写在清单里，不是一次性）。

**缺 key 时快速失败**：启动 API 号前按 `密钥文件 → $<PROVIDER>_API_KEY` 顺序预检，
都没有就报错退出并给出给法（`cca login <p> --key …` / `<PROVIDER>_API_KEY=… cca <p>`），
而不是把 claude 启起来撞 `apiKeyHelper failed` 或 401。

### 4.2 `settings.json` 的 env 是渲染产物

`cca add` / `cca login`（换 key 后重渲染）/ `cca sync` 会把清单渲染进该号 `settings.json`：
`env` 块写 `ANTHROPIC_BASE_URL` + 清单 `env` 的全部键，顶级写 `apiKeyHelper` 指向该号密钥文件，
**永不包含密钥本身**。
这样绕过 `cca` 直接跑 `claude`（或 IDE 指向同一配置目录）也能到对端点 —— 只是缺 key 会明确报
鉴权失败，而不会悄悄回落到 Anthropic 订阅。

真相始终在清单；`doctor` 比较两者，不一致给 WARN 并提示 `cca sync <profile>` 重新渲染
（sync 对 API 号是"合并 + 重渲染"，见 §5）。

## 5. 命令面

| 命令 | API 号行为 | 订阅号（回归要求） |
| --- | --- | --- |
| `cca add <name> [--provider <preset>] [--url U\|--base-url U] [--key V\|--key-stdin] [--model M] [--env K=V]… [--proxy P]`（`cca add --help` 打印配方）；<br>**已存在的 API 号 = 增量更新**：只改这次给的字段并列出 diff（`--env K=` 空值 = 删该变量） | 建目录 + 共享软链（同 `new`）+ 写清单 + 写密钥 + 渲染 settings env + 继承 001 的目录信任与 `hasCompletedOnboarding` | 不带 `--provider` 时**完全不变**：`add` = `new` + OAuth 登录 |
| `cca add <name> --base-url U --key …` | 无预设的通用端点，`provider=generic` | —— |
| `cca login <api号> [--key V\|--key-stdin]` | 换 key（重写 `.api-key`）+ 重渲染 settings（`apiKeyHelper` 指向同一文件，内容已换）；不带参数时按 `--key-stdin` → `$<PROVIDER>_API_KEY` 取 | 现状：`claude auth login` |
| `cca logout <api号>` | 删 `.api-key`（清单、配置、对话都保留） | 现状 |
| `cca <api号> [claude 参数…]` | 注入 env 后启动，参数原样透传 | 不变 |
| `cca ls` | 该号身份列显示 `api:deepseek`；额度列 `-` | 不变 |
| `cca status <api号>` | 账号 / 目录 / 选中 / 端点 / 模型 / key（掩码）/ 代理 / 额度(`-`) / 后台会话 | 不变 |
| `cca sync <api号>` | **合并**：读 `~/.cca/defaults/settings.json`，覆盖除 `env` 以外的键，再用清单重渲染 `env`（不再整文件覆盖） | 现状：整文件覆盖 |
| `cca doctor` | 见 §6.3 | 不变 |
| `cca rm <api号>` | 现状（删整个配置目录），输出写明"含 API key" | 不变 |
| `cca carry <源号> <目标号>` | 照旧（会话是本地 JSONL，与 provider 无关）；跨种类（订阅号 ↔ API 号）搬运允许，但打一行提示；**搬运"等输入"的会话时额外提示：面板登记只在 CLI daemon 存活期间有效**（实测 daemon 空闲退出会把它 settled(killed)，对话不受影响；`--bg --resume <id>` 可重新登记） | 不变 |
| `cca sessions` / `which` | 照旧；`cca sessions` 不写号（会话不绑账号），`cca which <号>` 一行给出类型与端点 | 不变 |

### 5.1 缺信息不静默：每一处都要给出"下一步"

原则：**任何一步缺信息，都要说清缺什么、并给一条能直接复制去跑的命令**，而不是抛一句
"失败"或悄悄用一个错值顶上去。

| 场景 | 行为 |
| --- | --- |
| `cca add ds --provider deepseek`，三种 key 来源都没有 | 退出码 1；错误里并列给三条可复制的命令（`--key` / `--key-stdin` / `$DEEPSEEK_API_KEY`），并注明 `--key` 会进 shell history |
| `--provider` 名字不认识（如 `deepseekk`） | 报错 + 列出已知预设（`deepseek`）+ 提示"任意兼容端点可用 `--base-url`"（不做模糊匹配，避免猜错供应商） |
| 给了 `--base-url` 但没 key | 同上；并说明该号会以 `provider=generic` 记录 |
| `--key-stdin` 但 stdin 为空 | 报错并给用法示例（`echo $KEY \| cca add …`）；**不写空密钥文件** |
| 启动时密钥文件缺失（被删/被清） | `cca ds` **快速失败**：`ds 缺少 API key：cca login ds --key … 或 DEEPSEEK_API_KEY=… cca ds`；不把 claude 启起来撞 401 |
| 密钥文件权限过宽（非 0600） | 启动时打一行 WARN（不阻止），`doctor` 给 WARN 并给出 `chmod 600 <路径>` |
| 清单损坏（JSON 不合法 / `base_url` 为空） | 启动前 FAIL：指出路径与原因，并给两条出路（修 JSON，或 `cca rm ds` 重建） |
| `settings.json` 的 `env` 与清单不一致 | 启动不阻止；`cca status ds` 与 `doctor` 各给一行提示 + `cca sync ds` |
| `ls` / `status` 里出现 key 缺失的号 | 状态行/身份列标 `key 缺失`（黄色），脚注给出修复命令 |
| `cca add <已存在的 API 号>` **不带任何 API 参数** | 拒绝并引导（列出可用的 `--model/--env/--url/--proxy/--key`），不把它当成 OAuth 登录 |
| `cca add <已存在的 API 号> --model/--env/--url/--proxy/--key` | **增量更新**：只改这次给的字段（其余原样保留），逐条打印 diff；不会"重新声明"冲掉别的字段 |
| 只给 `--url` 且主机不是已知预设、又没给 `--model` | 不强制，但提示一句：这个号会用 Claude 自带模型名（中转/网关正是如此）；需要就补 `--model` 或事后 `cca add <p> --model M` |
| `cca login <订阅号> --key …` | 拒绝并说明"API key 只对 API 号有意义" + 给出 `cca add <name> --provider …` 的例子（原先会把这参数透传给 `claude auth login` 撞未知参数） |
| 启动或 `cca carry` 到一个跑不起来的号（缺 key / 未登录） | 照做，但 stderr 明确提示"跑不了"并给修复命令（`cca login <p> --key …` / `cca login <p>`） |
| `settings.json` 的 `apiKeyHelper` 缺失或指向不存在的文件 | 启动前**自动重渲染**（与补 `hasCompletedOnboarding` 同类的机械修复）并提示；值漂移不自动改，由 `doctor` 报 |

参数校验（都要报错而不是静默忽略）：

* `--key` 与 `--key-stdin` 互斥；
* `--env` 不允许设置工具管理的键：`CLAUDE_CONFIG_DIR`、`ANTHROPIC_BASE_URL`（用 `--base-url`）、
  `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`（用密钥文件）、四个代理键（用 `--proxy`）；
* `--url` 与 `--base-url` 是同义词（同时给不同值则报错，不做静默取舍）；
* 端点**严格二选一**：`--provider <预设>` 或 `--url <端点>`；**两个都给直接报错**（否则"预设还能换
  端点"这层含义会让人误解），错误里给出替代写法（`--url … --model <预设模型名>`）。只给 `--url` 时
  若**主机**命中已知预设，则套用该预设的模型变量并打印一行说明（`--model/--env` 可覆盖）；不命中就
  不设任何模型变量 —— 中转类端点通常原样转发模型名，乱套预设反而错。`--provider generic` 显式表示
  "没有预设"；
* key 三种来源都拿不到时报错并列出三种给法。

`--model M` 的语义（**这个号所有槽位都用 M**，文档里要按槽位表说，别用"三个档位映射"这类行话）：

| 槽位 | 环境变量 | 不设时的行为（实测） |
| --- | --- | --- |
| 主 agent | `ANTHROPIC_MODEL` | 发 Claude 自带默认（`claude-sonnet-5`） |
| 别名 opus/sonnet/haiku | `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` | 解析成 Claude 真实 ID（`claude-haiku-4-5-20251001`） |
| 子 agent | `CLAUDE_CODE_SUBAGENT_MODEL` | 继承主 agent 模型 |

`--model M` 把上表全部设为 `M`（实测：建号后默认请求、`--model haiku`、`--model sonnet` 发出去的都是
`M` —— 三个 DEFAULT_* 变量把别名重定向了）。粒度控制用 `--env`，顺序是 `--model` 先、`--env` 后，
所以后者优先（实测 `--model base --env ANTHROPIC_MODEL=main`：默认发 `main`、`opus` 别名发 `base`）。

## 6. 显示、掩码与体检

### 6.1 掩码规则

`sk-abc…f3a2`（前 6 + 后 4）；长度 < 16 时只显示 `已配置`。**任何命令、任何日志、任何错误信息
都不打印完整 key** —— 有测试断言。

### 6.2 面板与详情

* `cca ls`：API 号没有邮箱，身份列直接显示 `api:<预设名>`（如 `api:deepseek`），没有预设则显示
  **端点主机名**（如 `api:sub2api.example.com`）—— 一眼看出指向哪里。不新增列，72 列仍然放得下。
  底部只在存在 API 号时加一行：`api 号没有订阅额度缓存，用量看供应商控制台`。
  `--wide` 下 档位/快照/额外 显示 `-`，登录列显示 key 状态（有 key = 是）。
* `cca status <api号>`：不显示订阅号专有的 登录 / 档位 / 额外用量 / 凭据到期 / 重新登录，
  换成 端点 / 模型 / key（掩码）/ 代理 四行。

### 6.3 `doctor` 对 API 号的检查

| 检查 | 级别 |
| --- | --- |
| 清单可解析（JSON 合法、`base_url` 非空） | FAIL |
| 密钥文件存在且权限为 0600 | FAIL（缺失，并给 `cca login <profile> --key …`）/ WARN（权限过宽，并给 `chmod 600`） |
| 密钥变量与预设一致（例如 `provider=deepseek` 却不含 `ANTHROPIC_MODEL`） | WARN |
| `base_url` 能解析（DNS） | WARN |
| `settings.json` 的 `env` 与清单一致 | WARN（提示 `cca sync <profile>`） |
| `apiKeyHelper` 可执行且输出非空（直接跑一次） | FAIL |
| 同时存在 `.credentials.json` 与 `apiKeyHelper`（订阅登录与网关凭据冲突） | WARN（冲突会导致启动告警与行为不确定） |
| `--online`：对 `base_url` 发一次最小请求（无 key 的 401 即算"端点可达且需要鉴权"） | WARN |
| `--probe-key`（隐含 `--online`）：带该号密钥真发一次 `max_tokens=1` 的请求 —— 200 = 密钥有效，401/403 = 被拒（连同端点返回的原因） | FAIL（被拒）/ 跳过（没配 key，缺 key 本身已 FAIL） |

## 7. 安全与合规边界

* 密钥只以 0600 文件存在；由 `apiKeyHelper` 在请求时读取，**不进 claude 的环境变量**、
  不进 argv、不进日志、不进 `settings.json`、不进 `git`、不进任何输出（掩码之外）。
* `--key` 会进 shell history，这是已知代价：`cca add` 在用了 `--key` 时**提示**一句
  "建议改用 `--key-stdin` 或 `$DEEPSEEK_API_KEY`"，但不阻止（使用者明确要"直接填"的便利）。
* 合规（写进 README）：API key 必须是**本人所有、仅本人使用**，不得共享或转借；
  发给第三方端点的内容受该供应商政策约束 —— 内部代码、数据要不要发给第三方模型，
  由使用者自己判断并遵守所属组织的规定。

## 8. 预设表

`PROVIDERS`（当前一条；以后加 provider 只是加数据）：

```python
"deepseek": {
    "base_url": "https://api.deepseek.com/anthropic",
    "key_env": "DEEPSEEK_API_KEY",
    "proxy": "none",
    "env": { …见 §3.2… },
    "doc": "https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/claude_code",
}
```

依据：DeepSeek 官方《使用 Anthropic API》与《接入 Claude Code》
（<https://api-docs.deepseek.com/zh-cn/guides/anthropic_api/>、
<https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/claude_code>）；
模型名会被服务端自动映射（`claude-opus*` → `deepseek-v4-pro`，`claude-sonnet/haiku*` →
`deepseek-flash`），所以预设里显式写上模型变量只是为了可预测与省排查。

本机实测（2026-09-25）：`api.deepseek.com` 直连 70ms 返回 401 `Authentication Fails (governor)`；
经内建集群代理 459ms 同样返回 401 —— 端点可达、代理非必需，故默认 `proxy=none`。

## 9. 测试与验收

全部离线（临时 `HOME` + 假 `claude`），沿用现有 `tests/test_claude_acct.py` 的骨架：

1. `add ds --provider deepseek --key …`：目录/共享软链/清单（0600）/`.api-key`（0600）/
   渲染后的 settings env 齐全；
2. 上述命令的 stdout+stderr **不含完整 key**；
3. 不带 `--provider` 的 `add` 行为与现在完全一致（回归）；
4. key 来源优先级 `--key` > `--key-stdin` > `$DEEPSEEK_API_KEY`；三者都无 → 报错列出三种给法；
5. `--key` 与 `--key-stdin` 互斥报错；
6. 通用端点：`--base-url` + `--key` → `provider=generic`，清单正确；
7. `--model M` 同时写四个模型变量；`--env` 可单独覆盖；
8. `--env CLAUDE_CONFIG_DIR=…` / `ANTHROPIC_BASE_URL=…` 等工具管理的键 → 报错；
9. `build_env` 对 API 号：BASE_URL / AUTH_TOKEN / 模型变量齐全，继承的 `ANTHROPIC_*` 被清掉，
   默认不注入内建代理，`--proxy` 与 `CCA_PROXY` 覆盖生效；
10. `build_env` 对订阅号：与现状逐键一致（回归）；
11. `login ds --key …` 换 key（内容替换，旧值不留）；`logout ds` 删 key 且清单保留；
12. 缺 key 时 `cca ds` 快速失败并给出提示；`ls` / `status` 的掩码不含完整 key；
13. `sync ds` 保留 `env`（合并语义）；`sync` 对订阅号不变；
14. `doctor` 对 API 号：权限过宽给 WARN、清单与 settings 不一致给 WARN；
15. `rm ds` 删掉密钥（输出含"API key"）；
16. 缺 key 的 `add`：错误信息里同时出现三条给法（`--key` / `--key-stdin` / `$DEEPSEEK_API_KEY`）；
17. `--provider deepseekk` → 报错列出 `deepseek` 且提示 `--base-url`；`--key-stdin` 空输入 →
    报错且**没有**写出空的 `.api-key`；
18. 清单损坏（写入非法 JSON）→ `cca ds` 在启动 claude 之前就以明确错误退出（断言假 claude 未被调用）；
19. 启动缺 key 的号 → 报错含 `cca login ds --key`；`status` / `ls` 对该号显示 `key 缺失` 与修复命令；
20. 渲染产物含顶级 `apiKeyHelper`（指向该号 `.api-key`），且 `settings.json` 全文不含密钥值；
21. helper 通道可用：直接执行渲染出的 `apiKeyHelper` 命令，输出恰为密钥（不带 banner）；
22. `doctor` 在同时存在 `.credentials.json` 与 `apiKeyHelper` 时给冲突 WARN；
23. 换 key（`cca login ds --key …`）后 helper 仍指向同一文件、渲染未变（密钥文件内容已换）；
24. `add` 撞已存在的 API 号 → 拒绝并引导；`login <订阅号> --key` → 拒绝并引导；
25. 启动缺 key 的 API 号 / 未登录的订阅号 → 提示"跑不了"与修复命令；
26. `doctor` 对未渲染 helper 的号给 FAIL 并提示 `cca sync`；订阅登录与 helper 并存给冲突 WARN；
27. `doctor --online` 探活：端点返回 401（或任何 HTTP 响应）算可达，"连接被拒"给 WARN
    （探活不带凭据，因此不读密钥；走该号生效的代理，`CCA_PROXY` 可覆盖）；
28. `--probe-key`：200 → "端点可达且密钥有效"；401 → FAIL 并给 `cca login … --key`；没配 key →
    "跳过密钥验证"（不谎报端点连不上）；
29. `add` 之后 `<profile>/.claude.json` 存在且含 `hasCompletedOnboarding`，并继承了 001 的信任目录；
30. `--url` 单独使用（无 `--provider`）：`env` 为空、面板显示 `api:<主机名>`；主机命中预设时自动套用
    并打印"识别到 …"；`--url` 与 `--base-url` 给不同值时报错；`--provider generic` 显式表示无预设；
31. `cca add --help` 打印两行配方（含内联 `--key` 与 `--url` 两种标准写法）**与槽位表**
    （`ANTHROPIC_MODEL` / 三个 `ANTHROPIC_DEFAULT_*` / `CLAUDE_CODE_SUBAGENT_MODEL` + 不设时发什么），
    且不创建任何号；
32. `--provider` 与 `--url` 同时给 → 报错并给出 `--url … --model <预设模型名>` 的替代写法；
33. 只给 `--url` 且无预设、无 `--model` → 建号成功但提示"会用 Claude 自带模型名"；给了 `--model` 或
    主机命中预设时不提示；
33b. 代理语义：默认 `builtin` → `HTTPS_PROXY` 注入内建、`NO_PROXY` 含端点主机且保留内建列表；
    `none`/`inherit` → 不注入；非法取值报错（含增量更新路径）；`proxy=none` 且 shell 无代理时
    `doctor` 提示"自动更新会失败"并给 `cca add <p> --proxy builtin`；
34. `cca add <已存在的 API 号>`：`--model` 更新五个变量、`--env K=V` 增改、`--env K=` 删除、
    `--url` 换端点、`--key` 换密钥，**未提到的字段原样保留**，输出逐条 diff；不带任何 API 参数时
    拒绝并引导（不会误入 OAuth 登录路径）。

端到端验收（实施后用真实 key 做一次，需使用者提供）：
`cca add ds --provider deepseek --key …` → **`cca doctor --online --probe-key` 报"端点可达且密钥有效"**
（一条命令的终局结论：真的带密钥发了 `max_tokens=1` 的请求）→ `cca ds -p "reply ok"` 出结果 →
`cca ds --bg <任务>` 后台会话能跑 → `cca ls` / `cca status ds` 显示正确 →
`cca ds` 直接启动 ds（不再有"切过去"这一步）。

## 9.1 已完成的验证（2026-09-25）

**真实 DeepSeek key 的端到端验收已通过**（使用者提供 key，全程只写进 0600 临时文件、不进 argv，
验收完覆写删除；输出只出现掩码 `sk-dfa…b926`）：

* `cca add dsdemo --provider deepseek --key-stdin < keyfile` → 建号 + 渲染 + 继承 001 的 3 条信任目录；
* `cca doctor --online --probe-key` → **`dsdemo: 端点可达且密钥有效（HTTP 200）`**（真的带密钥发出请求）；
* `cca dsdemo -p "reply with the single word ok"` → **`ok`**（真实 DeepSeek 应答；仅有
  `[claude-code:unrecognized_model]` 这一条无害提示）；
* `cca dsdemo --bg "say hi in one word"`（从已信任目录）→ 后台会话 `d2da97c9` 跑到 **`done`**，
  会话名由模型生成（"chinese greeting response"）—— 真实端点上后台会话也拿到了凭据；
* `cca ls` / `cca status dsdemo` 显示 `api:deepseek`、端点、模型、掩码、代理 `none`、额度 `-`；
* 验收后 `cca stop-bg` + `cca rm dsdemo --yes` 清理干净，workspace 绑定仍为 002。

## 9.2 第三方中转网关的逐槽位映射（2026-09-26）

使用者的第二个 API 号指向同事搭的**中转网关**（`--url https://<relay-host>:8080`，一个 URL 后面是
若干非 Claude 的模型名），要求与 `ds` **同时**跑。用现有命令面完成，未新增参数：

* 逐槽位映射 = `--model` 打底 + `--env ANTHROPIC_MODEL / ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL /
  CLAUDE_CODE_SUBAGENT_MODEL`；外加 `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1`
  与 `CLAUDE_CODE_AUTO_MODE_SERVER=0`。
* **实测优先级：`ANTHROPIC_MODEL` > `settings.json` 的 `model` 字段。** 把该号 settings 的 `model`
  分别改成 `sonnet`、`opus`（种子带的旧值），会话实际发出的仍是 env 里的 `gpt-6-astra` —— 所以
  "env 配好"已经足够，无需同步改顶层 `model`。依据：`claude -p --output-format stream-json --verbose`
  的 init 事件 `model` 字段，**与该号 `projects/*.jsonl` 里 assistant 消息的 `model` 字段一致**。
* **槽位别名解析实测**：默认 → `gpt-6-astra`；`--model opus` → `gpt-6-sol`；`--model sonnet` →
  `gpt-5.6-sol`；`--model haiku` → `gpt-5.6-luna`（别名确实走映射表，而不是 Claude 原生名）。
* **`effortLevel` 属于 settings.json 顶级 `modelSettings.<模型名>.effortLevel`**（CLI 2.1.282 的
  schema：`low|medium|high|xhigh`，逐模型生效，另有 `maxEffortLevel` 作上限）。它**不在 env 里**，
  故不在 `--env` 覆盖范围内：`cca` 不代管该键，只保证 `write_settings_env()` 重渲染时**不碰**
  `model` / `modelSettings` / `permissions` 等既有顶级键（回归用例
  `test_render_settings_env_keeps_other_keys` 已扩到这三个键）。
* **探活的第五种结论**：该网关会偶发 `502/503`（同一模型名连发三次：`502, 502, 200`），而真实会话
  一直正常 —— `--probe-key` 必须把 5xx 报成"端点/上游报错（与 key 无关）"，既不能报成"密钥有效"，
  也不能据此判 key 坏（§8 的 `endpoint_error`）。这次的误报正是加该状态的直接原因。
* 并行验证：`cca ls` 同一屏里 `ds` 与网关号各有后台会话在跑（串行守护只对 Claude 订阅号生效）。


真实 supplier key 的验收仍待使用者执行（§9 末段），但整条链路已在**本地 mock 端点**上端到端验证过
（不联网、不消耗任何订阅额度）：

* 起一个本机 Anthropic 协议 mock（`POST /v1/messages`，支持 SSE 与 JSON），
  `cca add mtest --provider deepseek --base-url http://127.0.0.1:8123 --key …` 建号；
* 渲染出的 `settings.json`：`env` 只含非密变量、顶级 `apiKeyHelper: cat <profile>/.api-key`，
  全文无密钥；
* 用**真实的 claude 2.1.281** 跑 `cca mtest -p "reply …"`：请求落到 mock，模型名 `deepseek-flash`
  （`[1m]` 后缀被 CLI 用于本地估窗口、发送时剥掉），响应文本正常回显；
* mock 记录的鉴权头证实凭据通道按官方文档工作：**同一个 key 同时出现在
  `Authorization: Bearer …` 与 `x-api-key: …`**（这正是选 `apiKeyHelper` 的理由）；
* helper 自检：渲染出的 `apiKeyHelper` 命令输出恰为密钥本身、stderr 为空（有单测守着）；
* `cca doctor --online` 对**真实 DeepSeek 端点**探活返回 `端点可达（HTTP 401）` —— 说明端点活着
  且需要鉴权（该探测刻意不带凭据，因此不读密钥）；
深入一层（同一套 mock）又验证了四条路径，并因此揪出一个真 bug：

* `--env ANTHROPIC_CUSTOM_HEADERS="X-CCA-Test: yes"` 确实到达线上（mock 收到该头）—— `--env`
  不是只写进清单看看的；
* 换 key **不需要重渲染**：`cca login <p> --key <new>` 之后下一次请求就带新 key（helper 是请求时
  读文件，不是启动时快照）；
* **后台会话也拿得到凭据**：`cca <api号> --bg '<task>'` 起来的会话连续向端点发了 4 个请求
  （这正是官方文档点名的坑："只在 shell 里 export 到不了 supervisor 托管的后台会话"）；
* **揪出的 bug**：API 号没有 `claude auth login` 那一步，`<profile>/.claude.json` 没人建 → 
  `hasCompletedOnboarding` 与 001 的目录信任**静默落空**（`-p` 模式看不出来；表现是首次交互启动
  走向导、`--bg` 报 "Workspace not trusted"）。修法：`ensure_api_config()` 在 `add`/`login`/`sync`/
  启动预检里负责建这个最小配置（`{"hasCompletedOnboarding": true}`），随后信任继承即可落盘。
  实测：新号立刻继承 001 的 3 条信任目录，`--bg` 起来并成功发请求。

* `cca doctor --online --probe-key` 在真实端点上用**假 key** 得到
  `密钥被端点拒绝（HTTP 401 {"error":{"message":"Authentication Fails, ****0001 is invalid …"）`
  —— 说明这条"一条命令验证密钥"的路径确实能给出终局结论（真 key 时应得到 `端点可达且密钥有效`）；
* 预设里的 `CLAUDE_CODE_EFFORT_LEVEL=max`、`CLAUDE_CODE_AUTO_COMPACT_WINDOW=786432` 被 CLI 接受，
  没有报错；唯一提示是 `[claude-code:unrecognized_model]`（模型不在自带目录里，属正常），
  已写进 README 排障表。

## 10. 兼容与迁移

* 现有订阅号零改动（§3.1）；全量用例保持绿。
* VERSION → 0.12.0（新功能）。
* README：新增"接第三方 key（API 号）"一节；合规边界补 §7 两条。
* `docs/specs/design.md` 顶部加一行指向本文。

## 11. 生态对比与取舍（调研结论）

调研了三个参照物，结论与采纳情况：

### 11.1 官方 gateway 文档（`code.claude.com/docs/en/llm-gateway-connect`）

**直接改掉了 v1 的一个决定**：文档明确说 gateway 配置要放 **settings 文件的 `env` 块**，
因为"只在 shell 里 export 到不了 supervisor 托管的后台会话"；而凭据若不想写进配置文件，
正解是 **`apiKeyHelper`**（值同时进两个 header、默认缓存 5 分钟）。这与我们"清单是真相 +
settings 是渲染产物 + 密钥不进配置文件"的设计**同向**，只是通道从"cca 注入 env"改成
"helper 读取"——采纳，见 §3.3/§4.1。

其余采纳项：header 语义与 401 排查（§6.3）、`ANTHROPIC_CUSTOM_HEADERS` 走 `--env`、
网关上下文上限与 `CLAUDE_CODE_AUTO_COMPACT_WINDOW`（预设已设，文档补排查）、
`ANTHROPIC_API_KEY` 的一次性授权弹窗（用 helper 绕开）、
`/fast`、Remote Control 与语音输入在网关凭据下不可用（README 里写明）。

### 11.2 claude-code-router（musistudio/CCR）

架构是**本地网关 + 控制面**：常驻进程监听 `127.0.0.1:3456`，做协议转换（OpenAI/Gemini ↔
Anthropic）、按条件路由、fallback、凭据池、请求日志与成本统计，还管多客户端（Claude Code /
Codex / Kimi CLI…）。

**不采纳**：协议转换、路由规则、fallback、观测面板 —— DeepSeek 这类端点原生就说 Anthropic
协议，直连少一个常驻进程、少一个端口、少一份经手全部 prompt 的第三方代码；这些需求用
"把 `--base-url` 指向 LiteLLM / CCR"就能满足，写在文档里而不是自己实现（§12 非目标）。

**部分采纳**：它的"provider 预设 + 一键配置"验证了预设表的做法；`/model` 里能看到网关模型
对应官方的 `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`，README 提一句即可。

### 11.3 cc-switch（farion1231/cc-switch）

做法是把 provider 的 `ANTHROPIC_BASE_URL` + **密钥**直接写进 Claude Code 的
`settings.json` `env` 块。**不采纳密钥那半**（密钥进配置文件会被复制/提交/sync 覆盖），
而且它自己就有漂移缺陷：issue #1790 "切换 Provider 后模型环境变量未同步到 settings.json"。
这正是我们设计里"清单是唯一真相、settings 是渲染产物、`doctor` 比对漂移并提示 `cca sync`"
要解决的问题 —— 反向验证。

## 12. 非目标

* DeepSeek（或任何供应商）的余额/用量查询：不做，额度看供应商控制台；
* 内置多家预设（kimi / glm / …）：不做，通用 `--base-url` 已覆盖，将来按需加数据；
* 把 key 写进 `settings.json`：不做（§7）；
* `apiKeyHelper` 方案：不做（v1 用 env 注入，机制更少）；
* 跨 provider 自动搬运会话：不做（`cca carry` 仍是人工显式动作）；
* 本地代理 / 协议转换 / 路由规则 / fallback / 请求日志与成本统计：不做 —— 原生 Anthropic 协议
  的端点直连即可；需要协议转换或路由时，把 `--base-url` 指向 LiteLLM、claude-code-router 这类
  网关；
* 网关模型发现（`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`）：不代设，需要就自己加进 `--env`。

## 13. 后续修正（2026-09-26）

`cca switch` 与 workspace 绑定已在后续一轮里整体删除（默认号概念取消、`carry` 独立成命令），
本文中"切换 / 绑定"相关的描述以 `explicit-profile.md` 为准：API 号的端点、清单、密钥、
模型映射等约定不受影响。
