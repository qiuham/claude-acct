import { displayWidth } from './text-width.js'
export { displayWidth }
import { onTakeoverCall } from './takeover-picker.js'
import { withPeerHint } from './peer-hint.js'
import { QUOTA_FIRST_MS, QUOTA_POLL_MS, parseQuota, withHint } from './quota-hint.js'
import { COMMAND as CCA_COMMAND, ccaCompletions, runCca } from './cca-command.js'
import { forgetList, hasList, onListArrow, onListFocus, onListMessage, renderList } from './list-pane.js'
import { bandMark, circleOf } from './marks.js'
import { bashTool, editTool, readTool, writeTool, taskTool, genericTool, toolOutput } from './renderers.js'

// 在输入框上方画 cca sideagent：一个sideagent一行，像子代理一样。
// Ctrl+X Tab 把焦点移进来，↑↓ 选，Enter 打开，鼠标也能点。

// 结束的sideagent显示多久（毫秒）
const LINGER_MS = 30000

// 面板 id 只能有字母、数字、下划线、横线，这里记住 id 对应哪个sideagent
// 自己说的话给的底色，和主界面一样显眼
const USER_BG = '#262626'
// 有含义的数字都给名字，别散在代码里
const LEASE_SECONDS = 10
const TEXT_LIMIT = 48          // 横条一行放多少字
const TICK_BUSY_MS = 500       // sideagent在跑时的刷新间隔
const TICK_IDLE_MS = 1000      // 都闲着时的刷新间隔
const NOTIFY_MAX = 50          // 记住多少个已通知的sideagent
const BAND_WIDE = 80           // 横条宽度分档
// bodyColumns 扣掉了引擎最右 5 格（折叠标记 [-]）。子代理列表只留 2 格边距，
// 所以横条要多画 3 格，耗时列才和它对齐；压到 [-] 下面，难看就调回 0。
// 横条拿到的 bodyColumns 是所在列的宽度减去引擎右边的 5 格；子代理列表的行宽是所在列宽度减 2
const ENGINE_RESERVED = 5
const ROW_RIGHT_PADDING = 2
const BAND_MID = 40
const BAND_WINDOW = 5          // 横条最多画几行，和子代理列表一样

const paneNames = {}
// 点开子代理的页面：面板编号 -> 哪个sideagent的哪个子代理
const agentPanes = {}
// 刚从输入框发出去的话：sideagent还没接住时先挂在正文里，和主界面一样看得见
const sentByPane = {}
// 每个面板已经看到几轮开始：新的一轮开始，就从“待接住”里挪走最老的一条
const sentSeen = {}
// 每个面板是不是跟着最新：人往上翻过就不跟
const followPane = {}
const paneSig = {}
// 刚回车过的输入框：下一帧画成空的，字已经挂到上面去了
const clearInput = {}
// 每页发过的话（最近 50 句）和输入框里 ↑ 翻到第几句；翻出来的那句只填一次
const historyByPane = {}
const recallAt = {}
const recallValue = {}
// 哪个面板的哪个元素现在拿着焦点
const focusOf = {}
// 横条上当前选中的行（键盘焦点在哪一行）：选中的画白点，其余空心
let bandFocus = ''
// 本会话编号：只显示这个会话派出去的sideagent，和子代理一样各归各的
let mySession = ''
// 现在磁盘上 sideagent.py 的版本（cca sideagent version）；状态里的对不上就标“旧版”
let codeVersion = ''
// 哪个 sideagent 的详情正开着：那一行的圆点画成 ●、整行加粗，和子代理“正在看”的一样
let viewedPeer = ''
let versionTriedAt = 0
const VERSION_RETRY_MS = 60000

// 横条每行的键：名字里可能有 # 之类，统一换成横线，焦点事件里对得上
function bandKey(name) {
  return ('peer-' + name).replace(/[^A-Za-z0-9_-]/g, '-')
}

// 两边都换成同一个写法再比，免得引擎对键做过替换
function sameKey(a, b) {
  const one = (key) => String(key).replace(/[^A-Za-z0-9_-]/g, '-')
  return one(a) === one(b)
}

// 折叠的工具组：key 存在就展开
const openGroups = new Set()

// 详情面板里显示哪些内容
const view = { think: true, detail: true }

// 已经通知过的sideagent，别重复记
const notified = new Set()
// 会话开起来的时间：比这更早结束的sideagent不叫醒主代理
let bootAt = 0
// 本会话有没有接管原生后台会话（sideagent_takeover）：有才每次刷新先同步一次
let hasExternal = false
// 额度快满的提醒（quota-hint.js）：最近一次 cca 给的提醒，和已经附给模型的
let quotaHint = null
const quotaHintsGiven = new Set()

let rows = []
let allDetails = []
let byName = {}

// 进度文件里的一行翻译成人话
// 进度文件的每一行 → 给人看的一句话（前缀查表，加新前缀只加一行）
const FRIENDLY_PREFIX = {
  TASK: 5,
  TOOL: 5,
  SAY: 4,
  RESULT: 7,
  STUCK: 6,
  RETRY: 6,
  NOTIFY: 0,
}

function friendly(line) {
  for (const [word, cut] of Object.entries(FRIENDLY_PREFIX)) {
    if (line.startsWith(word + ' ')) return line.slice(cut)
  }
  return line
}

// 时长照主界面的写法：1h 2m 3s，不用中文时分秒
function age(seconds) {
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return total + 's'
  const minutes = Math.floor(total / 60)
  if (minutes < 60) {
    const rest = total % 60
    return rest ? minutes + 'm ' + rest + 's' : minutes + 'm'
  }
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? hours + 'h ' + rest + 'm' : hours + 'h'
}

// 词元数照 Claude Code 的写法（Bi）：不到 1000 写整数，再大写成 12.3k、1.2m，保留一位小数
export function compactCount(count) {
  const total = Number(count) || 0
  if (total < 1000) return String(Math.round(total))
  const units = [[1e12, 't'], [1e9, 'b'], [1e6, 'm'], [1e3, 'k']]
  for (let i = 0; i < units.length; i += 1) {
    const [size, unit] = units[i]
    if (total < size) continue
    const shown = Math.round((total / size) * 10) / 10
    // 999950 四舍五入成 1000.0k 时进一级，和 Intl 的 compact 一样写成 1.0m
    if (shown >= 1000 && i > 0) return (Math.round((total / units[i - 1][0]) * 10) / 10).toFixed(1) + units[i - 1][1]
    return shown.toFixed(1) + unit
  }
  return String(total)
}

function tokens(count) {
  const total = Number(count) || 0
  if (total <= 0) return ''
  return compactCount(total) + ' tokens'
}

// ↑ 输入（整轮累计，含缓存读写）↓ 输出，和 Python 那边同一个写法
function arrows(tokensIn, tokensOut) {
  const k = (count) => ((Number(count) || 0) / 1000).toFixed(1) + 'k'
  return '↑' + k(tokensIn) + ' ↓' + k(tokensOut)
}

// 横条上的词元：和 Claude Code 子代理那一行一模一样，只显示一个数
// token_count = 最近一次调用的输入（含缓存，等于当前上下文）+ 累计输出 + 正在输出的估计
// 还没有动作（没说话、没调工具）时是 ↑，有了第一个动作以后一直是 ↓
function usedOf(state) {
  if (state.token_count !== undefined) {
    const shown = tokens(state.token_count)
    return shown ? (state.acted ? '↓ ' : '↑ ') + shown : ''
  }
  // 老版本进程没有 token_count：有 ↑↓ 两个数就照写，再老的只有一个合计
  if (Number(state.tokens_in) || Number(state.tokens_out)) return arrows(state.tokens_in, state.tokens_out)
  return tokens(state.tokens)
}


// 进程是不是改代码前起的：没有 code_version，或者和磁盘上的版本对不上
function isStale(state) {
  // 接管的原生会话进程不是我们起的，没有“旧代码要重启”这回事
  if (isAttached(state)) return false
  const mine = String(state.code_version || '')
  if (!mine) return true
  return Boolean(codeVersion) && mine !== codeVersion
}

// 问 cca 本号额度要不要提醒；返回下次多久再问。取不到就维持不提醒
async function refreshQuotaHint($) {
  try {
    const sid = await readSessionId($)
    const out = await $.process.run(['cca', 'quota-hint', '--json', ...(sid ? ['--session', sid] : [])],
      { timeoutMs: 120000 })
    const got = out && Number(out.exitCode) === 0 ? parseQuota(out.stdout) : { hint: null, nextMs: QUOTA_POLL_MS }
    quotaHint = got.hint
    return got.nextMs
  } catch {
    quotaHint = null
    return QUOTA_POLL_MS
  }
}

async function loadCodeVersion($) {
  if (codeVersion || Date.now() - versionTriedAt < VERSION_RETRY_MS) return
  versionTriedAt = Date.now()
  try {
    const out = await $.process.run(['cca', 'sideagent', 'version'], { timeoutMs: 5000 })
    if (out && Number(out.exitCode) === 0) codeVersion = String(out.stdout || '').trim()
  } catch {
    // 拿不到就只按“缺字段”判断旧版
  }
}

// 它这会儿在干什么：正在说的话 > 这一轮最后一步（工具、思考、说话）
// 和 Python 的 doing_of 一个口径；只看这一轮，碰到 task 就停
function peerDoing(events, live) {
  const said = String(live || '').replace(/\s+/g, ' ').trim()
  if (said) return said.length > 80 ? '…' + said.slice(-80) : said
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const ev = events[i]
    if (ev.parent) continue
    if (ev.kind === 'tool') return ev.name + (ev.summary ? '(' + clip(String(ev.summary), 60) + ')' : '')
    if (ev.kind === 'think') return 'Thinking…'
    if (ev.kind === 'say') return clip(String(ev.text || ''), 80)
    if (ev.kind === 'task' || ev.kind === 'result') return ''
  }
  return ''
}

function clip(text, limit) {
  const one = text.replace(/\s+/g, ' ').trim()
  return one.length > limit ? one.slice(0, limit) + '…' : one
}

// 每一列想要多宽：按内容算，分隔行不算
function columnWants(block, cols) {
  const want = new Array(cols).fill(3)
  for (const cells of block) {
    cells.forEach((cell, index) => {
      if (!/^:?-{2,}:?$/.test(cell)) want[index] = Math.max(want[index], clipWidthWidth(cell))
    })
  }
  return want
}

// 放不下就按比例缩，保底 4 格；余下的给最缺的列，超了从最宽的削
function columnBudgets(block, cols, width) {
  const want = columnWants(block, cols)
  // Markdown 表格每格左右各一个空格，边框 cols+1 个
  const available = Math.max(8, width - (3 * cols + 1))
  const total = want.reduce((sum, one) => sum + one, 0)
  if (total <= available) return want
  const budget = want.map((one) => Math.min(one, Math.max(4, Math.floor((one * available) / total))))
  let left = available - budget.reduce((sum, one) => sum + one, 0)
  while (left > 0) {
    let pick = -1
    let gap = 0
    for (let c = 0; c < cols; c += 1) {
      if (want[c] - budget[c] > gap) {
        gap = want[c] - budget[c]
        pick = c
      }
    }
    if (pick < 0) break
    budget[pick] += 1
    left -= 1
  }
  let over = budget.reduce((sum, one) => sum + one, 0) - available
  while (over > 0) {
    const widest = budget.indexOf(Math.max(...budget))
    if (budget[widest] <= 4) break
    budget[widest] -= 1
    over -= 1
  }
  return budget
}

// 按算好的宽度把一行拼回去；分隔行要拼成合法写法
function packRow(cells, budget) {
  const packed = cells.map((cell, index) => {
    const room = budget[index] ?? 3
    if (!/^:?-{2,}:?$/.test(cell)) return clipWidth(cell, room)
    const both = cell.startsWith(':') && cell.endsWith(':')
    if (both) return `:${'-'.repeat(Math.max(2, room - 2))}:`
    if (cell.startsWith(':')) return `:${'-'.repeat(Math.max(2, room - 1))}`
    return '-'.repeat(Math.max(3, room))
  })
  return `| ${packed.join(' | ')} |`
}

// 一个字符占几格：中日韩全角算 2 格
function charWidth(ch) {
  return /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE4F\uFF00-\uFF60\uFFE0-\uFFE6]/.test(ch) ? 2 : 1
}

// 按格数截断；本来就放得下就原样返回，放不下才截并加省略号
function clipWidth(text, budget) {
  const all = String(text)
  if (clipWidthWidth(all) <= budget) return all
  let used = 0
  let out = ''
  for (const ch of all) {
    const cell = charWidth(ch)
    if (used + cell > Math.max(1, budget - 1)) return out + '…'
    used += cell
    out += ch
  }
  return out
}

