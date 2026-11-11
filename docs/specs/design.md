# claude-acct 设计

相关：`api-profiles.md` —— API 号（API key + 第三方 Anthropic 兼容端点）。

状态：v0.3.0 已实现（展示层改用 Rich，与 podjob / podgraph 同源）；v0.2.0 起与远端 `platform/claude-acct`（private）同步。按 podjob / podgraph 的惯例不挂 CI：`make test` / `make lint` 在本机跑。
适用范围：单机 / 单个 Coder workspace 内的多 Claude Code 账号配置管理。

## 1. 目标与非目标

**目标**

* 多个 Anthropic 账号在同一台机器上各有独立登录态。
* 技能、插件、用户级 CLAUDE.md 只维护一份。
* 会话记录共用：任何账号都能 `-c` / `-r` 续任意历史会话。
* 把"串行使用、单一出口、不共享"这些约定变成工具行为，而不是靠记性。

**非目标（明确不做）**

* 不做身份伪装：不改出口 IP、不伪造设备指纹、不拆支付方式。
* 不做跨账号自动接力（一个号撞限后自动把任务交给下一个号）。
* 不管理第三方 CLI（codex / kimi 等）的配置。
* 不读取凭据内容 —— 只判断文件存在性；唯一例外是 `cca status` 的到期时间，用正则取 `expiresAt` / `refreshTokenExpiresAt` 两个数字字段（见 §4）。

## 2. 数据模型

```
profile := 一个有名字的 Claude Code 配置目录
  001  → ~/.claude-accounts/001       和其它号一样（老机器用 scripts/migrate-001.py 搬过来）
  002+ → ~/.claude-accounts/<name>    CLAUDE_CONFIG_DIR 指向它
```

身份事实只有两个来源，不重复存储：

* `<配置目录>/.credentials.json` —— 登录态（只判断存在性）
* `<配置目录>/.claude.json` —— `oauthAccount` 元数据（邮箱、档位、额外用量开关）

唯一的派生数据是 workspace 绑定表 `~/.claude-accounts/bind.tsv`：

```
# workspace_uuid<TAB>profile
89721061-043c-4e65-bbad-20f845422ef4	001
ab1c8ca7-828a-49f0-94ff-c2833aef44a1	002
```

用 workspace UUID 而不是 workspace 名：UUID 在 rebuild / 改模板参数时保持不变，
只有删除重建才变，是稳定的判别键。

## 3. 共享与隔离矩阵

| 路径 | 处理 | 原因 |
| --- | --- | --- |
| `projects/` | 软链到 001 | 会话记录唯一来源，跨号可续 |
| `skills/` `plugins/` | 软链到 001 | 只安装一次 |
| `CLAUDE.md` | 软链到 001 | 上下文单一来源 |
| `settings.json` | 建号时播种，之后各自演化 | CLI 会写它，文件软链有被 rename 重写替换的风险 |
| `.credentials.json` | 独立 | 隔离目标本身 |
| `.claude.json` | 独立 | 含账号元数据，共享会显示错账号 |
| `daemon/` `jobs/` `sessions/` `tasks/` `telemetry/` `history.jsonl` | 独立 | 并发写入边界与后台会话归属 |

`doctor` 会检查四条软链是否仍然指向 001，尤其检测"被 CLI 重写成普通文件/目录"的情况。

## 4. 命令面与选号

每个子命令都支持 `cca <命令> --help`（`COMMAND_USAGE` 表）：打印该命令的用法、闸门与例子，
免得为了查一个 flag 去翻 README。未知命令/无此表时回落到全局 usage。


命令见 README。**选号只有一种：命令里写的号** —— 默认号、workspace 绑定、`$CLAUDE_ACCT`
这三层已删除（决策与理由见 `explicit-profile.md`）：

```
cca <profile> [claude 参数…]        要落到某个号的唯一写法；不写号直接报错并给用法
cca <全局命令> […]                   ls / doctor / add / new / rm / login / logout / sync / carry
cca sessions                        共享池里的可续会话（会话不绑账号，任何号都能续）
```

第一个位置参数固定是 profile 名，其余参数原样透传给 `claude`，因此
`cca 002 -c`、`cca 002 -r <id>`、`cca 002 -p "..."` 都直接可用；
子命令名（`new`/`login`/…）与已移除命令名（`switch`/`bind`/`unbind`/`resume`）都保留，
profile 名不得与它们同名。

