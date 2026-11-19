"""Gemini CLI hook payloads ↔ Memo.

Gemini CLI hooks (~/.gemini/settings.json, key "hooks") send session_id,
transcript_path, cwd, hook_event_name and timestamp, plus source
(SessionStart), prompt (BeforeAgent), prompt + prompt_response (AfterAgent),
tool_name/tool_input/tool_response (AfterTool) and reason (SessionEnd).
Context goes back as hookSpecificOutput.additionalContext on SessionStart and
BeforeAgent. The transcript is one JSON document, not JSONL, so Memo takes the
reply from prompt_response instead of reading it.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput

_EVENT_NAMES = {"context": "SessionStart", "session-init": "BeforeAgent"}
_SOURCES = {"startup", "resume", "clear"}


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    source = raw.get("source")
    return NormalizedInput(
        host="gemini",
        event=event,
        session_id=str(raw.get("session_id") or os.environ.get("GEMINI_SESSION_ID") or ""),
        cwd=str(raw.get("cwd") or os.environ.get("GEMINI_PROJECT_DIR") or os.getcwd()),
        source=source if source in _SOURCES else None,
        prompt=_str(raw.get("prompt")),
        tool_name=_str(raw.get("tool_name")),
        tool_input=raw.get("tool_input"),
        tool_response=raw.get("tool_response"),
        last_assistant_message=_str(raw.get("prompt_response")),
        reason=_str(raw.get("reason")),
        raw=raw,
    )


def format_output(result: HookResult, event: str, inp: Optional[NormalizedInput] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    hook_name = _EVENT_NAMES.get(event)
    if result.additional_context and hook_name:
        out["hookSpecificOutput"] = {"hookEventName": hook_name, "additionalContext": result.additional_context}
    if result.system_message:
        out["systemMessage"] = result.system_message
    return out
