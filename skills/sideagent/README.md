# sideagent

`sideagent` 让 Claude Code 会话知道 cca 的 sideagent 功能：什么时候把活派给另一个号、怎么选号、怎么写交办、怎么等回报、怎么给别的会话发话，以及常见故障怎么查。工具本身由 cca（`qiuham/claude-acct`）的 MCP 服务 `sideagents` 提供，这个 Skill 只补工具说明里没有的判断和坑。

## 前提

- 已安装 cca；号的 MCP 服务已注册（`cca add` / `cca login` 会自动注册，老号跑一次 `cca mcp install --all`，重启会话生效）。
- 只适用于 Claude Code；Codex 没有这组工具。

## 安装

cca 管理的各个号共享 `~/.claude/skills`，装一次全部号可见。这份 skill 放在 cca 公开仓库的 `skills/sideagent/` 里：

```bash
git clone --depth 1 https://github.com/qiuham/claude-acct.git /tmp/claude-acct
mkdir -p "$HOME/.claude/skills"
rm -rf "$HOME/.claude/skills/sideagent"
cp -r /tmp/claude-acct/skills/sideagent "$HOME/.claude/skills/sideagent"
```

更新时重复上面几步。安装或更新后重启 Claude Code 会话。

## 维护

sideagent 的行为以 cca 仓库为准。cca 改了工具参数、状态或排障路径时，同步检查 `SKILL.md` 第 3–5 节和 `references/`；参数细节留在工具自带说明里，不要抄进来。

- `SKILL.md`：什么时候派、派给谁、并行、交办、给别的会话发话的硬规则。保持短：它在触发时整份进上下文。
- `references/sessions.md`：跨号会话的查法、选号规则、挂上、换号与额度撞墙。
- `references/troubleshooting.md`：按现象排障。