带 flag 的子命令（`carry` / `stop-bg`）走同一个 `parse_flags()`：**未知 flag 一律报错**
（附 `difflib` 猜的"是想用 --x 吗"），多余的第二个位置参数也报错。这条是踩出来的 ——
`cca carry ds 003 --olny=x` 曾被静默当成"没写 flag"，看着像成功了其实什么都没搬，
是所有失败方式里最坏的一种。只有 `=` 形式带值（`--only=<前缀>`），所以裸写 `--all` 与
`--only` 是两件事，不会把后面的位置参数吃掉。

`cca carry <源号> <目标号>` 是搬运会话的唯一入口（原先挂在 `cca switch --carry` 上，默认号取消后
独立成命令）：对每条选中的会话，源号 `stop` → 目标号补该目录的信任位（`--bg` 拒绝未信任目录）→
在会话起始目录下 `--bg --resume <完整id>`。动因是 agents 面板：面板读的是**该号 daemon 注册的
后台会话**，共享 transcript 只能 `--resume` 续，不会让会话出现在另一个号的面板里；
`--bg --resume` 一次才会（落成 `completed` / `need inputs`）。因此 `--all` 或显式 `--only` 点名
才会带上"正在跑的"（resume 会继续干活、烧目标号额度），交互回车只带没在跑的。
`--dry-run` 只打印计划 —— 不停会话、不接管。非 tty 必须给 `--only` 或 `--all`。

**会话不绑账号**：所有号的 `projects/` 是同一份目录，任何号都能续任何会话；按账号区分的只有
"谁在跑它"（各号 daemon 的后台登记）。所以 `cca carry` 搬的是"谁在跑"，不是会话文件本身。

`cca stop-bg <profile> [--force]` 是单独收尾某个号的命令：只 `stop` 不 `rm`（对话可 `attach` 回来），
默认跳过正在跑的会话。它与 `carry` 共用同一个 `stop_sessions()`。

`cca ls [--wide] [--json]` 是唯一的**号面板**：一行一个号 —— 身份 + 额度 + 在跑后台数。
额度读各号 `.claude.json` 的 `cachedUsageUtilization`（Claude Code 在会话启动/结束刷新的
快照），**不联网、不读凭据**。三个渲染约定：

* **时间一律绝对时刻**（本地时区，`09-25 02:00`）。相对时间（`3 小时后`）要读的人自己做减法，
  还丢掉"重置窗口跨没跨天"这个关键信息 —— 它直接决定明天还能不能跑。
* 窗口的 `resets_at` 已过去时标 `reset_passed`，百分比后加 `*` 并在表尾说明那是重置前的旧值。
  实测踩过：002 缓存写着 5 小时 98%，其实窗口刚清零。
* 加粗的是该号最紧的那个窗口（瓶颈）；`roomiest` 只进 `--json`（按最紧窗口的有效余量算、
  重置已过的按 0），面板不显示 —— 使用者明确表示这一列没必要。
* 后台列给 **在跑/总数**：只给在跑数会让一条 `blocked` 的挂起会话看起来像不存在。
* 快照超过 `USAGE_STALE_SECONDS` 标黄并给出绝对时刻。

**列宽自己算，绝不裁列**：Rich 对 `no_wrap` 的列按"最长内容"取最小宽度，几列一撑超过终端
宽度就静默裁掉尾列（实测 72~96 列下 `ls` 的"登录/后台"整列消失）。现在的做法是先按内容量出
各列宽（`display_width`，中文按 2 列），再用 `console.measure()` 量出"邮箱列宽为 0 时的表宽"，
把剩下的都给邮箱列；窄的时候按"邮箱先省略 → 邮箱省略 → 邮箱整列让位"的固定顺序牺牲。
`test_ls_fits_narrow_terminal_without_cropping` 在 60/72/80/96 列下断言表头齐全且不超宽。

`cca status [<profile>] [--json]` 不带号 = **每个号一段摘要**；点名一个号 = 单号详情：目录 /
类型 / 登录 / 档位 / 额外用量 / 凭据与重新登录到期 / 额度 / 快照 / **后台会话明细**
（id、状态、名字、目录 —— 面板只给在跑数）。

