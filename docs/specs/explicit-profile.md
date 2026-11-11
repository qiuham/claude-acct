# 账号显式化：去掉"默认号"与 workspace 绑定

关联：`design.md`（号的数据模型、共享与隔离矩阵、串行守护）、`api-profiles.md`（API 号）。
本文替换 `design.md` 里"解析顺序 / workspace 绑定 / `cca switch`"那部分。

状态：设计已与使用者确认（2026-09-26），等待实施。

## 1. 决策

删掉**隐式选号**这一整层：没有默认号，没有 workspace 绑定，没有 `$CLAUDE_ACCT` 优先级。
账号要么写在命令里，要么这条命令本身是全局的。`carry`（把会话从 A 号搬到 B 号）独立成命令。

使用者的三条判断：

1. 并行已经常态化（`ds` 与中转网关号同时跑），"同一时刻只有一个号"这个前提站不住。
2. workspace 只有一个，按 workspace uuid 绑定等于一个全局默认号，却还背着 workspace 的概念。
3. 命令里始终写着号（`cca ds …`），"当前号/默认号"从来没派上用场；它唯一的实际作用是切号时
   顺手收掉别的号的会话 —— 副作用比功能更显眼。

## 2. 原则

| 原则 | 含义 |
| --- | --- |
| 账号显式 | 要落到某个号的命令必须写出号，不写就报错并给一行用法 |
| 只读可汇总 | 只读命令不写号 = 汇总所有号；有副作用的命令必须写号 |
| 不做隐式选择 | 没有默认号、没有 workspace 绑定、没有 `$CLAUDE_ACCT` |
| 透传不变 | `cca <号> <参数…>` 里 `<参数…>` 就是 claude 自己的参数，cca 不解释、不拦截 |

## 3. 命令面

| 命令 | 变化 |
| --- | --- |
| `cca <号> [claude 参数…]` | 不变。`--bg`、`-c`、`--resume <id>`、`agents`、`logs`、`attach`、`stop` 等照旧透传 |
| `cca`（裸跑） | **改成报错**：`写清哪个号：cca <profile> [claude 参数…]`，并给 `cca ls` / `cca sessions` 提示 |
| `cca <参数…>`（参数写在号前，如 `cca -c`） | 报错，提示正确写法；不再提"当前号" |
| `cca ls` | 去掉 `*`（当前号）标记与脚注 |
| `cca status [<号>]` | 不写号 = 每个号一段摘要；写号 = 今天的单号详情 |
| `cca sessions` | **去掉号参数**：它问的是"共享池里有哪些可续会话"，会话不绑账号（见 §3.2）。每行标当前挂在哪个号上，历史会话标"归属未知" |
| `cca stop-bg <号>` | 必须写号（有副作用） |
| `cca which <号>` | 必须写号；只印一行（目录 / 类型 / 端点），脚本友好。详情归 `cca status <号>` |
| `cca carry <源号> <目标号> […]` | **新增**，见 §4 |
| `cca add/new/rm/login/logout/sync/doctor/version/help` | 不变（本来就是全局命令） |

删除：`cca switch`、`cca bind`、`cca unbind`、`cca resume`，以及 `switch -` 用的"上一个号"记录。

一条容易踩的坑：`cca status <号>` 是 **cca 自己的**单号详情（身份、额度、后台会话明细）。
claude 没有 `status` 子命令，所以 `cca <号> status` 会把它当成**一句提示词**去执行 —— 实测它会真起
一个会话，回你一段"我把账号和会话查了一遍…"。claude 认的是 `agents` / `logs` / `attach` / `stop` /
`respawn` / `rm`。列会话一律用 `cca status <号>` 或 `cca sessions <号>`。

### 3.1 会话不绑账号

所有号的 `projects/` 都指向同一份目录（内置号那份），所以**会话属于共享池，不属于任何账号**：
`001`、`ds`、`codex` 看到的是同一批 `.jsonl`，任何号都能 `--resume` 任何一条。transcript 里也不记
账号，历史会话只能标"归属未知"。

