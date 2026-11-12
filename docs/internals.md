# cca 实现细节

README 讲怎么用；这里记机制和文件格式，给改代码、排查疑难的人看。设计背景见 `docs/specs/`。

## sideagent

### 两种 sideagent

`cca/session/backends.py` 定义后端接口（发话、状态、结果、停、打断、同步），两个实现：

- **派出的**（spawned）：cca 起的常驻进程 `cca sideagent serve`（`sideagent_server.py`），claude 官方 stream-json 模式。
- **接管的**（attached）：Claude Code 自己跑的原生会话（`takeover.py`），我们只读它的对话记录、往它的消息套接字发话。

MCP 工具、命令行、主会话收工都只问 `backend_for(名字)`。面板也不看种类，只看两个通用字段：
`external`（进程不归我们管、进度靠同步）和 `status=waiting`（等确认）。

### 状态文件

一个 sideagent 一个文件夹 `~/.cca/sideagents/<名字>/`：

| 文件 | 内容 |
| --- | --- |
| `state.json` | 状态；`kind` 是 `spawned` 或 `attached` |
| `progress` | 进度行（`TASK` / `TOOL` / `SAY` / `RESULT` / `NOTIFY`…） |
| `events.jsonl` | 完整事件，面板详情页读它 |
| `result.json` | 任务记录：实例退出后 `sideagent_status` / `sideagent_result` 仍从这里返回会话编号和代码版本 |
| `summary.log` | 进度短语每次调用小模型的记录 |
| `run/` | 套接字、正在生成的文字；只在跑的时候有 |

停了删 `run/`、`state.json` 写 `finished_at`，整个文件夹留 30 天（`SIDEAGENT_KEEP_DAYS`）再清。

哪个主会话用着哪些 sideagent 记在 `~/.cca/sessions/<主会话编号>.json`（只有那个会话自己写）。
派出的只有一个主人；接管的可以同时挂在好几个主会话上（M 对 N），放手只解开自己那条。
面板先读本会话的名单，再读各个文件夹画横条。设计见 `docs/specs/sideagent-mn.md`。

### 归属：回报送给谁

只有一个来源：主会话那个 claude 进程在登记表里的条目（`<配置目录>/sessions/<进程号>.json`）。
派活时 MCP 服务按自己的父进程找到它，记下进程号、进程启动时刻和当时的会话编号；sideagent 每次投递前都到登记表现查：

1. 那个进程还在（文件在、启动时刻对得上）：用里面**现在**的会话编号和套接字。会话被继续、恢复后编号会变，照样投得到。
2. 那个进程没了、主会话重开成新进程：同一个号目录的登记表里，会话编号等于最近一次看到的那个的条目，
   就是继续了同一段对话的新进程，归属跟过去（进度文件里记一行 `OWNER 旧 -> 新`）。
3. 都找不到就不投，进度文件里记 `NOTIFY skip 原因`。

`NOTIFY ok` 是送到了，`NOTIFY fail` 是连不上主会话。sideagent 中途要汇报或提问用 claude 自带的 `SendMessage`，
`to` 填主会话的名字；起进程时 cca 用 `--append-system-prompt` 把这件事和主会话的名字告诉它。
`~/.cca/sideagents/main-session` 是旧机制，已经不用，可以删。

### 子代理派的 sideagent 只回给那个子代理

要装面板插件：

1. 子代理里调 `sideagent_send` 时，插件从这次调用拿到子代理编号（`tool.call` 的 `agentId`），塞进参数 `caller_agent`。
2. 完成回报和中途的 `SendMessage` 照旧投到主会话的消息通道；插件在 `session.receive` 里按发件实例
   （回报带 `cca-instance`，自己发的按发件套接字）认出来，用 `$.session.send({ to: { agentId } })` 转给那个子代理，不进主对话。
3. 那个子代理已经不在：退回主对话，开头写明原本是给谁的。
4. 横条和详情只在派活的那个界面里显示。没装插件时，子代理派的消息照旧进主对话。

### 任务状态与用量口径

| `state` | 意思 |
| --- | --- |
| `running` | 还在跑 |
| `finished` | 做完了，`text` 是回复 |
| `interrupted` | 被打断；`text` 是打断前说的话 |
| `merged` | 插话，已并进 `merged_into` 那条任务 |
| `stopped` / `failed` | 被停掉 / 出错 |

不给 `job_id` 时按这个顺序挑：在跑的 > 有内容的已完成 > 其他已完成、打断、插话 > 停掉、失败。