**会员到期时间不做**：本地只有 `billingType` / `subscriptionCreatedAt`（创建日，通常是几个月
前，对"什么时候到期"没有价值），`.credentials.json` 只有 token 到期，CLI 不缓存账单周期，
二进制里也没有 `current_period_end` 一类字段；月付/年付无从判断，升级/暂停还会挪动锚点。
先做过一版"创建日 + 整月"的推算，实测对不上且被要求"按能拿到的客观信息来"，于是整块删掉 ——
`status` 里能可靠给出的"到期"只有 token 与刷新令牌两行。真实续费日看 Apple/Stripe 账单。

凭据到期是本工具唯一读 `.credentials.json` 的场合，且刻意不 `json.loads` 整个文件：正则取
`expiresAt`（小时级，过期不影响使用，claude 会用刷新令牌自动续）与 `refreshTokenExpiresAt`
（月级，到期要重新登录），token 既不进内存也不进输出 ——
`test_status_shows_credential_expiry_without_leaking_token` 除了断言时间出现，还断言 token
字符串不出现在 stdout/stderr。

`cca rm <profile> [--yes] [--force]` 删掉某号的**本地配置目录**（凭据、`.claude.json`、
`sessions/`、daemon、jobs、本号 file-history / telemetry）。语义边界写在命令输出里：删的是
"这个号在这台机器上的东西"，**不是**这个号的历史（transcript 在共享池）与服务端状态（限制、
订阅账单）。四道闸 + 一次确认：默认号 001 拒绝（共享资产来源）；共享项若被 CLI 换成实体目录
拒绝（否则会连共享池一起删，实测发生过；`rmtree` 不跟软链，但实体目录会）；有交互会话或在跑
会话拒绝（`--force` 先停再删，停在跑的是有破坏性的，所以必须显式）；删除前打印文件数/体积并
要求**输入 profile 名**确认，`--yes` 供脚本/无 tty（读不到输入也报错而不是默认继续）。
删除后提示 `cca add <profile> --email …` 可加回来（历史对话都还在共享池里）。

`cca sessions [--all] [--wide] [--json]` 跨目录列出共享池里的可续会话（**会话不绑号**，项目 / 标题 /
最后提问 / 最后活动 / 归属），补的是 CLI 的空缺：`/resume` 选择器与 `-c` 都只看当前目录，
按 ID `-r <id>` 才跨目录可用。归属只在仍有 live 会话时给得出（来自各号 `claude agents`），
历史会话标"归属未知"——transcript 不记账号，工具不猜。`--wide` 不截断（加宽控制台 + 单元格溢出
而不是省略/折行），`--json` 给出结构化条目（含 `live` 归属）。

续会话走 claude 自己的参数：`cca <号> --resume <完整id>`（`-r` 同义、`-c` 续该号最近一条）。
**用哪个号续就烧哪个号的额度、走它的端点**；前缀/标题的模糊解析随 `cca resume` 一起删除了 ——
完整 id 从 `cca sessions` 里拷（那一列给的就是完整 id）。

`cca list`（别名 `cca ls`，`ccl` 是同一文件软链）与 `cca doctor` 也提供 `--json`；
`--json` 只影响输出，不改退出码语义。

`cca add <profile>` = `new` + `login`（目录已存在则跳过建号）：新号从建到能跑只需一条命令，
登录成功后的引导标记与目录信任播种复用 `cmd_login` 的那一步。

`cca <profile>` 启动前会补一次全局配置的 `hasCompletedOnboarding`：建号与 headless 登录都不写
这个标记，新号第一次交互启动会把"其实已登录"的配置当成全新安装，从选主题开始走引导、
甚至在引导里再要一次登录。只补这一个布尔，不碰 `oauthAccount`；`.claude.json` 不存在时不创建。

`cca login <profile>` 成功之后（此时全局配置刚生成）会做一次性播种：补 `hasCompletedOnboarding`
+ 把 001 已信任的目录（只有路径 + `hasTrustDialogAccepted`）合并进来；`cca sync <profile>` 也能
重跑这两步。规则是**只加不减**：目标号自己的信任记录、账号字段都不动，001 明确没信任的目录
（`hasTrustDialogAccepted` 非 true）不继承。动机与引导标记同源 —— 同一台机器、同一个人的第二个号，
不该在每个目录重新答一遍信任确认。

## 5. 环境构造

`build_env(profile)` 在继承当前环境的基础上：

