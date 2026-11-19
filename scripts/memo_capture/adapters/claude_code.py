"""Claude Code hook payloads ↔ Memo.

Payload fields follow the Claude Code hooks reference: session_id, cwd,
source, prompt, tool_name, tool_input, tool_response (or error +
is_interrupt on PostToolUseFailure), tool_use_id, transcript_path,
agent_transcript_path, reason, agent_id, agent_type, hook_event_name.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput

_MAX_AGENT_FIELD = 128
_SOURCES = {"startup", "resume", "clear", "compact"}

# Memo event name → Claude Code hookEventName for hookSpecificOutput.
_EVENT_NAMES = {
    "context": "SessionStart",
    "session-init": "UserPromptSubmit",
    "file-context": "PreToolUse",
}


def _short(value: Any) -> Any:
    return value if isinstance(value, str) and 0 < len(value) <= _MAX_AGENT_FIELD else None


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or raw.get("cwd") or os.getcwd()
    response = raw.get("tool_response")
    if response is None and raw.get("hook_event_name") == "PostToolUseFailure":
        response = {"error": raw.get("error"), "is_interrupt": raw.get("is_interrupt") is True}
    source = raw.get("source")
    return NormalizedInput(
        host="claude-code",
        event=event,
        session_id=str(raw.get("session_id") or raw.get("sessionId") or ""),
        cwd=str(cwd),
        source=source if source in _SOURCES else None,
        prompt=raw.get("prompt") if isinstance(raw.get("prompt"), str) else None,
        tool_name=raw.get("tool_name"),
        tool_input=raw.get("tool_input"),
        tool_response=response,
        tool_use_id=raw.get("tool_use_id") if isinstance(raw.get("tool_use_id"), str) else None,
        transcript_path=raw.get("transcript_path"),
        agent_transcript_path=raw.get("agent_transcript_path") if isinstance(raw.get("agent_transcript_path"), str) else None,
        reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
        agent_id=_short(raw.get("agent_id")),
        agent_type=_short(raw.get("agent_type")),
        raw=raw,
    )


def format_output(result: HookResult, event: str, inp: Optional[NormalizedInput] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    hook_name = _EVENT_NAMES.get(event)
    specific: Dict[str, Any] = {}
    if result.additional_context and hook_name:
        specific["additionalContext"] = result.additional_context
    if result.permission_decision and hook_name == "PreToolUse":
        specific["permissionDecision"] = result.permission_decision
        if result.permission_reason:
            specific["permissionDecisionReason"] = result.permission_reason
    if specific:
        specific["hookEventName"] = hook_name
        out["hookSpecificOutput"] = specific
    if result.system_message:
        out["systemMessage"] = result.system_message
    return out