插话（`mode: interject`）立刻写进它这一轮的输入，下一个工具调用结束时可见；这一轮最后一步没有工具调用时赶不上，
claude 会马上另开一轮跑它，跑完之前排队的下一条不发。

- `token_count`：和主界面子代理那一行同一个数 = 最近一次 API 调用的输入（含缓存读写，即当前上下文大小）
  + 这一轮累计输出 + 正在输出的估计（4 个字符算 1 个，一轮结束清零）。
- `tokens_in` / `tokens_out`：对账用，这一轮每次调用的输入 / 输出加起来，所以比上下文大。
- `stale`：`state.json` 的 `code_version` 和现在 `sideagent.py` 的哈希对不上（或没有），进程是改代码前起的。

### 按会话发话（`sideagent_send {session}`）

`takeover.message()`：`_locate()` 用各号 `claude agents --json --all` 找这条会话的全部登记，按
「在跑 > 没停的（`startedAt` 越新越前）> stopped / failed」排；有登记表条目且有 `messagingSocketPath`
就直接 `deliver()`；否则 `_wake_target()` 按顺序挑第一个能用的号（`usable` 回调看额度 / 登录 / key），
都不行退回发话人自己的号（按普通会话起，`--permission-mode bypassPermissions`），`_wake()` 起来后等套接字再投。
不写 `~/.cca/sessions` 名单，不建 `sideagents/<名字>/`。

### 接管的同步

本会话的 MCP 服务（`cca mcp serve`）起一条后台线程，每 2 秒按位置增量读接管会话的对话记录
（`~/.claude/projects/<目录>/<会话编号>.jsonl`），翻成面板事件；状态取原号登记表 `<配置目录>/sessions/*.json`：
busy = 运行中，waiting = 等确认（标黄，写出卡在哪个工具调用），idle = 空闲，不在登记表 = 结束。每做完一轮叫醒主会话一次。
同步线程写心跳 `~/.cca/sideagents/.sync-<会话>.beat`，面板看它 10 秒内写过就只读文件；MCP 服务不在时面板兜底起
`cca sideagent bg-sync <主会话>`。它派的子代理读 `<会话编号>/subagents/agent-*.jsonl`（旁边 `.meta.json` 的 `toolUseId`
是派它的 Agent 调用）。正在输出的文字要一条消息说完才出现（对话记录里没有半截的话）。

唤醒用 `claude --bg --resume <完整编号>`：原来是后台会话的不带别的参数，保留原编号和原设置；普通会话加
`--permission-mode bypassPermissions`。**必须是完整编号**，只给前 8 位 Claude Code 会复制出一个新编号的会话。

### 面板横条

每一行照主界面子代理那一行画：`❯ ◯ 名字  活动描述 … 12s · ↓ 12.3k tokens`。中间是进度短语（sideagent 那个号的小模型
每 30 秒写一句；没写出来、没配模型或调用失败时退回最后一个工具的活动描述，还没调工具时是任务描述）；
右侧耗时 · 词元，闲着写 `idle`，后面黄色接 `N queued` 和 `旧版`。详情页结果行只写耗时和一个词元数；
`tokens_in` / `tokens_out` 只在状态字段里。和子代理逐项的对照见 `docs/sideagent-vs-subagent.md`。

### 权限钩子

钩子本体是 `cca sideagent permission-hook`；面板按按钮时调 `cca sideagent permission-answer <实例名> allow|deny`。
装的时候先备份 `settings.json`（`settings.json.bak-permission-hook-<时间>`）。实测（Claude Code 2.1.316）：

- 后台会话里也跑这个钩子；会话启动以后才装上的钩子也生效。
- 钩子等着的时候，会话状态显示「等确认」，不报错。
- Claude Code 杀掉超时的钩子时不是拒绝，而是退回原生确认框。所以 settings 里给钩子配 660 秒，钩子自己 600 秒先回「拒绝」。

### 端到端测回报

单元测试不覆盖，要真的号：

1. 主会话调 `sideagent_send`，让它「先用 SendMessage 给主会话发一句『开始了』，再数一下 src 下的文件」。
2. 主会话应先收到一条 `<cross-session-message>`（「开始了」），做完再收到结果。
3. 没收到就看 `~/.cca/sideagents/<名字>/progress` 的 `NOTIFY` 行。

按会话发话的端到端：在一个空闲的号上 `cca <号> --bg '<一句话任务>'`，等它 `done`，用
`sideagent_send {session: <编号>}` 让它 SendMessage 回一句；再 `cca <号> stop <编号>` 让进程退出，重复一次，
应看到「已唤醒并送到」、编号不变、回话回来。