// Markdown 表格按当前宽度截断每一格：能放下就原样不动，放不下才按比例缩。
// 面板变宽就多显示，变窄自动截。
export function fitTables(text, width) {
  const lines = String(text).split('\n')
  const isRow = (line) => /^\s*\|.*\|\s*$/.test(line)
  const cellsOf = (line) => line.trim().slice(1, -1).split('|').map((cell) => cell.trim())

  const out = []
  let block = []
  const flush = () => {
    if (!block.length) return
    const cols = Math.max(...block.map((cells) => cells.length))
    const budget = columnBudgets(block, cols, width)
    for (const cells of block) out.push(packRow(cells, budget))
    block = []
  }
  for (const line of lines) {
    if (isRow(line)) block.push(cellsOf(line))
    else {
      flush()
      out.push(line)
    }
  }
  flush()
  return out.join('\n')
}

// 一串字占几格
function clipWidthWidth(text) {
  let used = 0
  for (const ch of String(text)) used += charWidth(ch)
  return used
}

// "25.0k" / "0.3m" / "512" 都还原成数
function scaled(text) {
  const unit = text.at(-1)
  const scale = { k: 1000, m: 1000000 }[unit] || 1
  return parseFloat(text) * scale
}

// 结果行末尾的 "3.2k tok · 7.5 秒"，排成 "7.5 秒 · ↓ 3.2k tokens"
// "↑25.0k ↓0.3k" 加起来，显示成一个数
function totalTokens(text) {
  const found = String(text).match(/[↑↓]\s*([\d.]+(?:k|m)?)/g) || []
  if (!found.length) return String(text).trim()
  let total = 0
  for (const part of found) {
    const num = part.replace(/[↑↓\s]/g, '')
    total += scaled(num)
  }
  return total >= 1000 ? (total / 1000).toFixed(1) + 'k' : String(Math.round(total))
}

// 刚做完的那一行：子代理做完时圆点变绿，右边是总耗时 · ↓ N tokens，中间仍是任务描述
function lingeringRow(name, result, events, summary, { label = '', model = '' } = {}) {
  const task = events.filter((ev) => ev.kind === 'task').pop()?.text
  const parts = resultParts(result)
  // 做完的那一行中间仍是最后一句进度短语（照子代理），没有才是任务描述
  return statusRow({
    mark: '✓', color: 'success', circleColor: 'success', name, running: false,
    desc: fullLine(summary) || fullLine(task), elapsed: parts.elapsed, whoText: whoOf(name, label, model),
    tokenText: parts.tokenText,
    warnText: '',
  })
}

// 结果行 “RESULT … · ↑25.0k ↓0.3k tok · 12.7k total · 8.9s” 拆成耗时和词元两段
function resultParts(raw) {
  const parts = String(raw || '').split('\u00B7').map((part) => part.trim())
  const seconds = parts.find((part) => /^[\d.]+\s*(s|秒)$/.test(part))
  const meta = resultMeta(raw)
  const tokenText = meta.split(' \u00B7 ').find((part) => part.includes('tokens') || /[↑↓]/.test(part)) || ''
  return { elapsed: seconds ? elapsedText(parseFloat(seconds) * 1000) : '', tokenText }
}

// 耗时：照 Claude Code 的写法（ln）：不到一分钟 “12s”，再长 “1m 23s”、“1h 2m 3s”
export function elapsedText(ms) {
  const value = Math.max(0, Number(ms) || 0)
  if (value < 60000) return value === 0 ? '0s' : Math.floor(value / 1000) + 's'
  let days = Math.floor(value / 86400000)
  let hours = Math.floor((value % 86400000) / 3600000)
  let minutes = Math.floor((value % 3600000) / 60000)
  let seconds = Math.round((value % 60000) / 1000)
  if (seconds === 60) { seconds = 0; minutes += 1 }
  if (minutes === 60) { minutes = 0; hours += 1 }
  if (hours === 24) { hours = 0; days += 1 }
  if (days > 0) return days + 'd ' + hours + 'h ' + minutes + 'm'
  if (hours > 0) return hours + 'h ' + minutes + 'm ' + seconds + 's'
  return minutes + 'm ' + seconds + 's'
}

// 横条上显示的名字：派活时给的名字，不给就是号名（ds#ff2832 显示成 ds），像子代理显示代理类型。
// 内部的实例名只用来追问和工具返回值，界面上不显示
export function labelOf(name, given) {
  return fullLine(given) || String(name || '').split('#')[0]
}

// 一行字：换行压成空格，不截断（截断交给画的时候按宽度来，和子代理一样）
function fullLine(text) {
  return String(text || '').replace(/\s+/g, ' ').trim()
}

// 路径照 Claude Code 显示：在工作目录下的写相对路径
function shownPath(path, cwd) {
  const value = String(path || '')
  const base = String(cwd || '').replace(/\/+$/, '')
  if (base && value.startsWith(base + '/')) return value.slice(base.length + 1)
  return value
}

// 工具的“活动描述”，照 Claude Code 各工具的 getActivityDescription 写
const ACTIVITY = {
  Bash: (input) => (input.command ? 'Running ' + fullLine(input.description || input.command) : 'Running command'),
  Read: (input, cwd) => (input.file_path ? 'Reading ' + shownPath(input.file_path, cwd) : 'Reading file'),
  Write: (input, cwd) => (input.file_path ? 'Writing ' + shownPath(input.file_path, cwd) : 'Writing file'),
  Edit: (input, cwd) => (input.file_path ? 'Editing ' + shownPath(input.file_path, cwd) : 'Editing file'),
  MultiEdit: (input, cwd) => (input.file_path ? 'Editing ' + shownPath(input.file_path, cwd) : 'Editing file'),
  NotebookEdit: (input, cwd) => (input.notebook_path ? 'Editing notebook ' + shownPath(input.notebook_path, cwd) : 'Editing notebook'),
  Glob: (input) => (input.pattern ? 'Finding ' + input.pattern : 'Finding files'),
  Grep: (input) => (input.pattern ? 'Searching for ' + input.pattern : 'Searching'),
  WebFetch: (input) => (input.url ? 'Fetching ' + input.url : 'Fetching web page'),
  WebSearch: (input) => (input.query ? 'Searching for ' + input.query : 'Searching the web'),
  Monitor: (input) => (input.description ? 'Monitoring: ' + input.description : 'Monitoring'),
  Agent: (input) => fullLine(input.description) || 'Running task',
  Task: (input) => fullLine(input.description) || 'Running task',
}

// 这一轮最后一个工具调用的活动描述；还没调工具（或这一轮已经结束）就是空
export function activityOf(events, cwd) {
  for (let i = (events || []).length - 1; i >= 0; i -= 1) {
    const ev = events[i]
    if (ev.parent) continue
    if (ev.kind === 'task' || ev.kind === 'result') return ''
    if (ev.kind !== 'tool') continue
    const input = ev.input && typeof ev.input === 'object' ? ev.input : {}
    const describe = ACTIVITY[ev.name]
    if (describe) return describe(input, cwd)
    return String(ev.name || '')
  }
  return ''
}

function resultMeta(raw) {
  const parts = raw.split('·').map((part) => part.trim())
  const seconds = parts.find((part) => /^[\d.]+\s*(s|秒)$/.test(part)) || ''
  const tokensPart = parts.find((part) => part.endsWith('tok'))
  const out = []
  if (seconds) out.push(seconds)
  // 新格式带 "12.7k total"：就是横条那个数，做完了一定有过动作，箭头是 ↓
  const totalPart = parts.find((part) => / total$/.test(part))
  if (totalPart) {
    out.push('↓ ' + totalPart.replace(/ total$/, '').trim() + ' tokens')
  } else if (tokensPart) {
    const bare = tokensPart.replace(/ tok$/, '').trim()
    // 只有 "↑12.3k ↓0.4k" 的原样留着；更老的只有一个数，显示成 "x tokens"
    out.push(/[↑↓]/.test(bare) ? bare : totalTokens(bare) + ' tokens')
  }
  return out.join(' · ')
}

// 横条回到“没选中”
function clearBandFocus($) {
  if (!bandFocus) return
  bandFocus = ''
  $.ui.invalidate('ui.render')
}

// 这个sideagent是不是本会话派的：派活的会话就是我，或者那段对话后来被挪成了我
// （Claude Code 的 /bg、← 挪后台会把对话复制成新编号接着跑，原来那份最后写一行 continued-in）
function ownedByMe(session) {
  const owner = String(session || '')
  if (!mySession || !owner) return false
  if (owner === mySession) return true
  const known = latestOf[owner]
  if (!known) wantLatest.add(owner)
  return known?.latest === mySession
}

// 别的会话编号各自最新的那一份：{编号: {latest, at}}；60 秒内不重查
const latestOf = {}
const wantLatest = new Set()
const LATEST_TTL_MS = 60000

// 刷新时顺手查一下没查过、或者查过但过了 60 秒的编号（一次 cca 调用查完）；有变化就重画
async function refreshLatest($) {
  const now = Date.now()
  for (const [owner, known] of Object.entries(latestOf)) if (now - known.at > LATEST_TTL_MS) wantLatest.add(owner)
  if (!wantLatest.size) return false
  const asked = [...wantLatest]
  wantLatest.clear()
  let answer = {}
  try {
    const out = await $.process.run(['cca', 'sideagent', 'latest', ...asked], { timeoutMs: 10000 })
    answer = JSON.parse(String(out?.stdout || '{}'))
  } catch {
    answer = {}
  }
  let changed = false
  for (const owner of asked) {
    const latest = String(answer?.[owner] || owner)
    if (latestOf[owner]?.latest !== latest) changed = true
    latestOf[owner] = { latest, at: now }
  }
  return changed
}

// 接管的（kind=attached）：会话不是我们起的，可以同时挂在好几个主会话上
export function isAttached(state) {
  return String(state?.kind || '') === 'attached'
}

// 本会话名单：~/.cca/sessions/<会话编号>.json 的 agents（{名字: {kind, label, description, caller_agent}}）。
// 会话编号会因为 /bg、← 换（原来那份对话写一行 continued-in）：一串编号的文件合起来算，我自己的条目优先
async function readRoster($, home) {
  const dir = home + '/.cca/sessions'
  let files = []
  try {
    files = await $.fs.list(dir)
  } catch {
    return {}
  }
  const chain = []
  for (const file of files) {
    if (!file.name.endsWith('.json')) continue
    const session = file.name.slice(0, -5)
    if (session === mySession) continue
    if (ownedByMe(session)) chain.push(session)
  }
  if (mySession) chain.push(mySession)
  const merged = {}
  for (const session of chain) {
    try {
      const data = JSON.parse(await $.fs.read(dir + '/' + session + '.json'))
      const agents = data?.agents
      if (agents && typeof agents === 'object') Object.assign(merged, agents)
    } catch {
      // 读不到就当这个会话没用着谁
    }
  }
  return merged
}

async function readJson($, path) {
  try {
    const data = JSON.parse(await $.fs.read(path))
    return data && typeof data === 'object' ? data : null
  } catch {
    return null
  }
}

async function readEvents($, path) {
  try {
    return (await $.fs.read(path)).split('\n').filter(Boolean)
      .map((line) => { try { return JSON.parse(line) } catch { return null } })
      .filter(Boolean)
  } catch {
    return []
  }
}

async function collect($) {
  const home = await $.env.get('HOME')
  if (!home) return { rows: [], details: [], byName: {} }
  const dir = home + '/.cca/sideagents'
  const nextRows = []
  const nextDetails = []
  const nextByName = {}
  let sawExternal = false
  const ended = []
  // 先读本会话名单，再一个个读 sideagents/<名字>/：不再扫整个目录按 owner_session 过滤
  const roster = await readRoster($, home)
  for (const [name, note] of Object.entries(roster)) {
    const folder = dir + '/' + name
    const state = await readJson($, folder + '/state.json')
    if (!state) continue
    // 接管的（attached）进程不归我们管，没有我们的套接字；派出的写了 finished_at、或者没有 run/sock，就是没在跑
    // （进程被硬杀时来不及删 run/，留下的旧套接字不算在跑）
    const attached = isAttached(state) || note?.kind === 'attached'
    if (!attached && (state.finished_at || !(await $.fs.exists(folder + '/run/sock')))) {
      ended.push({ name, note: note || {}, state })
      continue
    }
    if (state.code_version) await loadCodeVersion($)
    const peer = await readPeer($, folder, name, { state, note: note || {}, attached })
    if (attached) sawExternal = true
    nextRows.push(peer.row)
    nextByName[name] = peer.entry
    nextDetails.push(name)
  }

  hasExternal = sawExternal
  // 已经结束的sideagent：进度文件最近 30 秒内改过的，显示成“完成”
  for (const entry of await readLingering($, dir, ended)) {
    nextRows.push(entry.row)
    nextByName[entry.name] = entry.entry
  }
  return { rows: nextRows, details: nextDetails, byName: nextByName }
}

