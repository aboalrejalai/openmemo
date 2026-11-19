"""Host adapters: map each agent's hook payload onto one NormalizedInput."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class NormalizedInput:
    host: str
    event: str
    session_id: str = ""
    cwd: str = ""
    source: Optional[str] = None          # startup | resume | clear | compact
    prompt: Optional[str] = None
    tool_name: Optional[str] = None
    tool_input: Any = None
    tool_response: Any = None
    tool_use_id: Optional[str] = None
    transcript_path: Optional[str] = None
    agent_transcript_path: Optional[str] = None
    last_assistant_message: Optional[str] = None
    reason: Optional[str] = None
    agent_id: Optional[str] = None
    agent_type: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """What a handler wants to tell the host. Empty means "say nothing"."""
    additional_context: Optional[str] = None
    system_message: Optional[str] = None
    permission_decision: Optional[str] = None   # PreToolUse only: "deny"
    permission_reason: Optional[str] = None


def get_adapter(host: str):
    from . import claude_code
    adapters = {"claude-code": claude_code}
    try:
        from . import antigravity, codex, cursor, gemini, opencode, windsurf, zadloop
        adapters.update({"codex": codex, "cursor": cursor, "gemini": gemini, "antigravity": antigravity,
                         "windsurf": windsurf, "opencode": opencode, "zadloop": zadloop})
    except ImportError:
        pass
    if host not in adapters:
        raise ValueError(f"unknown host '{host}'")
    return adapters[host]
