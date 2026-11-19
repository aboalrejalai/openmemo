"""Spool events → durable queue rows, with every pre-storage filter applied.

A `prompt` event registers the next request number; every later event of the
session belongs to it, because the spool is drained oldest first.

Filters (in claude-mem's order): skipped tools, skipped Bash patterns, the
prompt's privacy flag, then tag stripping and redaction of the tool input and
response. A raw backup of each kept tool use goes to tool_uses.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

from . import privacy, settings, state


def _skip_bash(tool_input: Any) -> bool:
    pattern = settings.get("skip_bash_patterns")
    if not pattern:
        return False
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input or "")
    try:
        return re.search(pattern, command) is not None
    except re.error:
        return False


def enqueue_event(conn, kind: str, payload: Dict[str, Any]) -> None:
    sid = payload.get("content_session_id") or ""
    if not sid:
        return
    session = state.upsert_session(conn, sid, project=payload.get("project") or "unknown-project",
                                   host=payload.get("host") or "claude-code", cwd=payload.get("cwd") or "",
                                   project_key_source=payload.get("project_key_source") or "")
    if payload.get("transcript_path") and session["transcript_path"] != payload["transcript_path"]:
        state.update_session(conn, sid, transcript_path=payload["transcript_path"])
    if kind == "prompt":
        from . import session_init
        session_init.record_prompt(conn, payload)
        return
    prompt_number = payload.get("prompt_number") or int(session["prompt_counter"] or 0) or 1
    if kind in ("observation", "summarize") and state.prompt_is_private(conn, sid, prompt_number):
        return
    if kind == "observation":
        tool = payload.get("tool_name") or ""
        if tool in settings.csv("skip_tools") or (tool == "Bash" and _skip_bash(payload.get("tool_input"))):
            return
        tool_input = privacy.clean_json(payload.get("tool_input"))
        tool_response = privacy.clean_json(payload.get("tool_response"))
        conn.execute("""INSERT INTO tool_uses (content_session_id, tool_use_id, tool_name, tool_input,
                        tool_response, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                     (sid, payload.get("tool_use_id"), tool, tool_input, tool_response, state.now_ms()))
        message = {"tool_name": tool, "tool_input": tool_input, "tool_response": tool_response,
                   "cwd": payload.get("cwd"), "observed_at_ms": payload.get("observed_at_ms"),
                   "agent_id": payload.get("agent_id"), "agent_type": payload.get("agent_type")}
        state.enqueue(conn, sid, "observation", message, prompt_number)
    elif kind == "summarize":
        message = payload.get("last_assistant_message") or ""
        if payload.get("safety_net"):       # SessionEnd's copy of a turn Stop may already have sent
            if conn.execute("SELECT 1 FROM session_summaries WHERE content_session_id = ? AND prompt_number = ?",
                            (sid, prompt_number)).fetchone():
                return
        for row in conn.execute("SELECT payload FROM pending_messages WHERE content_session_id = ? AND "
                                "message_type = 'summarize' AND prompt_number = ?", (sid, prompt_number)):
            if json.loads(row["payload"]).get("last_assistant_message") == message:
                return      # the same reply for the same request is already queued
        state.enqueue(conn, sid, "summarize",
                      {"last_assistant_message": payload.get("last_assistant_message") or "",
                       "observed_model": payload.get("observed_model")}, prompt_number)
    elif kind == "session_end":
        state.enqueue(conn, sid, "session_end", {"reason": payload.get("reason"),
                                                  "ended_at_ms": payload.get("ended_at_ms")}, prompt_number)
    # subagent_stop is bookkeeping only; nothing to process.
