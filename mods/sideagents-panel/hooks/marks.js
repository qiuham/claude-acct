// 横条上的圆点：派出的 ◯ / ●（正在看），接管的 ◇ / ◆（正在看）；刚做完的保留 ✓，一眼能认出来
export const CIRCLE = '\u25EF'          // ◯
export const CIRCLE_VIEWED = '\u25CF'   // ●（Linux 上 Claude Code 用这个，macOS 是 ⏺）
export const DIAMOND = '\u25C7'         // ◇ 接管的
export const DIAMOND_VIEWED = '\u25C6'  // ◆ 接管的、正在看

// 终端横条那一格：接管的画菱形，派出的画圆
export function circleOf(attached, viewed) {
  if (attached) return viewed ? DIAMOND_VIEWED : DIAMOND
  return viewed ? CIRCLE_VIEWED : CIRCLE
}

// 老画法（非终端）：选中的画实心，其余空心；刚做完的保留 ✓
export function bandMark(row, selected) {
  if (selected) return { mark: row.attached ? DIAMOND_VIEWED : '●', color: undefined }
  if (row.mark === '✓') return { mark: '✓', color: row.color }
  // 没选中的空心用灰色，和这一行的灰字一致
  return { mark: row.attached ? DIAMOND : '○', color: 'inactive' }
}
