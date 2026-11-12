import { expect, mock, test as rawTest } from 'claude-code/testing'
import { bandMark, circleOf } from '../hooks/marks.js'
import { agoText, sessionRow, stateLook, summaryText, takenText } from '../hooks/takeover-picker.js'
import { checkMark, columnWidths } from '../hooks/list-pane.js'
import { ccaCompletions } from '../hooks/cca-command.js'
import { otherTasksText } from '../hooks/register.js'
import { QUOTA_POLL_MS, parseHint, parseQuota, withHint } from '../hooks/quota-hint.js'
import { activityOf, notifyKey, waitingWhat, headerMeta, whoOf, statusText, arrowTarget, recallStep, bandLayout, compactCount, displayWidth, elapsedText, foldTasks, footerRows, taskKind, workflowCounts, progressBar } from '../hooks/register.js'

const HOME = '/home/test'
const SID = 'sess-test'
const DIR = HOME + '/.cca/sideagents'
const SESSIONS = HOME + '/.cca/sessions'

// ---- 夹具翻译 ----
// 下面老测试的夹具是改目录前的平铺格式：~/.cca/sideagents/<名字>.json/.progress/.events/.live/.sock/.owner，
// 归属看 .owner 或状态里的 owner_session。面板现在读新格式（docs/specs/sideagent-mn.md）：
// 先读 ~/.cca/sessions/<会话>.json 名单，再读 ~/.cca/sideagents/<名字>/state.json、progress、events.jsonl、
// run/live、run/sock。这里把面板发出的新格式请求翻成老夹具上的请求，老测试的夹具和断言一条不改。
// 直接摆新格式的测试用 rawTest（不翻译）。
const NEW_FILES: Record<string, string> = {
  'state.json': '.json', progress: '.progress', 'events.jsonl': '.events', 'run/live': '.live',
  'run/sock': '.sock', 'summary.log': '.summary.log', 'result.json': '.last.json', log: '.log',
}
const AGENT_SUFFIXES = ['.last.json', '.json', '.progress', '.events', '.owner', '.live', '.sock']

function legacyOn(on: any) {
  const handlers: Record<string, any> = {}
  const call = async (kind: string, $: any, e: any) => (handlers[kind] ? await handlers[kind]($, e) : undefined)
  const flatList = async ($: any) => {
    try {
      return ((await call('fs.list', $, { path: DIR }))?.value || []) as any[]
    } catch {
      return []
    }
  }
  const flatRead = async ($: any, path: string) => {
    try {
      const got = await call('fs.read', $, { path })
      return got && typeof got.value === 'string' ? got.value : ''
    } catch {
      return ''
    }
  }
  const json = (text: string) => {
    try {
      const data = JSON.parse(text)
      return data && typeof data === 'object' ? data : null
    } catch {
      return null
    }
  }
  // 老夹具里有哪些 sideagent、各归谁（.owner 优先，再看状态的 owner_session），名单里那一条带上纸条里的名字
  const agents = async ($: any) => {
    const names = new Set<string>()
    for (const entry of await flatList($)) {
      const suffix = AGENT_SUFFIXES.find((one) => entry.name.endsWith(one))
      if (suffix && suffix !== '.last.json') names.add(entry.name.slice(0, -suffix.length))
    }
    const out: any[] = []
    for (const name of names) {
      const note = json(await flatRead($, DIR + '/' + name + '.owner')) || {}
      const state = json(await flatRead($, DIR + '/' + name + '.json')) || {}
      const attached = state.kind === 'attached'
      out.push({ name, owner: String(note.session || state.owner_session || ''),
        entry: { kind: attached ? 'attached' : 'spawned', label: note.label || '', description: note.description || '',
          caller_agent: note.caller_agent || '', ...(note.model ? { model: note.model } : {}) } })
    }
    return out
  }
  // 新路径 → (名字, 老后缀)；不是 sideagents/<名字>/ 下的返回 null
  const split = (path: string) => {
    if (!path.startsWith(DIR + '/')) return null
    const rest = path.slice(DIR.length + 1)
    const cut = rest.indexOf('/')
    if (cut < 0) return null
    const suffix = NEW_FILES[rest.slice(cut + 1)]
    return suffix ? { name: rest.slice(0, cut), suffix } : null
  }
  const listed = async ($: any, name: string) => (await flatList($)).some((entry) => entry.name === name)
  const wrapped: Record<string, any> = {
    'fs.list': async ($: any, e: any) => {
      const path = String(e.path)
      if (path === SESSIONS) {
        const owners = new Set((await agents($)).map((one) => one.owner).filter(Boolean))
        return { value: [...owners].map((owner) => ({ name: owner + '.json', kind: 'file', size: 10, isLink: false })) }
      }
      if (path.startsWith(DIR + '/') && !path.slice(DIR.length + 1).includes('/')) {
        const name = path.slice(DIR.length + 1)
        const back = Object.fromEntries(Object.entries(NEW_FILES).map(([fresh, old]) => [old, fresh]))
        return { value: (await flatList($)).filter((entry) => entry.name.startsWith(name + '.'))
          .map((entry) => ({ ...entry, name: back[entry.name.slice(name.length)] || entry.name }))
          .filter((entry) => !entry.name.includes('.') || entry.name.endsWith('.json') || entry.name.endsWith('.jsonl')
            || entry.name.endsWith('.log')) }
      }
      return call('fs.list', $, e)
    },
    'fs.read': async ($: any, e: any) => {
      const path = String(e.path)
      if (path.startsWith(SESSIONS + '/')) {
        const session = path.slice(SESSIONS.length + 1, -'.json'.length)
        const mine = (await agents($)).filter((one) => one.owner === session)
        if (!mine.length) throw new Error('没有这个名单')
        return { value: JSON.stringify({ session, agents: Object.fromEntries(mine.map((one) => [one.name, one.entry])) }) }
      }
      const hit = split(path)
      if (!hit) return call('fs.read', $, e)
      const text = await flatRead($, DIR + '/' + hit.name + hit.suffix)
      // 老格式退出后状态文件就没了；新格式 state.json 一直在：老夹具缺它时给个空的
      if (hit.suffix === '.json' && !json(text)) return { value: '{}' }
      return { value: text }
    },
    'fs.exists': async ($: any, e: any) => {
      const hit = split(String(e.path))
      if (!hit) return call('fs.exists', $, e)
      // 老面板只把列表里有 <名字>.json 的当成可能在跑
      if (hit.suffix === '.sock' && !(await listed($, hit.name + '.json'))) return { value: false }
      return call('fs.exists', $, { ...e, path: DIR + '/' + hit.name + hit.suffix })
    },
  }
  return (kind: string, handler: any) => {
    if (!wrapped[kind]) return on(kind, handler)
    handlers[kind] = handler
    return on(kind, wrapped[kind])
  }
}

// 老测试照旧写 test(...)：拿到的 on 带夹具翻译
const test = (name: string, fn: any) => rawTest(name, fn.length >= 2 ? ($: any, on: any) => fn($, legacyOn(on)) : fn)

// 横条的字画在每一行的 Client 里：先在插件自己的树里找，找不到再到每个 Client 画的东西里找
// （Client 里用 ui.drawn 拿到画出来的树自己找：带 in 的 find 找不到时会一直等）
function textOf(node: any): string {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  return textOf(node.props?.children ?? node.children)
}
function walk(node: any, out: any[] = []): any[] {
  if (!node || typeof node !== 'object') return out
  if (Array.isArray(node)) { for (const one of node) walk(one, out); return out }
  out.push(node)
  walk(node.props?.children ?? node.children, out)
  return out
}
function matches(text: string, want: any) {
  return want instanceof RegExp ? want.test(text) : text.includes(String(want))
}
async function findAny(ui: any, query: any) {
  const direct = await ui.find(query)
  if (direct) return direct
  for (const client of await ui.findAll({ type: 'Client' })) {
    const tree = await ui.drawn({ in: client.key })
    for (const node of walk(tree)) {
      if (query.type && node.type !== query.type) continue
      const text = textOf(node)
      if (query.text === undefined || matches(text, query.text)) return { ...node, text }
    }
  }
  return undefined
}

// Claude Code 传给 ui.render 的输入
const SITE = {
  plugin: 'sideagents-panel',
  component: 'AbovePrompt',
  viewport: { columns: 100, rows: 30 },
  props: {},
}

test('一个在跑的sideagent显示成一行', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  // 兜底：钩子返回 next(e) 时 Claude Code 自己画的东西
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', ($, e) => ({
    value: e.path === DIR
      ? [{ name: 'ds.json', kind: 'file', size: 10, isLink: false },
         { name: 'ds.progress', kind: 'file', size: 10, isLink: false }]
      : [],
  }))
  on('fs.exists', ($, e) => ({ value: e.path.endsWith('.sock') }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('ds.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('ds.progress')) return { value: 'TOOL Bash · ls -la\nSAY 正在改 peer.py\n' }
    return { value: '' }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  // 没选中时是空心圆，选中才是白点；名字、内容分开画
  expect(await findAny(ui, { type: 'Text', text: '◯ ' })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: '●' })).toBeUndefined()
  expect(await findAny(ui, { type: 'Text', text: 'ds' })).toBeDefined()
  // 显示最后一步：sideagent自己说的话
  expect(await findAny(ui, { type: 'Text', text: /正在改 peer.py/ })).toBeDefined()
})

test('没有sideagent时什么都不画', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
    on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [] }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /sideagent/ })).toBeUndefined()
})

test('在调工具时显示工具和参数', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('ds.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('ds.progress')) return { value: 'TOOL Bash · ls -la\n' }
    return { value: '' }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const line = await findAny(ui, { type: 'Text', text: /Bash · ls -la/ })
  expect(line).toBeDefined()
})

test('刚派活时显示交办的事', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('ds.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('ds.progress')) return { value: 'TASK 用 Bash 跑 sleep 3\n' }
    return { value: '' }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const line = await findAny(ui, { type: 'Text', text: /用 Bash 跑 sleep 3/ })
  expect(line).toBeDefined()
})

test('三个sideagent同时显示成三行', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [
    { name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false },
    { name: 'ds#bbb.json', kind: 'file', size: 10, isLink: false },
    { name: 'ds#ccc.json', kind: 'file', size: 10, isLink: false },
  ] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.progress')) return { value: 'TOOL Bash · sleep 8\n' }
    return { value: '' }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const lines = await findAny(ui, { type: 'Text', text: /Bash · sleep 8/ })
  expect(lines).toBeDefined()
  // 三行 sideagent 画在同一个自绘区域里
  const band = await ui.find({ type: 'Client', key: 'band' })
  expect((band?.props.props as any).rows.length).toBe(3)
})

test('只显示本会话派的sideagent，别的会话派的不出现', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [
    { name: 'mine.json', kind: 'file', size: 10, isLink: false },
    { name: 'other.json', kind: 'file', size: 10, isLink: false },
  ] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('mine.json')) {
      return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    }
    if (e.path.endsWith('other.json')) {
      return { value: JSON.stringify({ owner_session: '别的会话', started: 0 }) }
    }
    if (e.path.endsWith('.progress')) return { value: 'TOOL Bash · ls\n' }
    return { value: '' }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: 'mine' })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: 'other' })).toBeUndefined()
})

test('刚结束的sideagent显示完成，保留一会儿', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  // 只剩进度文件，没有 sock 和状态：sideagent已经销毁
  on('fs.list', () => ({ value: [
    { name: 'ds#done.progress', kind: 'file', size: 10, isLink: false, mtimeMs: Date.now() },
    { name: 'ds#old.progress', kind: 'file', size: 10, isLink: false, mtimeMs: Date.now() - 60000 },
  ] }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', ($, e) => ({
    value: e.path.endsWith('.owner')
      ? JSON.stringify({ session: SID, account: 'ds' })
      : 'TASK 跑一下\nRESULT 好了 · 25.2k tok · 8.9 秒\n',
  }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  // 和子代理做完一样：圆点变绿，不换成 ✓；右边是总耗时（照 Claude Code 写成 8s）
  const dot = await findAny(ui, { type: 'Text', text: '◯ ' })
  expect(dot?.props?.color).toBe('success')
  expect(await findAny(ui, { type: 'Text', text: /^ds · 8s · 25\.2k tokens$/ })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: /25.2k tokens/ })).toBeDefined()
  // 一分钟前的旧文件不显示
  expect(await findAny(ui, { type: 'Text', text: /ds#old/ })).toBeUndefined()
})

test('按行打开详情面板', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, tokens: 1200 }) }
    if (e.path.endsWith('.progress')) return { value: 'TASK 跑一下\nTOOL Bash · sleep 5\n' }
    return { value: '' }
  })
  const opened: string[] = []
  on('ui.open', ($, e) => {
    opened.push(e.id)
    return { value: { isPlaced: true } }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await ui.press({ key: 'peer-ds#aaa' })
  expect(opened).toEqual(['peer-ds-aaa'])
})

test('详情面板按主界面样子画：任务、工具、文字', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) {
      return { value: [
        JSON.stringify({ kind: 'task', text: '数一下文件' }),
        JSON.stringify({ kind: 'tool', name: 'Bash', input: { command: 'ls | wc -l' }, summary: 'ls | wc -l' }),
        JSON.stringify({ kind: 'tool_result', text: '25' }),
        JSON.stringify({ kind: 'say', text: '一共 **25** 个文件。' }),
      ].join('\n') + '\n' }
    }
    if (e.path.endsWith('.progress')) return { value: 'TASK 数一下文件\nTOOL Bash · ls | wc -l\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: /数一下文件/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /Bash\(ls \| wc -l\)/ })).toBeDefined()
  expect(await pane.find({ type: 'Markdown' })).toBeDefined()
})

test('正在生成的文字显示成一行', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.live')) return { value: '正在生成' }
    if (e.path.endsWith('.events')) return { value: JSON.stringify({ kind: 'task', text: '做点事' }) + '\n' }
    if (e.path.endsWith('.progress')) return { value: 'TASK 做点事\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: /正在生成/ })).toBeDefined()
})

test('Write、Edit、Read 都能画出来', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '改个文件' },
    { kind: 'tool', name: 'Write', input: { file_path: '/tmp/a.txt', content: '一\n二\n' }, summary: '/tmp/a.txt' },
    { kind: 'tool_result', text: 'ok' },
    { kind: 'tool', name: 'Edit', input: { file_path: '/tmp/a.txt', old_string: '二', new_string: '改过' } },
    { kind: 'tool_result', text: 'ok' },
    { kind: 'tool', name: 'Read', input: { file_path: '/tmp/a.txt' } },
    { kind: 'tool_result', text: '1 一\n2 改过' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 改个文件\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  // 这个用例要看全，窗口开大些
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 60 }, view: {} }, surface: 'terminal' })
  // 三个工具先折成一行
  expect(await pane.find({ type: 'Text', text: /Ran 3 tools/ })).toBeDefined()
  await pane.press({ key: 'group-ds-aaa-1' })
  // 点开后出现"收起"按钮，工具明细出现
  expect(await pane.find({ type: 'Button', key: 'group-ds-aaa-1-fold' })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /Write\(\/tmp\/a.txt\)/ })).toBeDefined()
})