// 一个在跑的sideagent：读它的进度、事件、状态，算出一条横条和一页详情
// 横条名字、派活的子代理、有没有起名：状态里有就用状态的，没有用本会话名单里记的那一条
function peerIdentity(name, state, note) {
  const given = state.label || note.label
  return { label: labelOf(name, given), named: Boolean(given),
    // 派活的子代理编号（插件从 tool.call 的 agentId 记下的）；主对话派的为空
    callerAgent: String(state.caller_agent || note.caller_agent || '') }
}

async function readPeer($, folder, name, { state, note = {}, attached = false }) {
  const read = async (path) => {
    try {
      return await $.fs.read(path)
    } catch {
      return ''
    }
  }
  const raw = (await read(folder + '/progress')).split('\n').filter(Boolean)
  const events = await readEvents($, folder + '/events.jsonl')
  const live = String(await read(folder + '/run/live')).trim()
  const nowSec = Date.now() / 1000
  const result = raw.find((line) => line.startsWith('RESULT '))
  const used = usedOf(state)
  const stale = isStale(state)
  const { label: label_, named, callerAgent } = peerIdentity(name, state, note)
  // API 正在出错重试时，横条上写这个（比“最后一步”更说明为什么没动静）
  const retrying = state.busy && state.api_retry ? String(state.api_retry) : ''
  const doing = retrying || peerDoing(events, live)
  // 进程活着不等于在干活：看它这一轮有没有跑
  const busy = state.busy === undefined ? !result : Boolean(state.busy)
  // 卡在等人确认（权限确认等）：标黄，写出卡在哪个工具调用上
  // 钩子转来的“请确认”（面板上能点）；没有的话看是不是卡在原生确认框上
  const permission = state.permission && typeof state.permission === 'object' ? state.permission : null
  const blocked = state.status === 'waiting' || Boolean(permission)
  if (permission && !toasted.has(String(permission.id))) {
    toasted.add(String(permission.id))
    try {
      await $.ui.toast(labelOf(name, state.label) + ' 等你确认：' + String(permission.summary || permission.tool || ''))
    } catch {
      // 提示弹不出来不要紧，横条那行已经标黄
    }
  }
  const stateLook = blocked ? BLOCKED_LOOK : look({ busy, running: true, alive: true })
  // 主界面的子代理行显示的是它的任务描述：取最近一次交办的事，压成一行
  const label = previewLine(events.filter((ev) => ev.kind === 'task').pop()?.text)
  const pending = blocked ? waitingWhat(events, state, permission) : ''
  return {
    row: busy
      ? { ...busyRow({ name, stateLook, raw, state, nowSec, used, label, stale, events }),
        ...(blocked ? { desc: pending } : {}),
        label: label_, named, callerAgent, attached }
      : { ...idleRow({ name, stateLook, events, state, label, stale }), label: label_, named, callerAgent, attached },
    entry: {
      mark: stateLook.mark,
      color: stateLook.color,
      callerAgent,
      lines: raw.map(friendly).filter(Boolean).slice(-10),
      events,
      live,
      running: busy,
      alive: true,
      busy,
      doing,
      used,
      stale,
      label: label_,
      turnStarted: Number(state.turn_started || 0),
      started: Number(state.started || 0),
      deliveredToCaller: Boolean(state.delivered_to_caller),
      notify: Boolean(state.notify),
      model: String(state.model || ''),
      external: attached,
      blocked,
      waiting: pending,
      permission,
    },
  }
}

function busyRow({ name, stateLook, raw, state, nowSec, used, label, stale, events }) {
  const last = raw.length ? friendly(raw[raw.length - 1]) : '等待任务'
  const started = Number(state.started || nowSec)
  const turnStart = Number(state.turn_started || 0)
  const time = elapsedText((turnStart > 0 ? nowSec - turnStart : nowSec - started) * 1000)
  // 子代理那一行中间只有一段：它的进度短语，没有就是任务描述。
  // 进度短语由 sideagent 自己那个号的小模型写（照 Claude Code 的 AgentSummary，每 30 秒）；
  // 还没写出来、没配模型或调用失败时，退回工具的活动描述
  // 派活时给了 description（像 Agent 的 description）就用它当任务描述，没给才用交办的原话
  const desc = fullLine(state.summary) || activityOf(events, state.cwd) || fullLine(state.description)
    || fullLine(label) || clip(last, TEXT_LIMIT)
  return statusRow({
    mark: stateLook.mark, color: stateLook.color, name, running: true, desc,
    elapsed: time, whoText: whoOf(name, state.label, state.model), tokenText: used, warnText: warnOf(state, stale),
  })
}

// 右边那一段之后的黄字：排着的条数（和子代理的 “2 queued” 一样），旧版本进程再加 “旧版”
function warnOf(state, stale) {
  const queued = Number(state.queued || 0)
  return [queued > 0 ? queued + ' queued' : '', stale ? '旧版' : ''].filter(Boolean).join(' \u00B7 ')
}

// 右边那段里时间左边的“号 · 模型”：每一行都写号，名字列已经是号名也照写，各行一个样子
export function whoOf(name, label, model) {
  const account = String(name || '').split('#')[0]
  return [account, String(model || '')].filter(Boolean).join(' \u00B7 ')
}

// 右边那一段（不含黄字）：号 · 模型 · 耗时 · 词元；横条对齐、Client 画、老画法都用这一个
export function statusText(row) {
  return [row.whoText, row.elapsed, row.tokenText].filter(Boolean).join(' \u00B7 ')
}

// 一行的全部字段：Client 画的新样子用分开的几段，老的 Button 画法用 text/meta
function statusRow(row) {
  return {
    ...row,
    text: row.desc,
    meta: [statusText(row), row.warnText].filter(Boolean).join(' \u00B7 '),
  }
}

function idleRow({ name, stateLook, events, state, label, stale }) {
  const said = events.filter((ev) => ev.kind === 'say' || ev.kind === 'result').pop()
  // 子代理做完、闲着时中间仍显示最后一句进度短语，没有短语才是任务描述
  const text = fullLine(state.summary) || fullLine(state.description) || fullLine(label)
    || previewLine(said?.text)
  // 子代理闲着时右边只写 “idle”，不带耗时和词元
  return statusRow({
    mark: stateLook.mark, color: stateLook.color, name, running: false, desc: text,
    elapsed: 'idle', whoText: whoOf(name, state.label, state.model), tokenText: '', warnText: warnOf(state, stale),
  })
}

// 刚结束、还没被回收的sideagent：进度文件最近 30 秒内改过、最后有 RESULT 的
// （停了的 state.json 写着 finished_at，早就结束的不用再看文件夹）
async function readLingering($, dir, ended) {
  const out = []
  const now = Date.now()
  for (const { name, note, state } of ended) {
    const finished = Number(state.finished_at || 0) * 1000
    if (finished && now - finished > LINGER_MS) continue
    let files = []
    try {
      files = await $.fs.list(dir + '/' + name)
    } catch {
      continue
    }
    const progress = files.find((file) => file.name === 'progress')
    if (!progress || now - Number(progress.mtimeMs || 0) > LINGER_MS) continue
    const one = await lingeringOne($, dir + '/' + name, name, { state, note })
    if (one) out.push(one)
  }
  return out
}

// 一个刚结束的 sideagent：读它的进度、事件，加上名单里记的名字、描述，算出横条那一行和详情；没有结果行就是 null
async function lingeringOne($, folder, name, { state, note }) {
  let raw = []
  try {
    raw = (await $.fs.read(folder + '/progress')).split('\n').filter(Boolean)
  } catch {
    return null
  }
  const result = raw.filter((line) => line.startsWith('RESULT ')).pop()
  if (!result) return null
  const ownerLabel = String(note.label || state.label || '')
  const callerAgent = String(note.caller_agent || state.caller_agent || '')
  const events = await readEvents($, folder + '/events.jsonl')
  const summary = raw.filter((line) => line.startsWith('SUMMARY ')).pop()?.slice(8)
    || note.description || state.description
  return {
    name,
    row: { ...lingeringRow(name, result, events, summary,
      { label: ownerLabel, model: note.model || state.model }), label: labelOf(name, ownerLabel),
      named: Boolean(ownerLabel), callerAgent, attached: false },
    entry: {
      mark: '✓',
      color: 'success',
      label: labelOf(name, ownerLabel),
      callerAgent,
      model: String(note.model || state.model || ''),
      started: Number(state.started || 0),
      deliveredToCaller: Boolean(state.delivered_to_caller),
      // 老进程退出时不写 delivered_to_caller，进度文件里那句 NOTIFY 才是可靠的
      notify: Boolean(state.notify) || raw.includes('NOTIFY 1'),
      lines: raw.map(friendly).filter(Boolean).slice(-10),
      events,
    },
  }
}

// 算不算子代理：Agent/Task 一定算；Skill 只有分叉执行（context: fork）时算，
// 这时它里面的动作都带 parent = 这次 Skill 调用的编号，和子代理一样
function isAgentCall(ev, events) {
  if (ev.kind !== 'tool') return false
  if (ev.name === 'Agent' || ev.name === 'Task') return true
  return ev.name === 'Skill' && Boolean(ev.id) && events.some((other) => other.parent === ev.id)
}

// 子代理那一行和它的页面标题怎么写：Agent(描述) 或 Skill(技能名)
export function agentTitle(agent) {
  if (agent.name === 'Skill') {
    const skill = String(agent.input?.skill || agent.summary || '')
    return { kind: 'Skill', desc: skill }
  }
  return { kind: 'Agent', desc: String(agent.input?.description || agent.input?.prompt || agent.summary || '') }
}

// ---- 后台任务（照 Claude Code 原生分类）
// cca 把 stream-json 里的 task_started / task_progress / task_updated / task_notification 记成 bgtask；
// 原生输入框下面那几行就是按 task_type 分的：子代理、后台命令、监视、工作流。
const TASK_DONE = new Set(['completed', 'failed', 'killed', 'stopped', 'cancelled', 'canceled', 'error'])

// 同一个 task_id 的几条事件并成一条：最新的描述、状态、进度
const TASK_FIELDS = ['task_type', 'tool_use_id', 'description', 'subagent_type', 'workflow_name',
  'last_tool_name', 'summary', 'workflow_progress', 'status']

function mergeTask(task, ev) {
  for (const key of TASK_FIELDS) {
    if (ev[key]) task[key] = ev[key]
  }
  // 是不是挪到了后台：false 也要记下（上面只收真值）；后来 Ctrl+B 挪到后台会再来一条 true
  if (typeof ev.is_backgrounded === 'boolean') task.is_backgrounded = ev.is_backgrounded
  if (ev.usage) task.usage = { ...(task.usage || {}), ...ev.usage }
  if (ev.subtype === 'task_notification' || TASK_DONE.has(String(ev.status || ''))) task.done = true
  return task
}

export function foldTasks(events) {
  const byId = new Map()
  for (const ev of events || []) {
    if (ev.kind !== 'bgtask' || !ev.task_id) continue
    byId.set(ev.task_id, mergeTask(byId.get(ev.task_id) || { id: ev.task_id, done: false }, ev))
  }
  return [...byId.values()]
}

// 原生四类：agent（子代理，含分叉执行的 Skill 和团队成员）、shell（后台命令）、monitor（监视）、workflow（工作流）
export function taskKind(task, toolOf) {
  const type = String(task.task_type || '')
  const tool = toolOf(task.tool_use_id)
  if (type === 'local_agent' || type === 'in_process_teammate' || type === 'remote_agent') return 'agent'
  if (type.startsWith('monitor') || tool?.name === 'Monitor') return 'monitor'
  // 前台命令也会登记成任务（is_backgrounded: false），它在跑完之前占着这一轮，不是后台任务，不画。
  // 主界面底部也只列挪到后台的命令（实测：ds#5d481a 两条前台 ASan 测试被当成后台行，点了没反应）
  if (type === 'local_bash') return task.is_backgrounded === false ? '' : 'shell'
  if (type === 'local_workflow') return 'workflow'
  return ''
}

// 工作流现在到哪：最后一个有名字的阶段
function workflowStage(progress) {
  const list = Array.isArray(progress) ? progress : []
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const item = list[i] || {}
    const label = item.title || item.phase || item.label || item.name || ''
    if (label) return String(label) + (item.status ? ' ' + item.status : '')
  }
  return ''
}

function kilo(count) {
  const n = Number(count) || 0
  return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n)
}

// 一个后台任务 → 底部那一行要用的东西
function taskMeta(task) {
  const usage = task.usage || {}
  return [usage.tool_uses ? usage.tool_uses + ' tools' : '', usage.total_tokens ? kilo(usage.total_tokens) + ' tok' : '']
    .filter(Boolean).join(' · ')
}

