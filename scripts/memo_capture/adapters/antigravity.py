"""Google Antigravity (App, CLI `agy`, IDE) hook payloads ↔ Memo.

Antigravity hooks (~/.gemini/config/hooks.json, one named entry per plugin)
send camelCase fields: conversationId, workspacePaths, transcriptPath,
modelName, toolCall {name, args}, stepIdx and error. It has six events
(SessionStart, PreToolUse, PostToolUse, PreInvocation, PostInvocation, Stop):
no prompt-submit and no session end. So the prompt is read from the
transcript on PreInvocation, the reply from the transcript on Stop, and the
worker ends a session once it goes quiet. Context goes back through
SessionStart's injectSteps.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    roots = raw.get("workspacePaths") or []
    cwd = (roots[0] if isinstance(roots, list) and roots else None) or raw.get("cwd") or os.getcwd()
    sid = str(raw.get("conversationId") or raw.get("conversation_id") or "")
    call = raw.get("toolCall") if isinstance(raw.get("toolCall"), dict) else {}
    error = _str(raw.get("error"))
    step = raw.get("stepIdx")
    return NormalizedInput(
        host="antigravity",
        event=event,
        session_id=sid,
        cwd=str(cwd),
        tool_name=_str(call.get("name")),
        tool_input=call.get("args"),
        tool_response={"error": error} if error else {},
        tool_use_id=f"{sid}-{step}" if sid and isinstance(step, int) else None,
        transcript_path=_str(raw.get("transcriptPath")),
        reason=_str(raw.get("terminationReason")),
        raw=raw,
    )


def format_output(result: HookResult, event: str, inp: Optional[NormalizedInput] = None) -> Dict[str, Any]:
    if event == "context" and result.additional_context:
        return {"injectSteps": [{"systemMessage": {"systemMessage": result.additional_context}}]}
    return {}
