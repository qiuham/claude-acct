// /cca：cca 的几个面板都收在这一个斜杠命令下面。
//   /cca            小菜单，回车进去
//   /cca takeover [--all] [第一句话]   接管挑选框；后面跟的话在接上后作为第一句话发给它
// 换号、看门狗不在面板里做，在终端用 cca handoff、cca watchdog。
// 每个子命令的面板自己管自己（takeover-picker.js），这里只分发。

import { closeList, openList } from './list-pane.js'
import { openPicker } from './takeover-picker.js'

export const COMMAND = 'cca'
export const MENU_ID = 'cca-menu'

// 子命令（菜单里的顺序）：名字、中文名、一句说明
export const SUBCOMMANDS = [
  { name: 'takeover', title: '接管', note: '把本机别的会话挂到这里的 sideagent 面板上' },
  { name: 'handoff', title: '换号', note: '这一轮结束后把本会话换到别的号（不写号就按额度挑）' },
  { name: 'stay', title: '留下', note: '本会话留在这个号，额度到提醒点也不搬（撞墙照搬）' },
]

// 换号 / 留下：交给 cca（它登记给常驻的 watchdog），把 cca 的回话原样显示
async function runCcaHandoff($, name, args) {
  let sid = ''
  try {
    sid = String((await $.session.id()) || '')
  } catch {
    sid = ''
  }
  const words = name === 'stay' ? ['--stay'] : [...args.split(/\s+/).filter(Boolean).slice(0, 1), '--current']
  const argv = ['cca', 'handoff', ...words, ...(sid ? ['--session=' + sid] : [])]
  try {
    const out = await $.process.run(argv, { timeoutMs: 60000 })
    const text = [out && out.stdout, out && out.stderr].filter(Boolean).join('\n').trim()
    return text || (out && Number(out.exitCode) === 0 ? '已登记' : 'cca 没有回话')
  } catch (err) {
    return 'cca 调用失败：' + String((err && err.message) || err)
  }
}

// 打开某个子命令的面板。用 switch 写死，不按名字动态调用（插件引擎要求 $ 只能直接传给认得的函数）
async function openSub($, name, args) {
  switch (name) {
    case 'takeover': {
      // 后面跟的话是接管后发给它的第一句话
      await openPicker($, { firstWords: args.trim() })
      return true
    }
    default:
      return false
  }
}

export async function openMenu($) {
  await openList($, {
    id: MENU_ID,
    title: 'cca',
    header: false,
    columns: [{ key: 'name', header: '', cap: 10 }, { key: 'title', header: '', cap: 8 }, { key: 'note', header: '' }],
    rows: SUBCOMMANDS.map((entry) => ({
      key: 'menu-' + entry.name,
      cells: { name: entry.name, title: entry.title, note: entry.note },
      data: entry.name,
    })),
    hints: [{ key: 'hint-enter', label: '回车 打开' }, { key: 'hint-close', label: 'Ctrl+X X 关' }],
    onEnter: async (row) => {
      await closeList($, MENU_ID)
      const name = String(row.data)
      if (name === 'handoff' || name === 'stay') {
        await $.ui.toast(await runCcaHandoff($, name, ''))
        return
      }
      await openSub($, name, '')
    },
    rowsHeight: SUBCOMMANDS.length + 1,
  })
}

// command.run 的入口：第一个词是子命令，其余原样交给它
export async function runCca($, e) {
  const args = String(e.args || '').trim()
  const [name, ...rest] = args.split(/\s+/)
  if (!name) {
    await openMenu($)
    return {}
  }
  if (name === 'handoff' || name === 'stay') {
    return { text: await runCcaHandoff($, name, rest.join(' ')) }
  }
  if (!(await openSub($, name, rest.join(' ')))) {
    return { text: '不认识的子命令：' + name + '。可用：' + SUBCOMMANDS.map((one) => one.name).join('、') }
  }
  return {}
}

// 输入框里敲 /cca 再敲子命令的头几个字母时，在补全列表里给出子命令（Claude Code 的 prompt.autocomplete）。
// 只认“/cca 加一个正在敲的词”：token 是光标前那段不含空白的字，引擎保证不为空
export function ccaCompletions(e) {
  const before = String(e.text || '').slice(0, Number(e.cursor) || 0)
  const match = /^\s*\/cca\s+(\S+)$/.exec(before)
  if (!match) return []
  const typed = match[1]
  return SUBCOMMANDS.filter((one) => one.name.startsWith(typed) && one.name !== typed)
    .map((one) => ({ text: one.name, label: one.name + '  ' + one.title, description: one.note }))
}
