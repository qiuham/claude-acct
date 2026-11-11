# OpenRouter 号：一个 key、多模型、按会话选型

关联：`design.md`（号的数据模型、命令面）、`api-profiles.md`（API 号的清单/密钥/代理约定）、
`explicit-profile.md`（账号显式化）。本文只写 OpenRouter 这一块，以及它带来的"选模型"子系统。

状态：设计已评审（2026-10-01），等待实施。

## 1. 背景与目标

使用者要接 OpenRouter，用法是**一个 key、经常换不同模型跑**（尤其免费模型）。核心诉求两条：

1. **按会话选模型**：A 会话用 X、B 会话用 Y，同时跑、互不干扰 —— 而不是"改这个号的默认"。
2. **选型可查**：四百多个模型里能按"免费/价格/上下文/关键词"筛，不用去网页翻 slug。

做到：`cca add or --provider openrouter --key sk-or-…` 之后，`cca or --model <slug>`、
`cca or --bg --model <slug> '<任务>'`、`cca model or --list --free` 都能用，`cca sessions` 里也能
看到每条会话用的模型。

非目标见 §8。

## 2. 术语

| 词 | 含义 |
| --- | --- |
| slug | OpenRouter 的模型 id，形如 `qwen/qwen3.8-27b:free`、`anthropic/claude-opus-4-8` |
| 变体 | slug 后缀，`:free`（免费版）、`:nitro`（加速）等 |
| 路由 | `openrouter/free`（免费路由，随机挑一个可用免费模型）、`openrouter/auto` 等 |
| `~…-latest` 别名 | `~anthropic/claude-opus-latest` 这类"永远指向该家族最新版"的 slug |
| 档位别名 | CC 的 `opus` / `sonnet` / `haiku` / `fable`，由 `ANTHROPIC_DEFAULT_*_MODEL` 决定解析到谁 |

## 3. 接线：`openrouter` 预设

`cca add or --provider openrouter --key sk-or-…` 建号，清单里写入：

