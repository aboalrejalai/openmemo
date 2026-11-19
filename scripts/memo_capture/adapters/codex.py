"""Codex CLI hook payloads ↔ Memo.

Codex hooks send hook_event_name, session_id, cwd, source, prompt, tool_name,
tool_input, tool_response, tool_use_id, transcript_path,
last_assistant_message, turn_id and model. Codex has no SessionEnd: the
worker wraps a Codex session up after it has been idle (see worker.py).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput

_EVENT_NAMES = {
    "context": "SessionStart",
    "session-init": "UserPromptSubmit",
    "file-context": "PreToolUse",
}
_SOURCES = {"startup", "resume", "clear", "compact"}


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    source = raw.get("source")
    return NormalizedInput(
        host="codex",
        event=event,
        session_id=str(raw.get("session_id") or ""),
        cwd=str(raw.get("cwd") or os.getcwd()),
        source=source if source in _SOURCES else None,
        prompt=_str(raw.get("prompt")),
        tool_name=_str(raw.get("tool_name")),
        tool_input=raw.get("tool_input"),
        tool_response=raw.get("tool_response"),
        tool_use_id=_str(raw.get("tool_use_id")),
        transcript_path=_str(raw.get("transcript_path")),
        last_assistant_message=_str(raw.get("last_assistant_message")),
        agent_id=_str(raw.get("agent_id")),
        agent_type=_str(raw.get("agent_type")),
        raw=raw,
    )


def format_output(result: HookResult, event: str, inp: Optional[NormalizedInput] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    hook_name = _EVENT_NAMES.get(event)
    if hook_name == "SessionStart" and inp is not None and inp.raw.get("hook_event_name") == "PostCompact":
        hook_name = None   # PostCompact takes no hookSpecificOutput; context goes in systemMessage below
        if result.additional_context:
            out["systemMessage"] = result.additional_context
    specific: Dict[str, Any] = {}
    if result.additional_context and hook_name:
        specific["additionalContext"] = result.additional_context
    if result.permission_decision == "deny" and hook_name == "PreToolUse":
        specific["permissionDecision"] = "deny"
        if result.permission_reason:
            specific["permissionDecisionReason"] = result.permission_reason
    if specific:
        specific["hookEventName"] = hook_name
        out["hookSpecificOutput"] = specific
    if result.system_message and "systemMessage" not in out:
        out["systemMessage"] = result.system_message
    return out