// 子代理那一行：分叉执行的 Skill 写 Skill(技能名)，其余 Agent(描述)；能点进去
function agentTaskRow(task, tool, allEvents) {
  const skill = tool.name === 'Skill'
  const desc = skill
    ? String(tool.input?.skill || task.description || '')
    : String(task.description || tool.input?.description || '')
  const agent = { ...tool, id: task.tool_use_id, name: skill ? 'Skill' : (tool.name || 'Agent'),
    input: tool.input || { description: desc } }
  const inner = allEvents.some((ev) => ev.parent === task.tool_use_id)
  const doing = inner ? agentDoing(agent, allEvents) : (task.last_tool_name || '启动中…')
  return { kind: skill ? 'Skill' : 'Agent', desc, doing: [doing, taskMeta(task)].filter(Boolean).join(' · '), agent }
}

function taskRow(task, kind, { toolOf, allEvents }) {
  const tool = toolOf(task.tool_use_id) || {}
  if (kind === 'agent') return agentTaskRow(task, tool, allEvents)
  if (kind === 'shell') {
    return { kind: 'Bash', desc: String(tool.input?.command || task.description || ''), doing: '后台运行中' }
  }
  if (kind === 'monitor') {
    return { kind: 'Monitor', desc: String(task.description || tool.input?.description || ''), doing: '监视中' }
  }
  const stage = workflowStage(task.workflow_progress)
  return { kind: 'Workflow', desc: String(task.workflow_name || task.description || ''),
    doing: [stage || task.last_tool_name || '运行中', taskMeta(task)].filter(Boolean).join(' · ') }
}

// 底部要画的行：有 bgtask 事件就照原生分类；旧记录没有，就按工具名猜（Agent/Task、分叉执行的 Skill）
export function footerRows(allEvents, rawEvents, { agents, agentDone }) {
  const tasks = foldTasks(rawEvents)
  if (!tasks.length) {
    return agents.filter((agent) => !agentDone(agent)).map((agent) => {
      const { kind, desc } = agentTitle(agent)
      return { kind, desc, doing: agentDoing(agent, allEvents), agent }
    })
  }
  const tools = new Map(rawEvents.filter((ev) => ev.kind === 'tool' && ev.id).map((ev) => [ev.id, ev]))
  const toolOf = (id) => (id ? tools.get(id) : undefined)
  const rows = []
  for (const task of tasks) {
    if (task.done) continue
    const kind = taskKind(task, toolOf)
    if (kind) rows.push(taskRow(task, kind, { toolOf, allEvents }))
  }
  return rows
}

// 这个面板要画的事件、有哪些子代理、子代理做没做完
function eventsOf(entry, { isAgent, target }) {
  const rawEvents = entry.events || []
  const allEvents = isAgent
    ? [
      ...rawEvents.filter((ev) => ev.kind === 'tool' && ev.id === target.agentId)
        .map((agent) => ({
          kind: 'task',
          text: agent.name === 'Skill'
            ? '/' + String(agent.input?.skill || '') + (agent.input?.args ? ' ' + String(agent.input.args) : '')
            : String(agent.input?.description || agent.input?.prompt || agent.summary || ''),
        })),
      ...rawEvents.filter((ev) => ev.parent === target.agentId),
    ]
    : rawEvents
  const tasks = isAgent ? [] : foldTasks(rawEvents)
  const agentTools = new Set(tasks.filter((task) => task.task_type === 'local_agent'
    || task.task_type === 'in_process_teammate' || task.task_type === 'remote_agent').map((task) => task.tool_use_id))
  const agents = tasks.length
    ? allEvents.filter((ev) => ev.kind === 'tool' && agentTools.has(ev.id))
    : allEvents.filter((ev) => isAgentCall(ev, allEvents))
  const taskOfTool = new Map(tasks.filter((task) => task.tool_use_id).map((task) => [task.tool_use_id, task]))
  const agentDone = (agent) => {
    const task = taskOfTool.get(agent.id)
    if (task) return task.done
    const back = allEvents.filter((ev) => ev.kind === 'tool_result'
      && (agent.id ? ev.id === agent.id
        : /Subagent hand-back|tools-used-before-summary/.test(String(ev.text || '')))).pop()
    if (!back) return false
    // 异步起的子代理，这时只回了“已启动”，要等这一轮结束才算做完
    const launched = /Async agent launched|agentId:\s*\w+/.test(String(back.text || ''))
    if (!launched) return true
    return allEvents.some((ev) => ev.kind === 'result' && Number(ev.t || 0) >= Number(agent.t || 0))
  }
  // 后台命令、监视这类工具行：任务结束前都算在跑，不因为工具先回了“已转到后台”就显示完成
  const taskRunning = (toolId) => {
    const task = taskOfTool.get(toolId)
    return task ? !task.done : undefined
  }
  return { allEvents, agents, agentDone, taskRunning }
}

// 面板顶上的标题行 + 分隔线
function paneHeader(el, { status, name, label, isAgent, paneId, bodyColumns, onBack, summary, meta, note, extra }) {
  const { Box, Text, Button } = el
  const wide = Math.max(10, (Number(bodyColumns) || 40) - 4)
  const title = isAgent
    ? Button({
      key: 'back-' + paneId,
      plain: true,
      onPress: onBack,
      children: [Text({ children: ['← ' + labelOf(name, label)], bold: true })],
    })
    : Text({ children: [labelOf(name, label)], bold: true })
  return [
    Box({
      flexDirection: 'row',
      justifyContent: 'space-between',
      width: wide,
      children: [
        Box({
          flexDirection: 'row',
          flexShrink: 1,
          overflow: 'hidden',
          children: [Text({ children: [status.mark + ' '], color: status.color }), title,
            ...(summary ? [Text({ children: ['  ' + summary], dimColor: true, wrap: 'truncate-end' })] : [])],
        }),
        // 右边：号 · 模型 · 词元，灰字，窄了先截左边的进度，右边这段保持完整
        ...(meta ? [Box({ flexShrink: 0, paddingLeft: 2, children: [Text({ children: [meta], dimColor: true })] })] : []),
      ],
    }),
    Text({ children: ['─'.repeat(wide)], dimColor: true }),
    ...(note ? [Text({ children: [note], dimColor: true })] : []),
    // 钩子转来的“请确认”：放在标题下面，一打开就看到
    ...(extra ? [extra] : []),
  ]
}

// 详情页顶上的状态行：运行中 · Bash(make test) · 12s · ↑12.3k ↓0.4k · 旧版
function headerSummary(entry, status) {
  if (!entry || entry.mark === '✓') return ''
  const nowSec = Date.now() / 1000
  const turnStart = Number(entry.turnStarted || 0)
  const time = entry.busy && turnStart > 0 ? age(Math.max(0, nowSec - turnStart)) : ''
  return [status.word, entry.busy ? entry.doing : '', time,
    entry.stale ? '旧版（改代码前起的，要重启才有新功能）' : '']
    .filter(Boolean).join(' · ')
}

// 详情页标题右边那段：号 · 模型 · 词元（例：openai · gpt-6-astra · ↑12.3k ↓0.4k）
export function headerMeta(name, entry) {
  return [String(name || '').split('#')[0], entry?.model, entry?.used].filter(Boolean).join(' · ')
}


// 正文里要画哪些事件
function visibleEvents(allEvents, { isAgent, view, agentIds }) {
  return allEvents.filter((ev) => {
    // 后台任务事件只用来分类和算进度，不上正文
    if (ev.kind === 'bgtask') return false
    // 子代理页面里，它自己的事件就是要画的
    if (ev.parent && !isAgent) return false
    // 子代理的调用和它的回执不上正文：只在输入框下面那几行显示，和主界面一样
    if (!isAgent && ev.kind === 'tool' && (agentIds.has(ev.id) || ev.name === 'Agent' || ev.name === 'Task')) return false
    if (!isAgent && ev.kind === 'tool_result' && agentIds.has(ev.id)) return false
    if (ev.kind === 'think' && !view.think) return false
    if (ev.kind === 'tool_result' && !view.detail) return false
    return true
  })
}

// 底部：排队的话、输入框、正在跑的子代理，跟着正文最后排
function paneFooter(el, ctx) {
  const { Box, Text } = el
  const { showInput, paneWidth, pendingLines, runningRows } = ctx
  const width = Math.max(10, paneWidth)
  const pending = pendingLines.map((said) => Box({
    width,
    height: 1,
    overflow: 'hidden',
    backgroundColor: USER_BG,
    children: [Text({
      wrap: 'truncate-end',
      dimColor: true,
      children: [
        Text({ children: ['> '], color: 'subtle' }),
        Text({ children: [clip(said, 44)] }),
        Text({ children: ['  插话中'], color: 'inactive' }),
      ],
    })],
  }))
  const input = showInput ? replyBox(el, ctx) : null
  // 底下只列子代理（含分叉执行的 Skill），和主界面输入框下面一样；后台命令、监视、工作流只写一行数量
  const agentRows = runningRows.filter((row) => row.agent)
  const agents = agentRows.length ? [taskBand(el, ctx, agentRows)] : []
  const others = otherTasksText(runningRows.filter((row) => !row.agent))
  const summary = others ? [Text({ dimColor: true, wrap: 'truncate-end', children: ['  ' + others] })] : []
  // 记下这页能用 ↑↓ 走到的行：输入框在前，子代理行跟着（ui.scroll 里用）
  footerKeys[ctx.paneId] = [
    ...(input ? ['reply-' + ctx.paneId] : []),
    ...agentRows.map((row, index) => agentRowKey(ctx.paneId, index)),
  ]
  return [...pending, ...(input ? [input] : []), ...agents, ...summary]
}

// 子代理以外还在跑的：“还有 2 个后台命令 · 1 个监视”；一个都没有就是空
export function otherTasksText(rows) {
  const names = { Bash: '后台命令', Monitor: '监视', Workflow: '工作流' }
  const counts = {}
  for (const row of rows) counts[row.kind] = (counts[row.kind] || 0) + 1
  const parts = Object.entries(counts).map(([kind, count]) => count + ' 个' + (names[kind] || kind))
  return parts.length ? '还有 ' + parts.join(' · ') : ''
}

export function agentRowKey(paneId, index) {
  return ('agent-' + paneId + '-' + index).replace(/[^A-Za-z0-9_-]/g, '-')
}

// 每页底部能用 ↑↓ 走的行（输入框、子代理行），按画的顺序
const footerKeys = {}

// 方向键在底部那几行之间走：页面能滚时引擎把 ↑↓ 当滚动，走不到第二个子代理。
// 焦点在输入框或子代理行上、按的是方向键（不是滚轮）时，改成挪焦点，正文不动。
// 输入框里 ↑：往前翻一句发过的话（照主输入框）。at 是现在翻到的位置，没翻过为 undefined
export function recallStep(history, at) {
  if (!history.length) return null
  const next = at === undefined ? history.length - 1 : Math.max(0, at - 1)
  return { at: next, text: history[next] }
}

export function arrowTarget(keys, current, by) {
  const at = keys.indexOf(current)
  if (at < 0 || Math.abs(by) !== 1) return null
  const next = at + by
  if (next < 0) return null
  // 到底了就停在最后一行，不再滚正文
  return keys[Math.min(next, keys.length - 1)]
}

// 详情页底部的后台任务行，和横条、主界面子代理列表同一个画法（band-rows.js）：
// ◯ 发暗、选中亮起 ❯，不用 Button 整行反白。子代理（含分叉 Skill）能点进去看它自己的页面，
// 后台命令、监视、工作流只显示。键盘用的按钮只压在圆点那一格上（和横条一样），↑↓ 走、回车打开
function taskBand(el, ctx, runningRows) {
  const { Box, Button, Text, Client } = el
  const width = Math.max(10, ctx.paneWidth)
  const focus = String(focusOf[ctx.paneId] || '')
  const rows = runningRows.map((row, index) => {
    const key = row.agent ? agentRowKey(ctx.paneId, index) : 'task-' + ctx.paneId + '-' + index
    if (row.agent) paneRowOpen[key] = () => ctx.onOpenAgent(key, row.kind + ' · ' + row.desc, row.agent)
    return {
      name: key,
      // 照原生后台任务行：Agent(查文档) 后面跟它现在在干什么
      label: row.kind + '(' + clip(String(row.desc || ''), 30) + ')',
      named: true,
      desc: String(row.doing || ''),
      elapsed: '',
      whoText: '',
      tokenText: '',
      warnText: '',
      circleColor: '',
      selected: focus === key,
      viewed: false,
      agent: Boolean(row.agent),
    }
  })
  const { labelWidth, statusWidth } = bandLayout(rows)
  const buttons = rows.map((row, offset) => Box({
    key: 'key-' + row.name,
    position: 'absolute',
    left: 2,
    top: offset,
    children: [Button({
      key: row.name,
      plain: true,
      label: row.label,
      // 点子代理行进它自己的页面
      onPress: () => paneRowOpen[row.name]?.(),
      children: [Text({ dimColor: !row.selected, children: ['\u25EF'] })],
    })],
  }))
  const client = Client({
    key: 'tasks-' + ctx.paneId,
    module: './band-rows.js',
    width,
    props: { width, labelWidth, statusWidth, more: 0, rows },
  })
  return Box({ flexDirection: 'column', width, children: [client, ...buttons] })
}

