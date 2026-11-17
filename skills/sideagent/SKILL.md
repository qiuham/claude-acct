---
name: sideagent
description: >-
  sideagent（cca）：把一件完整的活交给另一个号、另一家模型的独立会话（模型和额度自己挑；自带 Agent
  只能用 Claude、只花本号额度），也负责给别的会话发话——不管它在哪个号、是不是已经 done / stopped。
  Use when 准备把工作单元交出去（调研、实现、审查、验证、长期负责都算）；要多路并行或不同家族的独立判断；
  本号额度快用完、收到【cca 额度提醒】；要给别的会话发话，或某条会话在 ListAgents 里没有、SendMessage 发不到、被说成
  「下线 / 不在了 / 没上线」；用户提到 sideagent、某个号名或模型家族（gemini、deepseek、gpt）、takeover、
  handoff；收到 sideagent 的 <task-notification>；sideagent 卡住、blocked、一直不回；工具缺失、没回报。
  几步工具调用能做完、或严重依赖本会话默会上下文的，不要起 sideagent。
---

# sideagent

sideagent 是 cca 在另一个号上起的完整 Claude Code 会话：自己的上下文、工具、skill 和额度，干完自动把结果回报给派活的会话。用法照子代理（Agent / SendMessage），区别是**模型和额度由你挑**。

工具来自 cca 的 MCP 服务 `sideagents`：`sideagent_accounts`、`sideagent_send`、`sideagent_result`、`sideagent_status`、`sideagent_interrupt`、`sideagent_stop`、`sideagent_takeover`、`sideagent_release`。参数以工具自带说明为准，这里只写说明里没有的判断。

## 1. 什么时候派、派给谁

它的价值：**并行**（多条线同时推进）、**独立上下文**（大材料、长任务、长期职责整个交出去，主会话只留判断）、**独立视角**（不同模型家族盲点不同）、**额度调度**（Claude 订阅额度留给判断）。

**判据只有一条：这件事能不能独立写清验收标准。** 写得清就派，粒度不限——一个任务、一条业务线的日常、一个长期盯守都行；写不清（靠本会话的默会上下文）或几步工具就能做完，就自己做。

挑号先调 `sideagent_accounts`，按字段现挑，不看号名（号名因人因机器而异）。返回的顺序就是推荐顺序：

| 字段 | 看什么 |
| --- | --- |
| `usable` | `false` 的跳过（额度用满、没登录、没 key） |
| `priority` | 只有能用的订阅号有：**1 最先用**——周额度最早重置的号排前面 |
| `family` | 模型家族；同一网关后面常挂好几家，只有它能区分 |
| `kind` | `claude 订阅`（有 5 小时 / 7 天窗口）或 `api 按量`（按 token 花真钱） |
| `running` | 这个号已经在跑几个；拥塞了换一个 |
| `model` | 只有 api 号有；`sideagent_send` 的 `model` 传 `haiku` / `sonnet` / `opus` 选档 |

| 活的性质 | 挑什么 |
| --- | --- |
| 量大、机械、要快：读日志和长文档、批量改、初稿 | 便宜的 api 家族（DeepSeek 这类）或 `haiku` 档；能拆就多开几个 |
| 要独立视角：审 diff / 方案、核实关键结论 | `family` 和主会话不同的号，`opus` 档 |
| 要 Claude 的推理，只是想并行 | 自带 Agent；本号紧张换一个有额度的订阅号；订阅号都满用 `family: Claude` 的 api 号 |
| 长期盯一条线 | 额度稳、`running` 少的号，`keep: true` 或建一条正常会话让它自己负责 |

- **用户点名就照办**：说了号、家族或要独立视角，再小的活也派；没点名时才考虑「自己做更快」。
- **要用订阅号时，先用周额度最早重置的**（`priority` 小的）：它剩下的周额度到点就作废，晚重置的号留着不会跑。两个窗口任一用满就派不出去；api 号是真钱，订阅号有额度时优先用。
- **同族副本不算独立视角**：`gpt`、`gpt-ds`、`gpt-iso` 这种同一家族，互相验证只是花两份钱得一个结论。
- **别把活派回本号**：本号是 `basename "$CLAUDE_CONFIG_DIR"`（没设就是 001）。
- 非 Claude 号和其他号一样能收代码、日志、内部文档，只是交办里不放凭据。

## 2. 并行与团队

默认用法：主会话拆解、汇总、裁决，sideagent 多路并行。

| 场景 | 怎么拆 |
| --- | --- |
| 调研 / 选型 | 每个方向一个实例，各交「事实 + 来源」 |
| 重要问题要多份独立答案（团队式） | **每个家族派一个**，同一份交办；本机默认 `gemini`、`ds`、`cc`、`gpt` 四家，`usable: false` 的少一个也行 |
| 方案 / 设计 | 2–3 个实例各给一个侧重（最小改动、风险优先、性能优先） |
| 代码审查 | 按维度拆：正确性、安全、性能、兼容性 |
| 批量改动 | 每个文件或目录一个实例，写入范围不重叠 |

- **团队式**：每个成员拿同一份交办，别给其中一个塞你的结论；结论一致的直接采信，不一致的就是风险点——标出来，再派一轮或自己判。代价是 N 份钱，琐事派一个便宜的就够。
- **每路都花额度或真钱**：先看余量，别一上来开十个。
- **同一个 checkout 并行写会冲突**（`/mnt/work` 还是多个 workspace 共享的 CephFS）：分目录、分 git worktree，或者只派只读活。
- **每路都要能独立验收**：写清各自交回什么；回来后去重、标分歧。