test('工具输出收起来只留 3 行，点开看全部', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '跑个命令' },
    { kind: 'tool', name: 'Bash', input: { command: 'seq 5' }, summary: 'seq 5' },
    { kind: 'tool_result', text: 'LINE-A\nLINE-B\nLINE-C\nLINE-D\nLINE-E' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 跑个命令\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 60 }, view: {} }, surface: 'terminal' })
  // 只留前三行；后两行折起来
  expect(await pane.find({ type: 'Text', text: 'LINE-A' })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: 'LINE-C' })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: 'LINE-D' })).toBeUndefined()
  expect(await pane.find({ type: 'Text', text: /还有 2 行/ })).toBeDefined()
  await pane.press({ key: 'out-ds-aaa-2' })
  expect(await pane.find({ type: 'Text', text: 'LINE-E' })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /还有 2 行/ })).toBeUndefined()
})

test('Write 只留一行摘要，不画文件内容', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '写文件' },
    { kind: 'tool', name: 'Write', input: { file_path: '/tmp/b.txt', content: 'CONTENT-ONE\nCONTENT-TWO' }, summary: '/tmp/b.txt' },
    { kind: 'tool_result', text: 'ok' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 写文件\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 60 }, view: {} }, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: /Write\(\/tmp\/b\.txt\) · 2 行/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /CONTENT-ONE/ })).toBeUndefined()
})

test('连续的读文件收成一行汇总', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '看三个文件' },
    { kind: 'tool', name: 'Read', input: { file_path: '/tmp/a' } },
    { kind: 'tool_result', text: 'A' },
    { kind: 'tool', name: 'Read', input: { file_path: '/tmp/b' } },
    { kind: 'tool_result', text: 'B' },
    { kind: 'tool', name: 'Read', input: { file_path: '/tmp/c' } },
    { kind: 'tool_result', text: 'C' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 看三个文件\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 60 }, view: {} }, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: /Read 3/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /Ran 3 tools/ })).toBeUndefined()
})

test('Edit 默认只画 3 行 diff，点开看全部', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '改文件' },
    { kind: 'tool', name: 'Edit', input: { file_path: '/tmp/c.txt', old_string: 'l1\nl2\nl3\nl4', new_string: 'n1' } },
    { kind: 'tool_result', text: 'ok' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 改文件\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 60 }, view: {} }, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: /还有 2 行/ })).toBeDefined()
  const before = (await pane.find({ type: 'Code' })) as any
  expect(before.props.source.split('\n').length).toBe(6)
  await pane.press({ key: 'detail-ds-aaa-1' })
  const after = (await pane.find({ type: 'Code' })) as any
  expect(after.props.source.split('\n').length).toBe(8)
  expect(await pane.find({ type: 'Text', text: /还有 2 行/ })).toBeUndefined()
})

test('横条圆点：没选中空心，选中白点，做完留 ✓', async () => {
  expect(bandMark({ mark: '●', color: 'success' }, false)).toEqual({ mark: '○', color: 'inactive' })
  expect(bandMark({ mark: '●', color: 'success' }, true)).toEqual({ mark: '●', color: undefined })
  expect(bandMark({ mark: '✓', color: 'success' }, false)).toEqual({ mark: '✓', color: 'success' })
})

test('子代理单独成区，行内用 Agent 标记', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['Claude Code 自己画的'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '让子代理并行干活' },
    { kind: 'tool', name: 'Agent', id: 'a1', input: { description: '查文档' } },
    { kind: 'tool', name: 'Agent', id: 'a2', input: { description: '改代码' } },
    { kind: 'tool', name: 'Bash', id: 'b1', parent: 'a1', input: { command: 'grep -r foo' } },
    { kind: 'tool_result', id: 'b1', parent: 'a1', text: '结果' },
    { kind: 'tool_result', id: 'a1', text: '[Subagent hand-back]\n完成' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, busy: true }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 让子代理并行干活\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  // 正文里不画子代理的调用
  expect(await pane.find({ type: 'Text', text: /Agent\(查文档\)/ })).toBeUndefined()
  // 还在跑的挂在输入框下面；已经跑完的不显示
  expect(await hasTask(pane, /Agent\(改代码\)/)).toBe(true)
  // 子代理内部的 Bash 不混进正文
  expect(await pane.find({ type: 'Text', text: /grep -r foo/ })).toBeUndefined()
})

test('子代理块钉在面板底边，跟着滚动位置走', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '让子代理并行干活' },
    { kind: 'tool', name: 'Agent', id: 'a1', input: { description: '查文档' } },
    { kind: 'tool', name: 'Agent', id: 'a2', input: { description: '改代码' } },
    { kind: 'tool_result', id: 'a1', text: '[Subagent hand-back]\n完成' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 让子代理并行干活\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const mount = (offset: number) => $.ui.mount({
    plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa',
    viewport: { columns: 100, rows: 30 },
    props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset, bodyRows: 20 }, view: {} },
    surface: 'terminal',
  })
  const top = await mount(0)
  expect(await hasTask(top, /Agent\(改代码\)/)).toBe(true)
  // 正文不再自己开窗：底部排在正文最后，跟着一起滚
  expect(await top.find({ key: 'win-peer-ds-aaa' })).toBeUndefined()
  expect(await top.find({ key: 'agent-peer-ds-aaa-0' })).toBeDefined()
  // 底部那块排在窗口后面（正常流），不是浮在上面
  const footer = await top.find({ key: 'foot-peer-ds-aaa-1' })
  expect(footer?.props.position).toBeUndefined()
  await top.unmount()
})