// 详情页底部子代理行被点到时要做什么：键是行的地址（agentRowKey），值是打开它的页面
const paneRowOpen = {}

// 这个子代理现在在干什么：看它最后一条事件
function agentDoing(agent, allEvents) {
  const mine = allEvents.filter((ev) => ev.parent === agent.id)
  for (let i = mine.length - 1; i >= 0; i -= 1) {
    const ev = mine[i]
    if (ev.kind === 'tool') return ev.name + (ev.summary ? '(' + clip(ev.summary, 30) + ')' : '')
    if (ev.kind === 'think') return 'Thinking…'
    if (ev.kind === 'say') return clip(ev.text, 40)
  }
  return '启动中…'
}

// 给这个sideagent说一句的输入框
function replyBox(el, ctx) {
  const { Box, Text, Input } = el
  const { paneId, focused } = ctx
  const tint = focused ? 'text' : 'claude'
  return Box({
    width: Math.max(10, ctx.paneWidth),
    flexDirection: 'column',
    // 圆角框，用强调色描边，和主输入框区分得开
    borderStyle: 'round',
    borderColor: tint,
    children: [Box({
      flexDirection: 'row',
      paddingLeft: 1,
      paddingRight: 1,
      children: [
        Text({ children: ['❯ '], color: tint }),
        Box({
          flexGrow: 1,
          flexShrink: 1,
          children: [Input({
            key: 'reply-' + paneId,
            // 打开详情就能直接打字（和主输入框一样）；关面板用 Claude Code 自带的 Ctrl+X X，← 留给挪光标
            autoFocus: true,
            // 不要“submit”那个提示，和主输入框一致
            submitLabel: '',
            ...(ctx.clearValue ? { value: '' } : {}),
            ...(ctx.recall !== undefined ? { value: ctx.recall } : {}),
            onSubmit: (value) => {
              const said = String(value || '').trim()
              if (said) ctx.onSend(said)
            },
          })],
        }),
      ],
    })],
  })
}

// 缩进的节点统一包一层
function indentNodes(el, nodes, indent) {
  if (indent <= 0) return nodes
  return [el.Box({ flexDirection: 'column', paddingLeft: indent, children: nodes })]
}

// 连续的只读工具收成一行汇总，和主界面一样
const QUIET_TOOLS = new Set(['Read', 'Glob', 'Grep', 'LS', 'NotebookRead'])

function safeKey(prefix, key) {
  return prefix + '-' + key.replace(/[^A-Za-z0-9_-]/g, '-')
}

function toggleKey(groups, key, repaint) {
  if (groups.has(key)) groups.delete(key)
  else groups.add(key)
  repaint()
}

function quietLabel(names) {
  const reads = names.filter((name) => name === 'Read' || name === 'NotebookRead').length
  const searched = names.filter((name) => name === 'Grep' || name === 'Glob').length
  const listed = names.filter((name) => name === 'LS').length
  return [
    reads && 'Read ' + reads,
    searched && 'Searched ' + searched,
    listed && 'Listed ' + listed,
  ].filter(Boolean).join(' · ')
}

// 这条工具调用还在不在跑：有后台任务就看任务；子代理看它回没回；其余看后面有没有输出
function toolRunning(ev, following, ctx) {
  const inTask = ctx.taskRunning?.(ev.id)
  if (inTask !== undefined) return inTask
  if (ev.name === 'Agent' || ev.name === 'Task') return !ctx.agentDone(ev)
  return !(following && (following.kind === 'tool_result' || following.kind === 'result')) && !ev.parent
}

// 画一个工具调用；连续的几个收成一组，点开才展开
function renderTool(ev, ctx) {
  const { events, index, indent, el, width, groups } = ctx
  const { Box, Text, Button } = el
  let end = index
  const run = []
  while (end < events.length && (events[end].kind === 'tool' || events[end].kind === 'tool_result')) {
    const item = events[end]
    // 子代理的调用和它内部的动作不参与折叠，单独显示
    if (item.kind === 'tool' && (item.parent || item.name === 'Agent' || item.name === 'Task')) break
    if (item.kind === 'tool') run.push(item.name)
    end += 1
  }
  const single = () => {
    const tool = TOOL_VIEWS[ev.name] || ctx.genericTool
    const following = events[index + 1]
    const isRunning = toolRunning(ev, following, ctx)
    const detailKey = safeKey('detail', ctx.key)
    const opts = {
      open: groups.has(detailKey),
      key: detailKey,
      onToggle: () => toggleKey(groups, detailKey, ctx.repaint),
    }
    const out = tool(el, { ...ev, input: ev.input || {} }, width - indent, { ...opts, running: isRunning })
    return indentNodes(el, Array.isArray(out) ? out : (out?.children || [out]), indent)
  }
  if (run.length < 2) return { nodes: single() }

  const open = groups.has(ctx.key)
  const label = run.every((name) => QUIET_TOOLS.has(name))
    ? quietLabel(run)
    : 'Ran ' + run.length + ' tools'
  const groupKey = safeKey('group', ctx.key)
  const fold = Box({
    flexDirection: 'row',
    children: [
      ctx.iconBox('●', undefined),
      Button({
        key: open ? groupKey + '-fold' : groupKey,
        plain: true,
        label,
        onPress: () => toggleKey(groups, ctx.key, ctx.repaint),
        children: [Text({ children: [open ? label + ' · 收起' : label], dimColor: true })],
      }),
    ],
  })
  // 收起：整组一行，跳过组内其余事件；展开：先一行“收起”，再逐个往下画
  return open ? { nodes: [fold, ...single()] } : { nodes: [fold], skipTo: end - 1 }
}

// 工具名 -> 怎么画；没有的就用通用那种（键必须和事件里的工具名一致）
const TOOL_VIEWS = {
  Bash: bashTool,
  Edit: editTool,
  MultiEdit: editTool,
  Read: readTool,
  Write: writeTool,
  Task: taskTool,
  Agent: taskTool,
}

// 状态行写什么：按事件类型查表
const DOING = {
  tool: (ev) => 'Running ' + ev.name + (ev.summary ? '(' + clip(String(ev.summary), 60) + ')' : '') + '…',
  think: () => 'Thinking…',
}

function doingOf(last) {
  return (last && (DOING[last.kind] || (() => 'Working…'))(last)) || 'Working…'
}

// 最后一条回复的一行预览：跳过表格行和代码围栏，剩下的原样交给行去截断
function previewLine(text) {
  const line = String(text || '')
    .split('\n')
    .map((one) => one.trim())
    .find((one) => one && !one.startsWith('|') && !one.startsWith('```'))
  return clip(line || '', TEXT_LIMIT)
}

// 详情页顶上的“请确认”：写清要执行什么，两个按钮。热键 y/n，别的键不会误按
export function permissionBox($, el, name, permission) {
  const { Box, Text, Button } = el
  const press = (behavior) => async () => {
    try {
      await $.process.run(['cca', 'sideagent', 'permission-answer', name, behavior, String(permission.id || '')],
        { timeoutMs: 10000 })
    } catch {
      // 回答失败（请求已经超时或被别处答了）：下一次刷新会更新
    }
    answered.add(String(permission.id || ''))
    $.ui.invalidate('ui.render')
  }
  if (answered.has(String(permission.id || ''))) {
    return Box({ children: [Text({ children: ['已回答，等它接着跑…'], dimColor: true })] })
  }
  return Box({
    key: 'perm-' + String(permission.id || ''),
    flexDirection: 'column',
    borderStyle: 'round',
    borderColor: 'warning',
    paddingLeft: 1,
    paddingRight: 1,
    children: [
      Text({ children: ['等你确认：' + String(permission.summary || permission.tool || '')], color: 'warning', bold: true }),
      Text({ children: [clip(String(permission.input || ''), 300)], dimColor: true, wrap: 'wrap' }),
      ...(Number(permission.count || 1) > 1 ? [Text({ children: ['后面还有 ' + (Number(permission.count) - 1) + ' 条等确认'], dimColor: true })] : []),
      Box({
        flexDirection: 'row',
        children: [
          Button({ key: 'perm-allow', label: '允许 (y)', hotkey: 'y', onPress: press('allow'),
            children: [Text({ children: ['[ 允许 y ]'], color: 'success' })] }),
          Text({ children: ['  '] }),
          Button({ key: 'perm-deny', label: '拒绝 (n)', hotkey: 'n', onPress: press('deny'),
            children: [Text({ children: ['[ 拒绝 n ]'], color: 'error' })] }),
        ],
      }),
    ],
  })
}

// 详情页顶上的那一块：有“请确认”先画它；接管的会话画“拿掉”按钮；别的不画
function paneExtra($, el, { name, entry, paneId }) {
  if (entry.permission) return permissionBox($, el, name, entry.permission)
  if (entry.attached) return releaseBox($, el, name, paneId)
  return null
}

// 接管的会话：详情页顶上一个“拿掉”按钮，只从本会话的面板拿掉，会话本身不停（热键 x）
export function releaseBox($, el, name, paneId) {
  const { Box, Text, Button } = el
  const press = async () => {
    try {
      await $.process.run(['cca', 'sideagent', 'release', name, mySession], { timeoutMs: 10000 })
    } catch {
      // 拿不掉（已经被拿掉了）：下一次刷新会更新
    }
    delete byName[name]
    await closePane($, paneId)
    $.ui.invalidate('ui.render')
  }
  return Box({
    flexDirection: 'row',
    children: [
      Button({ key: 'release-' + name, label: '从面板拿掉 (x)', hotkey: 'x', onPress: press,
        children: [Text({ children: ['[ 从面板拿掉 x ]'], dimColor: true })] }),
      Text({ children: ['  会话本身不停'], dimColor: true }),
    ],
  })
}

// 已经按过的请求编号：同步线程还没把它清掉前，别再画按钮
const answered = new Set()
// 已经弹过提示的请求编号：每条只提示一次
const toasted = new Set()

// 状态的样子：图标、颜色、词
// 接管的原生后台会话在等人确认
const BLOCKED_LOOK = { mark: '!', color: 'warning', word: '等确认' }

// 卡在哪：最后一个还没有结果的工具调用；找不到就写登记表里的原因
export function waitingWhat(events, state, permission = null) {
  if (permission) return '等你确认：' + String(permission.summary || permission.tool || '') + '（点开按 y 允许 / n 拒绝）'
  const done = new Set(events.filter((ev) => ev.kind === 'tool_result').map((ev) => ev.id))
  const tool = events.filter((ev) => ev.kind === 'tool' && !done.has(ev.id)).pop()
  const what = tool ? tool.name + (tool.summary ? '(' + clip(tool.summary, 40) + ')' : '') : ''
  return '等确认：' + (what || String(state.waiting_for || '')) + '（claude attach 接进去点确认）'
}

function look(entry) {
  if (!entry) return { mark: '·', color: 'inactive', word: '结束', dim: true }
  if (entry.blocked) return BLOCKED_LOOK
  if (entry.busy) return { mark: '●', color: 'success', word: '运行中' }
  if (entry.alive) return { mark: '·', color: 'inactive', word: '空闲' }
  if (entry.running) return { mark: '●', color: 'success', word: '运行中' }
  return { mark: '✓', color: undefined, word: '完成' }
}

// 宽度分三档：窄了少画几列
function tierFor(columns) {
  if (columns >= BAND_WIDE) return 2
  if (columns >= BAND_MID) return 1
  return 0
}

// MCP 服务的同步线程每 2 秒写一次心跳；10 秒内写过就说明它在同步，面板不用自己起进程
export const BEAT_STALE_SECONDS = 10

async function syncBeating($) {
  try {
    const home = await $.env.get('HOME')
    const beat = Number(await $.fs.read(home + '/.cca/sideagents/.sync-' + mySession + '.beat'))
    return beat > 0 && Date.now() / 1000 - beat < BEAT_STALE_SECONDS
  } catch {
    return false
  }
}

// 本会话现在的编号。会话中途接上别的对话（/resume、换号重开后接着原对话）时编号会变，
// 所以不能只在启动时记一次：启动时记下的是新进程临时的编号，接上 de9cdb1b 以后就认不出它派的 sideagent
async function readSessionId($) {
  try {
    return String(await $.session.id())
  } catch {
    // 测试环境里没有 $.session.id，退回环境变量
    try {
      return String(await $.env.get('CLAUDE_CODE_SESSION_ID') || '')
    } catch {
      return ''
    }
  }
}

