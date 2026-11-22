// Memo session memory for ZadLoop.
//
// Listens to ZadLoop's committed session log and forwards each piece to
// `memo hook zadloop <event>`, the same hook contract the Claude Code,
// Codex, Cursor and OpenCode integrations use. All logic stays in the
// vault's Python (scripts/memo_capture); this file only maps ZadLoop events
// onto Memo's events and never throws into ZadLoop.
//
//   user/message (source.kind "user")  -> session-init  (one request)
//   tool/call + tool/result             -> observation
//   assistant/message                   -> remembered as the latest reply
//   turn/end                            -> summarize     (closes the request)
//   api-session/deleted                 -> session-end
//
// Subagent sessions (delegationDepth > 0) are skipped: their work reaches
// the parent session through its tool results.

import { spawn } from 'node:child_process'
import { homedir } from 'node:os'
import { join } from 'node:path'

const LAUNCHER = process.env.MEMO_LAUNCHER || join(process.env.OPENMEMO_HOME || join(homedir(), 'openmemo'), 'plugin', 'bin', 'memo')
const TIMEOUT_MS = 30000
const MAX_TEXT = 32000

function memo(event, payload) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn(LAUNCHER, ['hook', 'zadloop', event], { stdio: ['pipe', 'ignore', 'ignore'] })
    } catch {
      return resolve()
    }
    const timer = setTimeout(() => child.kill(), TIMEOUT_MS)
    child.on('error', () => { clearTimeout(timer); resolve() })
    child.on('close', () => { clearTimeout(timer); resolve() })
    child.stdin.on('error', () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

const clip = (text) => (text.length > MAX_TEXT ? text.slice(0, MAX_TEXT) : text)

const textOf = (content) =>
  clip((Array.isArray(content) ? content : [])
    .filter((p) => p && p.type === 'text' && typeof p.text === 'string')
    .map((p) => p.text).join('\n').trim())

function parseArgs(raw) {
  if (typeof raw !== 'string') return raw ?? {}
  try { return JSON.parse(raw) } catch { return { raw } }
}

export function apply(ctx) {
  const states = new Map()   // sessionId -> { queue, reply, calls }

  const stateOf = (id) => {
    let s = states.get(id)
    if (!s) states.set(id, (s = { queue: Promise.resolve(), reply: '', calls: new Map() }))
    return s
  }

  // One process at a time per session keeps prompt -> observations -> summary in order.
  const send = (id, event, payload) => {
    const s = stateOf(id)
    s.queue = s.queue.then(() => memo(event, payload)).catch(() => {})
  }

  const cwdOf = (session) => {
    try {
      const wd = ctx.get?.('workingDirectory')
      const dir = wd?.get?.(session)
      if (typeof dir === 'string' && dir) return dir
    } catch {}
    return session?.header?.cwd || ''
  }

  ctx.on('session/event', (session, event) => {
    try {
      const header = session?.header
      const id = header?.id || session?.id
      if (!id || !event || (header?.delegationDepth ?? 0) > 0) return
      const data = event.data || {}
      const base = { session_id: id, cwd: cwdOf(session) }

      switch (event.type) {
        case 'user/message': {
          if (data.source?.kind !== 'user') return   // skip injected context, reminders, schedules
          const prompt = textOf(data.content)
          if (prompt) send(id, 'session-init', { ...base, prompt })
          return
        }
        case 'tool/call': {
          if (data.callId) stateOf(id).calls.set(data.callId, { name: data.name, args: data.arguments })
          return
        }
        case 'tool/result': {
          const s = stateOf(id)
          const callId = data.message?.toolCallId || data.message?.source?.callId
          const call = callId && s.calls.get(callId)
          if (!call?.name) return
          s.calls.delete(callId)
          const output = textOf(data.message?.content)
          send(id, 'observation', {
            ...base,
            tool_name: call.name,
            tool_input: parseArgs(call.args),
            tool_response: data.message?.isError ? { error: output } : { output },
            tool_use_id: callId,
          })
          return
        }
        case 'assistant/message': {
          const text = textOf(data.message?.content)
          if (text) stateOf(id).reply = text
          return
        }
        case 'turn/end': {
          const s = stateOf(id)
          send(id, 'summarize', { ...base, last_assistant_message: s.reply })
          s.reply = ''
          s.calls.clear()
          return
        }
      }
    } catch {}
  })

  ctx.on('api-session/deleted', (sessionId, cwd) => {
    if (!states.has(sessionId)) return
    send(sessionId, 'session-end', { session_id: sessionId, cwd: cwd || '', reason: 'deleted' })
    const s = states.get(sessionId)
    s.queue.finally(() => states.delete(sessionId))
  })

  // A session that left memory keeps its Memo record; only the local cache goes.
  ctx.on('session/disposed', (session) => {
    const id = session?.header?.id || session?.id
    const s = id && states.get(id)
    if (s) s.queue.finally(() => { if (states.get(id) === s) states.delete(id) })
  })
}
