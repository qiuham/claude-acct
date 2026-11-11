// 通用列表面板：/cca 菜单、接管、换号、看门狗共用一套。
// 一个面板一份状态（按面板编号存），自己画表头、对齐、❯ 高亮、[x] 勾选框，
// 键盘和鼠标只有一个焦点：↑↓ 在各行之间挪（不滚动），空格/回车勾选或进入，点一下也一样。
//
// 用法：
//   openList($, { id, title, columns, rows, check, header, hints, onEnter, note })
//     columns: [{ key, header, cap }]，最后一列占满剩下的宽度
//     rows:    [{ key, cells: {列: 字}, colors: {列: 颜色}, disabled, reason, data }]
//     check:   true 时每行带勾选框，空格/回车勾选；false 时回车调 onEnter(row)
//     hints:   [{ key, hotkey, label: () => 字, onPress }]，画在底部那一行
//   setRows($, id, rows, note)   换一批行（比如加载完了、换了一步）
//   picked(id)                   勾选了哪些行（按行的键）

import { displayWidth } from './text-width.js'

const lists = {}

export function hasList(id) {
  return Boolean(lists[id])
}

export function listState(id) {
  return lists[id]
}

export async function openList($, spec) {
  const old = lists[spec.id]
  lists[spec.id] = {
    title: spec.title || '',
    columns: spec.columns || [],
    rows: spec.rows || [],
    check: Boolean(spec.check),
    header: spec.header !== false,
    hints: spec.hints || [],
    onEnter: spec.onEnter || null,
    note: spec.note || '',
    picked: new Set(spec.picked || []),
    focus: '',
    rowsHeight: spec.rowsHeight || 14,
    lines: spec.lines || [],
  }
  if (old) {
    $.ui.invalidate('ui.render')
    return
  }
  await $.ui.open({ id: spec.id, title: spec.title || '', focus: true, rows: (spec.rowsHeight || 14) + 4,
    backgroundColor: 'transparent' })
}

export function setRows($, id, rows, note = '') {
  const state = lists[id]
  if (!state) return
  state.rows = rows || []
  state.note = note
  const keys = new Set(state.rows.map((row) => row.key))
  for (const key of [...state.picked]) if (!keys.has(key)) state.picked.delete(key)
  if (!keys.has(state.focus)) state.focus = ''
  $.ui.invalidate('ui.render')
}

// 列表下面另加几行字（看门狗的“最近换号”“下一轮试算”这类）
export function setLines($, id, lines) {
  if (!lists[id]) return
  lists[id].lines = lines || []
  $.ui.invalidate('ui.render')
}

export function setNote($, id, note) {
  if (!lists[id]) return
  lists[id].note = note
  $.ui.invalidate('ui.render')
}

export function picked(id) {
  return lists[id] ? [...lists[id].picked] : []
}

export async function closeList($, id) {
  delete lists[id]
  try {
    await $.ui.close({ id })
  } catch {
    // 已经关了
  }
}

// 面板被关掉（Ctrl+X X）时清掉状态
export function forgetList(id) {
  delete lists[id]
}

// 勾选框：[x] 勾了（绿色加粗），[ ] 没勾；不能选的行显示 [-]
export function checkMark(row, isPicked) {
  if (row.disabled) return { text: '[-]', color: '' }
  if (isPicked) return { text: '[x]', color: 'success' }
  return { text: row.mark || '[ ]', color: '' }
}

// 每列的宽度：表头和各行里最宽的那个（按显示宽度，中文 2 格），不超过这一列的上限
export function columnWidths(columns, rows) {
  return columns.map((column) => {
    const widest = Math.max(displayWidth(column.header || ''),
      ...rows.map((row) => displayWidth(String(row.cells?.[column.key] ?? ''))))
    return { ...column, width: Math.min(column.cap || 40, widest) }
  })
}

async function activate($, id, key) {
  const state = lists[id]
  const row = state?.rows.find((one) => one.key === key)
  if (!row) return
  if (row.disabled) {
    if (row.reason) setNote($, id, row.reason)
    return
  }
  if (state.check) {
    if (state.picked.has(key)) state.picked.delete(key)
    else state.picked.add(key)
    $.ui.invalidate('ui.render')
    return
  }
  if (state.onEnter) await state.onEnter(row)
}

function focusTo($, id, key) {
  const state = lists[id]
  if (!state || state.focus === key) return
  state.focus = key
  // 挪到不能选的行，底部写它为什么不能选；挪走就清掉
  const row = state.rows.find((one) => one.key === key)
  state.note = row?.disabled ? String(row.reason || '') : ''
  $.ui.invalidate('ui.render')
}

// register.js 的 ui.focus 转过来：键盘焦点落在哪一行，那一行亮起 ❯
export function onListFocus($, id, element) {
  if (!lists[id]) return false
  if (lists[id].rows.some((row) => row.key === element)) focusTo($, id, element)
  return true
}

// register.js 的 ui.scroll 转过来：方向键在各行之间挪，不滚动；到头停住
export async function onListArrow($, id, by) {
  const state = lists[id]
  if (!state) return false
  const keys = state.rows.map((row) => row.key)
  if (!keys.length) return true
  const at = Math.max(0, keys.indexOf(state.focus))
  const next = keys[Math.min(keys.length - 1, Math.max(0, at + (by > 0 ? 1 : -1)))]
  if (next && next !== state.focus) {
    focusTo($, id, next)
    try {
      await $.ui.focus({ requestId: id, key: next })
    } catch {
      // 挪不动就算了
    }
  }
  return true
}

