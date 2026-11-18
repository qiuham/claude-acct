# sideagent 排障

按现象查。cca 的完整机制见 `platform/claude-acct` 仓库 README 的「sideagent」一节。

| 现象 | 查哪里 / 怎么办 |
| --- | --- |
| 工具不存在 | 这个号还没注册 MCP：`cca mcp install --all`，然后重启会话（新建的号会自动注册） |
| `sideagent_send {session}` 回「要给 account 和 message」 | 会话起得早，MCP 还是旧版：重启会话；急的话先 attach → send → detach |
| 干完没回报 | `~/.cca/sideagents/<实例名>/progress`（一个实例一个目录）里的 `NOTIFY` 行：`ok` 已送达，`skip 原因` 没送，`fail` 连不上主会话 |
| 回报要手动确认才进来 | 主会话那个号的 `settings.json` 里 `crossSessionInbound` 不是 `"accept"`；cca 派活时会自动补，补完要重启会话 |
| 状态里 `stale: true` | 实例是 cca 升级前起的：`sideagent_stop` 后重新派 |
| 续会话报 `400 … tool_use.signature: Extra inputs are not permitted` | 这段历史是 Gemini 号跑的。用 cca 续（`cca <号> -r`、`handoff`、挂上、`sideagent_send {session}`）会自动清掉；裸 `claude --resume` 才会撞到 |
| 会话「不在了 / 下线了 / 没上线」、SendMessage not reachable | 没有下线这回事：`ListAgents` 只列本号正在跑的。要它做事就 `sideagent_send {session}`（自动找号、唤醒）；只想知道在哪，按 `references/sessions.md` 查 |
| 额度到了提醒点会话没换号 / `--current` 之后没换过去 | `cca watchdog status` 看「下一步」和「不搬」的原因（15 分钟内重置、闲置 30 分钟、`--stay`）；日志 `~/.claude-accounts/watchdog.log`。它在这一轮结束时才动手；交互会话不自动换 |
| 会话被停了又自己起来、过几秒又停（那个号的 `daemon.log` 里有 `claimed-spare … (respawn)`） | 不是 cca 停的：连上（attach）后台会话时它卡着不响应（后台任务多、机器内存吃紧时常见），Claude Code 的 daemon 会杀掉重起。再启动一次就行（面板里启动，或 `cca <号> attach <编号>`）；在跑的后台任务会丢，续上后它会收到通知 |
| 报「卡住了：N 秒没有任何输出」 | 15 分钟没有一行输出，这一轮已放弃；文案里的「最后一次 API 错误」就是原因。只显示「API 没回应：…第 N 次重试」时还没卡死，别催。内网网关号（gemini 等）首字慢是网关的问题，cca 已放宽等待，见 `references/sessions.md` |
| 想看它在干什么 | `sideagent_status` 的 `doing`；装了面板插件时看输入框上方的横条 |
| 面板里点击没反应 | 鼠标只在全屏渲染下有效；`cca set tui fullscreen` |
