"""常量：命令名、环境变量、路径规则用到的所有固定值。"""

from __future__ import annotations

PROFILE_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

REMOVED_COMMANDS = {
    "switch": "默认号已取消：直接写号（cca ds / cca 002）；搬会话用 cca handoff <源号> <目标号>",
    "bind": "workspace 绑定已取消：直接写号，如 cca ds",
    "unbind": "workspace 绑定已取消：直接写号，如 cca ds",
    "resume": "改用 claude 自己的参数：cca <号> --resume <完整id>（先 cca sessions 找 id）",
    "wait": "wait 已删除：换号接着跑用 cca handoff <源号> <目标号>；额度用尽由 watchdog 盯",
    "migrate-001": "已改成一次性脚本：python3 scripts/migrate-001.py（--dry-run 先看计划）",
}

SUBCOMMANDS = (
    "new",
    "import",
    "add",
    "login",
    "logout",
    "rm",
    "mv",
    "rename",
    "status",
    "list",
    "ls",
    "handoff",
    "watchdog",
    "wake",
    "quota-hint",
    "model",
    "models",
    "sideagent",
    "plugin",
    "mcp",
    "trim",
    "stop-bg",
    "resume-bg",
    "sessions",
    "agents",
    "which",
    "sync",
    "doctor",
    "set",
    "version",
    "help",
)

RESERVED_NAMES = tuple(SUBCOMMANDS) + tuple(REMOVED_COMMANDS)

# 代理没有默认值：没配就拒绝启动 claude，避免账号从不明出口出去。
DEFAULT_PROXY_URL = ""

NO_PROXY_VALUE = "localhost,127.0.0.1,::1,.local,.svc,.svc.cluster.local,.cluster.local,.example.com"

PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

PROXY_ENV_VAR = "CCA_PROXY"

# 订阅号没有清单，代理单独存这个文件（一行地址）
ACCOUNT_PROXY_FILE = ".cca-proxy"

PROXY_DISABLED = ("none", "off", "-")   # 空串表示"没配置"，不留默认代理

PROXY_BUILTIN = "builtin"

PROFILE_MARKER = ".cca-profile.json"

KEY_FILE = ".api-key"

PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "key_env": "DEEPSEEK_API_KEY",
        "proxy": PROXY_BUILTIN,
        "env": {
            "ANTHROPIC_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
            # 推理档默认 xhigh（不是 max）：max 又慢又费 token，deepseek 上收益不明显。
            # 使用者要改就 `cca model ds --effort high`，那条命令会同步写进清单 env（见 _write_effort）。
            "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
        },
    },
    # OpenRouter：一个 key 打四百多个模型。默认给免费路由（0 成本、ctx 200k、支持工具），
    # 四档别名映到它家的 ~…-latest（永远指向该家族最新版）。工具搜索不设：混合模型场景下
    # 多数开源模型不支持；子代理不设：跟随主模型，免费场景不偷偷烧钱。
    "openrouter": {
        "base_url": "https://openrouter.ai/api",
        "key_env": "OPENROUTER_API_KEY",
        "proxy": PROXY_BUILTIN,
        "env": {
            "ANTHROPIC_MODEL": "openrouter/free",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "~anthropic/claude-opus-latest",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "~anthropic/claude-sonnet-latest",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "~anthropic/claude-haiku-latest",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "~anthropic/claude-fable-latest",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            "CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT": "1",
            "CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK": "1",
        },
    },
}

