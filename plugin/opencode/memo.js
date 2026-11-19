// Memo for OpenCode: forwards session events to `memo hook opencode <event>`.
//
// `memo install-opencode` copies this file to ~/.config/opencode/plugins/memo.js
// with __MEMO_LAUNCHER__ replaced by the vault's plugin/bin/memo. All logic
// stays in the vault's Python; this file only maps OpenCode's hooks onto
// Memo's events and never throws into OpenCode.
//
// OpenCode 1.x loads `server(ctx)`, which returns a hooks object; 2.x loads
// `setup(ctx)`, which registers hooks and returns a cleanup function.

import { spawn } from "node:child_process"

const LAUNCHER = process.env.MEMO_LAUNCHER || "__MEMO_LAUNCHER__"
const TIMEOUT_MS = 20000

function memo(event, payload) {
  return new Promise((resolve) => {
    let out = ""
    let child
    try {
      child = spawn(LAUNCHER, ["hook", "opencode", event], { stdio: ["pipe", "pipe", "ignore"] })
    } catch {
      return resolve({})
    }
    const timer = setTimeout(() => child.kill(), TIMEOUT_MS)
    child.stdout.on("data", (d) => (out += d))
    child.on("error", () => resolve({}))
    child.on("close", () => {
      clearTimeout(timer)
      try { resolve(JSON.parse(out || "{}")) } catch { resolve({}) }
    })
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

const textOf = (parts) =>
  (parts || []).filter((p) => p && p.type === "text" && !p.ignored && typeof p.text === "string")
    .map((p) => p.text).join("\n").trim()

function resultText(result) {
  if (typeof result === "string") return result
  if (!result) return ""
  if (Array.isArray(result.content)) return textOf(result.content)
  if (typeof result.output === "string") return result.output
  if (result.output && typeof result.output.output === "string") return result.output.output
  return ""
}

function createCore(directory) {
  const contexts = new Map()   // sessionID → context text ("" once fetched and empty)
  const replies = new Map()    // sessionID → latest assistant text
  const base = (sessionID) => ({ session_id: sessionID, cwd: directory })
  return {
    async context(sessionID) {
      if (!sessionID) return ""
      if (!contexts.has(sessionID)) {
        const res = await memo("context", base(sessionID))
        contexts.set(sessionID, typeof res.context === "string" ? res.context : "")
      }
      return contexts.get(sessionID)
    },
    prompt(sessionID, text) {
      if (sessionID && text) return memo("session-init", { ...base(sessionID), prompt: text })
    },
    tool(sessionID, tool, args, output, callID) {
      if (!sessionID || !tool) return
      return memo("observation", { ...base(sessionID), tool_name: tool, tool_input: args ?? {},
                                   tool_response: { output: output || "" }, tool_use_id: callID })
    },
    reply(sessionID, text) { if (sessionID && text) replies.set(sessionID, text) },
    summarize(sessionID, text) {
      if (!sessionID) return
      return memo("summarize", { ...base(sessionID), last_assistant_message: text || replies.get(sessionID) || "" })
    },
    forgetContext(sessionID) { contexts.delete(sessionID) },
    end(sessionID) {
      contexts.delete(sessionID)
      replies.delete(sessionID)
      if (sessionID) return memo("session-end", { ...base(sessionID), reason: "deleted" })
    },
  }
}

// OpenCode 1.x
async function server(ctx) {
  const core = createCore(ctx.directory || process.cwd())
  const latestReply = async (sessionID) => {
    try {
      const res = await ctx.client.session.messages({ path: { id: sessionID }, query: { directory: ctx.directory } })
      const done = (res.data || []).filter((m) => m.info?.role === "assistant" && m.info?.summary !== true
                                              && typeof m.info?.time?.completed === "number")
      done.sort((a, b) => a.info.time.completed - b.info.time.completed)
      return done.length ? textOf(done[done.length - 1].parts) : ""
    } catch {
      return ""
    }
  }
  return {
    "chat.message": async (_input, output) => {
      const sessionID = output?.message?.sessionID
      const text = textOf(output?.parts)
      if (output?.message?.role === "assistant") core.reply(sessionID, text)
      else await core.prompt(sessionID, text)
    },
    "tool.execute.after": async (input, output) => {
      await core.tool(input?.sessionID, input?.tool, input?.args ?? output?.args, output?.output, input?.callID)
    },
    "experimental.chat.system.transform": async (input, output) => {
      const context = await core.context(input?.sessionID)
      if (context && Array.isArray(output?.system)) output.system.push(context)
    },
    "experimental.session.compacting": async (input) => {
      core.forgetContext(input?.sessionID)
    },
    event: async ({ event }) => {
      const sessionID = event?.properties?.sessionID || event?.properties?.info?.id
      if (event?.type === "session.idle") await core.summarize(sessionID, await latestReply(sessionID))
      else if (event?.type === "session.deleted") await core.end(sessionID)
    },
  }
}

// OpenCode 2.x
async function setup(ctx) {
  const directory = ctx.location?.directory ?? ctx.directory
  if (!directory) return async () => {}
  const core = createCore(directory)
  const disposers = []
  const keep = (d) => { if (typeof d === "function") disposers.push(d) }
  keep(ctx.session?.hook?.("prompt", async (e) => core.prompt(e?.sessionID, e?.prompt?.text)))
  keep(ctx.session?.hook?.("context", async (e) => {
    const context = await core.context(e?.sessionID)
    if (!context || !Array.isArray(e?.system)) return
    const kept = e.system.filter((p) => !(typeof p?.text === "string" && p.text.startsWith("<memo-context>")))
    e.system.splice(0, e.system.length, ...kept, { type: "text", text: context })
  }))
  keep(ctx.session?.hook?.("compaction", async (e) => core.forgetContext(e?.sessionID)))
  keep(ctx.tool?.hook?.("execute.after", async (e) => {
    const output = e?.error ? String(e.error?.message || e.error) : resultText(e?.result)
    await core.tool(e?.sessionID, e?.tool, e?.input ?? e?.args, output, e?.callID)
  }))
  const controller = new AbortController()
  ;(async () => {
    while (!controller.signal.aborted && typeof ctx.event?.subscribe === "function") {
      try {
        for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
          if (ev?.location?.directory && ev.location.directory !== directory) continue
          const sessionID = ev?.data?.sessionID
          if (ev?.type === "session.text.ended") core.reply(sessionID, ev.data?.text)
          else if (ev?.type === "session.execution.succeeded") await core.summarize(sessionID)
          else if (ev?.type === "session.deleted") await core.end(sessionID)
        }
      } catch {}
      if (!controller.signal.aborted) await new Promise((r) => setTimeout(r, 10000))
    }
  })()
  return async () => {
    controller.abort()
    for (const d of disposers) { try { await d() } catch {} }
  }
}

export default { id: "memo", server, setup }
