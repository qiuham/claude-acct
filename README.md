# claude-acct（cca）

一台机器上管多个 Claude Code 号：**订阅号**各有登录态和额度，**API 号**接第三方端点；
会话记录、skill、插件在号之间共享——身份分开，会话互通。

- **额度按号算**：一个号撞墙，换个号接着跑同一条会话（`handoff`；额度快满时常驻的 `watchdog` 自动换），或者等它重置自动叫醒（`wake`）。
- **会话不绑号**：任何号都能续任何会话。
- **sideagent**：在对话里把活派给另一个号、另一家模型，干完自动回报。

## 安装

```bash
git clone https://github.com/qiuham/claude-acct.git
cd claude-acct
python3 -m pip install 'rich==15.0.0' 'questionary==2.1.0' 'prompt_toolkit==3.0.51'
make install       # ~/.local/bin 下软链 cca / claude-acct / ccl，并给每个号装面板插件
```

需要 Python 3.11+ 和 `claude` CLI。`cca` 直接跑仓库里的代码，`git pull` 就是升级。

## 快速开始

```bash
cca set proxy --global http://host:port      # 订阅号没配代理会拒绝启动，见「代理与出口」
cca add 002                                  # 建订阅号并登录（打出登录链接，自己去浏览器开）
cca add ds --provider deepseek --key-stdin   # 建 API 号（key 从 stdin 读，不进 shell history）
cca 002                                      # 用 002 启动；后面的参数原样交给 claude
cca ls                                       # 看每个号的额度
```

`cca --help` 是分组总览，每个子命令都有 `cca <命令> --help`。

## 核心概念

### 号写在命令里

没有默认号，写谁就是谁：`cca ds`、`cca 002 -c`、`cca ds --bg '<任务>'`。号名后面的参数原样交给
`claude`。不经 `cca` 直接跑 `claude` 时，用的是 `~/.claude` 这个配置目录（没迁移时它就是 001，
见「把 001 搬成普通号」）。

### 会话不绑号

所有号的 `projects/` 都指向同一个共享池，所以一条会话不属于任何号：**用哪个号续，就烧哪个号的
额度、走哪个号的端点**。按号区分的只有「谁在跑它」——每个号自己的后台会话登记。

- `cca sessions` 看池子里有什么（跨目录；`/resume` 和 `-c` 只看当前目录）。
- `cca status <号>` 看这个号在跑什么。
- 同一条会话只在一个号上跑；换号成功后旧号面板里的条目会被清掉，`cca agents` 里标「← 旧号」（读 `~/.cca/moves.jsonl`）。要搬就用 `cca handoff`。

### 总表 cca agents

```bash
cca agents            # 跨号会话总表：活着的 + 3 天内有活动的
cca agents --all      # 全部（含更早的已停会话）
cca agents --json     # 结构化输出
```

每个号各取一次 `claude agents`（并发，单号最多等 10 秒，取不到或超时的号跳过，表尾提示），按会话合并：同一条会话在几个号上都有记录时只留一行，
活着的那份为准、号列写它现在在哪个号；都没活着取最近的。一行：名字、短编号、现在的号、状态、在干什么/最近一句、多久前。
状态列沿用 `cca status` 的写法（blocked、working、idle、done……，另有 quota 撞额度、stopped、ended）。
要人处理的（blocked、waiting、quota）排最前，然后是在跑的，再按最近活动倒序。只看不动，进入用表尾提示的 `cca <号> attach <短编号>`。

和 `cca sessions` 的区别：`agents` 是现在的会话和状态；`sessions` 是共享池里能续的历史。

### 共享与隔离

| 路径 | 处理 | 原因 |
| --- | --- | --- |
| `projects/` | 软链到 `~/.claude/projects` | 会话的唯一来源，任何号都能续 |
| `skills/` `plugins/` `CLAUDE.md` | 软链到 `~/.claude` | 装一次全部号可用 |
| `settings.json` | 建号时从默认 settings 播种，之后各自演化 | CLI 会改写它，软链有被替换成实体文件的风险 |
| `.credentials.json` `.claude.json` | 各号独立 | 登录态和账号身份；cca 不读凭据内容 |
| `daemon/` `jobs/` `sessions/` | 各号独立 | 后台会话归属清楚，避免并发写 |

默认 settings 在 `~/.cca/defaults/settings.json`，和任何号都无关，想改新号的默认设置就改它；
`cca sync <号>` 用它重新播种一个已有的号。

### 隔离号

`cca new <号> --isolated` 建的号不共享 `projects/`、skills、插件，普通号看不到它的会话。
要用旧会话就单向导一次：`cca import <隔离号> <源号>`。隔离只分开配置和会话索引，项目文件照样共用。

