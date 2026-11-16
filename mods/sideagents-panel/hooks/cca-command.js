// /cca：cca 的几个面板都收在这一个斜杠命令下面。
//   /cca            小菜单，回车进去
//   /cca attach [--all] [第一句话]   挂上挑选框；后面跟的话在接上后作为第一句话发给它
//   /cca handoff [号]   本会话这一轮结束后换号；不写号弹挑选框（第一行「自动」= 按额度挑）
//   /cca stay           本会话留在本号
// 搬别的会话、看门狗不在面板里做，在终端用 cca handoff、cca watchdog。
// 每个子命令的面板自己管自己（attach-picker.js），这里只分发。

import { closeList, openList, setRows } from './list-pane.js'
import { openPicker } from './attach-picker.js'
import { parseQuota } from './quota-hint.js'

export const COMMAND = 'cca'
export const MENU_ID = 'cca-menu'
export const HANDOFF_ID = 'cca-handoff'

// 能换过去的号：额度提醒每轮问 cca 时顺带拿到（register.js 调 setHandoffTargets），补全和挑选框先用它
let handoffTargets = []

export function setHandoffTargets(list) {
  if (Array.isArray(list)) handoffTargets = list
}

// 子命令（菜单里的顺序）：名字、中文名、一句说明
export const SUBCOMMANDS = [
  { name: 'attach', title: '挂上', note: '把本机别的会话挂到这里的 sideagent 面板上' },
  { name: 'handoff', title: '换号', note: '这一轮结束后把本会话换到别的号（选号，或自动按额度挑）' },
  { name: 'stay', title: '留下', note: '本会话留在这个号，额度到提醒点也不搬（撞墙照搬）' },
]

// 换号 / 留下：交给 cca（它登记给常驻的 watchdog），把 cca 的回话原样显示
async function runCcaHandoff($, name, args) {
  const sid = await sessionId($)
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

async function sessionId($) {
  try {
    return String((await $.session.id()) || '')
  } catch {
    // 没有 $.session.id（测试环境）时退回环境变量
    try {
      return String((await $.env.get('CLAUDE_CODE_SESSION_ID')) || '')
    } catch {
      return ''
    }
  }
}

// 挑选框的行：第一行「自动」，然后是按先用谁排好的号
export function handoffRows(targets) {
  const auto = targets.find((one) => one.auto)
  const rows = [{
    key: 'handoff-auto',
    cells: { name: '自动', usage: '', note: auto ? '按额度挑，现在会选 ' + auto.profile : '按额度挑' },
    data: '',
  }]
  for (const one of targets) {
    rows.push({
      key: 'handoff-' + one.profile,
      cells: { name: one.profile, usage: `5h ${one.five}% · 7d ${one.seven}%`, note: one.roomy ? '余量充足' : '余量不多' },
      data: one.profile,
    })
  }
  return rows
}

function handoffNote(targets) {
  return targets.length ? '' : '现在没有两个窗口都还有余量的号；选「自动」会等有号可去再换'
}

// /cca handoff 不写号：先用手上的列表画出来，再问 cca 要一份最新的
export async function openHandoffPicker($) {
  await openList($, {
    id: HANDOFF_ID,
    title: '换到哪个号（这一轮结束后换）',
    header: false,
    columns: [{ key: 'name', header: '', cap: 8 }, { key: 'usage', header: '', cap: 18 }, { key: 'note', header: '' }],
    rows: handoffRows(handoffTargets),
    note: '正在取最新额度…',
    hints: [{ key: 'hint-enter', label: '回车 换号' }, { key: 'hint-close', label: 'Ctrl+X X 关' }],
    onEnter: async (row) => {
      await closeList($, HANDOFF_ID)
      await $.ui.toast(await runCcaHandoff($, 'handoff', String(row.data || '')))
    },
    rowsHeight: Math.max(15, handoffTargets.length + 1),
  })
  try {
    const sid = await sessionId($)
    const out = await $.process.run(['cca', 'quota-hint', '--json', ...(sid ? ['--session', sid] : [])],
      { timeoutMs: 120000 })
    const got = out && Number(out.exitCode) === 0 ? parseQuota(out.stdout).targets : null
    if (got) {
      setHandoffTargets(got)
      setRows($, HANDOFF_ID, handoffRows(got), handoffNote(got))
    } else {
      setRows($, HANDOFF_ID, handoffRows(handoffTargets), '取不到最新额度，列的是之前的数')
    }
  } catch {
    setRows($, HANDOFF_ID, handoffRows(handoffTargets), '取不到最新额度，列的是之前的数')
  }
}

// 打开某个子命令的面板。用 switch 写死，不按名字动态调用（插件引擎要求 $ 只能直接传给认得的函数）
async function openSub($, name, args) {
  switch (name) {
    case 'attach': {
      // 后面跟的话是挂上后发给它的第一句话
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
      if (name === 'handoff') {
        await openHandoffPicker($)
        return
      }
      if (name === 'stay') {
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
  if (name === 'handoff' && !rest.length) {
    await openHandoffPicker($)
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
  // /cca handoff <号的头几个字母>：补能换过去的号
  const target = /^\s*\/cca\s+handoff\s+(\S+)$/.exec(before)
  if (target) {
    return handoffTargets.filter((one) => one.profile.startsWith(target[1]) && one.profile !== target[1])
      .map((one) => ({ text: one.profile, label: `${one.profile}  5h ${one.five}% · 7d ${one.seven}%`,
        description: one.roomy ? '余量充足' : '余量不多' }))
  }
  const match = /^\s*\/cca\s+(\S+)$/.exec(before)
  if (!match) return []
  const typed = match[1]
  return SUBCOMMANDS.filter((one) => one.name.startsWith(typed) && one.name !== typed)
    .map((one) => ({ text: one.name, label: one.name + '  ' + one.title, description: one.note }))
}