// 自绘区域点了哪一行：焦点挪过去，再勾选或进入（鼠标和键盘始终是同一个焦点）
export async function onListMessage($, e, next) {
  if (!String(e.module || '').endsWith('list-rows.js')) return next(e)
  const id = String(e.data?.pane || '')
  const key = String(e.data?.pick || '')
  if (!lists[id] || !key) return {}
  focusTo($, id, key)
  try {
    await $.ui.focus({ requestId: id, key })
  } catch {
    // 挪不动就算了
  }
  await activate($, id, key)
  return {}
}

// 最近一次画出来的行（测试和调试用：按面板编号取）
const lastShown = {}
export function shownRows(id) {
  return lastShown[id] || []
}

// 一行：[❯][勾选框][各列…]，最后一列占满剩下的宽度。选中的整行白、第一列加粗，没选中的灰（各格自己的颜色照留）
function rowLine(el, p, row, { lit = false, header = false } = {}) {
  const { Box, Text } = el
  const dim = header || !lit || row.disabled
  // 只在要加粗时才传 bold：传 bold:false 终端会发“正常亮度”，把发暗也一起取消
  const strong = lit && !row.disabled ? { bold: true } : {}
  const cells = p.columns.map((column, index) => {
    const last = index === p.columns.length - 1
    const color = header ? '' : String(row.colors?.[column.key] || '')
    const text = String((header ? column.header : row.cells?.[column.key]) ?? '')
    const style = { dimColor: color ? false : dim, wrap: 'truncate', ...(color ? { color } : {}), ...(index === 0 ? strong : {}) }
    return last
      ? Box({ flexGrow: 1, width: 0, children: [Text({ ...style, children: [text] })] })
      : Box({ width: Math.max(0, Number(column.width) || 0), flexShrink: 0, marginRight: 2, children: [Text({ ...style, children: [text] })] })
  })
  const check = p.check
    ? [Box({ width: 4, flexShrink: 0, children: [Text({
      ...(row.checkColor && !header ? { color: row.checkColor, bold: true } : { dimColor: true }),
      children: [header ? '' : String(row.check || '')] })] })]
    : []
  return Box({ flexDirection: 'row', width: p.width, height: 1, children: [
    Box({ width: 2, flexShrink: 0, children: [Text({ ...strong, children: [lit ? '❯ ' : '  '] })] }),
    ...check,
    ...cells,
  ] })
}

export function renderList($, e, next) {
  const id = String(e.requestId || '')
  const state = lists[id]
  if (!state) return next(e)
  const el = $.ui.resolve(e)
  const { Box, Text, Button } = el
  const width = Math.max(40, (Number(e.props?.bodyColumns) || 80) - 2)
  if (!state.focus && state.rows.length) state.focus = state.rows[0].key
  const shown = state.rows.map((row) => {
    const mark = checkMark(row, state.picked.has(row.key))
    return { key: row.key, cells: row.cells || {}, colors: row.colors || {}, disabled: Boolean(row.disabled),
      check: mark.text, checkColor: mark.color, selected: state.focus === row.key }
  })
  const columns = columnWidths(state.columns, shown)
  // 直接画成 Box/Text，不用自绘区域（Client）：有人的终端里自绘区域整块画不出来，只剩勾选框（winter 实测）
  const top = state.header ? 1 : 0
  const rowLines = [
    ...(state.header ? [rowLine(el, { width, columns, check: state.check }, {}, { header: true })] : []),
    ...shown.map((row) => rowLine(el, { width, columns, check: state.check }, row, { lit: row.selected })),
  ]
  lastShown[id] = shown
  const list = shown.length ? [Box({ key: 'list-' + id, flexDirection: 'column', width, children: rowLines })] : []
  // 键盘用的按钮只压在每行开头一格（❯ 那一格）上：↑↓ 走、空格/回车按下，不整行反白
  const keys = shown.map((row, index) => Box({
    key: 'key-' + row.key,
    position: 'absolute',
    // 有勾选框的压在勾选框上（和横条压在圆点上一样），没有的压在 ❯ 上
    left: state.check ? 2 : 0,
    top: index + top,
    children: [Button({
      key: row.key,
      plain: true,
      label: String(row.cells[state.columns[0]?.key] || row.key),
      ...(index === 0 ? { autoFocus: true } : {}),
      onPress: () => activate($, id, row.key),
      children: [state.check
        ? Text({ ...(row.checkColor ? { color: row.checkColor, bold: true } : { dimColor: !row.selected }),
          children: [row.check] })
        : Text({ ...(row.selected ? { bold: true } : {}), children: [row.selected ? '❯' : ' '] })],
    })],
  }))
  // 底部一行提示：热键照原生选择列表的写法（热键用强调色，冒号，说明）
  const hintParts = []
  for (const hint of state.hints) {
    if (hintParts.length) hintParts.push(Text({ dimColor: true, children: [' · '] }))
    const label = typeof hint.label === 'function' ? hint.label() : String(hint.label || '')
    if (hint.hotkey) {
      hintParts.push(Button({ key: hint.key, plain: true, dimColor: true, hotkey: hint.hotkey, label,
        onPress: hint.onPress }))
    } else {
      hintParts.push(Text({ dimColor: true, children: [label] }))
    }
  }
  return Box({
    flexDirection: 'column',
    children: [
      Box({ flexDirection: 'column', width, children: [...list, ...keys] }),
      Text({ children: [' '] }),
      ...state.lines.map((one) => Text({ dimColor: !one.bold, ...(one.bold ? { bold: true } : {}),
        wrap: 'truncate-end', children: [String(one.text ?? one)] })),
      ...(state.note ? [Text({ color: 'warning', wrap: 'truncate-end', children: [state.note] })] : []),
      Box({ flexDirection: 'row', children: hintParts }),
    ],
  })
}