## 日常

### 启动、找会话、续会话

```bash
cca ds                       # 交互启动
cca ds --bg '<任务>'          # 后台会话（任务写成位置参数，别加 -p）
cca sessions                 # 跨目录列出能续的会话：完整 id / 目录 / 标题 / 最后活动 / 挂在哪个号
cca sessions --all --wide    # 不限 14 天、不截断
cca ds -r <完整id>            # 用 ds 续这一条（= --resume）
cca ds attach <8 位 id>       # 接上正在跑的后台会话
```

- `-r` 只认**完整 id**（短 id 会被当成搜索词或非法 UUID）；`attach` / `stop` / `rm` 收 8 位短 id。
- 别用 `-c` 跨号续：共享池里 `-c` 只看当前目录最近一条，可能是别的号留下的。
- `cca sessions` 的「归属」只标得出还在跑或挂起的会话；已经退出的标 `归属未知`——transcript 不记账号，会话本身没丢。
- 后台会话的 `rm` 只删登记，不删对话记录；真正连记录一起删的是 `claude project purge <path>`。

### 看额度：`cca ls` 和 `cca status`

```
PROFILE 邮箱                  5 小时(已用)     7 天(已用)        档位        订阅
001     alice@example.com      37% 11-08 22:20   90% 11-11 07:00  max 5x      有效
002     bob@example.com         0%~ -           100%~ 11-10 08:00 max 5x      有效
ds      api:deepseek          -                -                 -           -
```

- 百分比是**已用**，后面是重置时刻（本地时区的绝对时间）；加粗的是这个号最紧的窗口。
- 额度默认实时取，5 分钟缓存；`~` = 不是刚取的值（用了缓存或 claude 的本地快照），`*` = 重置时刻已过、数字是重置前的旧值。
- `--cached` 只看本地快照（不联网），`--refresh` 强制刷新，`--wide` 加后台会话数 / 登录 / 快照时刻 / 额外用量，`--json` 给结构化结果。
- API 号按量计费，没有额度列；用量去供应商控制台看。

**挑订阅号的默认顺序：周额度最早重置的先用。** 快重置的那个号，剩下的周额度到点就作废；晚重置的号留着，额度不会跑。
`sideagent_accounts`（标 `priority`）、`cca handoff` 的目标号选择框、`cca quota-hint` 和 `cca watchdog` 挑目标号时，都按这个排；
同一时刻再挑更空的，周额度已经重置过的排最后。

`cca status` 不带号 = 每个号一段摘要；带号 = 单号详情：

```
账号      002  bob@example.com
登录      是          档位  max 5x
凭据      11-08 22:02 到期（有效）
重新登录  12-04 06:33（刷新令牌到期时间）
额度      5 小时 0%（- 重置） · 7 天 23%（11-09 08:00 重置）
后台会话  working  846fa0c5  neutral-multi  [claude-opus-5-5]
```

- `凭据`是 access token，小时级，过期不影响使用（claude 启动时用刷新令牌自动续）。
- `重新登录`是刷新令牌到期时间，月级，**这个才要留意**：到期前后 `cca login <号>`。7 天内标黄。
- 两个时间都只从 `.credentials.json` 里正则取数字字段，token 不进内存也不进输出。
- 会员到期日 cca 拿不到（本地没有账单周期），看 Apple / Stripe / claude.ai 的 Billing 页。

### 换号：`cca handoff`

后台会话面板读的是每个号自己的登记，共享 transcript 只够 `--resume`，不会让会话出现在另一个号的面板里。
换号就是两步：源号停掉 → 目标号 `--bg --resume`。`cca handoff` 把这两步做好：

```bash
cca handoff ds 003                      # 计划表 → 选择框（↑↓ 移动，空格勾，回车确认）
cca handoff ds 003 --only=sam --only=daily   # 非交互：按编号前缀或完整标题点名，可重复
cca handoff ds 003 --all                # 连正在跑的一起搬（会继续跑，烧 003 的额度）
cca handoff ds 003 --dry-run            # 只看计划
cca handoff 003 --current               # 在会话里敲：这一轮结束就把现在这一条搬到 003（交给常驻的 watchdog）
cca handoff --stay                      # 在会话里敲：这条会话留在本号（见下面「额度」一节）
cca handoff                             # 不带参数：先选源号、再选目标号（目标号带额度百分比）
```