# 窗口相关的开关：从别的号的会话里起这个号时会继承过来，把新号的窗口顶掉。
# CLAUDE_CODE_AUTO_COMPACT_WINDOW 会盖过 settings 里的值，表现为"压缩得比配置早"。
WINDOW_ENV_KEYS = (
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

MANAGED_ENV_KEYS = (
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    *WINDOW_ENV_KEYS,
    *PROXY_KEYS,
)

SHARED_LINKS = ("projects", "skills", "plugins", "CLAUDE.md")

# 共享项里哪些是文件（其余是目录）
SHARED_FILES = ("CLAUDE.md",)

SUPPORTED_COUNTRIES_URL = "https://www.anthropic.com/supported-countries"

AGENTS_TIMEOUT = 10
# cca agents 默认只列活着的和这么多天内有活动的
AGENTS_RECENT_DAYS = 3
# live_index 并行查各号在跑的会话，最多开几个线程
LIVE_INDEX_WORKERS = 12

# 接管卡片：接管时交给主会话的几行字。最近几轮问答的原话，每轮每边截多少字，整张卡片最多多少字
TAKEOVER_CARD_ROUNDS = 3
TAKEOVER_CARD_TURN_CHARS = 300
TAKEOVER_CARD_TOTAL_CHARS = 2000
# 挑选框“已被 X 接管”里两个主会话标题一样时，带上它最后那句话的前几个字来区分
TAKEN_BY_ASK_CHARS = 12

# sideagent 目录（一个 sideagent 一个文件夹）和主会话名单（sessions/<会话编号>.json），见 docs/specs/sideagent-mn.md
# 停了以后（state.json 里写了 finished_at）整个文件夹留多少天；迁移时只有结果记录的老记录也按这个天数删
SIDEAGENT_KEEP_DAYS = 30
# 顺手清理（过期文件夹、主人死了的空名单）最多多久做一次：入口每次都调，没到点就直接返回
SIDEAGENT_CLEANUP_SECONDS = 600
# MCP 后台线程多久把“一串编号”里旧编号的名单并到最新编号一次；接力链的查询结果也缓存这么久
ROSTER_ADOPT_SECONDS = 30
# “名单的主人还在不在”要扫各号登记表：同一个主人几秒内只查一次
ROSTER_ALIVE_CACHE_SECONDS = 5

# handoff：停掉源号那条以后，等它的进程真的退出再到目标号 resume（还活着就 resume 只会复制一份副本）
HANDOFF_STOP_WAIT_SECONDS = 20
HANDOFF_STOP_POLL_SECONDS = 0.5
SELF_MOVE_NUDGE = ("（cca）这条会话已从 {source} 换到 {target} 接着跑（额度到了提醒点）。换号时停掉了还在跑的后台任务，"
                   "需要的话重新起；接着做刚才没做完的事。")

NON_RUNNING_STATES = ("idle", "blocked", "done", "failed", "parked", "stopped", "exited", "completed")

SESSIONS_DAYS = 14

SYNTHETIC_MODEL = "<synthetic>"

WIDE_CONSOLE_WIDTH = 500
# 默认表宽到这个值才显示倒计时：再窄的话两格时间会把表顶出终端，Rich 会静默裁列。
LIST_COUNTDOWN_MIN_WIDTH = 100

USAGE_WINDOWS = (("five_hour", "5 小时"), ("seven_day", "7 天"))

USAGE_COLUMNS = (("five", "5 小时(已用)"), ("seven", "7 天(已用)"))

USAGE_STALE_SECONDS = 6 * 3600

USAGE_LIVE_TTL = 300          # 实时额度在缓存里放多久（秒）；--refresh 可强制刷新

# `claude -p /usage` 要起一个完整的 CLI（慢账号上 10 秒以上），给足时间；
# 之前是 3（+10=13 秒），真机上首刷四个号经常全部超时 → 面板只能端出旧数字。
USAGE_LIVE_TIMEOUT = 20

# 取额度那次 `claude -p /usage` 只要它把额度写进 .claude.json，别的启动工作都省掉（都是文档里的正式开关）：
#   --safe-mode：不加载插件、钩子、MCP、CLAUDE.md、技能，登录照常（--bare 不行：它不读订阅登录，拿不到额度）
#   --no-session-persistence：不存这次的对话记录，免得 /resume 里堆满 /usage
#   ENABLE_CLAUDEAI_MCP_SERVERS=false：不去拉 claude.ai 上的 MCP 列表
#   DISABLE_AUTOUPDATER=1：这次启动不检查自动更新
# 不要设 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC：2.1.318 起它连额度查询也关掉，
# /usage 正文只剩用量分析、快照也不刷新，四个号全部报"CLI 没有返回额度"（11-05 实测）。
USAGE_CLAUDE_FLAGS = ("--safe-mode", "--no-session-persistence")
USAGE_CLAUDE_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false", "DISABLE_AUTOUPDATER": "1"}

# 本地快照（claude 自己刷新的 cachedUsageUtilization）在这么久以内，就优先用它、
# 不再起 `claude -p /usage`：快照本来就是同源数据，还省掉一次 CLI 启动。
# 和实时缓存同一个新鲜度窗口（5 分钟）：再旧的快照就不能当作"当前额度"，
# 否则 `cca ls` 会端出二三十分钟前的数字，和会话里看到的对不上。
SNAPSHOT_PREFER_SECONDS = USAGE_LIVE_TTL

# 自动压缩窗口 = 模型真实窗口 × 这个系数。原来硬编码 0.75（1M 只给 750k），
# 真机上显得"动不动就压"；0.9 留一成余量，压缩次数大约降到原来的 0.83。
# 想更保守/更激进用 CCA_COMPACT_WINDOW_RATIO 覆盖（夹在 0.5 ~ 0.98）。
COMPACT_WINDOW_RATIO = 0.9

SUBSCRIPTION_LIVE_TTL = 300

SUBSCRIPTION_BILLING = {
    "apple_subscription": "apple",
    "stripe_subscription": "stripe",
    "google_play_subscription": "google",
    "prepaid": "prepaid",
}

SUBSCRIPTION_STATUS = {
    "active": "有效",
    "canceled": "已取消",
    "past_due": "欠费",
    "trialing": "试用",
    "paused": "暂停",
    "expired": "已过期",
    "logged_out": "未登录",
}

SUBSCRIPTION_BILLING_LONG = {
    "apple_subscription": "apple（App Store）",
    "stripe_subscription": "stripe（银行卡）",
    "google_play_subscription": "google（Play 商店）",
    "prepaid": "prepaid（预付费）",
}

API_TOOLSET_ENV = {"CLAUDE_CODE_ARTIFACT_TOOLSET": ""}

# 自建网关要等上游开始出字才回响应头：推理模型想 60~200 秒才出第一个字很常见，网关大约
# 300 秒会掐断。Claude Code 默认等首字节约 190 秒就放弃、从头重算，于是慢的请求永远等不到。
# 330 秒比网关的 300 秒略长：能等的都等到，网关掐断时由网关的 504 说了算，而不是 CC 先放弃。
GATEWAY_FIRST_BYTE_KEY = "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"
GATEWAY_FIRST_BYTE_MS = "330000"
# 哪些端点算内网网关：主机在这些域下，且 provider 不是直连预设。
GATEWAY_HOST_SUFFIXES = (".example.com",)

TRANSCRIPT_MARKERS = (
    '"custom-title"',
    '"ai-title"',
    '"agent-name"',
    '"last-prompt"',
    '"cwd"',
)

STALE_LOG_MARKERS = ("job not found", "already exited")

RICH_VERSION = "15.0.0"

STATE_STYLES = {
    "busy": ("busy", "cyan"),
    "working": ("working", "yellow"),
    "blocked": ("blocked", "magenta"),
    "idle": ("idle", "dim"),
    "shell": ("idle", "dim"),        # 接管的会话停在终端里，按空闲画
    "done": ("done", "green"),
    "failed": ("failed", "bold red"),
    "waiting": ("waiting", "yellow"),
    "unknown": ("unknown", "dim"),
    "quota": ("quota", "magenta"),   # cca agents：停在额度错误上，要人换号
    "stopped": ("stopped", "dim"),
    "ended": ("ended", "dim"),
    "OK": ("OK", "green"),
    "WARN": ("WARN", "yellow"),
    "FAIL": ("FAIL", "bold red"),
}

NO_BROWSER = "/bin/false"

ADD_VALUE_FLAGS = ("--provider", "--key", "--url", "--base-url", "--model", "--env", "--proxy")

CATALOG_URL = "https://openrouter.ai/api/v1/models"

CATALOG_TTL = 3600

CATALOG_KEEP_FIELDS = ("id", "name", "context_length", "pricing", "supported_parameters")

PLUGIN_NAME = "sideagents-panel"
PLUGIN_MARKETPLACE = "mods"
# 改过名字的旧安装：装上新的以后顺手清掉，免得同一份插件加载两次
LEGACY_PLUGINS = ("cca-sideagents-panel", "cca-peers-panel")
LEGACY_MARKETPLACES = ("cca-mods",)

WAIT_LIMIT_MARKERS = ("429", "usage limit", "rate limit", "you've hit your", "hit your weekly")

WAIT_HELD_MARKERS = ("running in the background", "running as a background session")

WAIT_COPY_MARKERS = ("started a copy as",)

# "环境不可用"（可恢复）的标记：代理域名解析不了 / 出口地区不支持 / `CCA_PROXY` 没配。
# 真机踩过：一次 DNS 抖动，6 条会话全被判成终结 failed，到点的续跑一条没成。
WAIT_ENV_MARKERS = ("没有配置代理", "出口不明", "不支持这个地区")

WAIT_BOOKKEEPING_TYPES = frozenset({
    "queue-operation", "system", "attachment", "last-prompt", "custom-title", "ai-title",
    "agent-name", "mode", "permission-mode", "atis-latch", "pr-link", "file-history-snapshot",
    "summary", "relocated",
})

WAIT_ACTIVE_SECONDS = 120          # 最后一条对话两分钟内 = 真在跑

WAIT_LOG_MOTION_SECONDS = 8        # push 前比对日志输出的间隔：还在动就不碰它

WAIT_LOGS_TIMEOUT = 120            # `claude logs` 单次超时：无人值守时不许无限等

WAIT_BG_VERIFY_SECONDS = 45        # 挂回面板后盯多久（看它动了没有 / 有没有撞回额度）

WAIT_GIVE_UP_SECONDS = 24 * 3600   # 一条记录最多折腾多久：之后收尾，别让 --run 永远不退出

WAIT_BLOCKING_PERCENT = 95         # 只看"等哪个窗口"：真机 95% 就已被限流，百分比不能单独当判据

WAIT_DEFAULT_PROMPT = "额度已重置，继续刚才中断的工作。"

WAIT_RETRY_SECONDS = 300          # 额度还没放开时的退避
WAIT_JITTER_SECONDS = 120         # 每个号一个固定偏移，避免所有号同一秒醒来

WAIT_RETRY_HORIZON = 3600         # 同一条最多重试到 not_before + 1 小时

WAIT_USAGE = ("cca wait <号> [<会话id…>] [--prompt <提示>] [--window 5h|7d|auto] [--at HH:MM]\n"
              "              [--only <id,id>] [--all] [--dry-run] [--detach]\n"
              "cca wait --list [--full] | --cancel <记录id> | --run [--once]")

WAIT_PRUNE_DAYS = 7

WAIT_DETAIL_LABELS = {"session": "会话：", "prompt": "提示词：", "preview": "现在会看：",
                       "runner": "等待器：", "log": "日志：", "note": "", "result": ""}

ADD_USAGE = """cca add —— 建号（订阅号 / API 号）

订阅号（OAuth）:
  cca add <name> [--email X] [--console] [--sso]        等价 new + login

API 号（API key + Anthropic 兼容端点）—— 端点二选一，两行配方:
  cca add <name> --provider deepseek --key sk-xxxx      ① 预设：端点/模型变量/环境变量名都内置
  cca add <name> --url https://sub2api.example.com --key sk-xxxx
                                                        ② 自备端点（中转 / sub2api / 自建网关）
  cca add <name> --provider deepseek --key sk-xxxx --isolated
                                                        加 --isolated：独立配置目录（不共享对话池/CLAUDE.md）
  cca add or --provider openrouter --key sk-or-xxxx     ③ OpenRouter：一个 key 打几百个模型，
                                                        默认 openrouter/free；选型见 cca model --help

  两个都给会报错（端点二选一）。只给 `--url` 时若主机命中已知预设，会自动套用并说明。
  中转类端点通常把 Claude 的模型名原样转给上游，所以不必给 `--model`；要手动对齐就
  `--url … --model <模型名>`（必要时再 --env K=V）。

key 的三种给法（从上到下越来越不留痕）:
  --key sk-xxxx                 直接填（最方便；会留在 shell history）
  --key-stdin                   echo $KEY | cca add <name> --url … --key-stdin
  环境变量                       预设号用 $DEEPSEEK_API_KEY，通用端点用 $ANTHROPIC_API_KEY

模型怎么映射（`--model M` 的语义：**这个号所有槽位都用 M**）:
  槽位               环境变量                              不设时实际发出
  主 agent           ANTHROPIC_MODEL                       Claude 自带默认（如 claude-sonnet-5）
  别名 opus/sonnet/  ANTHROPIC_DEFAULT_OPUS_MODEL          Claude 真实模型 ID
  haiku              ANTHROPIC_DEFAULT_SONNET_MODEL        （实测 claude-haiku-4-5-20251001）
                     ANTHROPIC_DEFAULT_HAIKU_MODEL
                     —— 决定 `/model opus`、`--model haiku` 这类别名解析成谁
  子 agent           CLAUDE_CODE_SUBAGENT_MODEL            继承主 agent 的模型

  `--model M`      上面全部指到 M（适合整条链只有一个模型的端点，如 DeepSeek）
  不给 `--model`   发 Claude 原生模型名，由端点自己映射（中转/网关通常正是这样）

贵/便宜分开用（`--model` 打底，再用 `--env` 覆盖 —— 对已存在的号就是增量更新）:
  cca add ds --provider deepseek --model deepseek-flash[1m]
  cca add ds --env ANTHROPIC_MODEL=deepseek-v4-pro
  cca add ds --env ANTHROPIC_DEFAULT_OPUS_MODEL=deepseek-v4-pro

其他参数: --env K=V（额外变量，可重复；空值 K= 表示删除；端点/密钥/代理/配置目录这几个键不允许）
          --proxy builtin|none|inherit|<url>（默认 builtin：注入内建代理，但该号端点走直连 ——
          自动更新/遥测这类非端点流量需要代理；单号代理优先于 CCA_PROXY）
事后改（不必重建）: cca add <name> --model M / --env K=V / --env K=（删除）/ --url <新端点>
                   单号代理用 cca set proxy <name> <url|none>
换 key: cca login <name> --key sk-xxxx        删 key: cca logout <name>
"""

STATUS_USAGE = "cca status [<profile>] [--json]"

AGENTS_USAGE = "cca agents [--all] [--json]"

SESSIONS_USAGE = "cca sessions [--all] [--json] [--wide]"

LIST_USAGE = "cca ls [<号>…] [--active] [--wide] [--json] [--cached | --refresh]"

EMAIL_MIN_WIDTH = 12

EMAIL_MAX_WIDTH = 34

RM_USAGE = "cca rm <profile> [--yes] [--force]"

SET_USAGE = """cca set proxy <profile> <url|none>     给单个号设代理
cca set proxy --global <url|none>      给所有号设默认代理（写进 shell 启动文件）
cca set proxy <profile> / --global     看当前值
cca set tui fullscreen|default         所有号默认渲染模式（fullscreen 下鼠标能点面板）
cca set summary <号> [模型|off|default]  sideagent 横条进度短语用这个号的哪个模型（默认 haiku）

  单号设置优先于全局。none 表示明确直连，inherit 表示不注入、继承 shell 原值。
  全局会改写 shell 启动文件里的一段（bash: ~/.bashrc，zsh: ~/.zshrc，fish: config.fish）。
"""

TRIM_USAGE = "cca trim <会话id|前缀> [--keep=<tokens>] [--dry-run] [--force]"

TRIM_KEEP_DEFAULT = 400_000

MODEL_USAGE = ("cca model [<号>] [--main <slug>] [--sub <slug>] [--opus <slug>] [--sonnet <slug>] "
               "[--haiku <slug>] [--fable <slug>] [--effort <level>] [--all] [--json]")

MODEL_ALIAS_FLAGS = {
    "--opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "--sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "--haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "--fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
}

EFFORT_LEVELS = ("low", "medium", "high", "xhigh")

HANDOFF_USAGE = ("cca handoff <源号> <目标号> [--only=<前缀|标题>]… [--all] [--dry-run] "
               "[--attach] [--strip-tool-search] [--json] | cca handoff <目标号> --current | cca handoff --stay")

HANDOFF_CANCELLED = "已取消，什么都没动"

# handoff 选择框每一列的显示宽度（终端列数，中文占 2 列）；超出的用省略号截断
PICKER_TITLE_WIDTH = 24
PICKER_STATE_WIDTH = 8
PICKER_AGO_WIDTH = 10
PICKER_ASK_WIDTH = 48
PICKER_ACCOUNT_WIDTH = 10

# API 号一律禁掉的工具：这些是 Claude Code 里"托管/内部"的工具，第三方模型（gateway 上的
# gpt-*/deepseek-*）看到描述就会调，但对应实现往往没开 → 报
# "No such tool available: X … it cannot run"（真机：SendFeedback 反复出现）。
API_DISALLOWED_TOOLS = ("Artifact", "ArtifactComments", "ArtifactData", "SendFeedback")

MODEL_FOLLOW_KEYS = (
    ("main", "ANTHROPIC_MODEL"),
    ("sub", "CLAUDE_CODE_SUBAGENT_MODEL"),
    ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
)

MODELS_TIMEOUT = 15

MODELS_USAGE = ("cca models <号> [--json] —— 看该号端点上到底有哪些模型\n"
                "            cca model <号> --follow [--dry-run] —— 把同档位的新版本升上去\n"
                "\n"
                "  跟随规则（宁可不跟，也不猜）：只认**同前缀 + 同档位后缀**、版本号严格变大的候选；\n"
                "  跨档（sol→astra）、降级、同版本多候选、名字拆不出结构 —— 一律跳过。\n"
                "  改完要重启会话才生效。\n")

COMMAND_USAGE = {
    "set": SET_USAGE,
    "models": MODELS_USAGE,
    "add": ADD_USAGE,
    "rm": """cca rm <profile> [--yes] [--force] —— 删掉该号的本地配置

  删什么: <配置目录> 整个目录（凭据、API key、.claude.json、sessions/、daemon、jobs、本号历史）
  不删什么: 对话记录（在共享池，任何号都能续）、服务端账号状态、订阅账单
  闸门: 共享项被换成实体目录时拒绝（删了会连累所有号）；
        有交互会话或在跑的会话时拒绝（--force 会先停再删，停在跑的有破坏性）
  确认: 打印文件数/体积后要求**输入 profile 名**；脚本用 --yes
  例: cca rm ds            # 输 ds 确认
      cca rm ds --yes      # 免确认
      cca rm ds --force    # 连在跑的会话一起停掉再删
      cca rm ds --yes      # 删完若又冒出同名空目录（daemon 尾巴写 telemetry）再跑一次即可
""",
    "trim": """cca trim <会话id|前缀> [--keep=<tokens>] [--dry-run] [--force] —— 把超限会话的历史裁到窗口内

  什么时候用: 会话涨过端点上限后，连压缩请求都发不出去（400 maximum context length），
  这时只能裁掉更早的部分。裁之前一律备份到 ~/.claude-accounts/_session-backups/。
  默认裁到 400,000 tokens（1M 窗口留足余量）；切点一定选在干净的 user 边界，不会切断
  tool_use / tool_result 配对。正在跑的会话拒绝裁（进程还在写文件）：先 cca <号> stop <id>。
  有后台登记的会话会被拒（活着的进程握着裁剪前的历史）：先 cca <号> stop <id>；
  确认那条登记是残留（进程已退出）时加 --force。
  例: cca trim 4bd0a614 --dry-run         # 只看计划
      cca trim 4bd0a614 --keep=300000     # 裁到 30 万 tokens 以内
""",
    "model": """cca model [<号>] [--main <slug>] [--sub <slug>] [--opus/--sonnet/--haiku/--fable <slug>]
                [--effort low|medium|high|xhigh] [--all] [--json]
                cca model --list [--free] [--search <词>] [--sort price|ctx] [--refresh]

  看或改"这个号用哪些模型"。**不带号 = 只读汇总**（每个号一行）；写入必须写号，批量要 --all。
  改的是号级默认（新会话生效）；只想改一次，用 `cca <号> --model <slug>` 起会话。
  值原样收：slug（qwen/qwen3.8-27b:free）、~…-latest 别名、openrouter/free 路由都能填。
  --effort 写进 modelSettings[当前主模型]；--list 只读 OpenRouter 目录（公开、免 key，1 小时缓存）。
  例: cca model or --main openrouter/free
      cca model or --opus ~anthropic/claude-opus-latest --effort high
      cca model --list --free
""",
    "plugin": """cca plugin install [--all] [--profile 号] | cca plugin list

  把面板插件装进每个号（插件按号算，每个号一个配置目录）。
  装的是本仓库自带的 marketplace，幂等：反复跑不会重复。
  make install 会自动对所有号跑一次；cca add 建完号也会顺手装。
  例: cca plugin install --all
      cca plugin list
""",
    "wake": """cca wake <号>… [--only=<前缀>] [--at=HH:MM] [--dry-run] | cca wake [status] | --once

  后台会话撞额度后官方不会自动继续。睡前在原号上挑好会话排上：到这个号用满的窗口
  重置时（两个都满就等更晚的那个），自动 cca <号> --bg --resume <完整编号> 并说一句
  「额度已重置，继续」。不换号；同一个会话只叫一次。
  不带号或 status = 看排了哪些。到点由常驻的 cca watchdog 叫醒（排上时自动拉起，不用另开进程）。
  --only=<前缀> 非交互指定（可重复）；--at=HH:MM 只在取不到额度数据时兜底；
  --dry-run 只看计划；--once 立刻处理一遍到点的条目。
  例: cca wake 001
      cca wake 001 --only=d31c1a92
      cca wake status
""",
    "watchdog": """cca watchdog [status] | cca watchdog --dry-run | cca watchdog stop

  额度看门狗：全机一个常驻进程，管订阅号的后台会话换号和叫醒。不用手动起——
  `cca quota-hint`（面板插件定时调）和 `cca <号>` 启动订阅号会话时会顺手拉起；订阅号上没有活着的
  后台会话、也没有待叫醒的条目时自己退出。
  规矩：
    · 订阅号某个窗口到了提醒点（5 小时 95%、7 天 98%），这个号上还在干活的后台会话，在这一轮结束时
      搬走（编号、名字不变，后台任务会停掉，续上后会收到通知）。目标号的挑法同 cca quota-hint。
    · 已经撞墙的会话立刻搬，不看阈值也不看下面的例外。
    · 不搬：卡住的窗口不到 15 分钟就重置 · 会话闲了 30 分钟以上 · 会话里运行过 `cca handoff --stay`
      （同一个重置时刻内有效）· 源号额度已经恢复。
    · 全都满、没有可去的号：撞墙的会话排进 wake，等最早的重置后叫醒。
  status（不带参数同）看在不在跑、各号额度、接下来搬哪条、哪些因例外不搬、哪些待叫醒。
  --dry-run 只算一遍要做什么（只用缓存的额度），不动手。stop 停掉常驻进程。
  日志 ~/.claude-accounts/watchdog.log；搬成功会在面板上弹一条通知。
  例: cca watchdog status
      cca watchdog --dry-run
""",
    "handoff": """cca handoff <源号> <目标号> [--only=<前缀|标题>]… [--all] [--dry-run] —— 把会话搬到另一个号

  做两步: 源号 stop（只停不删，对话保留）→ 目标号 `--bg --resume <完整id>` 接管同一条
  默认只搬"没在跑的"（接管会继续跑，烧目标号的额度）；--all 连在跑的一起
  非 tty 必须给 --only 或 --all（脚本里不留"猜你想搬哪条"）
  --attach 搬完直接进那条会话（等价再敲一次 cca <目标号> attach <id>）
  --current 自己换号：在后台会话里敲 cca handoff [<目标号>] --current，源号和会话 id 自己认（不写目标号就按额度挑）；
            会话里的面板插件提供 /cca handoff [<号>]、/cca stay 两个快捷命令；
            登记给常驻的 watchdog，这一轮结束时它停源号、在目标号续跑，并让会话接着做（交互会话不支持）
  --stay    在后台会话里敲 cca handoff --stay：这条会话留在本号，这一段额度（同一个重置时刻）内
            不因为提醒点被搬走；撞墙了照样搬
  例: cca handoff 001 002 --only=abcde123
      cca handoff ds 002 --all --dry-run
      # 手工等价: cca ds stop-bg  然后  cca 002 --bg --resume <完整id>
""",
    "stop-bg": """cca stop-bg <profile>|--all [--force] —— 停掉后台会话（只停不删）

  默认跳过正在跑的；--force 连在跑的一起停。对话保留：cca <profile> agents 里还在，选中就续。
  --all 所有号一起停，换 workspace 前用：家目录是共享盘，进程和套接字跟着 pod 走，
        两个 workspace 跑同一个号会互相误判。停掉的记进 ~/.cca/stopped-bg.json，
        到新 workspace 用 cca resume-bg 照单续。在会话里敲时，本会话放到最后停。
  交互会话不是后台 job，会被跳过并说明（claude stop 停不掉它们）。
""",
    "resume-bg": """cca resume-bg [--dry-run] —— 照 stop-bg --all 记下的清单，在原号把会话续上

  停之前在跑的：续上后发一句"换了 workspace，接着做"；挂着的（blocked/done）：只挂回
  agents 列表，不跑模型、不耗额度。编号和名字都不变。续上一条划掉一条，失败的留着再跑一次。
  --dry-run 只列清单。
""",
    "agents": """cca agents [--all] [--json] —— 跨号会话总表（只看，不进入会话）

  每个号跑一次 claude agents，合成一张表：名字 / 编号 / 现在在哪个号 / 状态 / 在干什么 / 多久前。
  同一会话在几个号上都有记录时合成一行：以活着的为准，都没活着取最近的。
  要人处理的（blocked、等输入、撞额度）排最前，然后是在跑的，再按最近活动倒序。
  默认只列活着的和 3 天内有活动的；--all 列全部；--json 结构化输出。取不到的号跳过，表尾提示。
  进入某条：cca <号> attach <短编号>。和 cca sessions 的区别：这里是现在的会话和状态，
  sessions 是共享池里能续的历史。
""",
    "sessions": """cca sessions [--all] [--wide] [--json] —— 列出共享池里的可续会话（跨目录）

  /resume 与 -c 只看当前目录；这个命令列出所有项目目录的会话。
  **会话不绑号**：池子是所有号共用的，任何号都能续；每行标出它当前挂在哪个号上。
  续会话用 claude 自己的参数：cca <号> --resume <完整id>（-r 同义，-c 续该号最近一条）。
  --all 不限最近 14 天；--wide 不截断；--json 结构化输出。
""",
    "status": """cca status [<profile>] [--json] —— 不带号 = 每个号一段摘要；带号 = 单号详情

  目录/类型/登录/档位/额外用量/凭据与重新登录到期/额度/快照/后台会话明细；
  API 号显示端点、模型、密钥掩码、代理，以及 settings 与清单不一致时的修复命令。
""",
    "list": """cca ls [<号>…] [--active] [--wide] [--json] —— 号面板（cca list 同义）

  一行一个号：身份 + 额度（5 小时 / 7 天，已用 % + 重置时刻）+ 档位 + 订阅。
  额度默认实时取（5 分钟缓存）；--cached 只看本地快照，--refresh 强制刷新。
  --wide 加 后台会话数 / 登录 / 快照时刻 / 额外用量；--json 结构化输出。
  --active 只列订阅能用的号（有效 / 试用；状态为空的正常在用订阅也算）；API 号、未登录（登录失效）、
           订阅已取消或过期、状态未知（没取到）的藏起来，表尾说明藏了哪些。--cached 时按一天内取到的订阅判断。
""",
    "doctor": """cca doctor [--online] [--probe-key] [--json] —— 体检

  离线：软链、登录态、后台会话（含陈旧登记）、代理、API 号的清单/密钥权限/端点 DNS/settings 漂移
  --online     出口 IP + API 号端点探活（不带凭据）
  --probe-key  对 API 号**带密钥**发一次最小请求验证密钥：2xx = 有效，401/403 = 被拒，
               5xx = 端点/上游报错（与 key 无关），其它 4xx = 通了但请求被拒
""",
    "new": """cca new <profile> —— 建号：建目录 + 铺共享软链 + 播种默认 settings

  播种源是 ~/.cca/defaults/settings.json（跟任何账号解耦，可以直接编辑）；
  第一次没有时从 001 或 ~/.claude 的 settings 迁一份。
  订阅号建完显示"下一步: cca login <profile>"；API 号由 cca add 一次配好，建完直接 cca <name>。
""",
    "login": """cca login <profile> [--email X|--console|--sso] —— 登录订阅号（自动带代理）
            cca login <api号> [--key <key>|--key-stdin] —— 换 API key（不动其它配置）

  订阅号登录成功后会自动补 hasCompletedOnboarding、继承其它号已信任的目录、注册 sideagent MCP。

  **不自动开浏览器**：登录（和 `cca <号>` 启动的会话里 `/login`）都会带 `BROWSER=/bin/false`，
  CLI 于是把登录链接打出来让你自己复制去开（Coder/容器里没有本地浏览器，自动 open 只会失败）。
  想让它照旧自动开，就自己设 `BROWSER`（shell 里设、或写进该号 settings 的 `env`）——设了就尊重你。
""",
    "logout": """cca logout <profile> —— 订阅号走 claude auth logout；API 号删除其 API key

  API 号删 key 后，清单/配置/对话都保留；恢复用 cca login <profile> --key <key>。
""",
    "which": """cca which <profile> —— 这个号是谁：目录 / 类型 / 端点（一行）

  脚本友好的一行输出；要看额度与后台会话明细用 cca status <profile>。
""",
    "sync": """cca sync <profile> —— 用默认 settings（~/.cca/defaults/settings.json）重新播种该号

  订阅号整文件覆盖；API 号是"合并 + 按清单重渲染"（保留它的 env 与 apiKeyHelper）。
  顺带补 hasCompletedOnboarding 与其它号已信任的目录。
""",
    "mv": """cca mv <旧号> <新号> —— 给号改名（rename 同义）

  先停这个号的后台会话，再搬目录，并改写写死了旧路径的地方（apiKeyHelper、sideagent 状态）。
  对话不受影响：它们在共享池里。001 不能改名。
  例: cca mv codex gpt-ds
""",
    "import": """cca import <隔离号> <源号> —— 把源号看得到的会话单向复制进隔离号

  只给 cca new <号> --isolated 建的隔离号用：隔离号不共享会话池，要用旧会话就导一次。
  只复制隔离号里还没有的文件，不会写回源号。
  例: cca import private 002
""",
    "mcp": """cca mcp install [--all | --profile <号>] | cca mcp serve

  install  把 sideagent 的 MCP 工具（sideagent_send 等）注册进号的配置，重启会话生效。
           cca add / cca login 成功后会自动注册；老号用 --all 补齐，反复跑没有副作用。
  serve    MCP 服务本体，由 claude 拉起，一般不用手跑。
""",
    "sideagent": """cca sideagent <子命令> —— sideagent 的命令行入口（派活在对话里用 sideagent_* 工具）

  tell [--interject] <实例名> 「要说的话」   给 sideagent 发一句；面板输入框用的就是它
  takeover-candidates [--all]              本机各号能接管的会话（JSON，一会话一行）
  hook install|uninstall --profile <号>|--all   接管会话的「请确认」转到面板上点
  version                                  当前 sideagent 代码版本（面板据此标「旧版」）

  其余子命令（takeover、bg-sync、latest、stop-owned、permission-*）给面板和 MCP 服务内部用。
""",
    "version": "cca version —— 打印版本（VERSION 文件）\n",
    "quota-hint": """cca quota-hint [<号>] [--json] [--session <完整会话编号>] —— 额度快满时换不换号、换到哪

  在会话里（CLAUDE_CONFIG_DIR 指着哪个号）：那个号是订阅号、5 小时用到 95% 或 7 天用到 98% 时
  （CCA_QUOTA_HINT_PERCENT / CCA_QUOTA_HINT_WEEKLY_PERCENT 可调）给提醒：推荐换到哪个号（订阅号里，两个窗口离提醒点都还差 15 个点以上的优先，其中周额度最早重置的）、
  怎么换（后台会话：cca handoff <目标号> --current；交互会话：退出后 cca <目标号> --resume <编号>）。
  给后台会话提醒的同时，替它预约有条件的叫醒：万一撞墙停下，额度重置后自动叫醒它接着做（只叫真撞墙停下的）。
  面板插件在会话里定时跑 --json（远时 5 分钟一次；离阈值 15 个百分点以内 2 分钟一次，额度超过 2 分钟就现取），
  命中时把提醒附在下一次工具结果后面给模型，同一个窗口只提醒一次；换不换由模型自己判断。

  写了号：给人看这个号的情况和显式命令（cca handoff <源号> <目标号> --only=<编号>、cca wake <号> --only=<编号>）。
  不在会话里又没写号：给出现在要换号的话先用哪几个号。
""",}
COMMAND_USAGE["ls"] = COMMAND_USAGE["list"]
COMMAND_USAGE["rename"] = COMMAND_USAGE["mv"]


# 启动器怎么被调起：bin/claude-acct 或 python -m cca 在启动时填这一项。
LAUNCHER_ARGV: list[str] = []