1. 按 `proxy_settings()` 设置代理：默认注入内建代理（`DEFAULT_PROXY_URL`，与 `~/.bashrc` 中原
   `cc` 别名一致）；`CCA_PROXY=none/off/-/空` 表示"代理这事别管" —— 不注入内建代理，
   继承来的 `HTTP(S)_PROXY` 原样保留（不做清理，避免破坏别人 shell 里已有的代理设置）；
   `CCA_PROXY=<url>` 换成该地址，`NO_PROXY` 仍用内建那份（不把集群内网与 `.example.com`
   塞进代理）。
2. 非 001 号设置 `CLAUDE_CONFIG_DIR`；001 号**显式删除**该变量。

第 1 条的动因是实测故障：内建代理只在本集群存在，别处无条件注入会让 CLI 卡在
`getaddrinfo ENOTFOUND proxy.xray.svc.cluster.local`（登录页表现为 `Login failed`）。
默认仍保持"注入"，但给出开关，并让 `doctor` 显示代理状态、对代理主机做一次解析检查，
把问题提前暴露。

第 2 条是实测踩过的坑：`CLAUDE_CONFIG_DIR=$HOME claude auth status --json`
返回 `loggedIn: false`，因为凭据在 `$HOME/.claude/.credentials.json` 而 CLI 去找
`$HOME/.credentials.json`。`doctor` 会对当前 shell 里已设置的 `CLAUDE_CONFIG_DIR` 直接报 FAIL。

## 6. 串行使用守护

判据是 `claude agents --json` 给出的**权威后台会话状态**，用白名单排除"已停下"的状态
（`idle` / `blocked` / `done` / `failed` / `parked` / `stopped` / `exited` / `completed`）；
其余状态（`busy`、`working`、回退判据的 `unknown`、以及将来新增的词）一律保守地算作"在跑"。
实测里确实出现过 `busy` 之外还在消耗的 `working`，所以白名单比黑名单可靠：
新状态词只会让提醒偏保守，不会让守护静默失效。

* **API 号不参与这套约定**（`docs/specs/api-profiles.md`）：串行是为了"同一个订阅别被多号并行
  消耗"，而 API 号是另一家供应商、按量计费、不占 Claude 额度 —— 让 API 号跑批量的同时用订阅号
  干活是正常用法。判定里只要涉及 API 号就不提；两个订阅号同时跑才警告（`doctor` 同理，并把
  "API 号在跑"作为一行 dim 信息列出）。
* `cca <profile>` 启动前，若**其它** profile 有在跑会话 ——
  stderr 警告（带会话名）；若只有挂起会话 —— 降级为一行"提示"，不要求行动。
* 交互式终端额外打印一行 `→ 账号 002 [email]` 横幅；非 tty 不打印，避免污染管道。
* `doctor` 分列"在跑"与"挂起"，仅在多个 profile 同时有在跑会话时给"建议串行使用"。

回退：`claude agents` 不可用（未安装 / 非零退出 / 输出非 JSON / 超过 `AGENTS_TIMEOUT`）时，
退回 `daemon/roster.json` 的 pid 存活判断，状态记 `unknown`，并保守地按"可能在跑"处理。

不再把 roster 的 pid 存活当作主判据 —— 它把两件事混在一起：空闲预热的 `bg-spare`
进程和被回收的 pid 都会算成"在用"，而进程已退出但仍在清单里的会话又会被漏掉。
实测中 8 个后台会话只对应 5 个存活 worker。`claude agents` 仅在该 profile 已存在
`daemon/` 时才调用：新配置目录首次运行 claude 会自建 `.claude.json` / `daemon/` 等文件，
只为看一眼后台会话不该产生这种副作用。

选择"只提示不阻塞"：陈旧 PID 或误判不应让工具无法使用。

### 6.1 什么算"可停的后台任务"

`claude agents --json` 的条目里混着两类东西，`stop` 只认其中一类：

| 条目 | 判据 | `claude stop` |
| --- | --- | --- |
| 后台任务 | `kind: background` 且有 `id`（job id） | 认，按 job id 停 |
| 交互会话 | `kind: interactive`、`id: null`（只有 `sessionId`） | **不认**，回 `No job matching '<id>'` |

所以 `stop-bg` / `switch` 只对前者动手，后者单独回报一行"跳过 N 条非后台任务（…）——
交互会话不是 job"，不再拿 `sessionId` 前缀去撞 `No job matching`。
`stop` 用的是 `job`（真 job id）而不是展示用的短 id；展示短 id 在 `id` 缺失时退化取
`sessionId` 前缀，只用于看和匹配，不能当 job id 使。roster.json 回退判据里，worker 的 dict
key 就是 job id（`sessionId` 是另一个字段），所以那里也取 key。

