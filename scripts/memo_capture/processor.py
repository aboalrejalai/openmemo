"""Feed one session's queued events to the observer and keep what it writes.

Every observer call is a fresh conversation (since 0.1.1). A resumed
conversation re-read the whole session history on every call, so one small
tool event cost 50-90k tokens and grew with the session. Now each call carries
only the rules, the user's request, a few lines of prior context (the last
summary and the titles already recorded) and the new events:

  observation batch → <observation>… or <skip_summary/>
  summarize         → <summary>…  (skipped when the request left nothing)
  session_end       → wrap-up into the vault

The worker sends a session's events once it has been quiet for
`observer_quiet_sec` or has queued `observer_batch_size` events.
A failure leaves the remaining items queued with backoff, never lost.
"""

from __future__ import annotations

import json
from typing import List, Optional

from . import parser, prompts, settings, state, store
from .observer import POOL, Answer, ObserverError
from .paths import log_line

RECORDED_TITLES = 15          # titles shown so a fresh call does not repeat earlier observations
TRIVIAL_REPLY_CHARS = 200     # a request with no observations and a reply this short gets no summary


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


def _prior_summary(conn, sid: str) -> str:
    row = conn.execute("""SELECT request, completed, learned, next_steps FROM session_summaries
                          WHERE content_session_id = ? ORDER BY id DESC LIMIT 1""", (sid,)).fetchone()
    if not row:
        return ""
    labels = (("Request", "request"), ("Completed", "completed"), ("Learned", "learned"),
              ("Next steps", "next_steps"))
    return "\n".join(f"{label}: {row[key]}" for label, key in labels if row[key])


def _recorded_titles(conn, sid: str, number: Optional[int] = None, limit: int = RECORDED_TITLES) -> List[str]:
    sql = "SELECT title, subtitle FROM observations WHERE content_session_id = ?"
    args: list = [sid]
    if number is not None:
        sql += " AND prompt_number = ?"
        args.append(number)
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [f"{r['title']} — {r['subtitle']}" if r["subtitle"] else r["title"] for r in reversed(rows)]


def _ask(sid: str, text: str) -> Answer:
    """One fresh observer conversation: spawn, ask once, close."""
    obs = POOL.fresh(sid)
    try:
        return obs.ask(text)
    finally:
        POOL.drop(sid)


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
    """Process the ready items of one session. Returns how many were done.

    The first claim takes everything queued. Events that arrive while it runs
    wait for the next quiet period instead of costing a call each.
    """
    if conn is None:
        own = state.connect()
        try:
            return process_session(content_session_id, own)
        finally:
            own.close()
    done = 0
    vault = _hooks()
    quiet_ms = int(settings.get("observer_quiet_sec") or 0) * 1000
    batch = max(1, int(settings.get("observer_batch_size") or 1))
    first = True
    while True:
        if not first and not state.session_ready(conn, content_session_id, quiet_ms, batch):
            return done
        first = False
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
                POOL.drop(content_session_id)
                remaining = [r["id"] for g in groups[i:] for r in g]
                state.retry_later(conn, remaining, str(exc))
                return done
            state.finish(conn, [r["id"] for r in group])
            done += len(group)


def _run_group(conn, session, kind: str, number: int, group, vault) -> None:
    sid = session["content_session_id"]
    project = session["project"]
    language = settings.get("observer_language")
    request = _prompt_text(conn, sid, number)
    if kind == "observation":
        payloads = [json.loads(r["payload"]) for r in group]
        message = prompts.batch_prompt(project, request, number, payloads,
                                       prior_summary=_prior_summary(conn, sid),
                                       recorded_titles=_recorded_titles(conn, sid), language=language)
        answer = _ask(sid, message)
        reply = parser.parse(answer.text, prompts.valid_types(), prompts.valid_concepts())
        first = payloads[0]
        ids = store.save_observations(conn, session, number, reply.observations, tokens=answer.tokens,
                                      model=answer.model, agent_id=first.get("agent_id"),
                                      agent_type=first.get("agent_type"))
        if ids and vault:
            vault.observations_saved(conn, session, ids)
    elif kind == "summarize":
        payload = json.loads(group[0]["payload"])
        last = payload.get("last_assistant_message") or ""
        observed = _recorded_titles(conn, sid, number, limit=40)
        if not observed and len(last.strip()) < TRIVIAL_REPLY_CHARS:
            return      # nothing durable happened in this request: no call
        answer = _ask(sid, prompts.summary_request(project, request, number, last,
                                                   prior_summary=_prior_summary(conn, sid),
                                                   observations=observed, language=language))
        reply = parser.parse(answer.text, prompts.valid_types(), prompts.valid_concepts())
        if reply.observations:  # the model sometimes adds them here too
            store.save_observations(conn, session, number, reply.observations, tokens=0, model=answer.model)
        if reply.summary:
            summary_id = store.save_summary(conn, session, number, reply.summary, tokens=answer.tokens,
                                            model=answer.model)
            if vault:
                vault.summary_saved(conn, session, summary_id)
    else:
        return
    state.update_session(conn, sid, observer_session_id=answer.session_id,
                         observed_model=answer.model or session["observed_model"])