test('真实事件：子代理内部的命令不进正文', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const EVENTS = "{\"kind\": \"task\", \"text\": \"\u8bf7\u540c\u65f6\u8d77\u4e24\u4e2a Explore \u5b50\u4ee3\u7406\uff0c\u6bcf\u4e2a\u90fd\u8981\u8dd1\u4e45\u4e00\u70b9\uff0c\u597d\u8ba9\u6211\u770b\u8fdb\u5ea6\uff1a 1. \u5148 sleep 40 \u79d2\uff0c\u518d\u7edf\u8ba1 /home/ham/claude-acct/src \u4e0b .py \u6587\u4ef6\u6570\uff0c\u518d sleep 30 \u79d2\uff0c\u6700\u540e\u62a5\u6570\u5b57\u3002 2. \u5148 sle\"}\n{\"kind\": \"think\", \"text\": \"The user wants two Explore subagents launched in the same message, in parallel. Each has sleep instructions. Let me launch both in one message.\\n\\nNote: Explore agents are read-only but can run Bash. Let me just do it.\", \"t\": 1.8}\n{\"kind\": \"say\", \"text\": \"\u6211\u540c\u65f6\u542f\u52a8\u4e24\u4e2a Explore \u5b50\u4ee3\u7406\u3002\", \"tokens\": 24980, \"t\": 1.9}\n{\"kind\": \"tool\", \"name\": \"Agent\", \"id\": \"call_00_of5RbWqWnOhOdNKzdjdo0434\", \"input\": {\"description\": \"\u7edf\u8ba1 src \u4e0b .py \u6587\u4ef6\u6570\", \"prompt\": \"\u4f60\u7684\u4efb\u52a1\uff08\u6309\u987a\u5e8f\u6267\u884c\uff0c\u4e0d\u8981\u8df3\u6b65\uff09\uff1a\\n\\n1. \u5148\u6267\u884c `sleep 40` \u7b49\u5f85 40 \u79d2\u3002\\n2. \u7136\u540e\u7edf\u8ba1 /home/ham/claude-acct/src \u76ee\u5f55\u4e0b\uff08\u9012\u5f52\u6240\u6709\u5b50\u76ee\u5f55\uff09\u6240\u6709 .py \u6587\u4ef6\u7684\u6570\u91cf\u3002\u53ef\u4ee5\u7528\u7c7b\u4f3c `find /home/ham/claude-acct/src -type f -name '*.py' | wc -l` \u7684\u547d\u4ee4\u3002\u540c\u65f6\u8bb0\u5f55\u6709\u591a\u5c11\u4e2a .py \u6587\u4ef6\u3001\u76ee\u5f55\u662f\u5426\u5b58\u5728\uff08\u5982\u679c\u76ee\u5f55\u4e0d\u5b58\u5728\u6216\u6709\u9519\u8bef\uff0c\u8981\u5982\u5b9e\u62a5\u544a\u9519\u8bef\u4fe1\u606f\uff09\u3002\\n3. \u518d\u6267\u884c `sleep 30` \u7b49\u5f85 30 \u79d2\u3002\\n4. \u6700\u540e\u62a5\u544a\uff1a/home/ham/claude-acct/src \u4e0b .py \u6587\u4ef6\u7684\u603b\u6570\uff08\u4e00\u4e2a\u6570\u5b57\uff09\uff0c\u4ee5\u53ca\u4f60\u7528\u7684\u547d\u4ee4\u548c\u4efb\u4f55\u5f02\u5e38\u60c5\u51b5\u3002\\n\\n\u8981\u6c42\uff1a\u4e0d\u8981\u63d0\u524d\u7ed3\u675f\uff0c\u5fc5\u987b\u628a\u4e24\u4e2a sleep \u90fd\u6267\u884c\u5b8c\u3002\u62a5\u544a\u7528\u4e2d\u6587\u3002\", \"subagent_type\": \"Explore\"}, \"summary\": \"\u7edf\u8ba1 src \u4e0b .py \u6587\u4ef6\u6570\", \"parent\": \"\", \"t\": 3.0}\n{\"kind\": \"tool_result\", \"text\": \"Async agent launched successfully. (This tool result is internal metadata \u2014 never quote or paste any part of it, including the agentId below, into a user-facing reply.)\\nagentId: ae21ebab6c52fa9b3 (internal ID - do not mention to user. Use SendMessage with to: 'ae21ebab6c52fa9b3', summary: '<5-10 word recap>' to continue this agent.)\\nThe agent is working in the background. You will be notified automatically when it completes. You know nothing about its results until that notification arrives \u2014 do not report, assume, or predict them; continue other work or respond to the user in the meantime.\\nDo not duplicate this agent's work \u2014 avoid working with the same files or topics it is using.\\noutput_file: /tmp/claude-1000/-home-ham/0db3c38c-1fe2-44a8-aa36-47eaf0dae217/tasks/ae21ebab6c52fa9b3.output\\nDo NOT Read or tail this file via the shell tool \u2014 it is the full subagent JSONL transcript and reading it will overflow your context. If the user asks for progress, say the agent is still running; you'll get a completion notification.\", \"id\": \"call_00_of5RbWqWnOhOdNKzdjdo0434\", \"parent\": \"\", \"t\": 3.0}\n{\"kind\": \"tool\", \"name\": \"Agent\", \"id\": \"call_01_WvJ2DULHbxRA5fzDq63d1327\", \"input\": {\"description\": \"\u7edf\u8ba1 mods \u4e0b .js \u6587\u4ef6\u6570\", \"prompt\": \"\u4f60\u7684\u4efb\u52a1\uff08\u6309\u987a\u5e8f\u6267\u884c\uff0c\u4e0d\u8981\u8df3\u6b65\uff09\uff1a\\n\\n1. \u5148\u6267\u884c `sleep 50` \u7b49\u5f85 50 \u79d2\u3002\\n2. \u7136\u540e\u7edf\u8ba1 /home/ham/claude-acct/mods \u76ee\u5f55\u4e0b\uff08\u9012\u5f52\u6240\u6709\u5b50\u76ee\u5f55\uff09\u6240\u6709 .js \u6587\u4ef6\u7684\u6570\u91cf\u3002\u53ef\u4ee5\u7528\u7c7b\u4f3c `find /home/ham/claude-acct/mods -type f -name '*.js' | wc -l` \u7684\u547d\u4ee4\u3002\u540c\u65f6\u8bb0\u5f55\u76ee\u5f55\u662f\u5426\u5b58\u5728\uff08\u5982\u679c\u76ee\u5f55\u4e0d\u5b58\u5728\u6216\u6709\u9519\u8bef\uff0c\u8981\u5982\u5b9e\u62a5\u544a\u9519\u8bef\u4fe1\u606f\uff09\u3002\\n3. \u518d\u6267\u884c `sleep 30` \u7b49\u5f85 30 \u79d2\u3002\\n4. \u6700\u540e\u62a5\u544a\uff1a/home/ham/claude-acct/mods \u4e0b .js \u6587\u4ef6\u7684\u603b\u6570\uff08\u4e00\u4e2a\u6570\u5b57\uff09\uff0c\u4ee5\u53ca\u4f60\u7528\u7684\u547d\u4ee4\u548c\u4efb\u4f55\u5f02\u5e38\u60c5\u51b5\u3002\\n\\n\u8981\u6c42\uff1a\u4e0d\u8981\u63d0\u524d\u7ed3\u675f\uff0c\u5fc5\u987b\u628a\u4e24\u4e2a sleep \u90fd\u6267\u884c\u5b8c\u3002\u62a5\u544a\u7528\u4e2d\u6587\u3002\", \"subagent_type\": \"Explore\"}, \"summary\": \"\u7edf\u8ba1 mods \u4e0b .js \u6587\u4ef6\u6570\", \"parent\": \"\", \"t\": 3.8}\n{\"kind\": \"tool_result\", \"text\": \"Async agent launched successfully. (This tool result is internal metadata \u2014 never quote or paste any part of it, including the agentId below, into a user-facing reply.)\\nagentId: acd9a2df90c429930 (internal ID - do not mention to user. Use SendMessage with to: 'acd9a2df90c429930', summary: '<5-10 word recap>' to continue this agent.)\\nThe agent is working in the background. You will be notified automatically when it completes. You know nothing about its results until that notification arrives \u2014 do not report, assume, or predict them; continue other work or respond to the user in the meantime.\\nDo not duplicate this agent's work \u2014 avoid working with the same files or topics it is using.\\noutput_file: /tmp/claude-1000/-home-ham/0db3c38c-1fe2-44a8-aa36-47eaf0dae217/tasks/acd9a2df90c429930.output\\nDo NOT Read or tail this file via the shell tool \u2014 it is the full subagent JSONL transcript and reading it will overflow your context. If the user asks for progress, say the agent is still running; you'll get a completion notification.\", \"id\": \"call_01_WvJ2DULHbxRA5fzDq63d1327\", \"parent\": \"\", \"t\": 3.8}\n{\"kind\": \"say\", \"text\": \"\u6211\u4f1a\u6309\u987a\u5e8f\u6267\u884c\u8fd9\u4e9b\u6b65\u9aa4\uff1a\u5148\u7b49\u5f85 50 \u79d2\u3002\", \"tokens\": 2050, \"t\": 4.4}\n{\"kind\": \"tool\", \"name\": \"Bash\", \"id\": \"call_00_ET_090E8NLy3jzi5x8m9bnC5567\", \"input\": {\"command\": \"sleep 50\", \"description\": \"Wait 50 seconds\", \"timeout\": 60000}, \"summary\": \"sleep 50\", \"parent\": \"call_01_WvJ2DULHbxRA5fzDq63d1327\", \"t\": 4.6}\n{\"kind\": \"tool_result\", \"text\": \"<tool_use_error>Blocked: standalone sleep 50. To wait for a condition, use Monitor with an until-loop (e.g. `until <check>; do sleep 2; done`). To wait for a command you started, use run_in_background: true. Do not chain shorter sleeps to work around this block.</tool_use_error>\", \"id\": \"call_00_ET_090E8NLy3jzi5x8m9bnC5567\", \"parent\": \"call_01_WvJ2DULHbxRA5fzDq63d1327\", \"t\": 4.6}\n{\"kind\": \"say\", \"text\": \"\u4e24\u4e2a\u5b50\u4ee3\u7406\u5df2\u540c\u65f6\u542f\u52a8\uff0c\u90fd\u5728\u540e\u53f0\u8dd1\uff1a\\n\\n1. \u7b2c\u4e00\u4e2a\uff1a\u5148 sleep 40 \u79d2\uff0c\u6570 `src` \u4e0b `.py`\uff0c\u518d sleep 30 \u79d2\u3002\\n2. \u7b2c\u4e8c\u4e2a\uff1a\u5148 sleep 50 \u79d2\uff0c\u6570 `mods` \u4e0b `.js`\uff0c\u518d sleep 30 \u79d2\u3002\\n\\n\u4f30\u8ba1\u7b2c\u4e00\u4e2a\u7ea6 70 \u79d2\u540e\u7ed3\u675f\uff0c\u7b2c\u4e8c\u4e2a\u7ea6 80 \u79d2\u540e\u7ed3\u675f\u3002\u5b8c\u6210\u540e\u6211\u4f1a\u6536\u5230\u901a\u77e5\uff0c\u518d\u628a\u6570\u5b57\u62a5\u7ed9\u4f60\u3002\", \"tokens\": 621, \"t\": 4.9}\n{\"kind\": \"result\", \"text\": \"\u4e24\u4e2a\u5b50\u4ee3\u7406\u5df2\u540c\u65f6\u542f\u52a8\uff0c\u90fd\u5728\u540e\u53f0\u8dd1\uff1a\\n\\n1. \u7b2c\u4e00\u4e2a\uff1a\u5148 sleep 40 \u79d2\uff0c\u6570 `src` \u4e0b `.py`\uff0c\u518d sleep 30 \u79d2\u3002\\n2. \u7b2c\u4e8c\u4e2a\uff1a\u5148 sleep 50 \u79d2\uff0c\u6570 `mods` \u4e0b `.js`\uff0c\u518d sleep 30 \u79d2\u3002\\n\\n\u4f30\u8ba1\u7b2c\u4e00\u4e2a\u7ea6 70 \u79d2\u540e\u7ed3\u675f\uff0c\u7b2c\u4e8c\u4e2a\u7ea6 80 \u79d2\u540e\u7ed3\u675f\u3002\u5b8c\u6210\u540e\u6211\u4f1a\u6536\u5230\u901a\u77e5\uff0c\u518d\u628a\u6570\u5b57\u62a5\u7ed9\u4f60\u3002\", \"tokens_in\": 25601, \"tokens_out\": 668, \"seconds\": 4.465, \"t\": 4.9}\n{\"kind\": \"think\", \"text\": \"Foreground sleep is blocked. The user explicitly asks to sleep 50 seconds. I can use run_in_background for sleep 50. That's allowed: \\\"To wait for a command you started, use run_in_background: true.\\\"\\n\\nLet me run sleep 50 in background and monitor its exit. Actually run_in_background returns immediately but the command runs; I get notified when it exits. Let me do that.\\n\\nAlternatively, use Monitor with a command that sleeps 50 then exits. Monitor's command runs in background shell. `sleep 50` as monitor command would just exit after 50s with an event? It emits no stdout lines so no notification, but Monitor reports exit. Hmm.\\n\\nSimplest: Bash with run_in_background: true and timeout 60000. That runs detached and notifies on exit. Let's do that.\\n\\nBut I should be careful: run_in_background's timeout is how long it may run in background. Set 60000. Then I need to wait for it to complete before proceeding. The notification arrives as a task notification. I'll then proceed.\\n\\nActually a cleaner way: run `sleep 50; true` in background. When it completes, I'm re-invoked.\\n\\nLet me do step 1 that way.\", \"t\": 6.7}\n{\"kind\": \"say\", \"text\": \"\u524d\u53f0 sleep \u5df2\u88ab\u5c4f\u853d\uff0c\u6211\u6539\u7528\u540e\u53f0\u4efb\u52a1\u6267\u884c\u7b2c\u4e00\u6b65\u7684\u7b49\u5f85\u3002\", \"tokens\": 178, \"t\": 6.8}\n{\"kind\": \"tool\", \"name\": \"Bash\", \"id\": \"call_00_p8EXVjgmxDqbQB9mqlD51623\", \"input\": {\"command\": \"sleep 50; echo \\\"50s wait done\\\"\", \"description\": \"Wait 50 seconds in background\", \"timeout\": 60000, \"run_in_background\": true}, \"summary\": \"sleep 50; echo \\\"50s wait done\\\"\", \"parent\": \"call_01_WvJ2DULHbxRA5fzDq63d1327\", \"t\": 7.0}\n{\"kind\": \"tool_result\", \"text\": \"Command running in background with ID: bpj9n6hy2. Output is being written to: /tmp/claude-1000/-home-ham/0db3c38c-1fe2-44a8-aa36-47eaf0dae217/tasks/bpj9n6hy2.output. You will be notified when it completes. To check interim output, use Read on that file path.\", \"id\": \"call_00_p8EXVjgmxDqbQB9mqlD51623\", \"parent\": \"call_01_WvJ2DULHbxRA5fzDq63d1327\", \"t\": 7.1}\n{\"kind\": \"think\", \"text\": \"Now I need to wait for it to complete. I can't do anything else meaningful \u2014 the tasks are sequential. I should wait for the notification. Let me not call tools until notification? But I need to produce some output maybe. The system will re-invoke me when it exits. I'll just wait \u2014 but I must produce a message. Actually, if I end my turn, the notification will re-invoke me. But ending the turn might look like finishing. However, the background task notification resumes me.\\n\\nHmm, in this harness, background command completion re-invokes the agent. So I can end turn briefly? Safer: use Monitor to wait until the background output file contains \\\"done\\\". But that's chaining. Actually Monitor with until-loop on file content is permitted by the error message guidance.\\n\\nBut simplest: just end my message saying waiting, and the notification will re-invoke. Let me do that\u2014but risk: the parent agent may treat my message as final answer. The instructions say \\\"Do not use an unbounded command for a single notification... use Bash run_in_background\\\". So run_in_background is the sanctioned approach and it re-invokes on exit.\\n\\nI'll wait. Write a brief text and end turn? Then notification arrives. Let me instead poll with Monitor until-loop... that adds another wait mechanism.\\n\\nI think ending the turn with a short note and letting the completion notification re-invoke is standard. Let's do that.\\n\\nActually, could I just check the output file via Read after some time? No sleeping allowed.\\n\\nI'll end turn with a brief status.\", \"t\": 9.5}\n{\"kind\": \"say\", \"text\": \"\u7b2c\u4e00\u6b65\u7684 50 \u79d2\u7b49\u5f85\u5df2\u5728\u540e\u53f0\u5f00\u59cb\uff0c\u7b49\u5b83\u5b8c\u6210\u540e\u6211\u4f1a\u7ee7\u7eed\u7edf\u8ba1\u6587\u4ef6\u6570\u91cf\u3002\", \"tokens\": 148, \"t\": 9.7}\n{\"kind\": \"think\", \"text\": \"The second agent seems to have launched its sleep as a background command and returned early. Let me check if it resumes. Actually the note says it may resume on its own. Let me wait. But this is concerning \u2014 it stopped with background work still running. I should probably wait for the notification. But if the sleep was run in background detached, the agent may not resume. Actually the harness says \\\"It may resume on its own when that work completes or reports, and the same task-id notifies again if it does\\\".\\n\\nLet me not duplicate work. I'll wait. But maybe I should message it to continue? The safest is to wait for the re-notification. However, if it never resumes, the task fails. Let me consider: the agent ran sleep 50 in background, its turn ended. When the background command completes, the harness re-invokes the agent. That's the normal mechanism. So I should just wait.\\n\\nLet me inform the user briefly. Actually, no need to say much. Maybe I should just wait silently. But I need to output something. Let me note the status.\\n\\nActually, should I do anything? The first agent is still running. The second one is waiting on its sleep. I could just wait for notifications. I'll give a short status line.\", \"t\": 12.0}\n{\"kind\": \"say\", \"text\": \"\u7b2c\u4e8c\u4e2a\u5b50\u4ee3\u7406\u628a 50 \u79d2\u7b49\u5f85\u653e\u5230\u4e86\u540e\u53f0\u8fd0\u884c\uff0c\u7b49\u5b83\u9192\u6765\u4f1a\u7ee7\u7eed\u7edf\u8ba1\u3002\u7b2c\u4e00\u4e2a\u5b50\u4ee3\u7406\u8fd8\u5728\u8dd1\u3002\u7b49\u901a\u77e5\u3002\", \"tokens\": 481, \"t\": 12.2}\n{\"kind\": \"result\", \"text\": \"\u7b2c\u4e8c\u4e2a\u5b50\u4ee3\u7406\u628a 50 \u79d2\u7b49\u5f85\u653e\u5230\u4e86\u540e\u53f0\u8fd0\u884c\uff0c\u7b49\u5b83\u9192\u6765\u4f1a\u7ee7\u7eed\u7edf\u8ba1\u3002\u7b2c\u4e00\u4e2a\u5b50\u4ee3\u7406\u8fd8\u5728\u8dd1\u3002\u7b49\u901a\u77e5\u3002\", \"tokens_in\": 481, \"tokens_out\": 295, \"seconds\": 2.466, \"t\": 12.2}\n"
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.events')) return { value: EVENTS }
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  // 子代理内部跑的命令不出现在正文里
  expect(await pane.find({ type: 'Text', text: /sleep 50/ })).toBeUndefined()
})

test('点另一个sideagent就切过去，不留在标签里', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [
    { name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false },
    { name: 'ds#bbb.json', kind: 'file', size: 10, isLink: false },
  ] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.progress')) return { value: 'TASK 干活\n' }
    return { value: '' }
  })
  const opened: string[] = []
  const closed: string[] = []
  on('ui.open', ($, e) => { opened.push(e.id); return { value: { isPlaced: true } } })
  on('ui.close', ($, e) => { closed.push(e.id); return { value: undefined } })
  on('ui.panes', () => ({ value: [{ id: 'peer-ds-aaa' }] }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#bbb' })
  // 点 bbb 时把 aaa 的面板关掉
  expect(closed).toEqual(['peer-ds-aaa'])
  expect(opened).toEqual(['peer-ds-bbb'])
})

test('点子代理那一行，进到它自己的页面', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '让子代理并行干活' },
    { kind: 'tool', name: 'Agent', id: 'a1', input: { description: '查文档' } },
    { kind: 'tool', name: 'Agent', id: 'a2', input: { description: '改代码' } },
    { kind: 'tool', name: 'Bash', id: 'b1', parent: 'a1', input: { command: 'grep -r foo' } },
    { kind: 'tool_result', id: 'b1', parent: 'a1', text: '结果' },
    { kind: 'tool_result', id: 'a2', text: '[Subagent hand-back]\n完成' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 让子代理并行干活\n' }
    return { value: '' }
  })
  const opened: string[] = []
  on('ui.open', ($, e) => { opened.push(e.id); return { value: { isPlaced: true } } })
  on('ui.panes', () => ({ value: [] }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  await pane.press({ key: 'agent-peer-ds-aaa-0' })
  expect(opened).toContain('agent-peer-ds-aaa-0')
  // 用鼠标点那一行（自绘区域）也一样能进去
  opened.length = 0
  await pane.pointer({ type: 'down', x: 8, y: 0, button: 'left', in: 'tasks-peer-ds-aaa' } as any)
  await pane.pointer({ type: 'up', x: 8, y: 0, button: 'left', in: 'tasks-peer-ds-aaa' } as any)
  expect(opened).toContain('agent-peer-ds-aaa-0')
  await pane.unmount()

  // 子代理页面只画它自己的东西
  const own = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'agent-peer-ds-aaa-0', viewport: { columns: 100, rows: 30 }, props: { title: 'Agent', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  expect(await own.find({ type: 'Text', text: /查文档/ })).toBeDefined()
  expect(await own.find({ type: 'Text', text: /Bash\(grep -r foo\)/ })).toBeDefined()
  expect(await own.find({ type: 'Text', text: /改代码/ })).toBeUndefined()
  // 顶部一行可以点回sideagent详情
  opened.length = 0
  await own.press({ key: 'back-agent-peer-ds-aaa-0' })
  expect(opened).toEqual(['peer-ds-aaa'])
})

test('两个子代理都在跑就显示两行，各一行高', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '让子代理并行干活' },
    { kind: 'tool', name: 'Agent', id: 'a1', input: { description: '查文档' } },
    { kind: 'tool', name: 'Agent', id: 'a2', input: { description: '改代码' } },
    { kind: 'tool', name: 'Bash', id: 'b1', parent: 'a1', input: { command: 'ls' } },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  // 两行都在
  expect(await hasTask(pane, /Agent\(查文档\)/)).toBe(true)
  expect(await hasTask(pane, /Agent\(改代码\)/)).toBe(true)
  expect(await pane.find({ key: 'win-peer-ds-aaa' })).toBeUndefined()
  expect(await pane.find({ key: 'agent-peer-ds-aaa-0' })).toBeDefined()
  expect(await pane.find({ key: 'agent-peer-ds-aaa-1' })).toBeDefined()
  // 两个子代理各自一个可点的按钮
  expect(await pane.find({ key: 'agent-peer-ds-aaa-0' })).toBeDefined()
  expect(await pane.find({ key: 'agent-peer-ds-aaa-1' })).toBeDefined()
})

