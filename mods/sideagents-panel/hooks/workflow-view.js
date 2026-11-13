// 工作流详情页要用的纯函数：读出来的进度清单 → 按阶段分组的一行行；子代理对话记录 → 面板能画的事件。
// 不碰界面、不读文件，方便单独测。

// 子代理状态 → 行首的记号（照主界面：在跑实心、做完打勾、出错打叉、还没开始空心）
const MARKS = { done: '✓', error: '✗', start: '●', progress: '●' }

function kilo(count) {
  const n = Number(count) || 0
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n)
}

// 毫秒 → “48 秒”“3 分 5 秒”
export function durationText(ms) {
  const seconds = Math.round((Number(ms) || 0) / 1000)
  if (seconds < 60) return seconds + ' 秒'
  const minutes = Math.floor(seconds / 60)
  return minutes + ' 分' + (seconds % 60 ? ' ' + (seconds % 60) + ' 秒' : '')
}

// 一个子代理一行：记号 名字 模型 工具次数 词元 用时；在跑的下面再写它最近一步，出错的写原因
export function agentLine(item, now = Date.now()) {
  const state = String(item.state || '')
  const ms = item.durationMs || (item.startedAt ? now - Number(item.startedAt) : 0)
  const parts = [String(item.model || ''), item.toolCalls ? item.toolCalls + ' 工具' : '',
    item.tokens ? kilo(item.tokens) + ' tok' : '', ms ? durationText(ms) : ''].filter(Boolean)
  let detail = ''
  if (state === 'error') detail = String(item.error || '出错')
  else if (state !== 'done' && item.lastToolName) {
    detail = item.lastToolName + (item.lastToolSummary ? '(' + item.lastToolSummary + ')' : '')
  }
  return { mark: MARKS[state] || '○', label: String(item.label || item.agentId || ''), meta: parts.join('  '),
    detail, agentId: String(item.agentId || ''), state }
}

// 进度清单 → [{title, counts, agents: [行]}]，阶段按出现顺序；没写阶段的子代理放进一个空名字的组
export function workflowGroups(items, now = Date.now()) {
  const list = Array.isArray(items) ? items : []
  const groups = []
  const byIndex = new Map()
  const group = (index, title) => {
    const key = Number(index) || 0
    if (!byIndex.has(key)) {
      const made = { title: String(title || ''), agents: [] }
      byIndex.set(key, made)
      groups.push(made)
    }
    const found = byIndex.get(key)
    if (!found.title && title) found.title = String(title)
    return found
  }
  for (const item of list) {
    if (item?.type === 'workflow_phase') group(item.index, item.title)
  }
  for (const item of list) {
    if (item?.type === 'workflow_agent') group(item.phaseIndex, item.phaseTitle).agents.push(agentLine(item, now))
  }
  const shown = groups.filter((one) => one.agents.length || one.title)
  for (const one of shown) {
    const done = one.agents.filter((row) => row.state === 'done' || row.state === 'error').length
    one.counts = one.agents.length ? done + '/' + one.agents.length : '还没开始'
  }
  return shown
}

function toolSummary(input) {
  if (!input || typeof input !== 'object') return ''
  for (const key of ['command', 'file_path', 'path', 'pattern', 'url', 'query', 'description']) {
    const value = input[key]
    if (typeof value === 'string' && value.trim()) return value.split(/\s+/).join(' ').slice(0, 80)
  }
  return ''
}

function resultText(content) {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) return content.map((block) => (typeof block?.text === 'string' ? block.text : '')).join('\n')
  return ''
}

// 子代理对话记录（agent-<编号>.jsonl）→ 面板事件：说的话、思考、工具调用、工具输出，都挂在这个子代理下
export function transcriptEvents(text, agentId) {
  const out = []
  for (const line of String(text || '').split('\n')) {
    let row
    try {
      row = JSON.parse(line)
    } catch {
      continue
    }
    const content = row?.message?.content
    if (!Array.isArray(content)) continue
    for (const block of content) {
      if (row.type === 'assistant') out.push(...assistantBlock(block, agentId))
      else if (row.type === 'user' && block?.type === 'tool_result') {
        out.push({ kind: 'tool_result', id: String(block.tool_use_id || ''), text: resultText(block.content).slice(0, 4000),
          error: Boolean(block.is_error), parent: agentId })
      }
    }
  }
  return out
}

function assistantBlock(block, agentId) {
  if (block?.type === 'text' && String(block.text || '').trim()) {
    return [{ kind: 'say', text: String(block.text).slice(0, 4000), parent: agentId }]
  }
  if (block?.type === 'thinking' && String(block.thinking || '').trim()) {
    return [{ kind: 'think', text: String(block.thinking).slice(0, 4000), parent: agentId }]
  }
  if (block?.type === 'tool_use') {
    return [{ kind: 'tool', name: String(block.name || '?'), id: String(block.id || ''), input: block.input || {},
      summary: toolSummary(block.input), parent: agentId }]
  }
  return []
}
