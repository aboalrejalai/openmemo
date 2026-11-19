"""Feed one session's queued events to its observer and keep what it writes.

Order per session, as in claude-mem:
  new prompt → init (first) or continuation prompt with the user's request
  each tool event → observation prompt → <observation>… or <skip_summary/>
  Stop → summary prompt → <summary>…
  SessionEnd → wrap-up into the vault, observer closed
A failure leaves the remaining items queued with backoff, never lost.
"""

from __future__ import annotations

import json
from typing import List, Optional

from . import parser, prompts, settings, state, store
from .observer import POOL, Observer, ObserverError
from .paths import log_line


def _hooks():
    """The vault side (Markdown writer and wrap-up), optional for unit tests."""
    try:
        from . import writer
        return writer
    except ImportError:
        return None


def _prompt_text(conn, sid: str, number: int) -> str:
    row = state.get_prompt(conn, sid, number)
    return (row["prompt_text"] if row else "") or "[no prompt text recorded]"


def _ensure_prompt_context(conn, obs: Observer, session, number: int) -> None:
    """Tell the observer about request `number` before its first event."""
    if int(session["observer_prompt"] or 0) >= number and obs.alive():
        return
    project = session["project"]
    language = settings.get("observer_language")
    text = _prompt_text(conn, session["content_session_id"], number)
    if not obs.alive() and not obs.session_id:
        message = prompts.init_prompt(project, text, language=language)
    else:
        # A resumed or recycled conversation gets the rules again.
        message = prompts.continuation_prompt(project, text, number, language=language,
                                              include_guidance=not obs.alive())
    obs.ask(message)
    state.update_session(conn, session["content_session_id"], observer_prompt=number,
                         observer_session_id=obs.session_id)


def _observer_for(conn, session) -> Observer:
    sid = session["content_session_id"]
    obs = POOL.get(sid, session["observer_session_id"])
    if obs.too_long():
        POOL.drop(sid)
        state.update_session(conn, sid, observer_session_id=None, observer_prompt=0)
        obs = POOL.get(sid, None)
    return obs


def _batches(rows) -> List[list]:
    """Group consecutive observation rows of the same prompt up to batch size."""
    size = max(1, int(settings.get("observer_batch_size")))
    out: List[list] = []
    for row in rows:
        last = out[-1] if out else None
        if (last and row["message_type"] == "observation" and last[0]["message_type"] == "observation"
                and last[0]["prompt_number"] == row["prompt_number"] and len(last) < size):
            last.append(row)
        else:
            out.append([row])
    return out


def process_session(content_session_id: str, conn=None) -> int:
    """Process every ready item of one session. Returns how many were done."""
    if conn is None:
        own = state.connect()
        try:
            return process_session(content_session_id, own)
        finally:
            own.close()
    done = 0
    vault = _hooks()
    while True:
        rows = state.claim_batch(conn, content_session_id)
        if not rows:
            return done
        groups = _batches(rows)
        for i, group in enumerate(groups):
            session = state.get_session(conn, content_session_id)
            kind = group[0]["message_type"]
            number = int(group[0]["prompt_number"] or 1)
            try:
                if kind == "session_end":
                    POOL.drop(content_session_id)
                    if vault:
                        vault.session_end(conn, session)
                    state.update_session(conn, content_session_id, status="ended")
                else:
                    _run_group(conn, session, kind, number, group, vault)
            except ObserverError as exc:
                log_line("observer", f"{content_session_id}: {exc}")
                failed = POOL.observers.get(content_session_id)
                if failed is not None and failed.resume and failed.session_id == failed.resume:
                    # Resuming never produced an answer: start a fresh conversation next time.
                    state.update_session(conn, content_session_id, observer_session_id=None, observer_prompt=0)
                POOL.drop(content_session_id)
                remaining = [r["id"] for g in groups[i:] for r in g]
                state.retry_later(conn, remaining, str(exc))
                return done
            state.finish(conn, [r["id"] for r in group])
            done += len(group)


def _run_group(conn, session, kind: str, number: int, group, vault) -> None:
    obs = _observer_for(conn, session)
    _ensure_prompt_context(conn, obs, session, number)
    session = state.get_session(conn, session["content_session_id"])
    if kind == "observation":
        payloads = [json.loads(r["payload"]) for r in group]
        message = "\n\n".join(prompts.observation_prompt(p.get("tool_name") or "", p.get("tool_input") or "",
                                                         p.get("tool_response") or "", p.get("cwd") or "",
                                                         p.get("observed_at_ms")) for p in payloads)
        answer = obs.ask(message)
        reply = parser.parse(answer.text, prompts.valid_types(), prompts.valid_concepts())
        first = payloads[0]
        ids = store.save_observations(conn, session, number, reply.observations, tokens=answer.tokens,
                                      model=answer.model, agent_id=first.get("agent_id"),
                                      agent_type=first.get("agent_type"))
        if ids and vault:
            vault.observations_saved(conn, session, ids)
    elif kind == "summarize":
        payload = json.loads(group[0]["payload"])
        answer = obs.ask(prompts.summary_prompt(payload.get("last_assistant_message") or ""))
        reply = parser.parse(answer.text, prompts.valid_types(), prompts.valid_concepts())
        if reply.observations:  # the model sometimes adds them here too
            store.save_observations(conn, session, number, reply.observations, tokens=0, model=answer.model)
        if reply.summary:
            summary_id = store.save_summary(conn, session, number, reply.summary, tokens=answer.tokens,
                                            model=answer.model)
            if vault:
                vault.summary_saved(conn, session, summary_id)
    state.update_session(conn, session["content_session_id"], observer_session_id=obs.session_id,
                         observed_model=answer.model or session["observed_model"])
