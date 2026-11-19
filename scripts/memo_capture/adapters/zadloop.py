"""ZadLoop hook payloads ↔ Memo.

ZadLoop plugins are JavaScript Host plugins, so plugin/zadloop/index.js
listens to ZadLoop's session log and forwards each event to
`memo hook zadloop <event>` with an already flat payload: session_id, cwd,
prompt, tool_name, tool_input, tool_response, tool_use_id and
last_assistant_message. ZadLoop has no session-end hook for an open
session, so the worker closes a quiet ZadLoop session (see worker.wrap_up_idle).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput

# ZadLoop's built-in tools, renamed to the names Memo's prompts know. Their
# arguments already use Claude Code's keys (file_path, old_string, command...).
_TOOL_NAMES = {"read": "Read", "write": "Write", "edit": "Edit", "bash": "Bash", "grep": "Grep",
               "glob": "Glob", "web_fetch": "WebFetch", "read_image": "Read"}


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _session_id(raw: Dict[str, Any]) -> str:
    """ZadLoop ids are "session-<uuid>"; the bare UUID gives the usual 8-character file name."""
    sid = str(raw.get("session_id") or "")
    return sid[len("session-"):] if sid.startswith("session-") else sid


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    tool = _str(raw.get("tool_name"))
    return NormalizedInput(
        host="zadloop",
        event=event,
        session_id=_session_id(raw),
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