同理，`--carry` 的候选里排除交互会话：那是别人终端里正开着的会话，`--bg --resume` 只会
fork 出一份副本（双份上下文、双份额度），没有收益。

## 7. doctor 检查项

离线（默认）：配置目录与凭据存在性 · 四条共享软链的指向与完好性 · `settings.json` 存在性 ·
账号邮箱可读性 · 当前 shell 的 `CLAUDE_CONFIG_DIR` 污染（FAIL）· `CLAUDE_ACCT` 覆盖绑定（WARN）·
多 profile 并行后台会话 · 代理状态与代理主机可解析性（WARN）· 陈旧后台会话登记（WARN）。

陈旧登记的判据是 `claude logs <id>`：进程已退出时它返回 `job not found — it may have already
exited`。只查没在跑的会话（在跑的不用查），命中就提示 `cca <profile> rm <id>` 可清掉 ——
**只提示，不自动删**，与 `stop-bg` 不自动 `rm` 的原则一致。

`--online`（可选）：逐号 `claude auth status --json` 是否为 `loggedIn: true`；
经代理查询出口 IP 与地区，并提示对照支持地区列表自查（不硬编码地区清单，避免过期）。

`--json`：输出 `profiles` / `proxy` / `running` / `hanging` / `warns` / `fails`，退出码语义不变；
JSON 模式不构造 Rich Console —— 排障命令要能在没有 Rich 的环境里出结构化结果。

退出码：存在 FAIL 返回 1，否则 0。

## 8. 实现与测试

* Python 3 单文件实现：业务逻辑仅用标准库（`json/os/re/shutil/subprocess/sys/time/pathlib`）。
  选 Python 而非 bash：要解析 `.claude.json`，bash 处理 JSON 易碎；同时便于测试。
* **展示层用 `rich==15.0.0`**，与 `infra/pod` 的 podjob / podgraph 同源。约定照抄它们：
  表格无边框（`box=None`）、粗表头、计数右对齐、文字列 `ratio` + `overflow`、时间列 `min_width=17`、
  表头中英混排（技术标识英文大写，人读字段中文）、状态着色集中在 `STATE_STYLES`。
  引入 Rich 的直接动因是 CJK 宽度：f-string 按码点补齐，中文列必然错位，Rich 按显示宽度排版。
* **Rich 一律延迟导入**：`make_console()` 内部才 `import rich`；只有
  `status` / `list` / `sessions` / `doctor` 四个展示命令走 Rich，`run` / `login` / `switch` /
  `stop-bg` 等不碰 Rich —— 这些是出问题时用来救火的命令，必须在任何环境下可用。
  架构测试守着这条：import 本模块不得加载 rich，`make_console()` 之后才允许出现。
  缺 Rich 时展示命令直接报错退出（`require_rich()`，与 podjob 做法一致），不做静默降级。
* `cca` / `ccl` 是同一文件的软链，按 `argv[0]` 分派。
* 版本唯一来源是 `VERSION` 文件，`cca version` 读取它；单测里有版本一致性用例。
* 测试：标准库 `unittest`（pytest 亦可直接运行），使用临时 `HOME` 与假 `claude` 桩，
  零网络。测试环境固定 `COLUMNS`，让 Rich 在非 tty 下也有确定的表格宽度。
  覆盖解析顺序、建号、环境构造（含 001 号 unset 陷阱）、参数透传、绑定、列表、同步、
  doctor 各分支、串行守护、可续会话、引导标记与信任继承，以及渲染（延迟导入 + 宽字符对齐）。
* 安装：`make install` 软链到 `~/.local/bin`。CephFS 共享家目录，装一次所有 workspace 可用。
* 不挂 CI：与 podjob / podgraph 保持一致，测试与 lint 在本机 `make test` / `make lint` 跑。
  （曾经有过一个零依赖的 test job；展示层需要 rich 之后它要么装包、要么跳过渲染用例，
  为了不给这么小的工具增加流水线负担，直接去掉。）

## 9. 验证记录

