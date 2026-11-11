// 把sideagent的事件画成和主界面相近的样子。
// 思路来自 fnclaude/renderer 的 src/renderers（MIT），改成直接在 Mod 里用。

// 主界面收起来时只留几行，点开再看全部；这里跟着它走
const PREVIEW_LINES = 3
const FULL_LINES = 60

function oneLine(text) {
  return String(text || '').replace(/\s+/g, ' ').trim()
}

function previewLines(text, limit = PREVIEW_LINES) {
  const all = String(text || '').split('\n').map((line) => line.replace(/\s+$/, ''))
  const shown = all.slice(0, limit)
  return { shown, rest: Math.max(0, all.length - shown.length) }
}

// 一行：图标和正文放进同一个文本流，宽度不够时整体折行，图标不会单独掉到一行
function row({ el, mark, color, label, width, dim }) {
  const { Box, Text } = el
  return Box({
    flexDirection: 'row',
    ...(width ? { width } : {}),
    children: [
      Text({
        wrap: 'wrap',
        dimColor: dim,
        children: [
          Text({ children: [(mark || '') + ' '], color }),
          Text({ children: [String(label)] }),
        ],
      }),
    ],
  })
}

// "… 还有 N 行"：能点就点开，点不了就当普通一行
function moreRow(el, width, rest, opts = {}) {
  const { Box, Text, Button } = el
  const label = '… 还有 ' + rest + ' 行'
  if (!Button || !opts.onToggle) {
    return row({ el, mark: '', label, width, dim: true })
  }
  return Box({
    flexDirection: 'row',
    ...(width ? { width } : {}),
    children: [Button({
      key: opts.key,
      plain: true,
      onPress: opts.onToggle,
      children: [
        Text({ children: ['  '], dimColor: true }),
        Text({ children: [label], dimColor: true }),
      ],
    })],
  })
}

// Bash：命令一行；输出由 tool_result 单独画
export function bashTool(el, ev, width, opts = {}) {
  const command = oneLine(ev.input?.command || ev.summary || '')
  return row({ el, mark: '●', color: opts.running ? 'success' : undefined, label: 'Bash(' + command + ')', width })
}

// Edit：文件路径 + 几行 diff，其余收起来
export function editTool(el, ev, width, opts = {}) {
  const { Box, Text } = el
  const input = ev.input || {}
  const path = oneLine(input.file_path || '')
  const children = [row({ el, mark: '●', color: opts.running ? 'success' : undefined, label: 'Edit(' + path + ')', width })]
  const oldLines = String(input.old_string || '').split('\n')
  const newLines = String(input.new_string || '').split('\n')
  const diffLines = [
    ...oldLines.map((line) => '-' + line),
    ...newLines.map((line) => '+' + line),
  ]
  const limit = opts.open ? FULL_LINES : PREVIEW_LINES
  const shown = diffLines.slice(0, limit)
  const rest = diffLines.length - shown.length
  const rel = path.replace(/^\/+/, '')
  const diff = [
    '--- a/' + rel,
    '+++ b/' + rel,
    `@@ -1,${oldLines.length} +1,${newLines.length} @@`,
    ...shown,
  ].join('\n')
  const { Code } = el
  if (Code) {
    children.push(Box({
      flexDirection: 'row',
      ...(width ? { width } : {}),
      children: [Box({ width: 2, flexShrink: 0, children: [Text({ children: [''] })] }),
        Box({ flexShrink: 1, ...(width ? { width: Math.max(4, width - 2) } : {}), children: [Code({ source: diff, format: 'diff', wrap: 'truncate-end' })] })],
    }))
  } else {
    shown.forEach((line) => {
      children.push(row({ el, mark: '', label: line, width, dim: true }))
    })
  }
  if (rest > 0) children.push(moreRow(el, width, rest, opts))
  return { children }
}

// Read：一行路径；内容由 tool_result 画
export function readTool(el, ev, width, opts = {}) {
  const path = oneLine(ev.input?.file_path || '')
  return row({ el, mark: '●', color: opts.running ? 'success' : undefined, label: 'Read(' + path + ')', width })
}

// Write：只留一行摘要，不画文件内容（和主界面一样）
export function writeTool(el, ev, width, opts = {}) {
  const path = oneLine(ev.input?.file_path || '')
  const content = String(ev.input?.content ?? '')
  const lines = content ? content.split('\n').length : 0
  const label = 'Write(' + path + ')' + (lines ? ' · ' + lines + ' 行' : '')
  return row({ el, mark: '●', color: opts.running ? 'success' : undefined, label, width })
}

// Task：子任务描述
export function taskTool(el, ev, width, opts = {}) {
  const input = ev.input || {}
  const label = oneLine(input.description || input.prompt || '')
  return row({ el, mark: '●', color: opts.running ? 'success' : undefined, label: 'Agent(' + label + ')', width })
}

// 其它工具：名字 + 一句参数
export function genericTool(el, ev, width, opts = {}) {
  const label = ev.name + (ev.summary ? '(' + ev.summary + ')' : '')
  return row({ el, mark: '●', color: opts.running ? 'success' : undefined, label: label, width })
}

// 工具输出：收起来留 3 行，点开看最多 60 行
export function toolOutput(el, text, width, opts = {}) {
  // 子代理的原始回传是一大段框架文字，压成一行
  const raw = String(text || '')
  if (raw.includes('[Subagent hand-back]') || raw.includes('<tools-used-before-summary>')) {
    const nums = raw.match(/\b\d+\b/)
    return [row({ el, mark: '⎿', label: '子代理已返回结果' + (nums ? ' · ' + nums[0] : ''), width, dim: true })]
  }
  const { shown, rest } = previewLines(raw, opts.open ? FULL_LINES : PREVIEW_LINES)
  const children = []
  shown.forEach((line, index) => {
    children.push(row({ el, mark: index === 0 ? '⎿' : '', label: line, width, dim: true }))
  })
  if (rest > 0) children.push(moreRow(el, width, rest, opts))
  return children
}