| 项 | 值 | 理由 |
| --- | --- | --- |
| `base_url` | `https://openrouter.ai/api` | 官方给 Claude Code 的 Anthropic 兼容入口 |
| 密钥环境变量名 | `OPENROUTER_API_KEY` | 与生态一致；实际凭据仍走 `.api-key` + `apiKeyHelper`（同时发 Bearer 与 x-api-key），密钥不进 env/argv/日志 |
| `ANTHROPIC_MODEL` | `openrouter/free` | 零成本起步（0/0、ctx 200k、支持工具与推理） |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | `~anthropic/claude-opus-latest` | 档位语义保持 Claude，且自动跟最新版 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | `~anthropic/claude-sonnet-latest` | 同上 |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | `~anthropic/claude-haiku-latest` | 同上（后台小活） |
| `ANTHROPIC_DEFAULT_FABLE_MODEL` | `~anthropic/claude-fable-latest` | CC 新增的第四档 |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY` | `1` | 让会话内 `/model` 选择器自动列出网关模型 |
| `CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT` | `1` | 网关下省系统提示（官方推荐组合） |
| `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK` | `1` | 网关凭据下 `/fast` 的组织检查会误报 |
| `ENABLE_TOOL_SEARCH` | 不设（关） | 混合模型场景最稳；多数开源模型不支持工具搜索 |
| `CLAUDE_CODE_SUBAGENT_MODEL` | 不设 | 子代理跟随主模型；免费场景下别偷偷烧钱 |
| `proxy` | `builtin` | 与其它 API 号一致；端点主机自动进 `NO_PROXY`（实测直连可用） |

依据：官方 Ori Harness 公告给出的就是这套 env（本文只做取舍：密钥仍走 helper、
tool search 默认关、补上第四档 Fable）。

## 4. 按会话选模型（本设计的主线）

**机制**：`cca <号> --model <slug>` —— `--model` 透传给 `claude`，只作用于**这次启动**。
官方文档明确这是"同时跑不同模型"的正确做法（`/model` 是改号的默认，不是改会话）。

```bash
cca or --model qwen/qwen3.8-27b:free -p "…"      # 前台：这个会话用免费 Qwen
cca or --model ~anthropic/claude-opus-latest      # 另一个终端：这个会话用 Opus
cca or --bg --model openrouter/free '跑一批活'      # 后台会话也能各自指定
```

**实测**（ds 号，同一把 key 同时起两个后台会话）：`--model deepseek-v4-pro` 的会话 transcript
记 `deepseek-v4-pro`，`--model deepseek-flash` 的记 `deepseek-flash`，两条并行互不干扰。
模型与窗口都是**进程级 env**，同号多会话天然隔离。

**适用范围**：这套按目录算窗口的逻辑只对 OpenRouter 号生效（provider 为 `openrouter` 或端点主机是
`openrouter.ai`）。别的中转（codex 那类）的模型名不在这个目录里，套上去只会给每次启动刷无用提示；
它们要窗口就自己 `--env CLAUDE_CODE_AUTO_COMPACT_WINDOW=…`。

**配套：启动时按目录写上下文窗口。** 这是"多会话不同模型"能用的前提 —— 免费模型窗口从 64k 到
1M 都有，继承同一个值必然出事：

* `cca <号> --model <slug>` 启动时查目录里该 slug 的 `context_length`，给这个会话写
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW = ctx × 75%`（**向下**取整到千，如 262144 → 196000）；
* 判定规则（三分支，写死不许含糊）：
  1. slug 里含 CC **认得的** Claude 模型名（如 `anthropic/claude-opus-4-8`）→ **不写**，CC 自己知道窗口；
  2. 其它 slug（`qwen/…:free`、`~deepseek/…`、`openrouter/free`）→ 按目录写；
  3. 目录查不到该 slug（新模型/目录拉取失败）→ **不写**，并提示一句"窗口未知，可用 `/autocompact`
     或在会话里设 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`"。
* 号级别的默认窗口：建号时按 `ANTHROPIC_MODEL` 的窗口写一份（默认模型 `openrouter/free` → 150000）；
  `cca model or --main <slug>` 改主模型时同步改它。

**边界（要在文档里写清）**：会话内 `/model` 切到更大窗口的模型时，窗口大小保持启动时的值（env
只在启动时读）—— 这时用 CC 自带的 `/autocompact <size>` 调，或重新用 `cca or --model <slug>` 起。
`/model` 的 `Enter` 会把选择**写进该号默认**，只想改本次会话，要在选择器里按 `s`。

## 5. `cca model` 子命令

```bash
cca model                                    # 只读汇总：每个号 主模型 / 四档别名 / 子代理 / effort + 来源
cca model or                                 # 只看 or
cca model or --main <slug> [--sub <slug>]    # 写默认（主模型/子代理）
cca model or --opus <slug> --sonnet <slug> --haiku <slug> --fable <slug>
cca model or --effort <low|medium|high|xhigh>   # 写 modelSettings[主模型].effortLevel
cca model or --list [--free] [--search <词>] [--sort price|ctx] [--refresh]
```

* **只读可不带号，写入必须写号**（沿用既有约定）；批量要显式 `--all`。
* 值原样收：slug、变体、`~…-latest`、路由都能填，`cca` 不解释、不改写 —— "跟最新"还是"钉死"
  由使用者写什么决定。
* `--list` 读的是 §6 的目录缓存；`--free` 等价 `--list` 加"只看 `:free` 与 `openrouter/free`"；
  `--sort price` = 按输入价格升序（免费在前），`--sort ctx` = 按上下文窗口降序。
* `--pick <slug>…`（**可选，见 §8**）：维护"常用清单"并渲染成 CC 的 `modelPicker` 设置，
  让会话内 `/model` 选择器优先显示这几个。

## 6. 目录与它的缓存

* 源：`GET https://openrouter.ai/api/v1/models`（公开、免 key）。字段用到：`id`、`name`、
  `context_length`、`pricing.{prompt,completion,input_cache_read,input_cache_write}`、
  `supported_parameters`、`reasoning`、`top_provider`。
* 缓存：`~/.claude-accounts/.cache/openrouter-models.json`（0600），TTL 1 小时；`--refresh` 强制刷新；
  离线/失败时用旧缓存并打一行提示，**不阻塞**（建号、启动都不依赖它）。
* 展示：价格换算成"每 M token 的美元"（`$2/$10 每 M`），免费标 `FREE`；列 `ctx` 与是否支持工具。
* **不清楚就不猜**：目录里没有的 slug 一律不推断能力（不猜窗口、不猜工具支持）。

## 7. 可见性：每条会话用的什么模型

* `cca sessions`：会话清单加一列"最后使用的模型"（从 transcript 读，零额外成本）；
* `cca status <号>`：后台会话明细同样加这一列。
* 目的：A/B 混跑时"这条会话当时是谁在回答"一眼可见，也方便回头续会话。