* 120 个 unittest 用例全部通过（本机 Python 3.12.3，rich 15.0.0），ruff 干净。
* 实测确认额度快照的两个坑：缓存里的百分比是**上次刷新时**的值（001 的 5 小时 80% 对应的
  重置时间早已过去，实际是新的窗口），且各号刷新时机不同（002 是 4 分钟前、001 是 30 小时前）。
  因此面板必须同时给出重置倒计时与缓存新旧，并把"最空闲"按重置后的有效余量算。
* 实测确认额度表不能按 `no_wrap` 的自然宽度排版：`no_wrap` 列的最小宽度等于最长内容，
  7 列一撑就把 80 列终端下的尾列裁掉（"最空闲"变成"最空"）。改成自己算邮箱列宽 +
  倒计时用紧凑写法（`3小时后`），80 列下整表刚好放得下。
* 实测确认删号需要多道闸：001 是共享资产的来源（删了连累所有号）、共享项被 CLI 重写成实体
  目录时 `rmtree` 会连共享池一起删、交互/在跑的会话会受影响。`cca rm` 因此拒绝前两种、对第三
  种要求 `--force`，并要求输入 profile 名确认（`--yes` 给脚本）。
* 实测确认**会员到期时间拿不到**：本地只有 `billingType` / `subscriptionCreatedAt`，
  `.credentials.json` 只有 token 到期，二进制里搜不到 `current_period_end` 一类字段。做过一版
  "创建日 + 整月"的推算 —— 实测对不上（001 月付但不是创建日那天），改成只显示创建日后使用者
  认为"创建日太早、没意义"，最终整块删掉：**拿不到就不显示**，只保留能可靠给出的 token 到期。
* 实测（使用者反馈）确认面板只给"在跑"数会误导：002 那条会话是 `blocked`（挂起、不消耗额度），
  面板显示 `-` 而实际有会话 —— 改成 `在跑/总数`；同时去掉"最空闲"列（反馈没必要）。
* 实测确认 Rich 表格会**静默裁列**：`no_wrap` 列的最小宽度等于最长内容，几列一撑超过终端
  宽度就丢掉尾列（使用者贴回来的 `cca ls` 里"登录/后台"两列直接没了，本机在 72~96 列下复现）。
  改成自己算列宽 + 明确的牺牲顺序，并加 60/72/80/96 列的回归测试。
* 实测确认相对时间在额度场景是信息损失：`3 小时后`/`1 天前` 要读的人自己做减法，还看不出重置
  窗口跨没跨天。全部改成绝对本地时刻（表尾标时区），`cca sessions` 的"最后活动"一并改。
* 实测确认 access token 到期（小时级）与"能不能用"无关：001 的 access 已过期 20+ 小时，仍能
  正常跑（claude 启动时用刷新令牌自动续）；真正决定要不要重新登录的是 `refreshTokenExpiresAt`
  （月级）。因此 `status` 两行都给，并说明哪一行才要行动。
* 实测确认 `cca usage` 与 `cca ls`/`cca status` 三处信息重叠（同一批数据三种排法），去掉
  `usage`：额度并入面板，详细口径进 `status`。命令面回到"面板 + 单号详情 + 体检"。
* 实测确认 `claude agents --json` 的条目里混着**交互会话**（`id: null` / `state: null` /
  `kind: interactive`，只有 `sessionId`）与**后台任务**（`kind: background` + job id）：
  前者的展示短 id 是 `sessionId` 前缀，拿它去 `claude stop` 只会得到 `No job matching '<id>'`
  （同事的 `stop-bg --force` 就是这么"已停止 0/2"的）—— 驱动了 `job` 字段与 `is_job()` 过滤。
* 实测确认 `cca switch 002 --cary` 这类拼错的 flag 会被静默忽略、看着像成功 ——
  改成未知参数报错（difflib 提示 `--carry`），并顺带拒绝多余的位置参数。
* 实测确认 `--carry` 必须用**完整 UUID**：`claude agents --json` 的短 id 只是 `sessionId` 的前 8 位，
  拿它去 `--bg --resume` 无效 —— 会话条目因此同时保留 `id`（展示/匹配）与 `full`（执行）。
* 实测确认 `--dry-run` 必须完全不动作：第一版把早退放在"有候选"分支里，无候选时仍会绑定 workspace，
  已改成无论有无候选都在打印计划后直接退出。
* 实测确认 `switch` 该默认收尾：切号后旧号仍挂着 blocked/done 会话，正是使用者反复问
  "为什么还有后台会话"的来源；而真正需要保护的是**正在跑**的会话，因此默认收"没在跑的"、
  跳过"在跑的"并提示，`--force` 才连在跑的一起停。
