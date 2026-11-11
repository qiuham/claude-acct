// 额度快满的提醒：每隔几分钟在会话里跑一次 `cca quota-hint --json`（要不要提醒、推荐换到哪个号、
// 话怎么说，都在 cca 里算），命中时把提醒附在下一次工具结果后面给模型——模型读得到、人看不到、
// 不另起一轮。换不换号由模型自己判断。
//
// 同一个号同一个窗口只给一次（cca 给的 key 去重）；只给主对话，子代理的工具调用不附。

// 下次多久再问由 cca 说了算（离阈值近时 2 分钟、远时 5 分钟、API 号 1 小时）；读不到就按这个
export const QUOTA_POLL_MS = 5 * 60 * 1000
const QUOTA_POLL_MIN_MS = 30 * 1000
// 会话刚起来先等一会儿再查：开场那一阵不抢资源
export const QUOTA_FIRST_MS = 30 * 1000

// `cca quota-hint --json` 的输出 → { hint: { key, text } | null, nextMs }；读不懂就当不用提醒
export function parseQuota(stdout) {
  try {
    const data = JSON.parse(String(stdout || ''))
    const next = Number(data?.next_check_seconds) * 1000
    const hint = data && data.warn && data.key && data.text ? { key: String(data.key), text: String(data.text) } : null
    return { hint, nextMs: Number.isFinite(next) && next >= QUOTA_POLL_MIN_MS ? next : QUOTA_POLL_MS }
  } catch {
    return { hint: null, nextMs: QUOTA_POLL_MS }
  }
}

export function parseHint(stdout) {
  return parseQuota(stdout).hint
}

// 把提醒附进这次工具调用的结果；附过的 key 记在 given 里，不再附
export function withHint(result, hint, given, e) {
  if (!hint || !result || result.deny || e?.agentId || given.has(hint.key)) return result
  given.add(hint.key)
  return { ...result, context: [...(result.context || []), hint.text] }
}