## 3. 写交办、追问、收尾

```
派新活：account 写号名 → 返回实例名，每次都是零上下文的新实例
  sideagent_send {account: "ds", message: "...", description: "中性对冲调研"}
追问 / 回它的问题：account 写实例全名
  sideagent_send {account: "ds#a1b2c3", message: "..."}
```

交办写给一个只看得到这一条消息的同事（工作目录是主会话的启动目录）；快档和便宜家族写得更具体，判断留在你这边：

- 目标和**可检查的完成标准**：做到什么算完，交回什么（结论、路径、diff 摘要、测试输出）。
- 已知事实、已排除的方向、相关文件路径；**写入范围**（只读就明说只读）。
- 要独立视角时只给材料和要求，不写你的结论。
- `description` 给 3–5 个词；需要主会话最近的对话就开 `include_context: true`。

收尾：

- 默认后台跑，结果以 `<task-notification>` 自动送回——**不要轮询、不要 sleep**。
- 回它的问题用**实例全名**；只写号名会新开一个零上下文的实例，这是最常见的错。默认在它下一个工具调用结束时送到，`mode: "queue"` 则等这一轮干完。
- **回报是它的说法，不是事实**：转述或据此继续前，核对关键产物（diff、测试、文件、出处），便宜家族和快档尤其要抽查。
- 打断这一轮 `sideagent_interrupt`（进程和上下文留着）；整个停掉 `sideagent_stop`。

## 4. 别的会话：直接发话，没有「下线」

`ListAgents` 只列**本号正在跑**的会话；别的号上的、已经 `done` / `stopped` 的都不在里面，原生 `SendMessage` 发过去会 not reachable。这只说明它现在没在跑，**会话没有「下线」这回事**。

**给别的会话发话，一律：**

```
sideagent_send {session: "<编号前缀|完整编号|标题>", message: "..."}
```

cca 跨号找到它现在挂的号，没在跑就唤醒（那个号额度满了就换一个能用的号接着跑同一段对话），用原生跨会话消息送到；它的回话回到你这里。**不接管、不进面板**，不会把它的进度灌进你的上下文。

硬规则：

- 不说「下线了 / 不在了 / 没上线」，不「等它上线」——没人会自动拉起一条 `done` 的会话，要交给它就现在发。别人转述某会话「不在了」，也先查再说。
- 只想知道它在不在、在哪个号：只读查，报 `号 / 编号 / 状态`，别认领别人的职责：

  ```bash
  cca sideagent takeover-candidates --all \
    | jq -r '.[] | [.account, .id, .name, .state, .running] | @tsv' | grep -i <标题或编号>
  cca sessions --wide | grep -i <标题>      # 已退出的历史会话；--all 不限 14 天
  ```

- 发话会唤醒没在跑的会话、烧它那个号的额度（api 号是真钱）：确实需要它做事时再发。
- `sideagent_takeover` 只在要**持续在面板上看它**时用（进度会一直进你的上下文）；用完 `sideagent_release`。
- `sideagent_send` 回「要给 account 和 message」= 这个会话起得早，MCP 还是旧版：请用户重启会话；急的话先 `sideagent_takeover` → `sideagent_send {account: <返回的名字>}` → `sideagent_release`。

换号（`cca handoff`）、额度撞墙（`watchdog` / `wake`）、`blocked`、误唤醒副本的补救，见 `references/sessions.md`。

## 5. 本号额度快满：cca 会替你换号

换号由 cca 的常驻进程 watchdog 统一负责（自动拉起，不用管）。本号 5 小时额度到 95% 或 7 天额度到 98% 时，它会在**这一轮结束**时把还在干活的后台会话换到有余量的号（同一编号、同一名字），续上后你会收到一句「接着做」；撞墙停下的会立刻换。换号时停掉还在跑的后台任务（多是等待、监控循环），续上后会收到通知，需要就重起。

装了面板插件的会话会在某次工具结果后收到【cca 额度提醒】，告诉你这一轮结束后会换到哪个号。你要做的只有：

- **还有活**：什么都不用做，正常把这一轮做完。
- **这一轮就收尾、之后没活了**：运行 `cca handoff --stay` 留在本号（撞墙了照样会被换走）。
- 想指定目标号：`cca handoff <号> --current`，然后结束这一轮。
- 交互会话（人在终端前）不会被自动换：把提醒转告用户。
- 看它打算做什么：`cca watchdog status`；看会话现在都在哪个号：`cca agents`。

## 6. 加载工具与排障

工具通常延迟加载，用 ToolSearch 按关键词找：`sideagent_send sideagent_result sideagent_status sideagent_accounts sideagent_takeover`。找不到 = 这个号没注册：请用户 `cca mcp install --all` 后**重启会话**（新建的号会自动注册）。

想知道它在干什么：`sideagent_status` 的 `doing`，或面板横条。没回报、`stale`、Gemini 的 `400 tool_use.signature` 等见 `references/troubleshooting.md`；机制见 cca 仓库 `platform/claude-acct` 的 README「sideagent」一节和 `docs/internals.md`。