真正按账号区分的是"**谁在跑它**"：每个号 daemon 的后台登记（`active_workers`）。同一条会话可能同时
挂在两个号上（接管会产生副本），所以这层是多对多。两个命令因此分工明确：

* `cca sessions` —— 池子里有哪些可续会话（不写号）；
* `cca status <号>` —— 这个号在跑什么（后台会话明细）。

### 3.2 续会话

`cca resume <前缀|标题>` 删除。续会话走 claude 自己的参数 —— 任何号都能续任何会话，**用哪个号续
就烧哪个号的额度、走它的端点**：

```bash
cca sessions                 # 先看可续会话（跨目录、跨号，列出完整 id）
cca ds --resume <完整id>      # 用 ds 号续（= claude --resume）
cca ds -r <完整id>            # 同上，短参数
cca ds -c                    # 续该号最近一条
```

代价说清楚：**前缀/标题的模糊匹配没有了**，得从 `cca sessions` 里拷完整 id。这是有意的 ——
`cca <号>` 后面既然就是 claude 的参数，cca 不该在这里插一层自己的语法。

## 4. `carry`：把会话从 A 号搬到 B 号

```bash
cca carry <源号> <目标号> [--only <前缀|标题>]… [--all] [--dry-run]
```

语义就是两步：**先按 `stop-bg` 停掉当前挂在源号上的后台会话**（只停不删，对话保留），**再在目标号
以 `--bg --resume <完整id>` 接管同一条会话**。搬的是"谁在跑它"，会话文件本身不复制、上下文不丢
（它本来也不属于任何号），此后它的面板归属与额度都算目标号的。

* 默认只搬"没在跑的"（等输入 / 已结束）。不带 `--only` 时交互列出候选让你勾。
* `--only <前缀|标题>` 精确指定，可重复；非 tty 必须给 `--only` 或 `--all`，保证脚本可预期。
* `--all`：连正在跑的也先停再搬。接管后会继续跑，烧的是**目标号**的额度。
* `--dry-run`：只打印计划，不停、不接管。
* 匹配不唯一或匹配不到 → 报错退出，不动任何会话。
* 搬完打出每条会话的 `cca <目标号> attach <id>`。

手工等价的两步（文档里也写）：

```bash
cca ds stop-bg                 # 1. 停源号（只停不删）
cca 002 --bg --resume <完整id>  # 2. 目标号接管
```

## 5. 001 的地位

001 从"默认号"改称**内置号**。它的特殊性只剩三条，都与"当前选中谁"无关：

1. 配置目录固定在 `~/.claude`，且 `CLAUDE_CONFIG_DIR` 必须未设置；
2. 新号的 `settings.json` 播种与信任目录继承从它（以及其它号）来；
3. 不允许 `cca rm`。

**裸跑 `claude`（不经 cca）仍然是 001** —— 它的配置目录就是 `~/.claude`。文档里写明：这是文件系统事实，
不是残留的默认号概念，免得被误读成"删了默认号它也会跟着变"。

## 6. 实现清单

删除（代码）：

* `SUBCOMMANDS` 里的 `switch` / `bind` / `unbind` / `resume`；
* `cmd_switch`、`cmd_bind`、`cmd_resume`，以及 `cmd_which` 与单号详情里"为什么选中它"的那段文案
  （`$CLAUDE_ACCT` / workspace 绑定 / 默认号三层都消失，只剩"显式写的号"与"内置号"）；
* `resolve_profile()` 的 `$CLAUDE_ACCT` 与 workspace 绑定两层，只留显式参数与内置号兜底；
* `read_last_profile()` / `write_last_profile()` 与绑定文件（`# workspace_uuid\tprofile`）的读写；
* `cca ls` 的 `*` 标记与相应脚注、`COMMAND_USAGE` 里的 `switch` / `bind` / `resume` 三个条目、
  `usage()` 里的相关行。