test('sideagent做完会叫醒主代理一次，只叫一次', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [
    { name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false },
    { name: 'ds#aaa.progress', kind: 'file', size: 10, isLink: false, mtimeMs: Date.now() },
  ] }))
  let running = true
  on('fs.exists', () => ({ value: running }))
  let done = false
  const started = Math.floor(Date.now() / 1000)
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started, state: 'running', notify: true }) }
    if (e.path.endsWith('.events')) {
      return { value: [
        JSON.stringify({ kind: 'task', text: '干活' }),
        ...(done ? [JSON.stringify({ kind: 'result', text: '干完了', t: 10 })] : []),
      ].join('\n') + '\n' }
    }
    if (e.path.endsWith('.progress')) return { value: done ? 'RESULT 干完了\n' : 'TASK 干活\n' }
    return { value: '' }
  })
  const submits: string[] = []
  on('prompt.submit', ($, e) => { submits.push(String(e.text || '')); return { text: e.text } })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  expect(submits.length).toBe(0)

  done = true
  running = false
  await clock.advance(1500)
  expect(submits.length).toBe(1)
  expect(submits[0]).toContain('sideagent ds#aaa 完成')
  expect(submits[0]).toContain('干完了')

  // 再刷几轮也不重复叫
  await clock.advance(4000)
  expect(submits.length).toBe(1)
})

test('详情底下的输入框能把话发给sideagent', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: JSON.stringify({ kind: 'task', text: '干活' }) + '\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  const ran: string[][] = []
  on('process.run', ($, e) => { ran.push([...e.argv]); return { value: { exitCode: 0, stdout: '', stderr: '' } } })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  expect(await pane.find({ type: 'Input', key: 'reply-peer-ds-aaa' })).toBeDefined()
  await pane.input({ key: 'reply-peer-ds-aaa', text: '再数一遍' })
  expect(ran.length).toBe(1)
  // 回车是插话，和主界面忙时打字一样
  expect(ran[0]).toEqual(['cca', 'sideagent', 'tell', '--interject', 'ds#aaa', '再数一遍'])
  // 发出去的话挂在输入框上方，等它在下一个工具边界接住
  expect(await pane.find({ type: 'Text', text: /^> 再数一遍  插话中$/ })).toBeDefined()
})

test('常驻sideagent干完活显示空闲，不一直挂在运行中', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, busy: false }) }
    if (e.path.endsWith('.events')) return { value: JSON.stringify({ kind: 'task', text: '统计一下文件数' }) + '\n' }
    if (e.path.endsWith('.progress')) return { value: 'TASK 干活\nRESULT 好了 · 1.0 秒\n' }
    return { value: '' }
  })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)

  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  // 闲着时显示最后交办的那句话
  expect(await findAny(ui, { type: 'Text', text: /统计一下文件数/ })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: '●' })).toBeUndefined()
})

test('异步起的子代理：只回了“已启动”时还算在跑', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '起两个子代理' },
    { kind: 'tool', name: 'Agent', id: 'a1', input: { description: '查文档' } },
    { kind: 'tool_result', id: 'a1', text: 'Async agent launched successfully. agentId: abc123' },
    { kind: 'tool', name: 'Agent', id: 'a2', input: { description: '改代码' } },
    { kind: 'tool_result', id: 'a2', text: '[Subagent hand-back]\n完成' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, busy: true }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 起两个子代理\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  // 异步那个还在跑：底部有它一行
  expect(await pane.find({ key: 'agent-peer-ds-aaa-0' })).toBeDefined()
  // 已经交回结果的正常结束，不在底部
  expect(await pane.find({ key: 'agent-peer-ds-aaa-1' })).toBeUndefined()
})

test('宽表格按面板宽度截断，不折行', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const table = [
    '| 代理 | 任务 | 用时 | 结果 |',
    '|---|---|---|---|',
    '| 资源 | 查 CPU/内存/磁盘 | 2.0 秒 | 16 核，内存 23Gi（可用 13Gi），根盘 1007G 用了 324G |',
  ].join('\n')
  const events = [
    { kind: 'task', text: '看表格' },
    { kind: 'say', text: table },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, busy: false }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 看表格\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 60, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  const md = await pane.find({ type: 'Markdown' })
  expect(md).toBeDefined()
  const text = String(md?.props?.text || '')
  // 长格子被截断，出现省略号；表格仍是三行
  expect(text).toContain('…')
  expect(text.split('\n').length).toBe(3)
})

test('对端退出后，进度文件里的 NOTIFY 也能叫醒主对话', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [
    { name: 'ds#aaa.progress', kind: 'file', size: 10, isLink: false, mtimeMs: Date.now() },
  ] }))
  on('fs.exists', () => ({ value: false }))
  const finishedAt = Math.floor(Date.now() / 1000)
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.progress')) {
      return { value: 'TASK 干活\nRESULT 干完了 · 1.0 秒\nNOTIFY 1\n' }
    }
    if (e.path.endsWith('.events')) {
      return { value: JSON.stringify({ kind: 'result', text: '干完了', at: finishedAt + 10 }) + '\n' }
    }
    if (e.path.endsWith('.owner')) {
      return { value: JSON.stringify({ session: SID, account: 'ds' }) }
    }
    return { value: '' }
  })
  const submits: string[] = []
  on('prompt.submit', ($, e) => { submits.push(String(e.text || '')); return { text: e.text } })

  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  expect(submits.length).toBe(1)
  expect(submits[0]).toContain('完成')
})

// 一个在忙的 sideagent：状态、事件、正在说的话都可以换
function busyPeer(on, { state = {}, events = [], live = '', version = '' } = {}) {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) {
      return { value: JSON.stringify({ owner_session: SID, started: 0, busy: true, turn_started: Date.now() / 1000, ...state }) }
    }
    if (e.path.endsWith('.events')) return { value: events.map((ev) => JSON.stringify(ev)).join('\n') + '\n' }
    if (e.path.endsWith('.live')) return { value: live }
    if (e.path.endsWith('.progress')) return { value: 'TASK 跑测试\n' }
    return { value: '' }
  })
  const opened: string[] = []
  on('ui.open', ($, e) => { opened.push(e.id); return { value: { isPlaced: true } } })
  const ran: string[][] = []
  on('process.run', ($, e) => {
    ran.push([...e.argv])
    return { value: { exitCode: 0, stdout: (typeof version === 'function' ? version() : version) + '\n', stderr: '' } }
  })
  return { clock, ran, opened }
}

// 详情页底部的后台任务行画在 Client（band-rows.js）里：按它收到的行拼出屏幕上的字
async function taskTexts(pane: any): Promise<string[]> {
  const client = await pane.find({ type: 'Client' })
  const rows = (client?.props?.props?.rows || []) as any[]
  return rows.map((row) => '◯ ' + row.label + (row.desc ? '  ' + row.desc : ''))
}
async function hasTask(pane: any, pattern: RegExp): Promise<boolean> {
  return (await taskTexts(pane)).some((text) => pattern.test(text))
}

const TEST_EVENTS = [
  { kind: 'task', text: '跑一遍测试' },
  { kind: 'tool', name: 'Bash', summary: 'make test', input: { command: 'make test' }, parent: '' },
]

test('横条忙的那一行：中间是活动描述，右边和主界面子代理一样只有一个数', async ($, on) => {
  const { clock } = busyPeer(on, { state: { tokens_in: 30000, tokens_out: 400, token_count: 12345, acted: true,
    code_version: 'abc' }, events: TEST_EVENTS, version: 'abc' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  // 照 Claude Code 的 Bash 活动描述：Running + 命令说明（没有说明就是命令）
  expect(await findAny(ui, { type: 'Text', text: /^Running make test$/ })).toBeDefined()
  // 右边：耗时 · ↓ N tokens，中间用 “ · ” 隔开
  expect(await findAny(ui, { type: 'Text', text: /^ds · \d+s · ↓ 12\.3k tokens$/ })).toBeDefined()
  // 横条不显示对账用的累计
  expect(await findAny(ui, { type: 'Text', text: /累计/ })).toBeUndefined()
  // 版本对得上，不标旧版
  expect(await findAny(ui, { type: 'Text', text: /旧版/ })).toBeUndefined()
})

test('横条：还没调工具时中间是任务描述；正在说的话和思考不进这一行', async ($, on) => {
  const { clock } = busyPeer(on, { state: { code_version: 'abc' }, version: 'abc',
    events: [{ kind: 'task', text: '跑一遍测试' }, { kind: 'think', text: '想想' }], live: '我先看一下失败的那条' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /^跑一遍测试$/ })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: /我先看一下/ })).toBeUndefined()
  expect(await findAny(ui, { type: 'Text', text: /Thinking/ })).toBeUndefined()
})

test('横条：思考不改中间那段，仍是最后一个工具的活动描述', async ($, on) => {
  const { clock } = busyPeer(on, { state: { code_version: 'abc' }, version: 'abc',
    events: [...TEST_EVENTS, { kind: 'think', text: '想想' }] })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /^Running make test$/ })).toBeDefined()
})

test('横条：右侧列按显示宽度对齐（中文算 2 格），每行右端在同一列', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const now = Date.now() / 1000
  const peers: Record<string, any> = {
    'ds#a': { state: { busy: true, turn_started: now - 5, token_count: 900, acted: true, code_version: 'v' },
      task: '短' },
    'ds#bbbbbb': { state: { busy: true, turn_started: now - 125, token_count: 1234567, acted: true, queued: 2,
      code_version: 'old', label: '修复测试' }, task: '一个很长很长的中文任务描述，里面有全角字符：１２３' },
    'x#c': { state: { busy: false, code_version: 'v' }, task: 'idle one' },
  }
  on('fs.list', () => ({ value: Object.keys(peers).map((name) => ({ name: name + '.json', kind: 'file', size: 1, isLink: false })) }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    const name = Object.keys(peers).find((one) => e.path.includes('/' + one + '.'))
    if (!name) return { value: '' }
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, ...peers[name].state }) }
    if (e.path.endsWith('.events')) return { value: JSON.stringify({ kind: 'task', text: peers[name].task }) + '\n' }
    return { value: '' }
  })
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'v\n', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal', props: { bodyColumns: 100 } })
  const band = await ui.find({ type: 'Client', key: 'band' })
  const first = band?.props.props as any
  expect(first.rows.length).toBe(3)
  // 行宽照子代理：所在列宽度（bodyColumns + 5）减 2
  expect(first.width).toBe(103)
  const shown: string[] = []
  for (const row of first.rows) {
    const status = [statusText(row), row.warnText].filter(Boolean).join(' · ')
    shown.push(status)
    // 每一行同一个右侧列宽，字比列窄的靠右，右端就在同一列
    expect(displayWidth(status)).toBeLessThanOrEqual(first.statusWidth)
  }
  expect(shown).toContain('ds · 5s · ↓ 900 tokens')
  // 名字列写的是“修复测试”不是号名，所以时间左边补上号 ds
  expect(shown).toContain('ds · 2m 5s · ↓ 1.2m tokens · 2 queued · 旧版')
  expect(shown).toContain('x · idle')
  // 列宽就是最宽的那一个（“旧版”两个中文按 4 格算）
  expect(first.statusWidth).toBe(displayWidth('ds · 2m 5s · ↓ 1.2m tokens · 2 queued · 旧版'))
  expect(displayWidth('旧版')).toBe(4)
  // 名字列：最长的显示名字（“修复测试”中文 4 个字算 8 格）
  expect(first.labelWidth).toBe(8)
})

test('横条名字列不额外垫空格：只有一个短名字就紧跟两格', async () => {
  // 子代理列表是「类型 + 两格 + 描述」；名字只有 ds 时不该多出四格空地
  expect(bandLayout([{ name: 'ds', label: 'ds' }]).labelWidth).toBe(2)
  expect(bandLayout([{ name: 'ds', label: 'ds' }, { name: 'x', label: '长名字' }]).labelWidth)
    .toBe(displayWidth('长名字'))
})

test('横条：鼠标移上去只亮起 ❯，不反白；点一下打开详情', async ($, on) => {
  const { clock, opened } = busyPeer(on, { state: { token_count: 100, acted: true, code_version: 'abc' },
    events: TEST_EVENTS, version: 'abc' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const key = 'band'
  // 一整行不是 Button：鼠标下不会整行反白
  const client = await ui.find({ type: 'Client', key })
  expect(client).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: '❯ ' })).toBeUndefined()
  await ui.pointer({ type: 'enter', x: 10, y: 0, in: key })
  expect(await findAny(ui, { type: 'Text', text: '❯ ' })).toBeDefined()
  await ui.pointer({ type: 'leave', x: 10, y: 0, in: key })
  expect(await findAny(ui, { type: 'Text', text: '❯ ' })).toBeUndefined()
  await ui.pointer({ type: 'down', x: 10, y: 0, button: 'left', in: key })
  await ui.pointer({ type: 'up', x: 10, y: 0, button: 'left', in: key })
  expect(opened).toEqual(['peer-ds-aaa'])
})

test('数字和耗时的写法照 Claude Code', () => {
  expect(compactCount(900)).toBe('900')
  expect(compactCount(12000)).toBe('12.0k')
  expect(compactCount(12345)).toBe('12.3k')
  expect(compactCount(1234567)).toBe('1.2m')
  expect(compactCount(999950)).toBe('1.0m')
  expect(elapsedText(0)).toBe('0s')
  expect(elapsedText(8900)).toBe('8s')
  expect(elapsedText(125000)).toBe('2m 5s')
  expect(elapsedText(60000)).toBe('1m 0s')
  expect(elapsedText(3723000)).toBe('1h 2m 3s')
})

test('活动描述照 Claude Code 各工具的写法', () => {
  const at = (name: string, input: any) => activityOf([{ kind: 'tool', name, input }], '/w')
  expect(at('Bash', { command: 'make test', description: 'Run tests' })).toBe('Running Run tests')
  expect(at('Bash', { command: 'ls -la' })).toBe('Running ls -la')
  expect(at('Read', { file_path: '/w/src/a.py' })).toBe('Reading src/a.py')
  expect(at('Edit', { file_path: '/other/b.py' })).toBe('Editing /other/b.py')
  expect(at('Grep', { pattern: 'TODO' })).toBe('Searching for TODO')
  expect(at('Glob', { pattern: '**/*.ts' })).toBe('Finding **/*.ts')
  expect(at('Agent', { description: '查一下' })).toBe('查一下')
  expect(at('mcp__x__y', {})).toBe('mcp__x__y')
  // 子代理里面的工具不算；这一轮结束了就没有活动
  expect(activityOf([{ kind: 'tool', name: 'Read', input: { file_path: '/w/a' }, parent: 'p1' }], '/w')).toBe('')
  expect(activityOf([{ kind: 'tool', name: 'Read', input: { file_path: '/w/a' } }, { kind: 'result' }], '/w')).toBe('')
})

