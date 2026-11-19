"""One function per hook event. Each takes a NormalizedInput and returns a
HookResult (or None for "nothing to say").

Hooks that only record (observation, summarize, subagent-stop, session-end)
write one spool file and nudge the worker; they never wait on the model.
Hooks that answer the host (context, session-init, file-context) read and
write <vault>/.memo/state.db directly, so they work even while the worker is down.
"""

from __future__ import annotations

import re
from typing import Optional

from . import privacy, project, settings, spool, state, worker_client
from .adapters import HookResult, NormalizedInput

# Memo's own MCP tools: recording them would make memory about memory. Hosts
# name them differently (Claude: mcp__memo__memo_search, Cursor: memo_search).
_OWN_TOOL = re.compile(r"(^|__|[:.])memo_[a-z_]+$")


def _excluded(project_name: str) -> bool:
    return project_name in settings.csv("excluded_projects")


def _is_subagent(inp: NormalizedInput) -> bool:
    return bool(inp.agent_id and inp.agent_type)


def _skip_subagent(inp: NormalizedInput) -> bool:
    if not _is_subagent(inp):
        return False
    return bool(settings.get("skip_subagent_observations")) or inp.agent_type in settings.csv("skip_agent_types")


def _base_payload(inp: NormalizedInput) -> dict:
    proj = project.resolve(inp.cwd)
    return {"content_session_id": inp.session_id, "host": inp.host, "cwd": inp.cwd,
            "project": proj.name, "project_key_source": proj.source,
            "agent_id": inp.agent_id, "agent_type": inp.agent_type,
            "transcript_path": inp.transcript_path}


def context(inp: NormalizedInput) -> Optional[HookResult]:
    from . import context as ctx
    return ctx.session_start(inp)


def session_init(inp: NormalizedInput) -> Optional[HookResult]:
    if not inp.session_id:
        return None
    proj = project.resolve(inp.cwd)
    if _excluded(proj.name):
        return None
    worker_client.ensure_started()
    if not inp.prompt and inp.transcript_path:
        # Antigravity has no prompt-submit hook: PreInvocation fires before every
        # model call, so the prompt comes from the transcript, once per new prompt.
        _register_prompt_from_transcript(inp)
        return None
    if privacy.is_task_notification_only(inp.prompt or ""):
        return None
    from . import session_init as si
    si.spool_prompt(inp, proj, at_ms=state.now_ms())
    worker_client.nudge()
    return si.semantic_context(inp, proj)


def file_context(inp: NormalizedInput) -> Optional[HookResult]:
    from . import file_context as fc
    return fc.pre_tool_use(inp)


def observation(inp: NormalizedInput) -> Optional[HookResult]:
    if not inp.session_id or not inp.tool_name:
        return None
    if _OWN_TOOL.search(inp.tool_name) or _skip_subagent(inp):
        return None
    payload = _base_payload(inp)
    if _excluded(payload["project"]):
        return None
    if inp.host != "claude-code" and inp.transcript_path:
        _register_prompt_from_transcript(inp)
    payload.update({"tool_name": inp.tool_name, "tool_input": inp.tool_input,
                    "tool_response": inp.tool_response, "tool_use_id": inp.tool_use_id,
                    "observed_at_ms": state.now_ms()})
    spool.write_event("observation", payload)
    worker_client.nudge()
    return None


def _register_prompt_from_transcript(inp: NormalizedInput) -> None:
    """Hosts whose headless mode skips the prompt hook (cursor-agent -p): take the
    prompt from the transcript, once per new prompt."""
    from . import session_init as si
    from . import transcript
    text = transcript.last_user_prompt(inp.transcript_path)
    if not text:
        return
    try:                     # a read only; the worker checks again when it numbers the prompt
        conn = state.connect()
        number = state.current_prompt_number(conn, inp.session_id)
        row = state.get_prompt(conn, inp.session_id, number) if number else None
        if row is not None and (row["prompt_text"] == privacy.clean_prompt(text)[0] or row["private"]):
            return
    except Exception:
        pass
    # No time stamp: the same transcript prompt read twice is one spool file, one request.
    si.spool_prompt(NormalizedInput(host=inp.host, event="session-init", session_id=inp.session_id,
                                    cwd=inp.cwd, prompt=text), project.resolve(inp.cwd))
    worker_client.nudge()


def summarize(inp: NormalizedInput, safety_net: bool = False) -> Optional[HookResult]:
    if not inp.session_id or _is_subagent(inp):
        return None
    payload = _base_payload(inp)
    if _excluded(payload["project"]):
        return None
    from . import transcript
    text, model = (inp.last_assistant_message, None) if inp.last_assistant_message \
        else transcript.last_assistant_turn(inp.transcript_path)
    # No database here: the worker gives the summary its request number when it
    # drains the spool, after the prompt that came before it.
    payload.update({"last_assistant_message": privacy.clean_text(text, 32_000),
                    "observed_model": model})
    if safety_net:
        payload["safety_net"] = True            # skipped at ingest when Stop already sent this turn
    else:
        payload["stopped_at_ms"] = state.now_ms()   # two turns ending in the same words stay two events
    spool.write_event("summarize", payload)
    worker_client.nudge()
    return None


def subagent_stop(inp: NormalizedInput) -> Optional[HookResult]:
    if not inp.session_id:
        return None
    payload = _base_payload(inp)
    payload["subagent_stopped_at_ms"] = state.now_ms()
    spool.write_event("subagent_stop", payload)
    return None


def session_end(inp: NormalizedInput) -> Optional[HookResult]:
    if not inp.session_id:
        return None
    # Safety net: a headless run can exit before its Stop hook finishes. The
    # same last turn gives the same spool key, so a summary already spooled
    # by Stop is not duplicated.
    if inp.transcript_path:
        summarize(inp, safety_net=True)
    payload = _base_payload(inp)
    payload.update({"reason": inp.reason, "ended_at_ms": state.now_ms()})
    spool.write_event("session_end", payload)
    worker_client.nudge()
    return None
