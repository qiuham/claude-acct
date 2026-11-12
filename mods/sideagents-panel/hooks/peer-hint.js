// 原生 SendMessage 发不到（"No agent named 'X' is reachable" / 套接字过期）时，给模型补一句：
// 那多半是对方在别的号上（ListAgents 只列本号的会话），不是「下线」。用 sideagent_send {session} 发，
// cca 会找到它现在挂的号，必要时唤醒。只加给模型看的 context，不改原结果。

const UNREACHABLE = [/is reachable/i, /socket path is stale/i, /not reachable/i]

export function peerHintText(to) {
  const who = String(to || '').trim()
  const target = who && !who.startsWith('uds:') ? who : '<对方的名字或会话编号>'
  return `【cca】${who || '对方'} 不在本号的会话列表里，不代表它下线了：ListAgents 只列本号的会话，它多半在别的号上，或者正停着。`
    + `改用 sideagent_send {session: "${target}", message: ...} 发，cca 会找到它现在挂的号，没在跑就先唤醒再送到。`
}

export function withPeerHint(result, e) {
  if (!result || result.deny || String(e?.tool || '') !== 'SendMessage') return result
  let text = ''
  try {
    text = JSON.stringify(result)
  } catch {
    return result
  }
  if (!UNREACHABLE.some((re) => re.test(text))) return result
  return { ...result, context: [...(result.context || []), peerHintText(e.to)] }
}