- 选择框按最后活动倒序，**默认一条都不勾**；第一行一键全选「等输入 / 挂起的」（`blocked`），`a` 全选或全不选；确认前还会再问一次。非 tty 必须给 `--only` 或 `--all`。
- 正在跑的标「不动（在跑）」，交互终端正开着的标「搬不了」。`stopped` 的会话默认不列，要搬就 `--only=<编号前缀>` 点名。
- 续上的会话保留原名：cca 用 `/rename <原名>` 当启动提示（不用 `--name`，带它续会话会分叉出新编号；不改名则 Claude Code 会按提示词自动起名覆盖原名），原来要说的那句在续上后以消息形式送到；换号、回源号续、watchdog、wake、接管唤醒都是这样（watchdog 的搬迁直接走 handoff 的同一套）。
- 搬成功后源号面板里的条目会被清掉（只删面板条目，对话记录不动；带 worktree 的不删），换号记在 `~/.cca/moves.jsonl`；只有旧版本或清理失败才会残留 `stopped` 的旧条目。
- 家目录里开的会话会改到 `~/llm` 里接着跑（家目录一次只能信任一个会话，后台起不来），所以会话尽量在项目目录里开。
- 搬到 API 号前，如果 transcript 里有 tool search 记录会提醒（第三方端点会 422）：加 `--strip-tool-search` 先备份再剥掉。
- 目标号「等输入」的登记不会让后台进程常驻：退出 claude 后登记会被收掉，对话不受影响；要它重新出现就再 `cca <号> --bg --resume <完整id>`（只登记，不跑模型）。

### 额度：`watchdog`（常驻，自动换号 + 叫醒）、`wake`（手动排叫醒）、额度提醒

后台会话撞到额度（`429` / `usage limit`）会停在半路，官方不会自动继续。cca 用**一个常驻进程**处理——`cca watchdog`，
全机只有一个，**不用手动起**：面板插件定时调的 `cca quota-hint`、`cca <订阅号>` 启动会话、`cca wake` 排期、
`cca handoff --current / --stay` 都会顺手把它拉起来（崩了下次调用再拉）；订阅号上没有活着的后台会话、
也没有待叫醒的条目时，它自己退出（连续约 5 分钟没事才退，刚起 5 分钟内不退）。

**什么时候搬**（只对订阅号的**后台会话**；API 号按量计费不算；交互会话搬不了，只提醒请人处理）：

| 情况 | 怎么办 |
|---|---|
| 号上某个窗口到了提醒点（5 小时 95%、7 天 98%；`CCA_QUOTA_HINT_PERCENT` / `CCA_QUOTA_HINT_WEEKLY_PERCENT` 可调），会话还在干活 | **这一轮结束时**搬走：登记表连续两次 idle，或对话记录末尾是 `turn_duration`（后台 shell 还在跑时登记表是 shell，只看 idle 抓不到）连续两次。**不等后台任务**：换号时它们会被停掉，续上后会话会收到通知 |
| 会话已经撞墙停下 | 立刻搬，不看阈值，也不看下面的例外；源号额度其实已经恢复的不搬，交给 wake 叫醒 |
| 卡住的窗口不到 15 分钟就重置 | 不搬，等它重置 |
| 会话闲了 30 分钟以上（最后一次动静在 30 分钟前） | 不搬，它不耗额度；哪天被叫醒又撞墙，按撞墙处理 |
| 会话里运行了 `cca handoff --stay` | 这个号、这一段（同一个重置时刻）内不搬它；撞墙了照样搬；别的窗口后来也到了提醒点而没有对应记录的，照搬 |
| 全都满、没有可去的号 | 撞墙的会话排进 wake（`wake.json`），等最早的重置后叫醒（只叫真是撞墙停下的） |

搬去哪：订阅号里两个窗口离各自提醒点都还差 15 个点以上的优先，同档里周额度先重置的先用（周额度已经重置过的排最后）；
同一个号上的一批会话按编号分散到排前面的 3 个号，免得一起挤到一个号上把它的 5 小时窗口打满。目标号搬前现查一次额度、过地区检查，
过不了就换下一个。搬迁串行且慢（要等源号退出、等套接字登记），一轮最多搬 3 条，其余下一轮；搬失败的会话冷却 5 分钟。
续上的会话编号、名字不变（`/rename` 保名，见上），搬成功后写日志，并在面板上弹一条通知（例如「dagset：b005 → b004」）。

**会话里的入口**：