async function refresh($) {
  let fresh = { rows: [], details: [], byName: {} }
  const nowSession = await readSessionId($)
  const switched = Boolean(nowSession) && nowSession !== mySession
  if (switched) {
    await trace($, 'session changed ' + String(mySession).slice(0, 8) + ' -> ' + nowSession.slice(0, 8))
    mySession = nowSession
  }
  if (hasExternal && mySession && !(await syncBeating($))) {
    // 进程不归我们管的（接管的原生会话）平时由本会话的 MCP 服务在后台同步；
    // MCP 服务不在（没装、挂了）才自己起一次 cca 兜底（插件读文件只能整份读，要它按位置增量读）
    try {
      await $.process.run(['cca', 'sideagent', 'bg-sync', mySession], { timeoutMs: 15000 })
    } catch {
      // 同步失败就显示上一次的
    }
  }
  try {
    fresh = await collect($)
    // 有没认出来的派活会话：查一下它们现在最新的是哪一份，认出来了就再收一遍
    if (await refreshLatest($)) fresh = await collect($)
  } catch (error) {
    await trace($, 'refresh failed: ' + String(error))
  }
  await showNotices($)
  await closeGonePanes($, fresh)
  // 刚接上的对话里早就做完的 sideagent 不算“刚做完”，不再叫醒主对话（和启动时一样）
  if (switched) for (const row of fresh.rows) if (!row.running) notified.add(notifyKey(row.name, fresh.byName[row.name]))
  wakeForFinished($, fresh)
  const changed = !(same(fresh.rows, rows) && same(fresh.details, allDetails)
    && same(fresh.byName, byName))
  rows = fresh.rows
  allDetails = fresh.details
  byName = fresh.byName
  if (changed) $.ui.invalidate('ui.render')
  // 开着的详情页、子代理页、输出页：人没往上翻（停在最底下）就一直跟着最新的
  for (const [id, follow] of Object.entries(followPane)) {
    if (!follow) continue
    $.clock.after(50, () => { $.ui.scroll({ in: id, to: 'end' }).catch(() => {}) })
  }
  return fresh
}

// sideagent已经没了：把它开着的面板关掉，不留空白页
async function closeGonePanes($, fresh) {
  try {
    for (const pane of (await $.ui.panes()) || []) {
      const id = String(pane?.id || '')
      const name = paneNames[id] || agentPanes[id]?.name
      if (name && !fresh.byName[name]) await $.ui.close({ id })
    }
  } catch {
    // 关不掉就算了
  }
}

// 有sideagent做完了就叫醒主代理一轮；结果已经当场交回的、常驻的不叫
// 叫醒用的记号：接管的原生后台会话一直在，每做完一轮叫一次，所以带上这一轮的结束时刻
export function notifyKey(name, entry) {
  return entry?.external ? name + '@' + finishedAt(entry) : name
}

// 看门狗换号的通知（~/.cca/sideagents/notices.jsonl，一行一条 {at, text}）：面板启动以后新来的，弹一条提示。
// 按读到第几行记，不按时间比：时间里的时区写法不统一（-08:00 / +08:00），按字符串比会乱
let noticeLines = -1
async function showNotices($) {
  let text = ''
  try {
    text = String(await $.fs.read((await $.env.get('HOME')) + '/.cca/sideagents/notices.jsonl'))
  } catch {
    text = ''
  }
  const lines = text.split('\n').filter(Boolean)
  // 第一次读：只记下读到哪，开场不把旧通知全弹一遍；文件被清空过就从头记
  if (noticeLines < 0 || lines.length < noticeLines) {
    noticeLines = lines.length
    return
  }
  for (const line of lines.slice(noticeLines)) {
    try {
      await $.ui.toast(String(JSON.parse(line).text || ''))
    } catch {
      // 坏行或者没有 toast 就跳过
    }
  }
  noticeLines = lines.length
}

function wakeForFinished($, fresh) {
  for (const row of fresh.rows) {
    const entry = fresh.byName[row.name] || {}
    const key = notifyKey(row.name, entry)
    if (row.running || notified.has(key)) continue
    notified.add(key)
    // 子代理派的：回报已经转给那个子代理，不叫醒主对话
    if (entry.callerAgent) continue
    if (!entry.notify || entry.deliveredToCaller) continue
    if (finishedAt(entry) < bootAt) continue
    const result = (entry.events || []).filter((ev) => ev.kind === 'result').pop()
    const text = `sideagent ${row.name} 完成${row.meta ? ' · ' + row.meta : ''}`
      + (result?.text ? `\n${result.text}` : '')
    try {
      $.prompt.submit({ text }).catch(() => {})
    } catch {
      // 提交失败不影响面板
    }
  }
  if (notified.size > NOTIFY_MAX) notified.clear()
}

// 给sideagent说一句：只发不等。和主界面一样是插话：它在忙就送进这一轮，
// 下一个工具调用结束它就能看到；它闲着就当新的一轮
async function tellPeer($, peerName, text) {
  try {
    await $.process.run(['cca', 'sideagent', 'tell', '--interject', peerName, text], { timeoutMs: 10000 })
  } catch {
    // 发不出去就算了，下次再试
  }
}

// 开某个sideagent的详情；别的sideagent页、子代理页都关掉，一次只看一页
async function openPeerPane($, peerName) {
  const id = 'peer-' + String(peerName).replace(/[^A-Za-z0-9_-]/g, '-')
  try {
    for (const pane of (await $.ui.panes()) || []) {
      const other = String(pane?.id || '')
      if ((other.startsWith('peer-') || other.startsWith('agent-')) && other !== id) {
        await $.ui.close({ id: other })
      }
    }
  } catch {
    // 关不掉就算了
  }
  paneNames[id] = peerName
  viewedPeer = peerName
  // 开面板就试着把键盘拿过来：主输入框空着、没在跑的时候才能拿到
  try {
    await $.ui.open({ id, title: byName[peerName]?.label || labelOf(peerName), focus: true,
      backgroundColor: 'transparent' })
  } catch (error) {
    // 带 focus / 背景色被拒时退回最普通的打开方式，至少让面板出来
    try {
      await $.ui.open({ id, title: byName[peerName]?.label || labelOf(peerName) })
    } catch (again) {
      // 写进文件，方便事后看是哪一步被拒
      try {
        const home = await $.env.get('HOME')
        await $.fs.write(home + '/.cca/sideagents/panel-error.log',
          new Date().toISOString() + ' ' + id + ' ' + String(again || error) + '\n')
      } catch {
        // 连日志都写不了就算了
      }
    }
  }
  // 一打开就停在最新一条
  delete paneSig[id]
  followPane[id] = true
  try {
    await $.ui.scroll({ in: id, to: 'end' })
  } catch {
    // 还没画出来就等下一秒
  }
  try {
    await $.ui.scroll({ in: id, to: 'end' })
  } catch {
    // 还没画出来就等下一秒
  }
}

// 这个sideagent是什么时候做完的（秒）；没有结果事件就按 0
function finishedAt(entry) {
  const result = ((entry?.events) || []).filter((ev) => ev.kind === 'result').pop()
  if (!result) return 0
  // 新事件带绝对时间；老数据只能用“进程开始 + 这一轮用的秒数”估
  if (typeof result.at === 'number') return result.at
  if (typeof result.t !== 'number') return 0
  const started = Number(entry.started || 0)
  return started > 0 ? started + result.t : Date.now() / 1000
}

function same(a, b) {
  return JSON.stringify(a) === JSON.stringify(b)
}


// 面板排版用的小件：图标列、宽度、正文、一行
function paneStyles(el, bodyColumns) {
  const { Box, Text } = el
  // 同一列：符号固定 2 格，后面的字从同一列开始
  const iconBox = (mark, color) => Box({
    width: 2,
    flexShrink: 0,
    children: [Text({ children: [mark], color })],
  })
  // 左右各留 2 格，和主界面一样，不贴着面板边
  const paneWidth = Math.max(12, (Number(bodyColumns) || 40) - 4)
  const bodyWidth = Math.max(10, paneWidth - 2)
  const body = (node) => Box({ flexGrow: 1, flexShrink: 1, width: bodyWidth, children: [node] })
  // 图标和正文在同一个文本流里，折行不会把图标单独挤走
  const line = (mark, color, label, opts = {}) => Box({
    width: Math.max(10, paneWidth - (opts.level || 0)),
    children: [Text({
      wrap: 'wrap',
      dimColor: opts.dim,
      children: [
        Text({ children: [(mark || '') + ' '], color }),
        Text({ children: [String(label)], italic: opts.italic }),
      ],
    })],
  })
  return { iconBox, paneWidth, bodyWidth, body, line }
}

// 顶上那句“正在干什么”、正在生成的文字、以及什么都没有时的兜底
function statusNodes(el, ctx) {
  const { Box, Text } = el
  const { running, events, started, live, lines, line, iconBox, body } = ctx
  const out = []
  if (running) {
    const last = events.length ? events[events.length - 1] : null
    const secs = Math.max(0, Math.round(Date.now() / 1000 - (started || Date.now() / 1000)))
    out.push(line('✻', 'claude', doingOf(last) + ' ' + secs + 's', { dim: true }))
  }
  if (live) {
    if (out.length) out.push(Text({ children: [''] }))
    out.push(Box({
      flexDirection: 'row',
      children: [
        iconBox('…', 'inactive'),
        body(Text({ children: [clip(live.slice(-160), 160)], dimColor: true, italic: true })),
      ],
    }))
  }
  if (!out.length && !lines.length) out.push(Text({ children: ['还没有输出'], dimColor: true }))
  return out
}

// 正文：每种事件怎么画查表，连续的几个工具调用收成一组
function eventNodes(el, ctx) {
  const { Box, Text, Markdown } = el
  const { events, children, name, paneWidth, bodyWidth, line, iconBox, body } = ctx
  // 每种事件怎么画，查表；加新类型就加一行
  const views = {
    error: (ev, ctx) => [line('✗', 'error', clip(ev.text, 200), { level: ctx.indent })],
    task: (ev) => [Box({
      width: Math.max(10, paneWidth),
      backgroundColor: USER_BG,
      children: [Text({
        wrap: 'wrap',
        children: [
          Text({ children: ['> '], color: 'subtle' }),
          Text({ children: [ev.text] }),
        ],
      })],
    })],
    think: (ev, ctx) => {
      const after = ctx.events.slice(ctx.index + 1).find((item) => typeof item.t === 'number')
      const secs = after && typeof ev.t === 'number' ? Math.max(1, Math.round(after.t - ev.t)) : null
      return [Box({
        width: Math.max(10, paneWidth),
        paddingLeft: 2 + ctx.indent,
        children: [Text({
          wrap: 'wrap',
          color: 'subtle',
          italic: true,
          children: [secs ? 'Thought for ' + secs + 's' : 'Thinking…'],
        })],
      })]
    },
    say: (ev) => [Box({
      flexDirection: 'row',
      children: [iconBox('●', undefined), body(Markdown({ text: fitTables(ev.text, bodyWidth) }))],
    })],
    tool_result: (ev, ctx) => {
      const key = safeKey('out', ctx.key)
      return indentNodes(ctx.el, toolOutput(ctx.el, ev.text, paneWidth - ctx.indent, {
        open: ctx.groups.has(key),
        key,
        onToggle: () => toggleKey(ctx.groups, key, ctx.repaint),
      }), ctx.indent)
    },
    result: (ev) => {
      // 和子代理一样只留耗时和一个词元数；累计的 ↑↓ 只留在状态和结果字段里，界面不显示
      const used = ev.token_count
        ? '↓ ' + tokens(ev.token_count)
        : tokens((ev.tokens_in || 0) + (ev.tokens_out || 0))
      const meta = [ev.seconds ? elapsedText(ev.seconds * 1000) : '', used].filter(Boolean).join(' · ')
      return [Box({
        flexDirection: 'row',
        children: [iconBox('✻', 'subtle'), body(Text({ children: [meta], color: 'subtle' }))],
      })]
    },
    tool: (ev, ctx) => renderTool(ev, ctx),
  }

  for (let i = 0; i < events.length; i += 1) {
    const ev = events[i]
    const indent = ev.parent ? 2 : 0
    const prev = events[i - 1]
    // 每个新块上方空一行；只有工具调用和它自己的输出之间不空
    const noGap = prev?.kind === 'tool' && ev.kind === 'tool_result'
    if (i > 0 && !noGap) children.push(Text({ children: [' '] }))
    const view = views[ev.kind]
    if (!view) continue
    const out = view(ev, {
      index: i,
      events,
      indent,
      el,
      width: paneWidth,
      groups: openGroups,
      key: name + ':' + i,
      iconBox,
      genericTool: ctx.genericTool,
      agentDone: ctx.agentDone,
      taskRunning: ctx.taskRunning,
      repaint: ctx.repaint,
    })
    const nodes = Array.isArray(out) ? out : (out?.nodes || [])
    children.push(...nodes)
    if (out?.skipTo !== undefined) i = out.skipTo
  }

}