另加一张"已移除命令"小表（`switch` / `bind` / `unbind` / `resume` → 各自的替代写法）：命中就打印
说明后以非零码退出，而不是回落到"把 `switch` 当成号名"。这四个名字同时进保留名单，避免以后有人
建出同名的号。

保留：

* 001 的上述三条特殊性、`seed_trust_from_*`、`background_guard`（订阅号串行是额度约束，不是
  选号习惯，见 `design.md` §6）；
* API 号的全部既有约定（`api-profiles.md`）。

文档：README 的"切号 / 绑定"章节改写为"选号与搬运"（含 `carry` 与手工两步）；
`design.md` 的解析顺序段改写；`api-profiles.md` 里提到 `cca switch` 的句子改掉。

## 7. 非目标

* 不做目录级绑定（讨论过：粒度不必要）。
* 不读旧的 workspace 绑定文件，不迁移（明确决定：不留兼容层）。
* 不做号的别名或角色备注（另一件事）。
* 不改订阅号之间的串行提示与 API 号豁免。

## 8. 验证

用例（`tests/test_claude_acct.py`）：

1. 裸跑 `cca` 报错，提示里出现 `cca <profile>`，不启动 claude；
2. `cca -c` 报错提示写法，不再提"当前号"；
3. `cca status` 不带号 → 输出含全部号；`cca status <号>` 仍是单号详情；
4. `cca sessions` → 列共享池的会话，每条标出当前挂在哪个号上（两个号各自的 live 会话都要在）；
   `cca sessions 002` → 报"多余参数"（号参数已取消）；
5. `cca stop-bg` 不带号 → 报错；
6. `cca carry ds 002 --only <前缀>` → 停掉源号会话、目标号出现 `--bg --resume` 登记；
7. `cca carry` 匹配不唯一 / 非 tty 且没有 `--only`、`--all` → 报错且不改动会话；
8. `cca switch` / `bind` / `unbind` / `resume` → 明确提示"已移除"并给替代写法，而不是"未知命令"；
9. `cca which` 不带号 → 报错；`cca which <号>` 印一行；
10. `$CLAUDE_ACCT` 存在时不再影响任何命令的选号；
11. 旧的绑定文件存在时，cca 忽略它且不报错；
12. `cca ls` 输出不再有 `*` 标记。

手工验收（真实环境）：`cca ls` → `cca sessions` → `cca ds stop-bg` → `cca 002 --bg --resume <id>`
跑一遍，确认会话归属切换且对话连续；再确认 `cca ds` / `cca codex` 并行不受影响。

## 9. 验收记录（2026-09-26，真机）

`feat/explicit-profile` 分支上跑的真实环境验收（命令都用 worktree 里的 `bin/claude-acct`，
真实 `$HOME`；只做只读命令与 `--dry-run`，没有动任何在跑的会话）：

| 命令 | 结果 |
| --- | --- |
| `cca ls` | 四个号一行一个，**没有当前号 `*` 标记**；脚注的 `*` 只剩"重置时刻已过"这一个含义 |
| `cca status` | 不带号 → 001 / 002 / codex / ds 各一段摘要 |
| `cca sessions` | 一次列出共享池（表头写明"会话不绑号，任何号都能续"），live 的两条标出 `codex working`，历史会话标 `归属未知` |
| `cca carry ds 002 --dry-run` | 打印接管计划（1 条 blocked），末尾 `--dry-run：以上是计划，未停任何会话、未接管` |
| `cca switch` / `cca resume` | 非零退出 + `已移除 ——` 与替代写法 |
| 裸跑 `cca` | 非零退出 + `要写清哪个号` 与三个例子 |
| `cca which` | 非零退出 + `要写清哪个号。用法: cca which <profile>` |
| 并行 | `cca ls` 同屏显示 `codex 2/2`、`ds 1/2` —— 多号并行不受本轮改动影响 |

自动化：`make test` 171 用例全绿，`make lint` 通过。