- 额度提醒（装了面板插件的会话定时问 `cca quota-hint`：远时 5 分钟一次；用到 80% 以后 2 分钟一次，当前号额度超过
  2 分钟就现取，同一个号的现取在会话之间共享；同一个窗口只给一次）：到提醒点时在下一次工具结果后面给模型附一条**告知**
  （人看不到、不另起一轮）：「这一轮结束后 cca 会把会话换到 X（编号、名字不变，后台任务会停掉，续上后会收到通知）。
  如果这一轮就收尾、之后没活了，运行 `cca handoff --stay` 留在本号。」没有可换的号时只说接着做，撞墙后 cca 会在重置后叫醒。
  交互会话的提醒不变：请人退出后 `cca <目标号> --resume <编号>`。
- `cca handoff <目标号> --current`：模型想指定目标时用——登记给常驻进程，这一轮结束就搬（仍要求源号还在提醒点上）。
  重复敲只是覆盖同一条登记。先 `--current` 再 `--stay` 会取消搬迁，反过来则取消 stay。
- `cca handoff --stay`：这一轮就收尾、之后没活了，留在本号。只免「到提醒点」的搬迁。

**看它在干什么**：

```bash
cca watchdog status             # 在不在跑；各号额度；接下来搬哪条、为什么、搬去哪；哪些因例外不搬；待叫醒的
cca watchdog --dry-run          # 只算一遍要做什么（只用缓存的额度，不现取），不动手
cca watchdog stop               # 停掉常驻进程（下次有人调用 quota-hint 等又会拉起）
```

日志在 `~/.claude-accounts/watchdog.log`。升级 cca 后常驻进程仍跑旧代码，直到它退出或 `cca watchdog stop`；`status` 里显示启动时间。

**不换号，等重置后在原号叫醒**——`cca wake`（睡前手动排；排好后由 watchdog 到点叫）：

```bash
cca wake 001                    # 选 001 上要叫醒的会话
cca wake 001 --only=d31c1a92    # 非交互
cca wake status                 # 看排了哪些（同 cca watchdog status 里的「待叫醒」）
```

叫醒时刻用这个号用满的窗口的重置时间（两个都满就等更晚的），到点 `--bg --resume`（进程还在、闲着的直接发话，不复制副本）
并说一句「额度已重置，继续」。同一会话只叫一次。状态存在 `~/.claude-accounts/wake.json`（watchdog 替撞墙又没号可去的会话排的
条目带「撞墙才叫」标记）。

提醒点不等撞墙：撞墙后模型调不了 API，没法自己换号，所以由 cca 在到点时代为处理。5 小时窗口涨得快（几条会话一起用，几分钟就是几个点），
定在 95%；7 天窗口涨得慢，98% 也来得及换号。换号后第一轮要重新读一遍整段上下文（不走缓存），快做完时不值得，所以有 `--stay` 和 15 分钟 / 闲置两条例外。
在终端里也能看：`cca quota-hint <号>`（某个号的情况和命令），`cca quota-hint`（现在先用哪个号）。

**v2 相对旧版的变化**（升级后注意）：没有每条会话各挂一个换号帮手了；wake 没有自己的常驻进程；`cca watchdog order`、`--order`、
`cca <号> --watchdog`、`cca set watchdog` 都删了（目标号统一按上面的挑法，不再有第二套排序）；交互会话撞墙后不再被自动用
`--bg --resume` 复制出副本来救；`cca watchdog` 不带参数 = `status`（不会跑一轮）。

### 停、裁、删

```bash
cca stop-bg 001                 # 停掉 001 上没在跑的后台会话（只停不删，对话保留）
cca stop-bg 001 --force         # 连在跑的一起停
cca stop-bg --all --force       # 换 workspace 前：所有号一起停并记下清单（在会话里敲时本会话最后停）
cca resume-bg                   # 到新 workspace：照清单在原号续上（原来在跑的接着做，挂着的只挂回）
cca trim <会话id> --dry-run     # 会话涨过端点上限、连压缩都发不出去时：裁掉早期历史（先备份）
cca rm 003                      # 删号：打印要删什么 → 输入号名确认
```

`cca rm` 删的是 `~/.claude-accounts/<号>` 整个目录（凭据、`.claude.json`、登记、daemon、jobs）。
删不掉、也不该指望它删的：对话记录（在共享池里）、服务端账号状态、订阅与账单。闸门：

| 闸门 | 原因 |
| --- | --- |
| 共享项被换成实体目录时拒绝 | 否则会连共享池一起删（实测发生过） |
| 有交互会话或在跑的会话时拒绝 | `--force` 会先停再删（对话保留） |
| 要求输入号名确认 | 删凭据不可恢复；脚本用 `--yes` |

想加回来：`cca add <号>`，历史对话都还在。改名用 `cca mv <旧号> <新号>`。

## API 号

除了订阅号，cca 也能管**用 API key 打 Anthropic 兼容端点**的号。端点二选一：

