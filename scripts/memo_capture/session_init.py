"""UserPromptSubmit: register the session, number the prompt, mark privacy.

The hook spools the prompt like any tool event (`spool_prompt`), and the
worker numbers it when it drains the spool, in time order with the session's
tool events. A busy or locked database therefore delays a prompt, never drops
it (2026-11-02: three days of prompts were lost when the hook wrote directly).

With `semantic_inject` on (off by default, as in claude-mem), the most
relevant past observations for the prompt are returned as extra context.
"""

from __future__ import annotations

from typing import Optional

from . import privacy, settings, spool, state
from .adapters import HookResult, NormalizedInput
from .project import Project


def prompt_payload(inp: NormalizedInput, proj: Project, at_ms: Optional[int]) -> dict:
    text, private = privacy.clean_prompt(inp.prompt)
    payload = {"content_session_id": inp.session_id, "host": inp.host, "cwd": inp.cwd,
               "project": proj.name, "project_key_source": proj.source,
               "transcript_path": inp.transcript_path,
               "prompt_text": "" if private else (text or "[media prompt]"), "private": bool(private)}
    if at_ms is not None:
        payload["submitted_at_ms"] = at_ms      # makes every prompt its own event, even a repeated "continue"
    return payload


_last_at_ms = 0


def spool_prompt(inp: NormalizedInput, proj: Project, at_ms: Optional[int] = None) -> None:
    global _last_at_ms
    if at_ms is not None:
        # Two identical prompts in the same millisecond would share a spool key and
        # the second file would replace the first; keep each one its own event.
        at_ms = max(at_ms, _last_at_ms + 1)
        _last_at_ms = at_ms
    spool.write_event("prompt", prompt_payload(inp, proj, at_ms))


def record_prompt(conn, payload: dict) -> Optional[int]:
    """Number and store one spooled prompt (called by ingest). Returns its number,
    or None when it repeats the session's last prompt (a re-read transcript)."""
    sid = payload["content_session_id"]
    text, private = payload.get("prompt_text") or "", bool(payload.get("private"))
    if payload.get("submitted_at_ms") is None:
        number = state.current_prompt_number(conn, sid)
        row = state.get_prompt(conn, sid, number) if number else None
        if row is not None and (row["prompt_text"] == text or (row["private"] and private)):
            return None
    if state.get_session(conn, sid)["status"] != "active":
        state.update_session(conn, sid, status="active")   # an idle-ended session came back
    number = state.next_prompt_number(conn, sid)
    state.add_prompt(conn, sid, number, text, private, created_at=payload.get("submitted_at_ms"))
    return number


def register_prompt(inp: NormalizedInput, proj: Project) -> Optional[HookResult]:
    """Store a prompt directly (tools and tests). Hooks use spool_prompt."""
    conn = state.connect()
    state.upsert_session(conn, inp.session_id, project=proj.name, host=inp.host,
                         cwd=inp.cwd, project_key_source=proj.source)
    with state.transaction(conn):
        record_prompt(conn, dict(prompt_payload(inp, proj, state.now_ms()), content_session_id=inp.session_id))
    return semantic_context(inp, proj)


def semantic_context(inp: NormalizedInput, proj: Project) -> Optional[HookResult]:
    text, private = privacy.clean_prompt(inp.prompt)
    if private or not settings.get("semantic_inject") or not text:
        return None
    try:
        from . import search
        block = search.semantic_block(state.connect(), proj.name, text, int(settings.get("semantic_inject_limit")))
    except Exception:
        return None
    return HookResult(additional_context=block) if block else None