test('横条：缺 code_version 或版本对不上就标“旧版”', async ($, on) => {
  const { clock, ran } = busyPeer(on, { state: { code_version: 'old' }, events: TEST_EVENTS, version: 'new' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(ran.some((argv) => argv.join(' ') === 'cca sideagent version')).toBe(true)
  expect(await findAny(ui, { type: 'Text', text: /旧版/ })).toBeDefined()
})

test('横条：面板加载后代码又改了，新起的 sideagent 不标“旧版”（看到没见过的版本就现查一次）', async ($, on) => {
  const answers = ['v1', 'v2']
  const { clock, ran } = busyPeer(on, { state: { code_version: 'v2' }, events: TEST_EVENTS,
    version: () => answers.shift() || 'v2' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  await clock.advance(3000)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(ran.filter((argv) => argv.join(' ') === 'cca sideagent version').length).toBe(2)
  expect(await findAny(ui, { type: 'Text', text: /旧版/ })).toBeUndefined()
  // 真的旧版：查过一次还对不上，就不再为它反复查
  await clock.advance(120000)
  expect(ran.filter((argv) => argv.join(' ') === 'cca sideagent version').length).toBe(2)
})

test('横条：老进程没有 code_version，也标“旧版”', async ($, on) => {
  const { clock } = busyPeer(on, { events: TEST_EVENTS })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /旧版/ })).toBeDefined()
})

test('详情页顶上写清在干什么、词元，并说明口径', async ($, on) => {
  const { clock } = busyPeer(on, { state: { tokens_in: 30000, tokens_out: 400, token_count: 12345, acted: true,
    code_version: 'abc' }, events: TEST_EVENTS, version: 'abc' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  // 左边：在干什么；右边灰字：号 · 词元（没记到模型就不写）
  expect(await pane.find({ type: 'Text', text: /运行中 · Bash\(make test\) · / })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /^ds · ↓ 12\.3k tokens$/ })).toBeDefined()
  // 和子代理一样不显示累计的 ↑↓
  expect(await pane.find({ type: 'Text', text: /累计 ↑/ })).toBeUndefined()
  // 词元口径的说明行删掉了：和主界面一样只显示一个数
  expect(await pane.find({ type: 'Text', text: /最近一次调用的输入/ })).toBeUndefined()
  // 底下的状态行也带上参数
  expect(await pane.find({ type: 'Text', text: /Running Bash\(make test\)…/ })).toBeDefined()
})

test('刚结束的：结果行的 ↑↓ 原样显示', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#done.progress', kind: 'file', size: 10, isLink: false, mtimeMs: Date.now() }] }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', ($, e) => ({
    value: e.path.endsWith('.owner')
      ? JSON.stringify({ session: SID, account: 'ds' })
      : 'TASK 跑一下\nRESULT 好了 · ↑25.0k ↓0.3k tok · 8.9s\n',
  }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /↑25\.0k ↓0\.3k/ })).toBeDefined()
})

test('横条：还没有动作时箭头是 ↑', async ($, on) => {
  const { clock } = busyPeer(on, { state: { token_count: 8000, acted: false, code_version: 'abc' },
    events: [{ kind: 'task', text: '跑一遍测试' }], version: 'abc' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /↑ 8\.0k tokens/ })).toBeDefined()
})

test('刚结束的：结果行带 total 时显示成 ↓ 那一个数', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#done.progress', kind: 'file', size: 10, isLink: false, mtimeMs: Date.now() }] }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', ($, e) => ({
    value: e.path.endsWith('.owner')
      ? JSON.stringify({ session: SID, account: 'ds' })
      : 'TASK 跑一下\nRESULT 好了 · ↑25.0k ↓0.3k tok · 12.7k total · 8.9s\n',
  }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /↓ 12\.7k tokens/ })).toBeDefined()
})

test('横条：有进度短语就显示短语（照子代理），没有才退回活动描述', async ($, on) => {
  const { clock } = busyPeer(on, { state: { summary: 'Fixing null check in validate.ts', code_version: 'abc' },
    events: TEST_EVENTS, version: 'abc' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /^Fixing null check in validate\.ts$/ })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: /Running make test/ })).toBeUndefined()
})

test('详情里结果那一行只留耗时和一个词元数，不显示累计', async ($, on) => {
  const { clock } = busyPeer(on, { state: { busy: false, code_version: 'abc' }, version: 'abc',
    events: [{ kind: 'task', text: '跑' },
      { kind: 'result', text: '好了', tokens_in: 6009100, tokens_out: 38000, token_count: 145100, seconds: 387.2 }] })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: /^6m 27s · ↓ 145\.1k tokens$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /6009/ })).toBeUndefined()
})

test('横条名字照子代理：显示号名或派活时给的名字，不显示内部实例名', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const peers: Record<string, any> = {
    'ds#ff2832': { busy: true, turn_started: Date.now() / 1000, code_version: 'v' },
    'ds#a1b2c3': { busy: true, turn_started: Date.now() / 1000, code_version: 'v', label: '查日志' },
  }
  on('fs.list', () => ({ value: Object.keys(peers).map((name) => ({ name: name + '.json', kind: 'file', size: 1, isLink: false })) }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    const name = Object.keys(peers).find((one) => e.path.includes('/' + one + '.'))
    if (name && e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, ...peers[name] }) }
    return { value: '' }
  })
  const opened: any[] = []
  on('ui.open', ($, e) => { opened.push(e); return { value: { isPlaced: true } } })
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'v\n', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const band = await ui.find({ type: 'Client', key: 'band' })
  const labels = (band?.props.props as any).rows.map((row: any) => row.label).sort()
  expect(labels).toEqual(['ds', '查日志'])
  expect(await findAny(ui, { type: 'Text', text: /ff2832|a1b2c3/ })).toBeUndefined()
  // 内部实例名还在，用来打开详情；详情的标题也是显示名字
  await ui.press({ key: 'peer-ds#ff2832' })
  expect(opened[0].id).toBe('peer-ds-ff2832')
  expect(opened[0].title).toBe('ds')
})

test('做完、闲着的那一行中间仍显示最后一句进度短语', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#done.progress', kind: 'file', size: 10, isLink: false, mtimeMs: Date.now() }] }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', ($, e) => ({
    value: e.path.endsWith('.owner')
      ? JSON.stringify({ session: SID, account: 'ds' })
      : 'TASK 跑一下\nSUMMARY Running auth tests\nSUMMARY Fixing validate.ts\nRESULT 好了 · 12.7k total · 8.9s\n',
  }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /^Fixing validate\.ts$/ })).toBeDefined()
})

test('还没有进度短语、没调工具时，中间显示派活时给的 description', async ($, on) => {
  const { clock } = busyPeer(on, { state: { description: '统计 py 文件', code_version: 'abc' }, version: 'abc',
    events: [{ kind: 'task', text: '请统计一下 src 下面有多少个 py 文件，然后告诉我' }] })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: /^统计 py 文件$/ })).toBeDefined()
})

test('超过 5 个时和子代理列表一样只画 5 行，下面写 ↓ N more', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const names = ['ds#a1', 'ds#a2', 'ds#a3', 'ds#a4', 'ds#a5', 'ds#a6', 'ds#a7']
  on('fs.list', () => ({ value: names.map((name) => ({ name: name + '.json', kind: 'file', size: 1, isLink: false })) }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => (e.path.endsWith('.json')
    ? { value: JSON.stringify({ owner_session: SID, started: 0, busy: true, code_version: 'v' }) }
    : { value: '' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'v\n', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const band = await ui.find({ type: 'Client', key: 'band' })
  expect((band?.props.props as any).rows.length).toBe(5)
  expect(await findAny(ui, { type: 'Text', text: '  ↓ 2 more' })).toBeDefined()
})

test('宽度照 Claude Code（Bun.stringWidth，宽度不定的算 1 格）', () => {
  expect(displayWidth('↓ 12.3k tokens')).toBe(14)
  expect(displayWidth('· … ◯ ❯ ✻ ↑')).toBe(11)
  expect(displayWidth('旧版')).toBe(4)
  expect(displayWidth('ｅ２ｅ')).toBe(6)
  expect(displayWidth('é')).toBe(1)
  expect(displayWidth('✅')).toBe(2)
  expect(displayWidth('✔\uFE0F')).toBe(2)
  expect(displayWidth('👨‍💻')).toBe(2)
  expect(displayWidth('a\u200Bb')).toBe(2)
})

test('横条刷新几轮（9.9k→10.0k、59s→1m 0s、旧版忽有忽无），每行右端都在同一列', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const start = Date.now() / 1000
  const rounds = [
    { a: { token_count: 9900, at: 59 }, b: { token_count: 500, version: 'v' } },
    { a: { token_count: 10000, at: 60 }, b: { token_count: 500, version: 'old' } },
    { a: { token_count: 10049, at: 61 }, b: { token_count: 1000, version: 'v' } },
  ]
  let round = 0
  on('fs.list', () => ({ value: ['ds#a1', 'ds#b2'].map((name) => ({ name: name + '.json', kind: 'file', size: 1, isLink: false })) }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    const now = rounds[round]
    if (e.path.endsWith('ds#a1.json')) {
      return { value: JSON.stringify({ owner_session: SID, busy: true, acted: true, code_version: 'v',
        turn_started: start - now.a.at, token_count: now.a.token_count }) }
    }
    if (e.path.endsWith('ds#b2.json')) {
      return { value: JSON.stringify({ owner_session: SID, busy: true, acted: true, code_version: now.b.version,
        turn_started: start, token_count: now.b.token_count, label: '修复测试' }) }
    }
    return { value: '' }
  })
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'v\n', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  const seen: string[] = []
  for (round = 0; round < rounds.length; round += 1) {
    await clock.advance(1500)
    const ui = await $.ui.mount({ ...SITE, surface: 'terminal', props: { bodyColumns: 100 } })
    const band = await ui.find({ type: 'Client', key: 'band' })
    const props = band?.props.props as any
    // 画出来的每一行：行宽固定，右侧那个格子宽度一样、靠右、排在最后，所以右端在同一列
    const tree = await ui.drawn({ in: 'band' })
    const lines = walk(tree).filter((node: any) => node.props?.height === 1)
    expect(lines.length).toBe(2)
    for (const lineNode of lines) {
      expect(lineNode.props.width).toBe(103)
      const cells = ([] as any[]).concat(lineNode.props?.children ?? lineNode.children ?? []).filter((child: any) => child && typeof child === 'object')
      const last = cells[cells.length - 1]
      expect(last.props.width).toBe(props.statusWidth)
      expect(last.props.justifyContent).toBe('flex-end')
      expect(displayWidth(textOf(last))).toBeLessThanOrEqual(props.statusWidth)
      seen.push(textOf(last))
    }
    await ui.unmount()
  }
  expect(seen).toContain('ds · 59s · ↓ 9.9k tokens')
  expect(seen).toContain('ds · 1m 0s · ↓ 10.0k tokens')
  expect(seen.some((text) => text.includes('旧版'))).toBe(true)
})

// 两个在跑的：ds#m1 是主对话派的，ds#s1 是子代理 a1 派的
function twoOwners(on: any) {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const peers: Record<string, any> = {
    'ds#m1': { busy: true, code_version: 'v', description: '主对话派的' },
    'ds#s1': { busy: true, code_version: 'v', description: '子代理派的', caller_agent: 'a1', instance_socket: '/tmp/s1.sock' },
    'ds#g1': { busy: true, code_version: 'v', description: '子代理已经不在', caller_agent: 'gone' },
  }
  on('fs.list', () => ({ value: Object.keys(peers).flatMap((name) => [
    { name: name + '.json', kind: 'file', size: 1, isLink: false },
    { name: name + '.owner', kind: 'file', size: 1, isLink: false }]) }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($: any, e: any) => {
    const name = Object.keys(peers).find((one) => e.path.includes('/' + one + '.'))
    if (name && e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0, ...peers[name] }) }
    if (name && e.path.endsWith('.owner')) {
      return { value: JSON.stringify({ session: SID, caller_agent: peers[name].caller_agent || '', peer_socket: peers[name].instance_socket || '' }) }
    }
    return { value: '' }
  })
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'v\n', stderr: '' } }))
  const closed: string[] = []
  on('ui.close', ($: any, e: any) => { closed.push(e.id); return { value: undefined } })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  const sent: any[] = []
  on('session.send', ($: any, e: any) => {
    sent.push(e)
    return e.to === 'gone' ? { isDelivered: false, reason: '没有这个代理' } : { isDelivered: true }
  })
  // 引擎自己那一层：消息照原样进主对话
  on('session.receive', ($: any, e: any) => ({ text: e.text }))
  return { clock, closed, sent }
}