// 在面板里说了一句：先挂出来，再发给sideagent
function sendFromPane($, paneId, name, said) {
  const list = sentByPane[paneId] || []
  list.push(said)
  sentByPane[paneId] = list.slice(-20)
  historyByPane[paneId] = [...(historyByPane[paneId] || []), said].slice(-50)
  delete recallAt[paneId]
  clearInput[paneId] = true
  followPane[paneId] = true
  $.ui.invalidate('ui.render')
  $.ui.scroll({ in: paneId, to: 'end' }).catch(() => {})
  void tellPeer($, name, said)
}

// 刚发出去、sideagent还没接住的话；sideagent每开一轮就挪走最老的一条
function pendingSay(paneId, allEvents, entry, isAgent) {
  if (isAgent) return []
  const said = sentByPane[paneId] || []
  const rounds = allEvents.filter((ev) => ev.kind === 'task').length
  const seen = sentSeen[paneId] || 0
  if (rounds > seen) {
    said.splice(0, Math.min(rounds - seen, said.length))
    sentSeen[paneId] = rounds
  }
  sentByPane[paneId] = said
  // 只在“sideagent正忙、这话在排队”时画出来；闲着就等它写进正文，不闪两下
  return entry.running ? said.slice() : []
}

// 点一行子代理就进它自己的页面
async function openAgentPane($, { id, desc, agent, name }) {
  agentPanes[id] = { name, agentId: agent.id }
  try {
    for (const pane of (await $.ui.panes()) || []) {
      const other = String(pane?.id || '')
      if ((other.startsWith('peer-') || other.startsWith('agent-')) && other !== id) {
        await $.ui.close({ id: other })
      }
    }
  } catch {
    // 关不掉就算了
  }
  const opened = await $.ui.open({ id, title: clip(desc, 40), backgroundColor: 'transparent', focus: true })
  await trace($, 'agent pane open ' + id + ' ' + JSON.stringify(opened))
}

// 点开某个sideagent（或它的子代理）那一页的全部内容
async function renderPeerPane($, e, next) {
    const paneId = String(e.requestId || '')
    const isAgent = paneId.startsWith('agent-')
    if (!isAgent && !paneId.startsWith('peer-')) return next(e)
    const target = isAgent ? (agentPanes[paneId] || {}) : { name: paneNames[paneId] }
    const name = target.name
    const entry = byName[name]
    const { Box, Text } = $.ui.resolve(e)
    if (!entry) {
      return Box({ flexDirection: 'column', children: [Text({ children: ['这个sideagent已经结束'], dimColor: true })] })
    }
    if (closedForView($, e, entry, paneId)) return Box({ flexDirection: 'column', children: [Text({ children: [''] })] })
    // 照着主界面的样子画：任务、说的话、工具调用、工具输出
    const status = look(entry)
    const children = paneHeader($.ui.resolve(e), {
      status,
      name,
      label: entry.label,
      isAgent,
      paneId,
      bodyColumns: e.props.bodyColumns,
      onBack: () => openPeerPane($, name),
      summary: isAgent ? '' : headerSummary(entry, status), meta: isAgent ? '' : headerMeta(name, entry),
      extra: isAgent ? null : paneExtra($, $.ui.resolve(e), { name, entry, paneId }),
    })
    const { iconBox, paneWidth, bodyWidth, body, line } = paneStyles($.ui.resolve(e), e.props.bodyColumns)

    // 子代理发出来的事件都带 parent；点进来是子代理页时只留它自己的
    const { allEvents, agents, agentDone, taskRunning } = eventsOf(entry, { isAgent, target })
    const agentIds = new Set(agents.map((agent) => agent.id).filter(Boolean))
    const live = isAgent ? '' : entry.live
    const running = isAgent
      ? !allEvents.some((ev) => ev.kind === 'tool_result' && ev.id === target.agentId)
      : entry.running
    const events = visibleEvents(allEvents, { isAgent, view, agentIds })
    eventNodes($.ui.resolve(e), {
      events,
      children,
      name,
      paneWidth,
      bodyWidth,
      line,
      iconBox,
      body,
      agentDone,
      taskRunning,
      genericTool,
      repaint: () => $.ui.invalidate('ui.render'),
    })
    const pendingLines = pendingSay(paneId, allEvents, entry, isAgent)
    children.push(...statusNodes($.ui.resolve(e), {
      running,
      events,
      started: entry.started,
      live,
      lines: entry.lines,
      line,
      iconBox,
      body,
    }))
    // 只用还在跑的子代理；都跑完了这块就不显示，和主界面一样
    const runningRows = footerRows(allEvents, isAgent ? [] : (entry.events || []), { agents, agentDone })
    // sideagent详情底下留一个输入框，子代理页面没有
    const showInput = !isAgent
    if (!runningRows.length && !showInput) return Box({ flexDirection: 'column', children })

    const footer = paneFooter($.ui.resolve(e), {
      paneId,
      name,
      showInput,
      focused: focusOf[paneId] === ('reply-' + paneId),
      paneWidth,
      pendingLines,
      runningRows,
      allEvents,
      clearValue: Boolean(clearInput[paneId]), recall: recallValue[paneId],
      onSend: (said) => sendFromPane($, paneId, name, said),
      onOpenAgent: (id, desc, agent) => openAgentPane($, { id, desc, agent, name }),
    })
    if (clearInput[paneId]) delete clearInput[paneId]
    delete recallValue[paneId]
    return Box({
      flexDirection: 'column',
      paddingLeft: 2,
      paddingRight: 2,
      children: [...children, ...footer],
    })
}

// 输入框上方：一个sideagent一行。固定列：图标 1 格、名字、动作、右边耗时和 token
// 同一会话里插件可能被加载两份（用户设置和项目设置指向同一个文件）。
// 用租约让只有一份画横条：租约 10 秒有效，持有者每秒续期，别人就安静。
const INSTANCE = 'panel-' + Math.random().toString(36).slice(2, 8)

async function ownsBand($) {
  try {
    const home = await $.env.get('HOME')
    const path = home + '/.cca/sideagents/panel-lease'
    const raw = String(await $.fs.read(path)).trim()
    const [owner, stamp] = raw.split(' ')
    const age = Date.now() / 1000 - Number(stamp || 0)
    if (owner && owner !== INSTANCE && age < LEASE_SECONDS) return false
    await $.fs.write(path, INSTANCE + ' ' + Math.floor(Date.now() / 1000))
    return true
  } catch {
    // 读写不了就当自己持有，至少不会没界面
    return true
  }
}

// 调试用：每一步写一行，001 那边到底走到哪儿一目了然
async function trace($, text) {
  try {
    const home = await $.env.get('HOME')
    const path = home + '/.cca/sideagents/panel-debug.log'
    const old = await $.fs.read(path).catch(() => '')
    const lines = String(old || '').split('\n').filter(Boolean).slice(-200)
    lines.push(new Date().toISOString() + ' ' + INSTANCE + ' ' + text)
    await $.fs.write(path, lines.join('\n') + '\n')
  } catch {
    // 写不了就算了
  }
}

async function renderBand($, e, next) {
    // 只画“当前在看的界面”派的：看主对话时只显示主对话派的，看子代理 X 的记录时只显示 X 派的
    const list = rowsInView(rows, e.props?.view)
    if (!list.length) {
      return next(e)
    }
    const owns = await ownsBand($)
    if (!owns) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const columns = Number(e.props.bodyColumns) || 80
    // 行宽照子代理列表：所在列的宽度减 2（mT=2）
    const width = Math.max(20, columns + ENGINE_RESERVED - ROW_RIGHT_PADDING)
    // 终端：照子代理那一行画（Client 画字，鼠标移上去不反白）；别的界面还用老的 Button 画法
    if (e.surface === 'terminal' && $.ui.resolve(e).Client) return clientBand($, e, width, list)
    const tier = tierFor(width)
    const marks = list.map((row) => bandMark(row, sameKey(bandKey(row.name), bandFocus)))
    const rowNodes = list.map((row, index) => {
      const selected = sameKey(bandKey(row.name), bandFocus)
      return Box({
        key: 'row-' + row.name,
        flexDirection: 'row',
        width,
        paddingLeft: 2,
        justifyContent: 'space-between',
        children: [
          Box({
            flexDirection: 'row',
            flexShrink: 1,
            overflow: 'hidden',
            children: [
              Box({ width: 2, flexShrink: 0, children: [Text({ children: [marks[index].mark], color: marks[index].color })] }),
              Button({
                key: 'peer-' + row.name,
                // 不给数字热键：容易误触。横条拿到键盘后 Tab/方向键选行，回车打开
                plain: true,
                ...(index === 0 ? { autoFocus: true } : {}),
                label: row.name,
                onPress: async () => {
                  // 点过的那一行就是选中：焦点事件万一没到，也照样画白点
                  bandFocus = 'peer-' + row.name
                  await trace($, 'click ' + row.name)
                  try {
                    await openPeerPane($, row.name)
                    await trace($, 'opened ' + row.name)
                  } catch (error) {
                    await trace($, 'open failed ' + row.name + ' ' + String(error))
                  }
                },
                children: [
                  // 选中的名字是白字，没选中的发灰
                  Text({ children: [row.label || row.name], bold: selected, dimColor: !selected }),
                  ...(tier >= 1 ? [Text({ children: ['  ' + row.text], dimColor: true, wrap: 'truncate-end' })] : []),
                ],
              }),
            ],
          }),
          ...(tier >= 1
            ? [Box({
                flexShrink: 1, justifyContent: 'flex-end',
                children: [Text({ children: [row.meta], dimColor: true, tabular: true, wrap: 'truncate-end' })],
              })]
            : []),
        ],
      })
    })
    return Box({
      flexDirection: 'column',
      // 和上面那行提示（Tip…）隔开一行，不然挤在一起
      children: [Text({ children: [' '] }), ...rowNodes],
    })
}

// 某个界面该显示哪些行：行的 callerAgent（派活的子代理编号）等于这个界面在看的代理；
// 主对话的界面（没有 agentId）只显示主对话派的
export function rowsInView(list, view) {
  const agent = String(view?.agentId || '')
  return list.filter((row) => String(row.callerAgent || '') === agent)
}

// 照 Claude Code 子代理列表（Xz/Gle）画横条：
// 名字列宽 = 最长名字（短的才需要补空格对齐），最多 28 格；右侧列宽 = 各行右侧文字里最宽的那个（中文按 2 格），
// 所以每一行右侧的 token 那一段右端都落在同一列。
export function bandLayout(list) {
  // 不再额外垫 4 格：只有一个短名字（比如 ds）时，后面就是固定的 2 格，和子代理列表一样
  const labelWidth = Math.min(28, Math.max(0, ...list.map((row) => displayWidth(row.label || row.name))))
  const statusWidth = Math.max(0, ...list.map((row) => displayWidth(
    [statusText(row), row.warnText].filter(Boolean).join(' \u00B7 '))))
  return { labelWidth, statusWidth }
}

function clientBand($, e, width, rows) {
  const { Box, Button, Text, Client } = $.ui.resolve(e)
  const { labelWidth, statusWidth } = bandLayout(rows)
  // 照子代理列表：最多画 5 行，选中的那一行一定在里面，下面多的写 “↓ N more”
  const picked = rows.findIndex((row) => sameKey(bandKey(row.name), bandFocus))
  const first = Math.max(0, Math.min(rows.length - BAND_WINDOW, picked - BAND_WINDOW + 1))
  const shown = rows.slice(first, first + BAND_WINDOW)
  const below = rows.length - first - shown.length
  // 键盘用的按钮压在每一行圆点那一格上：Tab/方向键选行、回车打开；只占一格，指针下反白也只这一格
  const buttons = shown.map((row, offset) => Box({
    key: 'key-' + bandKey(row.name),
    position: 'absolute',
    left: 2,
    top: offset,
    children: [Button({
      key: 'peer-' + row.name,
      plain: true,
      label: row.name,
      ...(first + offset === 0 ? { autoFocus: true } : {}),
      onPress: async () => {
        bandFocus = 'peer-' + row.name
        await openPeerPane($, row.name)
      },
      children: [Text({
        color: row.circleColor || undefined,
        dimColor: !row.circleColor && !sameKey(bandKey(row.name), bandFocus),
        children: [circleOf(row.attached, viewedPeer === row.name)],
      })],
    })],
  }))
  const client = Client({
    key: 'band',
    module: './band-rows.js',
    width,
    props: {
      width,
      labelWidth,
      statusWidth,
      more: below,
      rows: shown.map((row) => ({
        name: row.name,
        label: row.label || row.name,
        named: Boolean(row.named),
        desc: row.desc || row.text || '',
        elapsed: row.elapsed || '',
        whoText: row.whoText || '',
        tokenText: row.tokenText || '',
        warnText: row.warnText || '',
        circleColor: row.circleColor || '',
        attached: Boolean(row.attached),
        selected: sameKey(bandKey(row.name), bandFocus),
        viewed: viewedPeer === row.name,
      })),
    },
  })
  // 子代理列表上面空一行（marginTop 1），这里一样
  return Box({ flexDirection: 'column', marginTop: 1, width, children: [client, ...buttons] })
}