```bash
cca add ds --provider deepseek --key-stdin       # ① 预设：端点、模型、密钥变量名都内置
cca add sub --url https://gw.example.com --key-stdin   # ② 自备端点：中转 / sub2api / 自建网关

cca ds                         # 启动
echo "$KEY" | cca login ds --key-stdin   # 换 key（不用重启）
cca logout ds                  # 删 key（配置与对话保留）
cca add --help                 # 配方都在这里
```

**事后改配置不用重建**：对已存在的 API 号，`cca add` 只改这次给的字段，并列出改了什么。

```bash
cca add sub --model deepseek-flash[1m]          # 所有槽位都用这个模型
cca add sub --env CLAUDE_CODE_EFFORT_LEVEL=max  # 加 / 改一个变量；--env FOO= 删掉
cca add sub --url https://new.example.com       # 换端点
cca model sub --opus gpt-6.1-sol --haiku gpt-6-luna   # 单独改某一档
cca models sub                                  # 端点上有哪些模型
cca model sub --follow                          # 把同档位的新版本升上去
```

**模型槽位**：

| 槽位 | 环境变量 | 不设时 |
| --- | --- | --- |
| 主模型 | `ANTHROPIC_MODEL` | Claude 自带默认 |
| `opus` / `sonnet` / `haiku` / `fable` 别名 | `ANTHROPIC_DEFAULT_<档>_MODEL` | Claude 真实模型 id |
| 子代理 | `CLAUDE_CODE_SUBAGENT_MODEL` | 跟主模型 |

不给 `--model` 时发 Claude 原生模型名，由端点自己路由——转发 Claude 的中转就该这样；只有端点只认自家模型名
（DeepSeek、GPT/Gemini 网关、本地 vLLM）才需要设。

**上下文窗口（非 Claude 模型必看）**：Claude Code 不认识的模型名一律按 **200k** 窗口算，
`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 只能往下压、抬不上去。模型真有 1M 窗口时，**模型名后面加 `[1m]`**
（`gpt-6-astra[1m]`；这个后缀只给客户端看，不会发给端点），再按需设压缩点：

```bash
cca add relay --model 'gpt-6-astra[1m]' --env CLAUDE_CODE_AUTO_COMPACT_WINDOW=900000
cca relay -p /context     # 核对：应显示 / 900k
```

不设压缩点时，`[1m]` 模型默认在 1M 的约 79% 处压缩。订阅号和 `claude-…` 模型不用管，Claude Code 认识它们。

其余要点：

- **配置**：`<号目录>/.cca-profile.json`（0600）是真相来源，`settings.json` 由它渲染；密钥单独放 `.api-key`（0600），
  经 `apiKeyHelper` 交给 claude——同时进 `Authorization` 和 `x-api-key`，不写进配置、也不进环境变量（agent 跑的 Bash 读不到）。
- **key 的给法**：`--key-stdin`、环境变量（预设号用 `$DEEPSEEK_API_KEY`，通用端点 `$ANTHROPIC_API_KEY`）、`--key <key>`（会进 shell history）。
- **代理**：API 号默认不配，端点直连；`--proxy http://host:port` 给非端点流量（自动更新、遥测）走代理，端点仍放进 `NO_PROXY`。
- **第一方工具自动排除**：第三方端点不认 Artifact 工具的 schema，cca 在 settings 里关掉工具集并在启动参数里禁用，两条都要（CLI 自己重启会话时会丢启动参数）。
- **后台会话**：`cca <api号> --bg '<任务>'`，目录要已信任（`cca add` 会继承其它号已信任的目录）。
- 需要协议转换或按请求路由（OpenAI / Gemini 原生端点、fallback）时，把 `--url` 指向 LiteLLM、claude-code-router 这类网关——cca 不做代理。

### OpenRouter：一个 key、多模型、按会话选

```bash
cca add or --provider openrouter --key-stdin            # 默认模型 openrouter/free（零成本）
cca or --model qwen/qwen3.8-27b:free -p "…"              # 这次启动用谁
cca or --model ~anthropic/claude-opus-latest             # 另一个终端同时用另一个模型
cca model --list --free                                  # 目录：免费模型 + 窗口 + 价格
cca model or --main openrouter/free --opus ~anthropic/claude-opus-latest   # 改号的默认
```

- `--model` 只影响这次启动；会话里 `/model` 按 Enter 改的是号默认，按 `s` 只改本次。
- 预设把四档别名映到 `~anthropic/claude-{opus,sonnet,haiku,fable}-latest`，打开网关模型发现（`/model` 能列出网关的模型）；
  工具搜索故意不开（多数开源模型不支持）。