* 实测故障驱动了代理开关：内建代理只在本集群存在，别处无条件注入会让 `claude auth login`
  卡在 `getaddrinfo ENOTFOUND proxy.xray.svc.cluster.local`（登录页显示 `Login failed`），
  因此加 `CCA_PROXY`（默认不变），并让 doctor 提前把"代理解析不了"报出来。
* 实测确认 `cca resume` 的解析：完整 id 与 `c9eb`/`c9eb7be8` 前缀、标题 `daily`/`sam` 都能命中唯一会话；
  2 位输入按"标题子串"处理（前缀门槛是 4 位），多条匹配时列候选并退出。
* 实测确认 `--wide` 用 `overflow="fold"` 会把标题折成多行、反而看不到完整字符串，
  所以改成加宽控制台（`WIDE_CONSOLE_WIDTH`）+ `overflow="ignore"`。
* 实测确认 f-string + 码点补齐无法对齐中文列（`ccl` 的"档位/登录/后台"整体右移），
  改用 Rich 后宽字符按显示宽度排版；`status` 多号必须放在**同一张 grid** 里，
  否则各号各算列宽、标签列对不齐（第一版就是这个问题，已修）。
* 架构测试确认 import 本模块不加载 rich、`make_console()` 之后才加载 ——
  保证 `run` / `login` / `switch` 在没有 rich 的环境里也能跑。
* 实测确认 `/resume` 选择器与 `-c` 按当前目录过滤，而按 ID `-r <session-id>` 跨目录可续
  （在 `/tmp` 建的会话回到 `$HOME` 用 `cca 002 -r <id>` 成功续上）—— 驱动了 `cca sessions`。
* 实测确认 `claude agents --json` 的 `state` 除 `busy` / `idle` / `blocked` / `done` 外
  还会出现 `working`，守护因此改用"非运行状态白名单 + 其余按在跑"的保守判据。
* 实测确认后台会话的 `rm`（agents 视图里 `ctrl+x` 的同一动作）**只删登记不删对话**：
  `rm` 后 transcript 仍在共享池，按完整 id `-r` 仍可续；连记录一起删的是
  `claude project purge <path>`（transcripts / tasks / file history / config entry）。
* 实测确认 `-r` 只接受完整 session id：短 id 在 print 模式下报 "not a UUID"，
  在交互模式下会被当成选择器的搜索词 —— 所以 `cca sessions` 打印完整 id。
* 实测确认 transcript 的 `cwd` 会中途被改写（memory 工具落在
  `<config>/projects/<项目>/memory` 的 attachment 只占少数），归组必须取会话起始 cwd。
* 实测确认新配置目录缺 `hasCompletedOnboarding` 时，交互启动会走首次引导（选主题 → sign-in），
  即使 `.credentials.json` 有效、`auth status` 报 `loggedIn: true`、`-p` 能正常出结果。
* 实测确认 `claude agents --json` 的条目字段是
  `id` / `kind` / `name` / `cwd` / `sessionId` / `startedAt` / `state`，
  `state` 取值含 `busy` / `idle` / `blocked` / `done` —— 串行守护据此判定"在跑"。
* 实测确认 roster 的 pid 存活与真实会话数不一致（8 个后台会话只对得上 5 个存活 worker，
  且混有空闲 `bg-spare`），因此它降级为回退判据。
* 实测确认登录的自动回调地址可由 `BROWSER` 钩子取出（`redirect_uri=http://localhost:<port>/callback`），
  手动版的 `platform.claude.com` code 页只是同一流程的降级路径。
* 远端 `platform/claude-acct` 已建立（private，id 293）；全新 clone 后重跑用例通过。
* 实测确认 `CLAUDE_CONFIG_DIR=$HOME` 会使 `auth status` 报未登录 —— 驱动了 001 号的处理方式。
* 实测确认新配置目录首次运行会自建 `.claude.json` / `.claude.json.lock` / `backups/`。
* 实测确认 headless 登录可用：`claude auth login` 打印授权 URL（回调走 platform.claude.com
  托管页），本地浏览器打开后把 code 贴回即可，无需端口转发。
* 实测确认两个 Coder workspace 共享同一个 `$HOME`（同一 CephFS，inode 相同），
  因此 workspace 不提供配置隔离，必须靠 `CLAUDE_CONFIG_DIR` 区分。
