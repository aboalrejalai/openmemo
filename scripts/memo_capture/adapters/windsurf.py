"""Windsurf / Devin Local hook payloads ↔ Memo.

Windsurf's Cascade docs now live under Devin Local, whose hooks
(~/.config/devin/config.json, key "hooks") follow Claude Code's contract:
PascalCase events, session_id, prompt, tool_name, tool_input, and
hookSpecificOutput.additionalContext on SessionStart and UserPromptSubmit.
DEVIN_PROJECT_DIR names the project root. The payload is read the Claude Code
way and recorded under its own host name.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import HookResult, NormalizedInput, claude_code


def normalize(raw: Dict[str, Any], event: str) -> NormalizedInput:
    inp = claude_code.normalize(raw, event)
    inp.host = "windsurf"
    inp.cwd = os.environ.get("DEVIN_PROJECT_DIR") or str(raw.get("cwd") or "") or inp.cwd
    return inp


def format_output(result: HookResult, event: str, inp: Optional[NormalizedInput] = None) -> Dict[str, Any]:
    return claude_code.format_output(result, event, inp)