## 8. 非目标

* **不自己维护模型目录**：只读 OpenRouter 的 `/api/v1/models`。
* **不做"模型→能力"推断**：不自动开关 tool search、不自动声明 `_SUPPORTED_CAPABILITIES`。
* **不替使用者选模型**：没有自动降级、没有兜底切换、没有"贵了就换"的策略。
* **不做余额面板**：OpenRouter 有 credits 接口，等有人真要看再加。
* **`--pick` / `modelPicker` 渲染列为可选**：按"一个会话一个模型"的用法它只是锦上添花，
  主线做完再看要不要。
* **不改** API 号既有的"端点二选一"（`--provider` 预设 vs `--url`）、密钥通道与代理策略。

## 9. 验证

离线 fixture（不联网，`tests/test_claude_acct.py`）：

1. `cca add or --provider openrouter --key …` 渲染出的 env：§3 表里的键齐全、`ENABLE_TOOL_SEARCH`
   与 `CLAUDE_CODE_SUBAGENT_MODEL` 不出现、`ANTHROPIC_MODEL=openrouter/free`；
2. 目录解析：给一份 fixture JSON，`--list` 的行数/字段正确；`--free` 只留 `:free` 与 `openrouter/free`；
   `--search qwen` 命中；`--sort price` 免费在前、`--sort ctx` 窗口降序；
3. 窗口策略三分支：认得的 Claude slug 不写、未知 slug 按目录写（`ctx×75%` 取整到千）、
   查不到就不写并给出提示；
4. `cca model` 不带号只读（不写文件）、`--all` 批量写、写 `--main` 时号级默认窗口同步更新；
5. `cca sessions` / `cca status` 的模型列（fixture transcript）；
6. 缓存：TTL 内不重复请求、`--refresh` 强制、目录不可用时用旧缓存且不报错。

真机验收（需要使用者提供 key）：

```bash
cca add or --provider openrouter --key sk-or-…
cca or -p "只回两个字：收到"                       # 默认免费路由
cca or --model ~anthropic/claude-haiku-latest -p "…"   # 指定模型
cca or --bg --model openrouter/free '说一句 hello'      # 后台
cca model or --list --free                          # 看当前免费清单
cca sessions                                        # 模型列
```

## 10. 已知风险与坑

| 风险 | 说明与应对 |
| --- | --- |
| 免费模型有速率限制、行为随机 | `openrouter/free` 是**随机**挑一个免费模型；响应里会回报实际用了谁（transcript 里能看到）。要可复现就用具体 `:free` slug |
| `/model` 改的是默认 | 会话内按 `Enter` 会把选择写进该号默认，影响以后所有会话；只想改本次按 `s` |
| 窗口不跟随会话内切换 | env 启动时读；切大窗口模型后用 `/autocompact` 或重开 |
| 认得的 Claude slug 不写窗口 | `anthropic/claude-opus-4-8` 这类 CC 自己知道窗口；`CLAUDE_CODE_MAX_CONTEXT_TOKENS` 对它无效（除非 `DISABLE_COMPACT`），文档要写明 |
| 工具搜索默认关 | 换来的是"任何模型都能跑"；Anthropic 系想省系统提示 token 可临时 `ENABLE_TOOL_SEARCH=1` |
| 端点拒网关工具 schema | **已落地（两层）**：API 号（含 OpenRouter）的 settings env 带 `CLAUDE_CODE_ARTIFACT_TOOLSET=""`（跟着账号走，rewind/attach 重启也生效），启动时再带 `--disallowedTools Artifact,ArtifactComments,ArtifactData`（`api_launch_args()`，carry 路径同样生效）；使用者自己传了就不再叠加 |

## 11. 验收记录

自动化：`make test` 200 用例全绿（新增 19 个：预设渲染、目录 fixture 读取、坏缓存降级、窗口三分支、
会话模型不写号级配置、`cca model` 只读/写入/`--all`、目录过滤排序与价格换算、
`last_session_model` 跳过 `<synthetic>`、`sessions` 显示模型列），`make lint` 通过。

**真机验收待使用者提供 OpenRouter key**（`cca add or --provider openrouter --key sk-or-…`）。
届时按 §9 的命令跑一遍，并把输出要点补到本节；重点确认两件事：
真实目录的字段与 §6 一致、`openrouter/free` 的真实 `context_length`（若不再是 200000，
按实测值更新 §3 的默认窗口 = 窗口 × 75% 向下取整到千）。