// 焦点落到输入框上就把它画亮，和主界面一样
async function onFocus($, e, next) {
  const id = String(e.requestId || '')
  const element = String(e.element || '')
  await trace($, 'focus comp=' + String(e.component) + ' id=' + id
    + ' el=' + element + ' origin=' + String(e.origin?.kind))
  if (e.component === 'AbovePrompt') {
    // 横条：记住键盘焦点落在哪一行，重画时那一行亮起 ❯ 并变亮（和子代理选中一样）；
    // 焦点整个离开横条（element 为空）就回到没选中
    if (element !== bandFocus) {
      bandFocus = element
      $.ui.invalidate('ui.render')
    }
  } else if (id.startsWith('peer-') || id.startsWith('agent-')) {
    const reply = 'reply-' + id
    const rows = (footerKeys[id] || []).filter((key) => key !== reply)
    const current = String(focusOf[id] || '')
    // 输入框里按 ↓：引擎会把焦点挪到正文里的按钮（折叠的工具组之类），看着像乱跳。
    // 改成挪到下面的子代理行；没有子代理行就留在输入框（日志里实测：el=group-… 来回跳）
    if (e.origin?.kind === 'person' && current === reply && element && element !== reply && !rows.includes(element)) {
      if (!rows.length) return {}
      focusOf[id] = rows[0]
      $.ui.invalidate('ui.render')
      return next({ ...e, element: rows[0] })
    }
    // 输入框的自动焦点把焦点拉回去：人刚把焦点放在子代理行上，就不让它抢
    if (e.origin?.kind === 'plugin' && element === reply && rows.includes(current)) return {}
    focusOf[id] = element
    $.ui.invalidate('ui.render')
  } else if (hasList(id)) {
    onListFocus($, id, element)
  }
  return next(e)
}

async function onBandMessage($, e, next) {
  const data = e.data && typeof e.data === 'object' ? e.data : {}
  if (!String(e.module || '').endsWith('band-rows.js')) return next(e)
  await trace($, 'band click ' + JSON.stringify(data))
  // 详情页底部的子代理行（同一个画法）：点一下打开它自己的页面
  if (data.open && paneRowOpen[data.open]) {
    await paneRowOpen[data.open]()
    return {}
  }
  if (data.open && byName[data.open]) {
    bandFocus = 'peer-' + data.open
    await openPeerPane($, String(data.open))
  }
  return {}
}

// 详情只在派活的那个界面里开着：用户切到别的界面（主对话或别的子代理）就关掉
function closedForView($, e, entry, paneId) {
  if (String(entry.callerAgent || '') === String(e.props?.view?.agentId || '')) return false
  void trace($, 'closedForView ' + paneId + ' caller=' + entry.callerAgent + ' view=' + e.props?.view?.agentId)
  void closePane($, paneId)
  return true
}

async function closePane($, id) {
  try {
    await $.ui.close({ id })
  } catch {
    // 已经关了就算了
  }
}

// 子代理里调 sideagent_send：把调用方子代理的编号塞进参数，sideagent 记下“归它”。
// 主对话的调用没有 agentId，不动
function onSideagentCall(_$, e, next) {
  if (!e.agentId || !String(e.tool || '').endsWith('__sideagent_send')) return next(e)
  return next({ ...e, caller_agent: e.agentId })
}

// 从外面投进主会话的跨会话消息：是子代理派的 sideagent 发的，就转给那个子代理，不进主对话
async function onReceive($, e, next) {
  if (e.agentId || e.origin?.kind !== 'peer') return next(e)
  const home = await $.env.get('HOME')
  const owner = await senderOwner($, home, String(e.text || ''))
  if (!owner.callerAgent) return next(e)
  let sent = { isDelivered: false, reason: '' }
  try {
    sent = await $.session.send({ to: { agentId: owner.callerAgent },
      text: `（来自 sideagent ${owner.name}）\n${innerText(String(e.text || ''))}` })
  } catch (error) {
    sent = { isDelivered: false, reason: String(error) }
  }
  if (sent.isDelivered) return { consumed: `转给子代理 ${owner.callerAgent}（sideagent ${owner.name}）` }
  // 转不到（那个子代理不在了）：退回主对话，说清原本是给谁的
  return next({ ...e, text: `（原本是给子代理 ${owner.callerAgent} 的，它已经不在：${sent.reason || '没送到'}）\n${e.text}` })
}

// 跨会话消息外面那层的属性：cca-instance（我们自己的回报带）或 from="uds:<socket>"（它自己用 SendMessage 发的）
export function senderOf(text) {
  const head = /<cross-session-message\b([^>]*)>/.exec(text)?.[1] || ''
  return {
    instance: /\bcca-instance="([^"]+)"/.exec(head)?.[1] || '',
    socket: (/\bfrom="uds:([^"]+)"/.exec(head)?.[1] || ''),
  }
}

function innerText(text) {
  return text.replace(/^[\s\S]*?<cross-session-message\b[^>]*>\n?/, '').replace(/\n?<\/cross-session-message>[\s\S]*$/, '')
}

// 发件的是哪个 sideagent、归哪个子代理：按 cca-instance 找它的状态和本会话名单里那一条；
// 没有就按发件 socket 在本会话名单里各个 sideagent 的状态里对
async function senderOwner($, home, text) {
  const { instance, socket } = senderOf(text)
  if (!instance && !socket) return {}
  const dir = home + '/.cca/sideagents'
  const roster = await readRoster($, home)
  const lookup = async (name) => {
    const state = (await readJson($, dir + '/' + name + '/state.json')) || {}
    const note = roster[name] || {}
    return { name, callerAgent: String(state.caller_agent || note.caller_agent || ''),
      socket: String(state.instance_socket || state.peer_socket || '') }
  }
  if (instance) return lookup(instance)
  for (const name of Object.keys(roster)) {
    const found = await lookup(name)
    if (found.socket === socket) return found
  }
  return {}
}

// 会话结束：本会话派出去的sideagent一起停掉，和子代理一样不留在后台
function registerSessionEnd(on) {
  on('session.end', async ($, e, next) => {
    if (mySession) {
      try {
        await $.process.run(['cca', 'sideagent', 'stop-owned', mySession], { timeoutMs: 5000 })
      } catch {
        // 停不掉就算了，守护进程闲置久了也会自己退
      }
    }
    return next(e)
  })
}

// 详情页里的方向键，照主界面：输入框里 ↑ 翻出上一句、↓ 进子代理行；
// 子代理行之间 ↑↓ 选，第一行再 ↑ 回输入框。滚正文用滚轮或 PageUp/PageDown
async function onArrow($, id, by) {
  const current = String(focusOf[id] || '')
  if (current === 'reply-' + id && by < 0) {
    const step = recallStep(historyByPane[id] || [], recallAt[id])
    if (step) {
      recallAt[id] = step.at
      recallValue[id] = step.text
      $.ui.invalidate('ui.render')
    }
    return
  }
  const keys = footerKeys[id] || []
  // 焦点还没落到底部几行（刚打开面板）：↓ 进第一行，一般是输入框
  let target = by > 0 ? keys[0] : null
  if (keys.includes(current)) target = arrowTarget(keys, current, by)
  await trace($, 'arrow ' + id + ' by=' + by + ' current=' + current + ' target=' + target + ' keys=' + keys.join(','))
  if (!target || target === current) return
  try {
    await $.ui.focus({ requestId: id, key: target })
  } catch (error) {
    await trace($, 'arrow focus failed ' + String(error))
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    // sideagent进程自己也会加载这个插件；它不是界面，不用启动面板和通知
    if (await $.env.get('CCA_SIDEAGENT')) return next(e)
    // /cca：接管面板收在这一个命令下（名字被占用时不影响面板）
    try {
      await $.command.register({
        name: CCA_COMMAND,
        description: 'cca：takeover 接管会话、handoff 本会话换号、stay 留在本号；不带参数弹菜单',
        argumentHint: 'takeover [第一句话] | handoff [号] | stay',
        immediate: true,
      })
    } catch {
      // 名字被占用就不注册
    }
    // 记下本会话编号：面板只显示这个会话派的sideagent（每次刷新还会再问一次，见 refresh）
    mySession = await readSessionId($)
    // 先定基准，开场那次刷新不算“刚做完”
    bootAt = Date.now() / 1000
    await refresh($)
    for (const row of rows) if (!row.running) notified.add(notifyKey(row.name, byName[row.name]))
    // sideagent在干活时问得勤一点，闲下来就慢下来
    const tick = async () => {
      await refresh($)
      const busy = rows.some((row) => row.running)
      $.clock.after(busy ? TICK_BUSY_MS : TICK_IDLE_MS, () => { void tick() })
    }
    $.clock.after(TICK_IDLE_MS, () => { void tick() })
    // 额度快满时提醒模型可以换号（cca quota-hint）；附在之后的工具结果上
    const quotaTick = async () => {
      const nextMs = await refreshQuotaHint($)
      $.clock.after(nextMs, () => { void quotaTick() })
    }
    $.clock.after(QUOTA_FIRST_MS, () => { void quotaTick() })
    return next(e)
  })

  registerSessionEnd(on)

  // 输入框上方：一个sideagent一行
  on('ui.render', { component: 'AbovePrompt' }, renderBand)

  // 焦点落到输入框上就把它画亮，和主界面一样
  on('ui.focus', onFocus)

  on('ui.scroll', async ($, e, next) => {
    const id = String(e.requestId || '')
    // 接管挑选框：↑↓ 在各行之间挪，不滚动（和原生选择列表一样）
    // cca 的列表面板：↑↓ 在各行之间挪，不滚动（和原生选择列表一样）
    if (hasList(id) && e.origin?.kind === 'person' && !e.pointer && Math.abs(Number(e.by || 0)) === 1) {
      await onListArrow($, id, Number(e.by))
      return {}
    }
    if (!id.startsWith('peer-') && !id.startsWith('agent-')) return next(e)
    // 方向键（没有指针位置，一次一行）：照主界面，↑↓ 不滚正文，交给 onArrow
    if (e.origin?.kind === 'person' && !e.pointer && Math.abs(Number(e.by || 0)) === 1) {
      await onArrow($, id, Number(e.by))
      return {}
    }
    // 人自己滚的：翻离底部就不再自动跟，滚回底部恢复
    if (e.origin?.kind === 'person') {
      followPane[id] = Number(e.offset || 0)
        >= Number(e.contentRows || 0) - Number(e.bodyRows || 0) - 1
    }
    return next(e)
  })

  // 子代理页关掉以后回到sideagent的详情
  on('ui.close', async ($, e, next) => {
    const id = String(e.id || '')
    if (id.startsWith('agent-')) await trace($, 'agent pane close ' + id + ' origin=' + JSON.stringify(e.origin || null))
    const done = await next(e)
    // cca 的列表面板关了（Ctrl+X X）：清掉它的状态，下次打开从头来
    forgetList(id)
    // 关掉的面板不再跟着滚
    delete followPane[id]
    // 面板关了，横条那行回到没选中
    if (id.startsWith('peer-') || id.startsWith('agent-')) {
      if (id.startsWith('peer-')) viewedPeer = ''
      clearBandFocus($)
    }
    if (id.startsWith('agent-') && agentPanes[id] && byName[agentPanes[id].name]) {
      await openPeerPane($, agentPanes[id].name)
    }
    return done
  })

  // /cca（菜单、接管）的列表面板
  on('command.run', { command: CCA_COMMAND }, ($, e) => runCca($, e))
  // 敲 /cca t… 时补全列表里给出 takeover
  on('prompt.autocomplete', async ($, e, next) => {
    const mine = ccaCompletions(e)
    if (!mine.length) return next(e)
    const done = await next(e)
    return { suggestions: [...mine, ...(done?.suggestions || [])] }
  })
  on('ui.render', { component: 'Pane' }, renderList)

  // 点某一行开的面板：那个sideagent的完整进度
  on('ui.render', { component: 'Pane' }, renderPeerPane)

  // 横条那一行（Client）被鼠标点了：打开它的详情
  on('ui.message', ($, e, next) => onListMessage($, e, (passed) => onBandMessage($, passed, next)))

  // 子代理派 sideagent：记下派活的子代理；它发回来的消息转给那个子代理，不进主对话
  // 模型调 sideagent_takeover 却没给会话：打开挑选框；子代理派 sideagent：记下派活的子代理
  // 原生 SendMessage 发不到：提醒模型对方多半在别的号上，改用 sideagent_send {session}（见 peer-hint.js）
  on('tool.call', async ($, e, next) => withHint(withPeerHint(
    await onTakeoverCall($, e, (call) => onSideagentCall($, call, next)), e), quotaHint, quotaHintsGiven, e))
  on('session.receive', onReceive)
}