- 写了 `--model <slug>` 时，cca 查 OpenRouter 目录给这次会话设压缩窗口（模型窗口 × 0.9，`CCA_COMPACT_WINDOW_RATIO` 可调）；
  `claude-…` 模型不设，目录里没有的不设并提示。
- 目录公开免 key，缓存 1 小时（`~/.claude-accounts/.cache/openrouter-models.json`），拉不到就用旧缓存，不阻塞启动。

### 一个端点后面挂了好几个模型

```bash
cca add relay --url https://relay.example.com:8080 --key-stdin \
  --env 'ANTHROPIC_MODEL=gpt-6-astra[1m]' \
  --env 'ANTHROPIC_DEFAULT_OPUS_MODEL=gpt-6.1-sol[1m]' \
  --env 'ANTHROPIC_DEFAULT_HAIKU_MODEL=gpt-6-luna[1m]' \
  --env 'CLAUDE_CODE_SUBAGENT_MODEL=gpt-6.1-sol[1m]' \
  --env CLAUDE_CODE_AUTO_COMPACT_WINDOW=900000

cca relay                       # 主模型
cca relay --model opus          # 这次用 opus 档映射的模型
```

- `ANTHROPIC_MODEL` 优先于 `settings.json` 的 `model` 字段，env 配好就够了。
- 推理档 `effortLevel` 写在 `settings.json` 的 `modelSettings.<模型名>.effortLevel`；`cca model <号> --effort high` 会替你写。
- API 号之间没有互斥：`cca ds --bg …` 和 `cca relay --bg …` 可以同时跑。

### 第三方端点排障

| 现象 | 处理 |
| --- | --- |
| `401` | key 无效或被吊销；`cca doctor --online --probe-key` 真发一次最小请求验证 |
| `400` 提到 `context_management` / `Extra inputs are not permitted` | `--env CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` |
| `400` 提到 `thinking` / `adaptive` | `--env CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1` |
| `400 … tool_use.signature` | Gemini 号跑过的历史；用 cca 续（`-r` / `handoff` / 接管）会自动清掉 |
| `400` 上下文 / 输出超限 | 先看模型名有没有 `[1m]`、有没有压缩点；已经超过端点上限时只能裁：`cca trim <会话> --dry-run` |
| 老是压缩、`/context` 显示 200k | 模型名没加 `[1m]`，见上文「上下文窗口」 |
| 日志里 `[claude-code:unrecognized_model]` | 正常：模型名不在 Claude Code 的目录里 |
| `/fast` 报网络或组织限制 | `CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS=1` / `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK=1` |
| Remote Control / 语音输入不可用 | 网关凭据下官方禁用，切回订阅号 |
| `settings.json` 与清单不一致 | `cca doctor` 会报，`cca sync <号>` 重渲染 |
| 端点通不通 / key 能不能用 | `cca doctor --online`（不带凭据探活）/ `--probe-key`（带密钥，`max_tokens=1`）：2xx 有效，401/403 被拒，5xx 是端点自己的问题，与 key 无关 |

## sideagent：让另一个号并行干活

sideagent 是另一个号上的完整 Claude Code 会话：自己的上下文、工具和额度，干完自动把结果回报给派活的会话。
用法照子代理（Agent / SendMessage），区别是**模型和额度由你挑**。派活、追问、接管都在**对话里用工具**；
什么时候派、派给谁、交办怎么写，见共享 skill `sideagent`。

```bash
cca mcp install --all          # 注册工具；cca add / login 成功后会自动注册，重启会话生效
cca plugin install --all       # 面板插件；make install 和 cca add 已经顺手装了
```

| 工具 | 干什么 |
| --- | --- |
| `sideagent_accounts` | 能派活的号：类型、模型家族、额度满没满、在跑几个 |
| `sideagent_send` | `account` 写号名 = 派新活（新实例，零上下文，干完退出）；写实例全名（`ds#a1b2c3`）= 追问那一个；写 `session` = 给别的会话发话（见下） |
| `sideagent_result` / `sideagent_status` | 取结果 / 看状态（`doing` 是它这会儿在干什么） |
| `sideagent_interrupt` / `sideagent_stop` | 打断这一轮（进程留着）/ 整个停掉 |
| `sideagent_takeover` / `sideagent_release` | 接管已有的会话 / 从面板拿掉 |

