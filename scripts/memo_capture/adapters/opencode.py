"""OpenCode hook payloads ↔ Memo.

OpenCode plugins are JavaScript, so plugin/opencode/memo.js forwards each
event to `memo hook opencode <event>` with an already flat payload:
session_id, cwd, prompt, tool_name, tool_input, tool_response, tool_use_id
and last_assistant_message. Context goes back as {"context": text}; the
plugin pushes it into the system prompt.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput

# OpenCode's built-in tools, renamed to the names Memo's prompts know.
_TOOL_NAMES = {"read": "Read", "write": "Write", "edit": "Edit", "bash": "Bash", "grep": "Grep",
               "glob": "Glob", "list": "LS", "webfetch": "WebFetch", "patch": "Edit", "apply_patch": "Edit"}


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    tool = _str(raw.get("tool_name"))
    return NormalizedInput(
        host="opencode",
        event=event,
        session_id=str(raw.get("session_id") or ""),
        cwd=str(raw.get("cwd") or os.getcwd()),
        prompt=_str(raw.get("prompt")),
        tool_name=_TOOL_NAMES.get(tool, tool) if tool else None,
        tool_input=raw.get("tool_input"),
        tool_response=raw.get("tool_response"),
        tool_use_id=_str(raw.get("tool_use_id")),
        last_assistant_message=_str(raw.get("last_assistant_message")),
        reason=_str(raw.get("reason")),
        raw=raw,
    )


def format_output(result: HookResult, event: str, inp: Optional[NormalizedInput] = None) -> Dict[str, Any]:
    return {"context": result.additional_context} if result.additional_context else {}