## 启动环境（`env.session_env`）

所有起会话的路径（启动、handoff、接管、wake、watchdog、按会话发话）都走 `session_env()`：

- 代理注入；没配代理的订阅号拒绝启动。`cca doctor` 在没配代理时跳过出网检查，避免从本机出口暴露 IP。
- 经代理查一次出口（ip-api）：不支持的地区拒绝；`TZ` 跟出口时区（`CCA_TZ` 可覆盖），探测失败用 `Asia/Singapore`。
- 有会话的时区和要起的不一致时拦住（daemon 会把旧时区固化给新会话）；只认 claude 进程，带 `CLAUDE_CONFIG_DIR` 的普通脚本不算。
- `LANG` 固定 `en_US`；关遥测和错误上报（`DISABLE_TELEMETRY`、`DISABLE_ERROR_REPORTING`），不影响自动更新。
- 设 `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1`（CLI 的隐藏变量）：把当前接口当官方处理，不打第三方标记。

## 看门狗（`cca watchdog`）

全机一个常驻进程（`watchdog.lock` 的 flock 保证唯一，崩溃时内核自动放锁），由 `cca quota-hint`（订阅号的后台会话在问）、
`cca <订阅号>` 启动、`cca wake` 排期、`cca handoff --current / --stay` 顺手拉起；订阅号上没有活着的后台会话、没有待叫醒条目、
没有意图，连续 10 轮（且启动满 5 分钟）后自己退出。每轮 30 秒；有「这一轮结束就搬」的会话时，对这几条每 2 秒复查一次。

状态文件（都在 `~/.claude-accounts/`）：

- `wake.json`：待叫醒的条目（`cca wake` 写，watchdog 撞墙又没号可去时也写 `only_if_limited` 条目），格式没变。
- `watchdog.json`：`{"version": 1, "moves": {会话: {account, target, at}}, "stays": {会话: {account, at, windows: {窗口: 重置时刻}}}, "fails": {会话: {account, n, until, bad: [搬不进去的号]}}}`；`bad` 里的号下一轮不再挑。
- `~/.cca/moves.jsonl`：每次换号成功一行 `{at, session, from, to}`（只留最近 2000 条），`cca agents` 的「← 旧号」读它。
  `moves` 是会话里敲 `cca handoff <目标> --current`，`stays` 是 `cca handoff --stay`；同一条会话只留一条，后写的覆盖先写的。
  `stay` 记下命令执行时各窗口的重置时刻，同一时刻（差 120 秒内）有效；窗口都重置了、或会话不在那个号上，就清掉。
- `watchdog.lock`（单例锁）、`watchdog.state.lock`（`watchdog.json` 的读改写锁）、`watchdog.pid`（只给 status 展示）、
  `watchdog.spawn`（10 秒内多次 ensure_running 只 fork 一次）、`watchdog.log`。
- 旧版的 `watchdog.sessions`、`watchdog.order` 不再读（启动时记一行日志提醒可以删）。

搬成功往 `~/.cca/sideagents/notices.jsonl` 追加一行，面板变成横幅：
`{"at": ISO 时间, "text": "dagset：b005 → b004"}`。会话编号不变，所以不再写 `lineage.jsonl`（面板读取侧保留给旧记录）。

## `claude agents --json` 的两类条目

清单里混着**后台任务**（`kind: background` + job id，`claude stop` 认）和**交互会话**（`kind: interactive`、没有 job id，
就是别处终端里正开着的会话）。后者停不掉，硬停只会得到 `No job matching '<id>'`，所以 `stop-bg` / `handoff` 直接跳过并说明。

后台登记的寿命：「等输入」的登记不会让 daemon 保持存活——退出 `claude` 后 supervisor 空闲 5 秒就退出，
并把这些登记 `settled (killed)`，面板随之清空，对话本身不受影响（实测 `<号目录>/daemon.log`）。

## 第三方端点为什么要禁 Artifact 工具

第三方端点会拒这些工具的 schema（实测 `400 Invalid schema for function 'Artifact': "^[^\0]*$" is not a "regex"`）。
API 号的 settings 里带 `CLAUDE_CODE_ARTIFACT_TOOLSET=""`，启动时再带 `--disallowedTools=Artifact,ArtifactComments,ArtifactData`
（`=` 单 token：它是变参，写成两个 token 会吞掉后面的位置参数）。两条都要：CLI 自己重启会话（rewind / attach / 用 saved options 续）
时会丢启动参数，只有 settings 兜得住。自己传了 `--disallowedTools` 就不再叠加。