- **追问**：它在忙，消息在它下一个工具调用结束时送到（`mode: "queue"` 则等这一轮干完再开一轮）；已经退出就用它的会话记录接着开。
- **回报**：后台派的活干完以 `<task-notification>` 自动送回（`notify: false` 就不送）；不要轮询。
- **其它参数**：`description`（3–5 个词，横条和回报摘要用）、`model`（这个实例用哪一档）、`keep: true`（干完不退，最多闲置 30 分钟）、`include_context`（带上主会话最近的对话）。
- 状态里 `stale: true` = 实例是 cca 升级前起的，新功能用不上，停掉重派。

### 给别的会话发话

```
sideagent_send {session: "<编号前缀|完整编号|标题>", message: "..."}
```

不管它在哪个号、在跑还是已经 `done` / `stopped`，都能送到：cca 跨号找到这条会话现在挂的号
（在跑的优先；否则最近一次登记的；handoff 留下的 `stopped` 旧登记排最后），没在跑就在那个号上
`--bg --resume` 唤醒再投；那个号额度用满就换一个能用的号接着跑同一段对话。投的是原生跨会话消息，
对方回话会回到你这里。**不接管、不进面板**，它的进度不会灌进你的上下文。

原生 `SendMessage` 只认本号正在跑的会话，发不到时（not reachable、`ListAgents` 里没有）就用这个——
会话没有「下线」这回事，也不用「等它上线」。只想看它在不在、在哪个号，用只读的 `cca sessions` 或
`cca sideagent takeover-candidates --all`。

### 接管：在面板上持续看一条会话

`sideagent_takeover {session: ..., account: ...}` 把一条已有的会话挂到当前会话的面板上：它的进度、
完成通知会一直送进当前会话，之后用返回的名字（如 `003#bg-36709e4a`）`sideagent_send`。适合要一起盯着干的活；
只发一句话用上面的 `session`，不用接管。主会话退出时不停它；`sideagent_release` 只从面板拿掉，
要停用 `cca <号> stop <短id>`。面板里 `/cca takeover [第一句话]` 打开挑选框。

### 面板

输入框上方一个 sideagent 一行：谁在跑、在干什么（它那个号的小模型每 30 秒写一句进度，`cca set summary <号>` 改用哪个模型）、
用了多久、多少 tokens。点一行看详情，底下能直接打字插话。鼠标点击要全屏渲染（`cca` 启动的会话默认就是；`cca set tui default|fullscreen` 切换）。

### 接管会话的「请确认」在面板里点

接管来的会话如果要人确认工具调用，装上权限钩子后确认会转到面板：那一行标黄，点开按 `y` 允许、`n` 拒绝，600 秒没人点按拒绝处理。
没被接管的会话不受影响（照常弹原生确认框，每次多约 80 毫秒）。

```bash
cca sideagent hook install --profile ds2     # 装到一个号（会先备份 settings.json）
cca sideagent hook install --all
cca sideagent hook uninstall --profile ds2
```

状态文件、归属怎么认、接管怎么同步、子代理派的 sideagent 怎么回报、怎么端到端测，见 [docs/internals.md](docs/internals.md)；
和 Claude Code 子代理的逐项对照见 [docs/sideagent-vs-subagent.md](docs/sideagent-vs-subagent.md)。

## 维护

### 代理与出口

**没有默认代理**：订阅号没配代理就拒绝启动，避免账号从不明出口出去。

```bash
cca set proxy --global http://host:port   # 所有号的默认（写进 shell 启动文件，新终端生效）
cca set proxy 002 http://host:port        # 单号，优先于全局
cca set proxy 002 none                    # 明确直连
cca set proxy 002                         # 看当前值
```

只支持 `http://` / `https://`（Claude Code 不认 socks）。环境变量 `CCA_PROXY` 也当全局默认读。
启动时经代理查一次出口：

- 出口在 Claude 不支持的地区（如中国大陆、香港）直接拒绝启动。
- 传给 claude 的 `TZ` 跟出口 IP 走（探测失败用 `Asia/Singapore`）；`CCA_TZ=auto` 跟本机，`CCA_TZ=<IANA 名>` 写死。
  已有会话的时区和出口对不上时会拦住新启动，`cca doctor` 会列出这些会话并问要不要强杀。`cca ls` 等终端显示固定上海时间。
- `LANG` 固定 `en_US`；默认关遥测和错误上报，不影响自动更新。

### 老机器上的 001（一次性迁移脚本）

所有号（包括 001）都住在 `~/.claude-accounts/<号>`；`~/.claude` 只放共享资产
（`projects/` `skills/` `plugins/` `CLAUDE.md`），不属于任何号。cca 不再认"住在 `~/.claude` 的 001"。

老机器上 001 还在 `~/.claude` 的，跑一次脚本搬过去：

```bash
python3 scripts/migrate-001.py --dry-run   # 看哪些搬走、哪些留下
python3 scripts/migrate-001.py             # 先备份到 ~/.claude-accounts/_backup-001-<时间>/，再搬
```