test('横条只在派活的那个界面显示：主对话只见主对话派的，子代理 a1 的界面只见 a1 派的', async ($, on) => {
  const { clock } = twoOwners(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const main = await $.ui.mount({ ...SITE, surface: 'terminal', props: { view: {} } })
  const mainRows = ((await main.find({ type: 'Client', key: 'band' }))?.props.props as any).rows.map((row: any) => row.name)
  expect(mainRows).toEqual(['ds#m1'])
  await main.unmount()
  const sub = await $.ui.mount({ ...SITE, surface: 'terminal', props: { view: { agentId: 'a1' } } })
  const subRows = ((await sub.find({ type: 'Client', key: 'band' }))?.props.props as any).rows.map((row: any) => row.name)
  expect(subRows).toEqual(['ds#s1'])
  await sub.unmount()
  // 别的子代理的界面：两个都不显示，交给引擎自己画
  const other = await $.ui.mount({ ...SITE, surface: 'terminal', props: { view: { agentId: 'b2' } } })
  expect(await other.find({ type: 'Client', key: 'band' })).toBeUndefined()
})

test('切到别的界面时，子代理派的 sideagent 的详情自动关掉', async ($, on) => {
  const { clock, closed } = twoOwners(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const sub = await $.ui.mount({ ...SITE, surface: 'terminal', props: { view: { agentId: 'a1' } } })
  await sub.press({ key: 'peer-ds#s1' })
  await sub.unmount()
  const pane = { plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-s1', viewport: { columns: 100, rows: 30 }, surface: 'terminal' as const }
  const props = { title: 'ds', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 } }
  // 在 a1 的界面里开着：不关
  const inView = await $.ui.mount({ ...pane, props: { ...props, view: { agentId: 'a1' } } })
  expect(closed).toEqual([])
  await inView.unmount()
  // 切回主对话：关掉
  await $.ui.mount({ ...pane, props: { ...props, view: {} } })
  expect(closed).toContain('peer-ds-s1')
})

test('子代理派的 sideagent 发回来的消息转给那个子代理，不进主对话', async ($, on) => {
  const { clock, sent } = twoOwners(on)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const wrap = (attrs: string, body: string) => `<cross-session-message from="uds:/tmp/x.sock" from-name="ds"${attrs}>\n${body}\n</cross-session-message>`
  // 完成回报（带 cca-instance）：转给 a1，主对话收不到
  const done = await $.session.receive({ origin: { kind: 'peer' }, text: wrap(' cca-instance="ds#s1"', '<task-notification>好了</task-notification>') })
  expect(done?.consumed).toBeDefined()
  expect(sent[0].to).toBe('a1')
  expect(sent[0].text).toContain('<task-notification>好了</task-notification>')
  // 它自己用 SendMessage 发的（没有 cca-instance）：按发件 socket 认出是 ds#s1，也转给 a1
  const mid = await $.session.receive({ origin: { kind: 'peer' },
    text: '<cross-session-message from="uds:/tmp/s1.sock" from-name="ds">\n卡住了，要不要继续？\n</cross-session-message>' })
  expect(mid?.consumed).toBeDefined()
  expect(sent[1].to).toBe('a1')
  // 主对话派的：照常进主对话
  const mine = await $.session.receive({ origin: { kind: 'peer' }, text: wrap(' cca-instance="ds#m1"', '主对话的回报') })
  expect(mine?.consumed).toBeUndefined()
  expect(String(mine?.text)).toContain('主对话的回报')
  expect(sent.length).toBe(2)
  // 派活的子代理已经不在：退回主对话，并说清原本是给谁的
  const lost = await $.session.receive({ origin: { kind: 'peer' }, text: wrap(' cca-instance="ds#g1"', '迟到的回报') })
  expect(lost?.consumed).toBeUndefined()
  expect(String(lost?.text)).toContain('原本是给子代理 gone 的')
  expect(String(lost?.text)).toContain('迟到的回报')
})

test('子代理里调 sideagent_send：插件把调用方子代理编号塞进参数；主对话调用不动', async ($, on) => {
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('fs.list', () => ({ value: [] }))
  const seen: any[] = []
  on('tool.call', ($: any, e: any) => { seen.push(e); return { result: 'ok', text: 'ok' } })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await $.tool.call({ tool: 'mcp__sideagents__sideagent_send', account: 'ds', message: '干活', agentId: 'a1' } as any)
  await $.tool.call({ tool: 'mcp__sideagents__sideagent_send', account: 'ds', message: '干活' } as any)
  expect(seen[0].caller_agent).toBe('a1')
  expect(seen[1].caller_agent).toBeUndefined()
})


// 照 openai#c46034 的真实事件：code-review 是分叉执行的 Skill，里面的动作都带 parent = Skill 调用编号
test('分叉执行的 Skill 当子代理画：底部一行 Skill(名字)，点进去看它里面的动作', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const events = [
    { kind: 'task', text: '审查代码' },
    { kind: 'tool', name: 'Skill', id: 's1', input: { skill: 'code-review', args: '--high' }, summary: '/code-review --high' },
    { kind: 'tool', name: 'Bash', id: 'b1', parent: 's1', input: { command: 'git diff HEAD' }, summary: 'git diff HEAD' },
    { kind: 'tool_result', id: 'b1', parent: 's1', text: 'diff' },
    // 没有分叉的 Skill：里面没有带 parent 的动作，照普通工具画
    { kind: 'tool', name: 'Skill', id: 's2', input: { skill: 'simplify' } },
    { kind: 'tool_result', id: 's2', text: 'Launching skill: simplify' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK 审查代码\n' }
    return { value: '' }
  })
  const opened: any[] = []
  on('ui.open', ($, e) => { opened.push(e); return { value: { isPlaced: true } } })
  on('ui.panes', () => ({ value: [] }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()

  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 100, rows: 30 }, props: { title: 'ds#aaa', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  // 底部一行：Skill(名字) 加它正在做的事，和子代理一样
  const row = await pane.find({ key: 'agent-peer-ds-aaa-0' })
  expect(row).toBeDefined()
  expect(await hasTask(pane, /^◯ Skill\(code-review\)\s+Bash\(git diff HEAD\)/)).toBe(true)
  await pane.press({ key: 'agent-peer-ds-aaa-0' })
  expect(opened.at(-1)).toMatchObject({ id: 'agent-peer-ds-aaa-0', title: 'Skill · code-review' })
  await pane.unmount()

  const own = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'agent-peer-ds-aaa-0', viewport: { columns: 100, rows: 30 }, props: { title: 'Skill', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  expect(await own.find({ type: 'Text', text: /\/code-review --high/ })).toBeDefined()
  expect(await own.find({ type: 'Text', text: /git diff HEAD/ })).toBeDefined()
})


// ---- 后台任务照原生分类（bgtask 事件，cca 从 stream-json 的 task_* 记下来）
const BG_EVENTS = [
  { kind: 'task', text: '各种后台任务' },
  { kind: 'tool', name: 'Agent', id: 'tA', input: { description: '查文档' } },
  { kind: 'bgtask', subtype: 'task_started', task_id: 'a1', task_type: 'local_agent', tool_use_id: 'tA', description: '查文档' },
  { kind: 'tool', name: 'Read', id: 'r1', parent: 'tA', input: { file_path: '/x.md' }, summary: '/x.md' },
  { kind: 'bgtask', subtype: 'task_progress', task_id: 'a1', usage: { tool_uses: 3, total_tokens: 12000 }, last_tool_name: 'Read' },
  { kind: 'tool', name: 'Skill', id: 'tS', input: { skill: 'code-review', args: '--high' } },
  { kind: 'bgtask', subtype: 'task_started', task_id: 's1', task_type: 'local_agent', tool_use_id: 'tS', description: 'code-review' },
  { kind: 'tool', name: 'Bash', id: 'tB', input: { command: 'sleep 30; echo hi', run_in_background: true } },
  { kind: 'bgtask', subtype: 'task_started', task_id: 'b1', task_type: 'local_bash', tool_use_id: 'tB', description: 'sleep' },
  { kind: 'tool_result', id: 'tB', text: 'Command running in background with ID: b1' },
  { kind: 'tool', name: 'Monitor', id: 'tM', input: { description: 'deploy 日志' } },
  { kind: 'bgtask', subtype: 'task_started', task_id: 'm1', task_type: 'local_bash', tool_use_id: 'tM', description: 'deploy 日志' },
  { kind: 'tool', name: 'Workflow', id: 'tW', input: {} },
  { kind: 'bgtask', subtype: 'task_started', task_id: 'w1', task_type: 'local_workflow', tool_use_id: 'tW', workflow_name: 'review-changes' },
  { kind: 'bgtask', subtype: 'task_progress', task_id: 'w1', workflow_progress: [{ title: 'Review', status: 'done' }, { title: 'Verify', status: 'running' }] },
  // 做完的不显示
  { kind: 'tool', name: 'Agent', id: 'tD', input: { description: '早做完了' } },
  { kind: 'bgtask', subtype: 'task_started', task_id: 'd1', task_type: 'local_agent', tool_use_id: 'tD', description: '早做完了' },
  { kind: 'bgtask', subtype: 'task_updated', task_id: 'd1', status: 'completed' },
  { kind: 'bgtask', subtype: 'task_notification', task_id: 'd1', status: 'completed', tool_use_id: 'tD' },
]

test('后台任务按原生四类分：子代理（含分叉 Skill）、后台命令、监视、工作流；做完的不显示', () => {
  const tasks = foldTasks(BG_EVENTS)
  const tools = new Map(BG_EVENTS.filter((ev: any) => ev.kind === 'tool').map((ev: any) => [ev.id, ev]))
  const kinds = Object.fromEntries(tasks.map((task: any) => [task.id, taskKind(task, (id: string) => tools.get(id))]))
  expect(kinds).toEqual({ a1: 'agent', s1: 'agent', b1: 'shell', m1: 'monitor', w1: 'workflow', d1: 'agent' })
  expect(tasks.find((task: any) => task.id === 'd1').done).toBe(true)
  const rows = footerRows(BG_EVENTS, BG_EVENTS, { agents: [], agentDone: () => true })
  const brief = rows.map((row: any) => row.kind + '(' + row.desc + ') ' + row.doing)
  expect(brief).toEqual([
    'Agent(查文档) Read(/x.md) · 3 tools · 12.0k tok',
    'Skill(code-review) 启动中…',
    'Bash(sleep 30; echo hi) 后台运行中',
    'Monitor(deploy 日志) 监视中',
    'Workflow(review-changes) Verify running',
  ])
  // 只有子代理那两行能点进去
  expect(rows.map((row: any) => Boolean(row.agent))).toEqual([true, true, false, false, false])
})

test('工作流那一行：进度条、当前阶段、出错几个；新记录用算好的数', () => {
  const ev = (extra: any) => [
    { kind: 'tool', name: 'Workflow', id: 'tW', input: {} },
    { kind: 'bgtask', subtype: 'task_started', task_id: 'w1', task_type: 'local_workflow', tool_use_id: 'tW', workflow_name: '出题' },
    { kind: 'bgtask', subtype: 'task_progress', task_id: 'w1', usage: { total_tokens: 377774 }, ...extra },
  ]
  const brief = (events: any[]) => footerRows(events, events, { agents: [], agentDone: () => true })
    .map((row: any) => row.kind + '(' + row.desc + ') ' + row.doing)
  expect(brief(ev({ workflow_counts: { total: 30, done: 16, error: 2, running: 12, phase: 'Verify' } })))
    .toEqual(['Workflow(出题) ▕██████░░░░▏ 18/30 · Verify · 2 出错 · 377.8k tok'])
  // 旧记录只有完整清单：现算
  const items = [
    { type: 'workflow_phase', index: 1, title: 'Generate' },
    { type: 'workflow_agent', index: 1, phaseIndex: 1, phaseTitle: 'Generate', state: 'done' },
    { type: 'workflow_agent', index: 2, phaseIndex: 1, phaseTitle: 'Generate', state: 'progress' },
  ]
  expect(brief(ev({ workflow_progress: items }))).toEqual(['Workflow(出题) ▕█████░░░░░▏ 1/2 · Generate · 377.8k tok'])
  expect(workflowCounts({ workflow_progress: items })).toEqual({ total: 2, done: 1, error: 0, running: 1, phase: 'Generate' })
  expect(progressBar(0, 0)).toBe('')
  expect(progressBar(3, 3)).toBe('▕██████████▏ 3/3')
})

test('没有 bgtask 的旧记录：照旧按工具名认子代理', () => {
  const old = [
    { kind: 'tool', name: 'Agent', id: 'a1', input: { description: '查文档' } },
    { kind: 'tool', name: 'Bash', id: 'b1', input: { command: 'ls' } },
  ]
  const rows = footerRows(old, old, { agents: [old[0]], agentDone: () => false })
  expect(rows.map((row: any) => row.kind + '(' + row.desc + ')')).toEqual(['Agent(查文档)'])
})

test('详情页：底部一行一个子代理（含 Skill）和工作流（带进度），后台命令、监视写一行数量；做完的不画，子代理那一行能点进去', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  const text = BG_EVENTS.map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($: any, e: any) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: text }
    if (e.path.endsWith('.progress')) return { value: 'TASK 各种后台任务\n' }
    return { value: '' }
  })
  const opened: any[] = []
  on('ui.open', ($: any, e: any) => { opened.push(e); return { value: { isPlaced: true } } })
  on('ui.panes', () => ({ value: [] }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 120, rows: 40 }, props: { title: 'ds#aaa', bodyColumns: 100, placement: 'inline', scroll: { offset: 0, bodyRows: 60 }, view: {} }, surface: 'terminal' })
  for (const label of [/^◯ Agent\(查文档\)/, /^◯ Skill\(code-review\)/, /^◯ Workflow\(review-changes\)/]) expect(await hasTask(pane, label)).toBe(true)
  expect(await hasTask(pane, /Bash\(sleep 30/)).toBe(false)
  expect(await pane.find({ type: 'Text', text: /还有 1 个后台命令 · 1 个监视$/ })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: /早做完了/ })).toBeUndefined()
  // 子代理那一行点进去看它自己的动作
  await pane.press({ key: 'agent-peer-ds-aaa-0' })
  expect(opened.at(-1)).toMatchObject({ id: 'agent-peer-ds-aaa-0', title: 'Agent · 查文档' })
})

test('后台命令那条工具行：工具先回了“转到后台”，任务没结束前圆点一直是在跑的颜色', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#aaa.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  let events = BG_EVENTS.slice(7, 10).concat([{ kind: 'say', text: '等它跑完' }])
  on('fs.read', ($: any, e: any) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: SID, started: 0 }) }
    if (e.path.endsWith('.events')) return { value: events.map((ev) => JSON.stringify(ev)).join('\n') + '\n' }
    if (e.path.endsWith('.progress')) return { value: 'TASK 后台命令\n' }
    return { value: '' }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  await band.press({ key: 'peer-ds#aaa' })
  await band.unmount()
  const markOf = async () => {
    const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds-aaa', viewport: { columns: 120, rows: 40 }, props: { title: 'ds#aaa', bodyColumns: 100, placement: 'inline', scroll: { offset: 0, bodyRows: 60 }, view: {} }, surface: 'terminal' })
    const texts: any[] = await pane.findAll({ type: 'Text' })
    const at = texts.findIndex((one, i) => i > 0 && /^Bash\(sleep 30/.test(String(one.text)) && texts[i - 1].text === '● ')
    await pane.unmount()
    return at > 0 ? texts[at - 1].props?.color : 'missing'
  }
  expect(await markOf()).toBe('success')
  events = events.concat([{ kind: 'bgtask', subtype: 'task_notification', task_id: 'b1', status: 'completed', tool_use_id: 'tB' }])
  await clock.advance(5000)
  expect(await markOf()).toBeUndefined()
})

