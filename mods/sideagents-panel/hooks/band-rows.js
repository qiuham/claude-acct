// 整个横条一个自绘区域，照 Claude Code 2.1.316 自带子代理列表（Xz/Gle）画。
// 所有行在同一帧里重画：右侧列宽各行一定一样，不会因为某一行晚一帧重画而左右晃。
//
// 每一行：[指针 2 格][圆点 + 空格][名字列][  描述（占满剩下的）][ 右侧：耗时 · ↓ N tokens]
// 鼠标移上去：那一行亮起 ❯ 并取消发暗，不反白（Button 在指针下一定反白，所以行不用 Button 画）。
// 点一下：把那一行的内部名字发给插件，由插件打开详情。
//
// props: { width, labelWidth, statusWidth, more,
//          rows: [{ name, label, named, desc, elapsed, whoText, tokenText, warnText, circleColor,
//                   attached, selected, viewed }] }

// 这个文件由 Client 单独载入，不 import 别的文件：记号照 marks.js 抄一份
const POINTER = '❯'       // ❯
const CIRCLE = '◯'        // ◯ 派出的
const CIRCLE_VIEWED = '●' // ●（Linux 上 Claude Code 用这个，macOS 是 ⏺）
const DIAMOND = '◇'       // ◇ 接管的
const DIAMOND_VIEWED = '◆' // ◆ 接管的、正在看
const DOT = ' · '

// 派出的 ◯ / ●（正在看），接管的 ◇ / ◆
function markOf(row) {
  if (row.attached) return row.viewed ? DIAMOND_VIEWED : DIAMOND
  return row.viewed ? CIRCLE_VIEWED : CIRCLE
}

function line(surface, p, row, lit) {
  const { Box, Text } = surface.elements
  // 子代理那一行：没选中、没在看就整行发暗；在看的加粗
  const dim = !lit && !row.viewed
  // 只在要加粗时才传 bold：传 bold:false 会让终端发“正常亮度”，把发暗也一起取消掉
  const bold = row.viewed ? { bold: true } : {}
  const statusText = [row.whoText, row.elapsed, row.tokenText].filter(Boolean).join(DOT)
  const warn = row.warnText ? (statusText ? DOT : '') + row.warnText : ''
  return Box({
    flexDirection: 'row',
    width: p.width,
    height: 1,
    children: [
      Box({
        width: 4,
        flexShrink: 0,
        children: [
          Text({ dimColor: dim, ...bold, children: [lit ? POINTER + ' ' : '  '] }),
          Text({
            color: row.circleColor || undefined,
            dimColor: !row.circleColor && dim,
            ...bold,
            // 派出的 ◯ / ●（正在看），接管的 ◇ / ◆
            children: [markOf(row) + ' '],
          }),
        ],
      }),
      Box({
        width: Math.max(0, Number(p.labelWidth) || 4),
        flexShrink: 0,
        // 显示派活时给的名字，没给就是号名（像子代理显示代理类型）；没起名的跟着整行发暗
        children: [Text({ ...bold, dimColor: dim && !row.named, wrap: 'truncate',
          children: [String(row.label || row.name || '')] })],
      }),
      Box({
        flexGrow: 1,
        width: 0,
        paddingLeft: 2,
        children: [Text({ dimColor: dim, ...bold, wrap: 'truncate', children: [String(row.desc || '')] })],
      }),
      // 右侧列：所有行同一个宽度（各行右侧文字最宽的那个），文字靠右，右端固定在行尾
      Box({
        width: Math.max(0, Number(p.statusWidth) || 0),
        flexShrink: 0,
        marginLeft: 1,
        justifyContent: 'flex-end',
        children: [Text({
          dimColor: dim,
          ...bold,
          children: [statusText, ...(warn ? [Text({ color: 'warning', children: [warn] })] : [])],
        })],
      }),
    ],
  })
}

export default function BandRows(props, surface) {
  const { Box, Text } = surface.elements
  const p = props || {}
  const rows = Array.isArray(p.rows) ? p.rows : []
  const hover = typeof surface.state?.hover === 'number' ? surface.state.hover : -1
  // 每次都重设监听：拿到的是最新的行
  surface.onPointer((ev) => {
    const index = ev.y >= 0 && ev.y < rows.length ? ev.y : -1
    if (ev.type === 'leave') {
      if (hover !== -1) surface.setState({ hover: -1 })
    } else if (ev.type === 'enter' || ev.type === 'move') {
      if (index !== (surface.state?.hover ?? -1)) surface.setState({ hover: index })
    } else if (ev.type === 'up' && ev.button === 'left' && index >= 0) {
      surface.post({ open: rows[index].name })
    }
  })
  const children = rows.map((row, index) => line(surface, p, row, Boolean(row.selected) || hover === index))
  if (p.more > 0) children.push(Text({ dimColor: true, children: ['  ↓ ' + p.more + ' more'] }))
  return Box({ flexDirection: 'column', width: p.width, children })
}