账号自己的东西（凭据、`settings.json`、daemon、jobs、history…）和 `~/.claude.json` 一起搬到
`~/.claude-accounts/001`；共享资产留在原地，新目录里建软链。`~/.cca/defaults/settings.json`
还不存在时，顺手用 001 的 `settings.json` 生成一份（新号播种都读它）。没搬之前，cca 看不到这个 001。

### 新号的首次启动

新号第一次交互启动本来会走引导（选主题、再登录一次）、并逐个目录问「是否信任」。cca 替你处理：
启动前补 `hasCompletedOnboarding`；`cca login` / `cca add` / `cca sync` 把其它号已信任的目录补进来。
两处都只加不减，不碰账号字段。

### 体检：`cca doctor`

离线检查软链、登录态、后台会话（含陈旧登记）、代理、会话时区、API 号的清单 / 密钥权限 / 端点 DNS / settings 漂移；
`--online` 加出口 IP 和端点探活，`--probe-key` 带密钥验证 key。

## 合规边界（硬性）

- 一个号对应一份**你本人拥有、仅你本人使用**的订阅；不得借给他人。
- 只用官方 `claude` 客户端；订阅凭据不交给任何第三方 harness。
- API key 必须本人所有、仅本人使用，不得共享或转借。
- 发给第三方端点的内容受该供应商政策约束——内部代码与数据是否外发，自行判断并遵守组织规定。
- 保持单一出口，不要按账号分流代理。
- 人工切换账号；本工具不提供跨账号自动接力。
- 不做任何身份伪装：不改出口、不伪造设备指纹、不拆支付方式。
- 保留订阅发票 / 收据——遇到误封时，这是「账号属于本人」最有力的申诉材料。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `没有配置代理` | `cca set proxy <号> http://host:port` 或 `--global`；明确直连写 `none` |
| 代理不通 | `cca doctor`（代理解析）/ `cca doctor --online`（出口 IP） |
| `有正在跑的会话时区不对` | `cca doctor` 列出并问是否强杀；强杀后新会话就用出口时区 |
| `loggedIn: false` 但明明登录过 | 没迁移的 001 被设了 `CLAUDE_CONFIG_DIR`（见「把 001 搬成普通号」） |
| `-r` / `/resume` 看不到旧会话 | `/resume` 只看当前目录：`cca sessions` 跨目录找；还没有就 `cca doctor` 看 `projects` 软链是否被换成了实体目录 |
| 新号第一次进去要求登录 / 走引导 | 首次引导：`cca <号>` 启动前会自动补标记 |
| `cca status` 凭据已过期 | 正常：access token 小时级，启动时自动续；看`重新登录`那一行 |
| `cca stop-bg` 报 `No job matching` | 那条是交互会话（没有 job id），cca 会跳过并说明 |
| `doctor` 报会话「已退出但仍在后台清单里」 | `cca <号> rm <id>` 清登记（对话记录不受影响） |
| 切号时提示其它号有挂起的后台会话 | 不用处理：`done` / `blocked` 不耗额度，只是别去回应那些 `blocked` 会话 |
| 切号时警告其它号仍有会话在跑 | 两个订阅号同时跑会同时耗额度；看 `cca status <号>`，等它跑完或 `cca <号> stop <id>`。API 号不参与这条约定 |
| flag 拼错 | 直接报错并猜你想用哪个；未知参数和多余的位置参数都不会被静默忽略 |
| 第三方端点 400 / 401 | 见「第三方端点排障」 |
| sideagent 报 `卡住了：N 秒没有任何输出` | 一轮里 900 秒（`CCA_SIDEAGENT_STALL` 可改）一行输出都没有。门槛比 Claude Code 自己重试 API 的最长静默还长，它在重试时面板和 `sideagent_status` 会显示「API 没回应：504 网关超时，第 2/10 次重试」；卡住的文案会带最后一次 API 错误。内网网关号（`.example.com` 上的 gemini、gpt、cc）推理模型首字常要几分钟，cca 默认给它们 `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS=330000`（比网关 300 秒的掐断略长）；要改用 `cca add <号> --env CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS=<毫秒>` |

## 开发

```bash
make test     # 标准库 unittest，零网络
make lint     # ruff
```

`VERSION` 是版本的唯一来源。展示类命令（`ls` / `status` / `sessions` / `doctor`）的表格用 `rich`，延迟导入：
缺 rich 时只有这些命令会报错。设计文档在 `docs/specs/`（总体设计 `design.md`），实现细节见 `docs/internals.md`。