// 详情页内容比窗口长时，引擎把 ↑↓ 当滚动：焦点在底部几行上时改成挪焦点
test('arrowTarget：输入框 ↓ 到第一个子代理，再 ↓ 到第二个，↑ 回去；到底停住；不在这几行上不管', () => {
  const keys = ['reply-p', 'agent-p-0', 'agent-p-1']
  expect(arrowTarget(keys, 'reply-p', 1)).toBe('agent-p-0')
  expect(arrowTarget(keys, 'agent-p-0', 1)).toBe('agent-p-1')
  expect(arrowTarget(keys, 'agent-p-1', -1)).toBe('agent-p-0')
  expect(arrowTarget(keys, 'agent-p-0', -1)).toBe('reply-p')
  // 最后一行再 ↓：停在原地（不滚正文）
  expect(arrowTarget(keys, 'agent-p-1', 1)).toBe('agent-p-1')
  // 输入框再 ↑：交给引擎滚正文
  expect(arrowTarget(keys, 'reply-p', -1)).toBeNull()
  // 焦点不在这几行、或是一次滚好几行（翻页）：不管
  expect(arrowTarget(keys, 'back-p', 1)).toBeNull()
  expect(arrowTarget(keys, 'agent-p-0', 20)).toBeNull()
})

test('recallStep：输入框里 ↑ 从最近一句往前翻，翻到最早停住；没发过就不动', () => {
  expect(recallStep([], undefined)).toBeNull()
  const history = ['第一句', '第二句', '第三句']
  expect(recallStep(history, undefined)).toEqual({ at: 2, text: '第三句' })
  expect(recallStep(history, 2)).toEqual({ at: 1, text: '第二句' })
  expect(recallStep(history, 0)).toEqual({ at: 0, text: '第一句' })
})

test('详情页标题右边：号 · 模型 · 词元；缺哪项就不写哪项', () => {
  expect(headerMeta('openai#0f57ec', { model: 'gpt-6-astra', used: '↑12.3k ↓0.4k' })).toBe('openai · gpt-6-astra · ↑12.3k ↓0.4k')
  expect(headerMeta('ds#84b089', { model: '', used: '' })).toBe('ds')
  expect(headerMeta('ds2', {})).toBe('ds2')
})

// 接管的原生后台会话（sideagent_takeover）：没有我们的套接字也显示；等确认标黄；每次刷新先 bg-sync
test('接管的原生后台会话：没有套接字也显示，等确认标黄并写卡在哪，刷新时先让 cca 同步', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: '003#bg-707148f7.json', kind: 'file', size: 10, isLink: false }] }))
  // 套接字不存在：普通 sideagent 会被当成没在跑，接管的会话不看这个
  on('fs.exists', ($, e) => ({ value: !String(e.path).endsWith('.sock') }))
  const events = [
    { kind: 'task', text: 'Run the bash command: touch /tmp/x' },
    { kind: 'tool', name: 'Bash', id: 't1', input: { command: 'touch /tmp/x' }, summary: 'touch /tmp/x' },
  ].map((ev) => JSON.stringify(ev)).join('\n') + '\n'
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) {
      return { value: JSON.stringify({ kind: 'attached', owner_session: SID, busy: true, status: 'waiting',
        waiting_for: 'permission prompt', label: 'exp-block', model: 'claude-haiku-5-5', code_version: 'v', started: 0 }) }
    }
    if (e.path.endsWith('.events')) return { value: events }
    if (e.path.endsWith('.progress')) return { value: 'TASK Run the bash command\nTOOL Bash · touch /tmp/x\n' }
    return { value: '' }
  })
  const ran: string[][] = []
  on('process.run', ($, e) => { ran.push([...e.argv]); return { value: { exitCode: 0, stdout: 'v\n', stderr: '' } } })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(band, { type: 'Text', text: /等确认：Bash\(touch \/tmp\/x\)/ })).toBeDefined()
  await band.unmount()
  // 看到接管的会话以后，下一次刷新先跑 bg-sync
  await clock.advance(20000)
  expect(ran.some((argv) => argv.join(' ') === 'cca sideagent bg-sync ' + SID)).toBe(true)
})

test('MCP 服务在同步（心跳 10 秒内）时，面板刷新不再自己起 bg-sync', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: '003#bg-707148f7.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', ($, e) => ({ value: !String(e.path).endsWith('.sock') }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.beat')) return { value: String(Date.now() / 1000) }
    if (e.path.endsWith('.json')) {
      return { value: JSON.stringify({ kind: 'attached', owner_session: SID, busy: false, status: 'idle',
        label: 'x', code_version: 'v', started: 0 }) }
    }
    return { value: '' }
  })
  const ran: string[][] = []
  on('process.run', ($, e) => { ran.push([...e.argv]); return { value: { exitCode: 0, stdout: 'v\n', stderr: '' } } })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(20000)
  expect(ran.some((argv) => argv.join(' ').startsWith('cca sideagent bg-sync'))).toBe(false)
})

test('waitingWhat 写出卡住的工具；notifyKey 让接管的会话每做完一轮叫一次', () => {
  const events = [{ kind: 'tool', name: 'Bash', id: 'a', summary: 'ls' }, { kind: 'tool_result', id: 'a' },
    { kind: 'tool', name: 'Edit', id: 'b', summary: '/x.py' }]
  expect(waitingWhat(events, {})).toContain('Edit(/x.py)')
  expect(waitingWhat([], { waiting_for: 'permission prompt' })).toContain('permission prompt')
  const entry = { external: true, events: [{ kind: 'result', at: 100 }] }
  expect(notifyKey('003#bg-x', entry)).toBe('003#bg-x@100')
  expect(notifyKey('ds#1', { events: [] })).toBe('ds#1')
})

// 接管挑选框（/takeover、sideagent_takeover 没给会话）
const CANDIDATES = [
  { account: '006', id: '7e6a6b38', session_id: '7e6a6b38-aaaa', name: 'backt', title: 'backt', kind: 'background',
    cwd: '/mnt/work/src/winter/llm/factor', state: 'blocked', running: false, last: '回测跑完了', taken_over: false },
  { account: '006', id: '4571c5a2', session_id: '4571c5a2-bbbb', name: 'train', title: 'train', kind: 'background',
    cwd: '/mnt/work/src/winter/llm/factor', state: 'done', running: false, last: '', taken_over: false },
]

async function startPicker($: any, on: any, extra?: (on: any) => void) {
  mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  extra?.(on)
  const registered: any[] = []
  on('command.register', ($: any, e: any) => { registered.push(e); return { value: { command: e.name } } })
  on('session.start', () => ({ cwd: HOME }))
  on('fs.list', () => ({ value: [] }))
  const ran: string[][] = []
  on('process.run', ($: any, e: any) => {
    ran.push([...e.argv])
    if (e.argv.includes('takeover-candidates')) return { value: { exitCode: 0, stdout: JSON.stringify(CANDIDATES), stderr: '' } }
    if (e.argv.includes('takeover')) {
      return { value: { exitCode: 0, stdout: JSON.stringify({ name: '006#bg-' + e.argv[3].slice(0, 8), account: '006',
        session_id: e.argv[3], label: 'backtest', card: '接管了 006/' + e.argv[3].slice(0, 8) + '「backtest」\n回顾：回测跑完了' }), stderr: '' } }
    }
    return { value: { exitCode: 0, stdout: 'v\n', stderr: '' } }
  })
  const opened: any[] = []
  const closed: string[] = []
  on('ui.open', ($: any, e: any) => { opened.push(e); return { value: { isPlaced: true } } })
  on('ui.close', ($: any, e: any) => { closed.push(e.id); return { value: undefined } })
  on('ui.toast', () => ({ value: undefined }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  return { registered, ran, opened, closed }
}

const PICKER_PANE = { plugin: 'sideagents-panel', component: 'Pane', requestId: 'takeover-picker',
  viewport: { columns: 120, rows: 30 },
  props: { title: '接管会话', bodyColumns: 110, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} } }

test('/cca takeover 打开挑选框：列出会话，回车勾选，y 接管勾选的', async ($, on) => {
  const { registered, ran, opened, closed } = await startPicker($, on)
  expect(registered.find((one) => one.name === 'cca')).toBeDefined()
  await $.command.run({ command: 'cca', args: 'takeover' } as any)
  expect(opened[0]).toMatchObject({ id: 'takeover-picker', focus: true })
  expect(ran.some((argv) => argv.join(' ') === 'cca sideagent takeover-candidates --all')).toBe(true)
  const pane = await $.ui.mount({ ...PICKER_PANE, surface: 'terminal' } as any)
  // 一个会话一行：标题、号、原生状态（等确认用醒目色）、多久前、人最后交给它的话（没有就用它最后说的一句）
  expect(await pane.find({ type: 'Text', text: '006' })).toBeDefined()
  expect((await pane.find({ type: 'Text', text: 'blocked' }))?.props?.color).toBe('error')
  expect(await pane.find({ type: 'Text', text: '回测跑完了' })).toBeDefined()
  expect(await pane.find({ type: 'Text', text: '[ ]' })).toBeDefined()
  await pane.press({ key: 'pick-006-7e6a6b38' })
  expect(await pane.find({ type: 'Text', text: '[x]' })).toBeDefined()
  expect((await pane.find({ key: 'pick-confirm' }))?.props?.hotkey).toBe('y')
  await pane.press({ key: 'pick-confirm' })
  // 只接管勾选的那一个，接完关掉挑选框
  const takeovers = ran.filter((argv) => argv.includes('takeover'))
  expect(takeovers).toEqual([['cca', 'sideagent', 'takeover', '7e6a6b38-aaaa', '--account=006']])
  expect(closed).toContain('takeover-picker')
})

test('模型调 sideagent_takeover 没给会话：打开挑选框，回话让模型等人挑；给了会话就照常调', async ($, on) => {
  const seen: any[] = []
  const { opened } = await startPicker($, on, (hook) => {
    hook('tool.call', ($: any, e: any) => { seen.push(e); return { result: 'engine', text: 'engine' } })
  })
  const answer: any = await $.tool.call({ tool: 'mcp__sideagents__sideagent_takeover' } as any)
  expect(opened.map((one) => one.id)).toContain('takeover-picker')
  expect(String(answer?.result || '')).toContain('不要替用户决定')
  expect(seen).toEqual([])
  await $.tool.call({ tool: 'mcp__sideagents__sideagent_takeover', session: '7e6a6b38' } as any)
  expect(seen.length).toBe(1)
})

// 钩子转来的“请确认”：横条标黄写“等你确认”，详情页顶上两个按钮，按了调 cca permission-answer
test('接管会话的请确认转到面板：横条标黄，详情页按 y 允许就回答这条请求', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds2#bg-6b6dd1ed.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', ($, e) => ({ value: !String(e.path).endsWith('.sock') }))
  const permission = { id: 'r1', tool: 'Bash', summary: 'Bash(echo A > /tmp/cca-peer-test/a.txt)',
    input: '{"command":"echo A > /tmp/cca-peer-test/a.txt"}', created: 1, count: 1 }
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) {
      return { value: JSON.stringify({ kind: 'attached', owner_session: SID, busy: true, status: 'waiting',
        waiting_for: 'permission prompt', label: 'bgperm', code_version: 'v', started: 0, permission }) }
    }
    if (e.path.endsWith('.events')) return { value: JSON.stringify({ kind: 'task', text: 'write a.txt' }) + '\n' }
    return { value: '' }
  })
  const ran: string[][] = []
  on('process.run', ($, e) => { ran.push([...e.argv]); return { value: { exitCode: 0, stdout: 'v\n', stderr: '' } } })
  const toasts: string[] = []
  on('ui.toast', ($, e) => { toasts.push(String(e.text)); return { value: undefined } })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const band = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(band, { type: 'Text', text: /等你确认：Bash\(echo A/ })).toBeDefined()
  // 同一条请求只提示一次
  await clock.advance(5000)
  expect(toasts.filter((text) => text.includes('等你确认')).length).toBe(1)
  await band.press({ key: 'peer-ds2#bg-6b6dd1ed' })
  await band.unmount()
  const pane = await $.ui.mount({ plugin: 'sideagents-panel', component: 'Pane', requestId: 'peer-ds2-bg-6b6dd1ed', viewport: { columns: 100, rows: 30 }, props: { title: 'bgperm', bodyColumns: 80, placement: 'inline', scroll: { offset: 0, bodyRows: 20 }, view: {} }, surface: 'terminal' })
  const allow = await pane.find({ key: 'perm-allow' })
  expect(allow?.props?.hotkey).toBe('y')
  expect((await pane.find({ key: 'perm-deny' }))?.props?.hotkey).toBe('n')
  await pane.press({ key: 'perm-allow' })
  expect(ran.some((argv) => argv.join(' ') === 'cca sideagent permission-answer ds2#bg-6b6dd1ed allow r1')).toBe(true)
})

test('横条右边：时间左边写号和模型；名字列已经是号名就只写模型', () => {
  expect(whoOf('openai#0f57ec', '', 'gpt-6-astra')).toBe('openai · gpt-6-astra')
  expect(whoOf('openai#0f57ec', 'codex resume', 'gpt-6-astra')).toBe('openai · gpt-6-astra')
  expect(whoOf('ds#84b089', 'review', '')).toBe('ds')
  expect(whoOf('ds#84b089', '', '')).toBe('ds')
  expect(statusText({ elapsed: '1m 2s', whoText: 'openai · gpt-6-astra', tokenText: '↓ 12.3k tokens' }))
    .toBe('openai · gpt-6-astra · 1m 2s · ↓ 12.3k tokens')
  expect(statusText({ elapsed: 'idle', whoText: 'gpt-6-astra', tokenText: '' })).toBe('gpt-6-astra · idle')
})

test('会话中途接上别的对话（编号变了）：按新编号认 sideagent，原来那段对话派的照样显示', async ($, on) => {
  const clock = mock.clock(on)
  let sid = 'temp-new-process'
  mock.env(on, { HOME })
  on('session.id', () => ({ value: sid }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#old.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: 'de9cdb1b', started: 0, busy: true }) }
    if (e.path.endsWith('.progress')) return { value: 'TOOL Bash · ls\n' }
    return { value: '' }
  })
  on('process.run', () => ({ value: { exitCode: 0, stdout: '\n', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  // 刚起的新进程编号对不上：不显示
  let ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: 'ds' })).toBeUndefined()
  await ui.unmount()
  // 接上原来那段对话：下一次刷新就认出来
  sid = 'de9cdb1b'
  await clock.advance(10000)
  ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: 'ds' })).toBeDefined()
})

test('接管的原生会话不标“旧版”：进程不是我们起的', async ($, on) => {
  const { clock } = busyPeer(on, { state: { kind: 'attached', code_version: 'old' }, version: 'new' })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const band = await ui.find({ type: 'Client', key: 'band' })
  const row = (band?.props.props as any).rows[0]
  expect(row.warnText).not.toContain('旧版')
})

