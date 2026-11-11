# sideagent 目录和 M 对 N 接管（设计，2026-11-04 定）

## 一、两种 sideagent，格式一样，只有生命周期不同

| | 派出的（spawned） | 接管的（attached） |
|---|---|---|
| 怎么来 | `sideagent_send` 新开的 | `sideagent_takeover` 挂到一个本来就在跑的会话上 |
| 主人几个 | 只有一个，不能串（别的会话接管它要拒绝） | 可以多个（M 对 N） |
| 主人放手 / 主人会话结束 | 进程停掉 | 只解开这个主人这一条，会话照跑 |
| 横条上的记号 | ◯ 平时，● 正在看 | ◇ 平时，◆ 正在看（不再写“接管”两个字） |

## 二、目录

官方做法：一个东西一个文件，每个文件只有一个写的人；关系挂在主人名下（`jobs/<id>/state.json` 的 `children`、子代理挂在父会话目录 `subagents/` 下）。照这个做：

```
~/.cca/
  sideagents/<名字>/          一个 sideagent 一个文件夹（名字照旧：派出的 ds#a6b6a4，接管的 003#bg-xxxxxxxx）
    state.json                原来的 <名字>.json；多一个字段 kind: spawned | attached，finished_at
    events.jsonl              原来的 .events
    progress                  原来的 .progress
    summary.log               原来的 .summary.log
    result.json               原来的 .last.json
    log                       原来的 .log
    run/                      只在跑的时候有，停了整个删掉
      sock  live  result.lock 原来的 .sock、.live、.last.lock
  sessions/<主会话完整编号>.json   这个会话用着哪些 sideagent（只有这个会话自己写）
  sideagents/notices.jsonl、lineage.jsonl、perm/、main-session  照旧
```

`sessions/<编号>.json` 的样子：

```json
{
  "session": "de9cdb1b-…", "account": "003", "pid": 2096645, "proc_start": "…",
  "instance_socket": "/tmp/cc-socks-1000/1387450.sock",
  "agents": {
    "ds#a6b6a4":   {"kind": "spawned",  "since": 1793784132.4, "label": "", "description": "…", "caller_agent": ""},
    "003#bg-36709e4a": {"kind": "attached", "since": 1793790000.0}
  }
}
```

原来 `.owner` 里的东西（会话、号、label、description、caller_agent、instance_socket）搬到这里：主人那部分放顶层，每个 sideagent 的那部分放 `agents` 里。

## 三、规则

1. **查“我用着谁”**：读 `sessions/<我>.json`。主会话编号会因为 `/bg`、← 换（`session_chain` 已经能顺着 continued-in 和 lineage 找到一串）——一串编号的文件合起来算；写的时候写到最新的编号，并把旧编号文件里的条目并过来、删掉旧文件。
2. **查“谁用着它”**：扫 `sessions/*.json`，主人进程死了（pid + proc_start 对不上）的不算。
3. **派出**：写 `sideagents/<名字>/state.json`（kind=spawned），在 `sessions/<我>.json` 里加一条。
4. **接管**：
   - 目标是别人派出的 sideagent → 拒绝：“这是 X 派出的 sideagent，不能接管”。
   - 目标已经有 `sideagents/<名字>/`（别人接管过）→ 不重建、不覆盖，只在我的 `sessions` 文件里加一条；同步（transcript → events/progress）照旧只有一份，大家共用。
   - 接管卡片照旧交给主会话（`card()`）。
5. **放手**（`sideagent_release`）：只从我的 `sessions` 文件里删这一条。没有活着的主人再挂着它时，删 `run/`、写 `finished_at`；会话本身不动。
6. **主人会话结束**：派出的照现在的逻辑停；接管的只当作解开（读的时候主人死了就不算，清理时删掉这条）。
7. **通知和权限确认**：接管的会话要通知 / 等确认时，发给所有活着的主人；权限确认谁先答算谁的（`permission.py` 现在是按单个主人找，改成找全部）。
8. **停了以后**：删 `run/`，`state.json` 写 `finished_at`，其余留着。
9. **清理**：`finished_at` 超过 `SIDEAGENT_KEEP_DAYS`（config，默认 30）的整个文件夹删掉；`sessions/` 里主人死了且 agents 为空的文件删掉。清理在 `cca sideagent ls`/面板刷新这类已有入口里顺手做，不另起进程。
10. **挑选框**：列出时带 `taken_by: [别的主人的会话标题或短编号]`，面板显示“已被 X 接管”；被我接管的照旧标出来。

## 四、迁移

运行时（cca、MCP、面板）只认新格式，不带任何兼容老格式的代码（11-04 定）。迁移是单独的脚本，换代码时手动跑：

```
python3 scripts/migrate_sideagent_layout.py --dry-run   # 先看要搬什么
python3 scripts/migrate_sideagent_layout.py             # 搬
```

- 平铺的 `<名字>.json/.events/.progress/…` 搬进 `sideagents/<名字>/`，改成新文件名；`.sock`、`.live`、`.last.lock` 不搬（进程重启后会在 `run/` 里重建）。老格式的 `kind: bg` 改成 `attached`。
- 还在跑的老守护进程（老 socket 能连上）跳过，脚本最后列出名字：等它们停了再跑一次。
- 换代码以后，各会话要 `/mcp` 重连 sideagents（`/reload-plugins` 不重启 MCP 服务），否则老 MCP 和新守护进程对不上。
- 从 `.owner` 和 state 里的 `owner_session` 生成 `sessions/<主人>.json`。
- 只有 `.last.json` 的老记录：超过 30 天的直接删，其余搬进去。

## 五、面板（mods/sideagents-panel）

- 读路径全改成新目录（`register.js` 里 `readOwner`、`readOwnerNote`、`senderOwner`、状态 / 事件 / 进度 / live 的路径）。
- 横条的数据来源改成：先读 `sessions/<本会话一串编号>.json` 拿到名单，再读每个 `sideagents/<名字>/`。不再扫整个目录按 owner_session 过滤。
- 接管的行：圆点换成 ◇ / ◆。
- 接管挑选框：被别人接管的行，最后一列写“已被 X 接管”（仍然可以选，M 对 N）。

## 六、不变的

- MCP 工具名、参数、返回不变；sideagent 名字格式不变。
- `cca sideagent …` 命令行用法不变。
- 测试：原有测试照过；新加：两个主人接管同一个、一个放手另一个还在、接管别人派出的被拒、主人死了不算、迁移、清理。
