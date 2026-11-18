# 跨号会话：查、发话、挂上、换号

SKILL.md 第 4 节的展开。号名、编号都是例子，按现查结果填。

## 查到的状态怎么说

| 查到 | 说法 | 要它做事 |
| --- | --- | --- |
| `running=true` | 在 `<号>` 跑着 | `sideagent_send {session}`，下一个工具调用结束时送到 |
| `done`、`running=false` | 没在跑，会话还在 | `sideagent_send {session}`，会自动唤醒 |
| 同编号在某号上是 `stopped`、别的号上不是 | 旧版本或清理失败留下的旧登记（现在搬成功会清掉源号条目） | 照样 `sideagent_send {session}`，cca 会选没停的那条 |
| 只在 `cca sessions` 里、`归属未知` | 已退出，transcript 还在 | `sideagent_send {session}` 会在一个能用的号上接着跑 |
| 两处都查不到 | 找不到这条会话的登记 | 说清可能编号或标题不对、或被清理了，别编「下线」 |

想亲眼看它在干什么（给用户的建议）：`cca <号> attach <短编号>`，退出不影响它。续一段没在跑的会话自己接着聊：`cca <号> --resume <完整编号>`。

`attach-candidates` 只列登记在某个号下的会话（`stopped` / `failed` 和 sideagent 自己的会话不列），一秒级；`taken_by` 非空说明有别的会话挂上着它。`cca sessions` 要扫 transcript，第一次十几秒，之后走缓存。

## `sideagent_send {session}` 怎么选号

1. 跨号找这条会话的全部登记，排序：在跑的 > 没停的（最近登记的在前）> `stopped` / `failed`。
2. 在跑：直接投递，不起进程。
3. 没在跑：按上面的顺序挑第一个**能用**的号唤醒（`--bg --resume <完整编号>`，编号不变、不复制副本）；那个号额度用满、没登录或没 key 就看下一个；都不行用**你自己的号**接着跑；还不行就报出每个号为什么不行。
4. 写了 `account` 就只在那个号上找、在那个号上唤醒，不换号。

所以：

- 唤醒后它在那个号上跑，烧的是那个号的额度；api 号还会把整段历史发给第三方端点。不想用默认挑的号就写 `account`。
- `session` 写标题时要和会话标题完全一样；写编号前缀时要能唯一对上，对上多个会报出来让你写完整编号。
- 别人派出的 sideagent（`ds#...`）只能由派它的主会话追问，这里会拒绝。

## 挂上（`sideagent_attach`）

挂上 = 把会话挂到你的面板上：它的进度和每一轮的完成通知都会送进你的会话。只在要**和它一起持续干、在面板上盯着**时用；发一句话用 `sideagent_send {session}`。

```
sideagent_attach {session: "<编号前缀|完整编号|标题>"}
→ 已挂上 b004/843fcef9（routine-online）… 名字 b004#bg-843fcef9
sideagent_send {account: "b004#bg-843fcef9", message: "..."}
sideagent_detach {name: "b004#bg-843fcef9"}      # 不用了就放
```

- 不写 `account` 时接它现在挂的号（同 `sideagent_send {session}` 的排序）。
- 挂上别人的会话前先问用户一声；一次只接要一起干的那一条，多对多能接不等于该接。
- `sideagent_detach` 只从面板拿掉，会话照跑；`sideagent_stop` / `interrupt` 对挂上的会话无效，要停用 `cca <号> stop <短编号>`。
- 不带 `session` 调 `sideagent_attach`，装了面板的会话里会打开挑选框交给用户，你只收到「已打开挑选框」。

## 卡住与收不到

- **`blocked`** = 卡在等确认：`sideagent_status` 的 `waiting_for` 写着卡在哪。装了权限钩子的号在面板里按 `y` / `n`（`cca sideagent hook install --profile <号>`），没装的 `cca <号> attach <短编号>` 进去点。这期间它不处理消息，别反复发、也别当成卡死。
- **`API 没回应：… 第 N 次重试`**（`sideagent_status` 的 `doing`、面板横条）= Claude Code 在自己重试 API，不是卡死，别催、别重发。gemini 这类走内网网关的号，推理模型首字常要几分钟。
- **`卡住了：N 秒没有任何输出`** = 一轮里 15 分钟一行输出都没有，这一轮已放弃，文案里带最后一次 API 错误。用 `sideagent_send {account: <实例全名>}` 让它接着做；同一个号反复这样，换一个号派。
- **跨会话消息要对方同意才进来**：发话看目标号 `settings.json` 的 `crossSessionInbound`，收回报看你自己号的；不是 `"accept"` 时要对方批准，cca 只自动补你自己这边。
- **唤醒了错的副本**（旧版 cca 会这样）：先 `cca <号> stop <短编号>`，再翻 transcript 末尾，看它在这期间有没有执行操作（发帖、改文件、提 MR），有就如实告诉用户。

## 换号与额度撞墙

```bash
cca handoff <源号> <目标号> --dry-run          # 先看计划
cca handoff <源号> <目标号> --only=<编号前缀>   # 源号停 → 目标号 --bg --resume，同一段对话接着跑
cca watchdog --dry-run                        # 撞额度的后台会话自动搬到有额度的号
cca wake <号> --only=<编号前缀>                # 不换号：等这个号额度重置后在原号叫醒
```

- `handoff` 的选择框按最后活动倒序、默认一条都不勾；`--only` 是编号前缀匹配，写完整标题时必须全场唯一。`stopped` 的会话只有 `--only` 能点名。搬成功后源号面板里的条目会被清掉，换号记在 `~/.cca/moves.jsonl`（`cca agents` 的「← 旧号」读它）。
- `watchdog` 是自动拉起的常驻进程，只管订阅号：号到了提醒点（5 小时 95% / 周 98%），还在干活的后台会话在这一轮结束时换号，撞墙的立刻换；所有号都满时撞墙的会话等重置再叫醒（`cca wake` 也写进同一份待叫醒表）。`cca watchdog status` 看它打算做什么，`cca agents` 看会话都在哪个号（刚换过号的标「← 旧号」）。同名的几段对话里只有一段在跑时，按名字发话会自动发给在跑的那段。
- 这几条都由用户决定要不要做：建议命令，别替用户搬别人的会话。