test('接管挑选框：状态上色、多久前、勾选符号、各列按显示宽度对齐', () => {
  // 状态照原生的字，只上颜色
  expect(stateLook('working')).toEqual({ text: 'working', color: 'warning' })
  expect(stateLook('blocked')).toEqual({ text: 'blocked', color: 'error' })
  expect(stateLook('failed').color).toBe('error')
  expect(stateLook('idle').color).toBe('')
  const now = 1_000_000
  expect(agoText(now - 10, now)).toBe('刚刚')
  expect(agoText(now - 125, now)).toBe('2 分钟前')
  expect(agoText(now - 3 * 3600, now)).toBe('3 小时前')
  expect(agoText(now - 2 * 86400, now)).toBe('2 天前')
  expect(agoText(0, now)).toBe('')
  expect(checkMark({}, true)).toEqual({ text: '[x]', color: 'success' })
  expect(checkMark({}, false).text).toBe('[ ]')
  expect(checkMark({ mark: '[=]' }, false).text).toBe('[=]')
  expect(checkMark({ disabled: true }, true).text).toBe('[-]')
  expect(sessionRow({ id: 'a', account: '003', state: 'busy', taken_over: true }, { mark: '[=]' }).mark).toBe('[=]')
  expect(summaryText({ ask: '查一下 503', last: '最后一句' })).toBe('查一下 503')
  expect(summaryText({ last: '最后一句' })).toBe('最后一句')
  expect(summaryText({ last: '最后一句' })).toBe('最后一句')
  // 中文按 2 格：列宽取表头和各行里最宽的那个（表头“状态”4 格，比 busy/idle 宽）
  const widths = columnWidths([{ key: 'title', header: '会话' }, { key: 'state', header: '状态' }],
    [{ cells: { title: '查 sub2api 503', state: 'busy' } }, { cells: { title: 'g5 场景测试', state: 'idle' } }])
  expect(widths.map((one) => one.width)).toEqual([14, 4])
})

test('对话被挪到后台换了编号（/bg）：原来那段对话派的 sideagent 照样显示在新会话里', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME })
  on('session.id', () => ({ value: 'new-bg' }))
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  on('fs.list', () => ({ value: [{ name: 'ds#old.json', kind: 'file', size: 10, isLink: false }] }))
  on('fs.exists', () => ({ value: true }))
  on('fs.read', ($, e) => {
    if (e.path.endsWith('.json')) return { value: JSON.stringify({ owner_session: 'old-fg', started: 0, busy: true }) }
    if (e.path.endsWith('.progress')) return { value: 'TOOL Bash · ls\n' }
    return { value: '' }
  })
  const asked: string[][] = []
  on('process.run', ($, e) => {
    if (e.argv[2] === 'latest') {
      asked.push([...e.argv])
      return { value: { exitCode: 0, stdout: JSON.stringify({ 'old-fg': 'new-bg' }), stderr: '' } }
    }
    return { value: { exitCode: 0, stdout: '\n', stderr: '' } }
  })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  expect(await findAny(ui, { type: 'Text', text: 'ds' })).toBeDefined()
  expect(asked[0]).toEqual(['cca', 'sideagent', 'latest', 'old-fg'])
})

test('/cca 不带参数弹菜单：回车进接管；不认识的子命令给出可用的', async ($, on) => {
  const { opened } = await startPicker($, on)
  await $.command.run({ command: 'cca', args: '' } as any)
  expect(opened[0]).toMatchObject({ id: 'cca-menu', focus: true })
  const menu = await $.ui.mount({ ...PICKER_PANE, requestId: 'cca-menu', surface: 'terminal' } as any)
  expect(await menu.find({ type: 'Text', text: 'takeover' })).toBeDefined()
  for (const name of ['handoff', 'watchdog']) expect(await menu.find({ type: 'Text', text: name })).toBeUndefined()
  await menu.press({ key: 'menu-takeover' })
  expect(opened.map((one) => one.id)).toContain('takeover-picker')
  const answer: any = await $.command.run({ command: 'cca', args: 'nope' } as any)
  expect(String(answer?.text || '')).toContain('takeover')
  // 换号、看门狗已不在面板里
  const gone: any = await $.command.run({ command: 'cca', args: 'handoff' } as any)
  expect(String(gone?.text || '')).toContain('不认识的子命令')
})

test('敲 /cca t… 时补全列表给出子命令；不是 /cca 的不管', () => {
  expect(ccaCompletions({ text: '/cca t', cursor: 6 }).map((one: any) => one.text)).toEqual(['takeover'])
  expect(ccaCompletions({ text: '/cca w', cursor: 6 })).toEqual([])
  expect(ccaCompletions({ text: '/cca takeover 先看', cursor: 15 })).toEqual([])
  expect(ccaCompletions({ text: '/model t', cursor: 8 })).toEqual([])
})

test('前台命令（is_backgrounded: false）不当成后台任务画在底部', () => {
  const rows = footerRows([], [
    { kind: 'tool', name: 'Bash', id: 'tF', input: { command: 'make test' } },
    { kind: 'bgtask', subtype: 'task_started', task_id: 'f1', task_type: 'local_bash', tool_use_id: 'tF', description: 'make test', is_backgrounded: false },
  ], { agents: [], agentDone: () => false })
  expect(rows).toEqual([])
})

test('后台命令、监视只写一行数量', () => {
  expect(otherTasksText([{ kind: 'Bash' }, { kind: 'Bash' }, { kind: 'Monitor' }])).toBe('还有 2 个后台命令 · 1 个监视')
  expect(otherTasksText([])).toBe('')
})

// ---- 新格式（docs/specs/sideagent-mn.md）：直接摆 sessions/<会话>.json 名单 + sideagents/<名字>/ 文件夹 ----

// 新格式的假文件系统：files 是 {完整路径: 内容}，lists 是 {目录: [条目]}
function newLayout(on: any, files: Record<string, string>, lists: Record<string, any[]>) {
  on('fs.list', ($: any, e: any) => {
    if (lists[e.path]) return { value: lists[e.path] }
    throw new Error('没有这个目录')
  })
  on('fs.read', ($: any, e: any) => {
    if (e.path in files) return { value: files[e.path] }
    throw new Error('没有这个文件')
  })
  on('fs.exists', ($: any, e: any) => ({ value: e.path in files }))
}

function rosterOf(session: string, agents: Record<string, any>) {
  return JSON.stringify({ session, account: '005', pid: 1, proc_start: 'p1', agents })
}

rawTest('新格式：横条按本会话名单读；派出的画 ◯，接管的画 ◇（不写“接管”）；别的会话名单里的不出现', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const file = (path: string) => ({ name: path, kind: 'file', size: 10, isLink: false })
  newLayout(on, {
    [SESSIONS + '/' + SID + '.json']: rosterOf(SID, {
      'ds#a1': { kind: 'spawned', since: 1, label: '查日志', description: '', caller_agent: '' },
      '003#bg-12345678': { kind: 'attached', since: 2 },
    }),
    [SESSIONS + '/other.json']: rosterOf('other', { 'ds#o9': { kind: 'spawned', since: 1, label: '别人的' } }),
    [DIR + '/ds#a1/state.json']: JSON.stringify({ kind: 'spawned', busy: true, started: 0, code_version: 'v' }),
    [DIR + '/ds#a1/progress']: 'TOOL Bash · ls -la\n',
    [DIR + '/ds#a1/run/sock']: '',
    // 接管的：没有 run/sock 也显示
    [DIR + '/003#bg-12345678/state.json']: JSON.stringify({ kind: 'attached', busy: false, status: 'idle',
      label: 'exp-a', started: 0 }),
    [DIR + '/ds#o9/state.json']: JSON.stringify({ kind: 'spawned', busy: true, started: 0 }),
    [DIR + '/ds#o9/run/sock']: '',
  }, { [SESSIONS]: [file(SID + '.json'), file('other.json')] })
  // other 这个编号最新的还是它自己：不是我这段对话的
  on('process.run', ($, e) => ({ value: { exitCode: 0,
    stdout: e.argv[2] === 'latest' ? JSON.stringify({ other: 'other' }) : 'v\n', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const band = await ui.find({ type: 'Client', key: 'band' })
  const shown = (band?.props.props as any).rows
  expect(shown.map((row: any) => row.name).sort()).toEqual(['003#bg-12345678', 'ds#a1'])
  expect(shown.find((row: any) => row.name === '003#bg-12345678').attached).toBe(true)
  expect(shown.find((row: any) => row.name === 'ds#a1').attached).toBe(false)
  // 名字列：派出的用名单里记的名字，接管的用会话标题
  expect(shown.find((row: any) => row.name === 'ds#a1').label).toBe('查日志')
  expect(await findAny(ui, { type: 'Text', text: '◯ ' })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: '◇ ' })).toBeDefined()
  expect(await findAny(ui, { type: 'Text', text: /接管/ })).toBeUndefined()
  expect(await findAny(ui, { type: 'Text', text: /别人的/ })).toBeUndefined()
})

rawTest('新格式：刚停的派出的显示完成（finished_at 和进度文件都新，进程被硬杀留下的 run/sock 不算在跑）；早就停了的不显示', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  const now = Date.now()
  newLayout(on, {
    [SESSIONS + '/' + SID + '.json']: rosterOf(SID, {
      'ds#done': { kind: 'spawned', since: 1, label: '数文件' },
      'ds#old': { kind: 'spawned', since: 1, label: '老的' },
    }),
    [DIR + '/ds#done/state.json']: JSON.stringify({ kind: 'spawned', finished_at: now / 1000 - 3, started: 0 }),
    [DIR + '/ds#done/progress']: 'TASK 跑一下\nRESULT 好了 · 25.2k tok · 8.9 秒\n',
    [DIR + '/ds#done/run/sock']: '',
    [DIR + '/ds#old/state.json']: JSON.stringify({ kind: 'spawned', finished_at: now / 1000 - 3600, started: 0 }),
    [DIR + '/ds#old/progress']: 'RESULT 早就好了\n',
  }, {
    [SESSIONS]: [{ name: SID + '.json', kind: 'file', size: 10, isLink: false }],
    [DIR + '/ds#done']: [{ name: 'progress', kind: 'file', size: 10, isLink: false, mtimeMs: now }],
    [DIR + '/ds#old']: [{ name: 'progress', kind: 'file', size: 10, isLink: false, mtimeMs: now - 3600000 }],
  })
  on('process.run', () => ({ value: { exitCode: 0, stdout: '{}', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const ui = await $.ui.mount({ ...SITE, surface: 'terminal' })
  const band = await ui.find({ type: 'Client', key: 'band' })
  const shown = (band?.props.props as any).rows
  expect(shown.map((row: any) => row.label)).toEqual(['数文件'])
  expect(await findAny(ui, { type: 'Text', text: /25.2k tokens/ })).toBeDefined()
})

rawTest('新格式：sideagent 自己用 SendMessage 发的话，按发件 socket 在本会话名单里认出它、转给派它的子代理', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HOME, CLAUDE_CODE_SESSION_ID: SID })
  on('command.register', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: HOME }))
  on('ui.render', () => ({ type: 'Text', props: {}, children: ['x'] }))
  newLayout(on, {
    [SESSIONS + '/' + SID + '.json']: rosterOf(SID, { 'ds#s1': { kind: 'spawned', since: 1, caller_agent: 'a1' } }),
    [DIR + '/ds#s1/state.json']: JSON.stringify({ kind: 'spawned', instance_socket: '/tmp/s1.sock', started: 0 }),
  }, { [SESSIONS]: [{ name: SID + '.json', kind: 'file', size: 10, isLink: false }] })
  on('process.run', () => ({ value: { exitCode: 0, stdout: '{}', stderr: '' } }))
  const sent: any[] = []
  on('session.send', ($: any, e: any) => { sent.push(e); return { isDelivered: true } })
  on('session.receive', ($: any, e: any) => ({ text: e.text }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: HOME })
  await clock.advance(1500)
  const got = await $.session.receive({ origin: { kind: 'peer' },
    text: '<cross-session-message from="uds:/tmp/s1.sock" from-name="ds">\n卡住了\n</cross-session-message>' })
  expect(got?.consumed).toBeDefined()
  expect(sent[0].to).toBe('a1')
})

rawTest('圆点：派出的 ◯/●，接管的 ◇/◆；接管挑选框里被别的会话接管的写“已被 X 接管”', () => {
  expect(circleOf(false, false)).toBe('◯')
  expect(circleOf(false, true)).toBe('●')
  expect(circleOf(true, false)).toBe('◇')
  expect(circleOf(true, true)).toBe('◆')
  expect(bandMark({ attached: true }, false).mark).toBe('◇')
  expect(bandMark({ attached: true }, true).mark).toBe('◆')
  expect(bandMark({}, false).mark).toBe('○')
  expect(takenText({ taken_by: ['ham-b3', 'de9cdb1b'], ask: '加节点' })).toBe('已被 ham-b3、de9cdb1b 接管 · 加节点')
  expect(takenText({ taken_by: [], last: '好了' })).toBe('好了')
  expect(takenText({})).toBe('')
})


// ---- 额度快满的提醒（quota-hint.js）----
rawTest('quota hint: only a warn with key and text is a hint', () => {
  expect(parseHint('{"account":"b004","warn":false}')).toBe(null)
  expect(parseHint('not json')).toBe(null)
  expect(parseHint('{"warn":true,"key":"b004:five_hour:1","text":"换号"}')).toEqual({ key: 'b004:five_hour:1', text: '换号' })
})

rawTest('quota hint: attached once, main thread only, never on a deny', () => {
  const hint = { key: 'b004:five_hour:1', text: '【cca 额度提醒】' }
  const given = new Set<string>()
  const ok = { result: 'ls output', context: ['earlier'] }
  expect(withHint({ deny: 'no' }, hint, given, {})).toEqual({ deny: 'no' })
  expect(withHint(ok, hint, given, { agentId: 'sub-1' })).toBe(ok)
  expect(withHint(ok, hint, given, {})).toEqual({ result: 'ls output', context: ['earlier', '【cca 额度提醒】'] })
  expect(withHint(ok, hint, given, {})).toBe(ok)
  expect(withHint(ok, null, new Set(), {})).toBe(ok)
})

rawTest('quota hint: cca decides when to ask next; junk falls back to the default', () => {
  expect(parseQuota('{"warn":false,"next_check_seconds":60}')).toEqual({ hint: null, nextMs: 60000 })
  expect(parseQuota('{"warn":false,"next_check_seconds":3600}').nextMs).toBe(3600000)
  expect(parseQuota('{"warn":false,"next_check_seconds":1}').nextMs).toBe(QUOTA_POLL_MS)
  expect(parseQuota('{"warn":false}').nextMs).toBe(QUOTA_POLL_MS)
  expect(parseQuota('oops')).toEqual({ hint: null, nextMs: QUOTA_POLL_MS })
})
