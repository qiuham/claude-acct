// 接管挑选框：挑本机各号已有的 Claude Code 会话，接管成 sideagent（挂到本会话的面板上）。
// 两个入口：/cca takeover；模型调 sideagent_takeover 却没说接哪个（不让模型替人挑）。
// 画法、键盘、鼠标都交给通用列表（list-pane.js）：↑↓ 走，空格/回车勾选，y 接管勾选的，Ctrl+X X 关。数据来自 `cca sideagent takeover-candidates`。

import { closeList, openList, picked, setRows } from './list-pane.js'

export const PICKER_ID = 'takeover-picker'

const view = { rows: [], busy: false, firstWords: '' }

export async function loadRows($, all) {
  const argv = ['cca', 'sideagent', 'takeover-candidates', ...(all ? ['--all'] : [])]
  try {
    const out = await $.process.run(argv, { timeoutMs: 60000 })
    const rows = JSON.parse(String(out?.stdout || '[]'))
    return Array.isArray(rows) ? rows : []
  } catch {
    return []
  }
}

export function rowKey(row) {
  return 'pick-' + String(row.account) + '-' + String(row.id)
}

// 状态：照原生的字（busy / idle / blocked …），只上颜色：在跑黄色，等确认、出错醒目色，其余发暗
export function stateLook(state) {
  const text = String(state || '')
  if (text === 'working' || text === 'busy') return { text, color: 'warning' }
  if (text === 'blocked' || text === 'waiting' || text === 'failed') return { text, color: 'error' }
  return { text, color: '' }
}

// 多久前：刚刚 / 2 分钟前 / 1 小时前 / 3 天前
export function agoText(at, now = Date.now() / 1000) {
  const seconds = Math.max(0, now - Number(at || 0))
  if (!at) return ''
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return Math.floor(seconds / 60) + ' 分钟前'
  if (seconds < 86400) return Math.floor(seconds / 3600) + ' 小时前'
  return Math.floor(seconds / 86400) + ' 天前'
}

// 最后一列：人最后交给它的那句话（自己写的，最好认）；没有就用它最后说的
export function summaryText(row) {
  return String(row.ask || row.last || '')
}

// 会话列表的列：接管和换号共用
export const SESSION_COLUMNS = [
  { key: 'title', header: '会话', cap: 36 },
  { key: 'account', header: '号', cap: 10 },
  { key: 'state', header: '状态', cap: 10 },
  { key: 'ago', header: '活动', cap: 10 },
  { key: 'summary', header: '你最后说的' },
]

// 一个会话 → 通用列表的一行
export function sessionRow(row, { key, now = Date.now() / 1000, mark = '' } = {}) {
  const look = stateLook(row.state)
  return {
    key: key || rowKey(row),
    cells: {
      title: String(row.title || row.name || row.id || ''),
      account: String(row.account || row.profile || ''),
      state: look.text,
      ago: agoText(row.active_at, now),
      summary: summaryText(row),
    },
    colors: look.color ? { state: look.color } : {},
    mark,
    data: row,
  }
}

// 被别的会话接管了：最后一列写“已被 X 接管”（X 是那些会话的标题或短编号）；M 对 N，仍然可以选
export function takenText(row) {
  const by = Array.isArray(row.taken_by) ? row.taken_by.filter(Boolean) : []
  if (!by.length) return summaryText(row)
  const rest = summaryText(row)
  return '已被 ' + by.join('、') + ' 接管' + (rest ? ' · ' + rest : '')
}

function shownRows() {
  // 本会话已经接管过的用 [=] 标出来
  return view.rows.map((row) => {
    const shown = sessionRow(row, { mark: row.taken_over ? '[=]' : '' })
    return { ...shown, cells: { ...shown.cells, summary: takenText(row) } }
  })
}

// firstWords：/cca takeover 后面跟的话，接管成功后作为第一句话发给接上的会话
// 在跑的会话也一直列出来（不再要按 r 切换）；停掉的由 cca 那边就不列
export async function openPicker($, { firstWords } = {}) {
  if (firstWords !== undefined) view.firstWords = firstWords
  await openList($, {
    id: PICKER_ID,
    title: view.firstWords ? '接管会话 · 接上后先说：' + view.firstWords.slice(0, 30) : '接管会话',
    columns: SESSION_COLUMNS,
    rows: [],
    check: true,
    note: '正在列出能接管的会话…',
    hints: [
      { key: 'hint-toggle', label: '空格/回车 勾选' },
      { key: 'pick-confirm', hotkey: 'y', label: () => '接管(' + picked(PICKER_ID).length + ')',
        onPress: () => takeoverPicked($) },
      { key: 'hint-close', label: 'Ctrl+X X 关' },
    ],
  })
  view.rows = await loadRows($, true)
  setRows($, PICKER_ID, shownRows(), view.rows.length ? '' : '本机没有能接管的会话')
}

async function takeoverPicked($) {
  const keys = picked(PICKER_ID)
  if (view.busy || !keys.length) return
  view.busy = true
  const done = []
  const failed = []
  const cards = []
  for (const key of keys) {
    const row = view.rows.find((one) => rowKey(one) === key)
    if (!row) continue
    try {
      const out = await $.process.run(['cca', 'sideagent', 'takeover', row.session_id, '--account=' + row.account],
        { timeoutMs: 90000 })
      const result = JSON.parse(String(out?.stdout || '{}'))
      if (result.error) failed.push(row.id + '：' + result.error)
      else {
        done.push(result.name)
        if (result.card) cards.push(result.card)
        // 接上就把第一句话发过去（走跨会话消息；它在忙就排到下一个工具调用之后）
        if (view.firstWords) {
          await $.process.run(['cca', 'sideagent', 'tell', '--interject', result.name, view.firstWords], { timeoutMs: 15000 })
        }
      }
    } catch (error) {
      failed.push(row.id + '：' + String(error))
    }
  }
  view.busy = false
  await closeList($, PICKER_ID)
  const said = view.firstWords && done.length ? '，并发了第一句话' : ''
  view.firstWords = ''
  const text = (done.length ? '已接管：' + done.join('、') + said : '没有接上')
    + (failed.length ? '；失败：' + failed.join('；') : '')
  try {
    await $.ui.toast(text)
  } catch {
    // 没有 toast 就算了
  }
  // 接上的会话各带一张卡片（在干什么、最后交给它的事、它最近说的），交给主会话的模型读，人看不到
  if (cards.length) {
    try {
      await $.session.append({ message: { type: 'user', content: [{ type: 'text', text: cards.join('\n\n') }] } })
    } catch {
      // 记不进去就算了：面板上照样能看到
    }
  }
  // 横条下一次刷新（1 秒内）就会出现接上的行
  $.ui.invalidate('ui.render')
}

// 模型调 sideagent_takeover 却没给会话：打开挑选框让人挑，不让模型替人选
export async function onTakeoverCall($, e, next) {
  if (!String(e.tool || '').endsWith('__sideagent_takeover')) return next(e)
  if (String(e.session || '').trim()) return next(e)
  try {
    await openPicker($, { all: Boolean(e.all) })
  } catch {
    return next(e)
  }
  return { result: '已在面板打开“接管会话”挑选框，等用户自己挑；接上后横条会出现对应的行，'
    + '名字形如 <号>#bg-<编号>，用 sideagent_send 发给那个名字。不要替用户决定接哪个。' }
}
