"""Persist what the observer wrote: state rows (with dedup) and the vault.

Every saved observation and summary also goes to the Markdown writer, which
keeps sessions/<project>/<date>/<session>.md in the vault in step.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Dict, List, Optional

from . import state
from .parser import Observation, Summary


def content_hash(obs: Observation) -> str:
    """Same title + facts in the same project = the same memory."""
    norm = lambda s: re.sub(r"\s+", " ", (s or "").strip().lower())
    basis = json.dumps([norm(obs.type), norm(obs.title), sorted(norm(f) for f in obs.facts)], ensure_ascii=False)
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


WINDOW = 1_000_000_000


def next_id(conn, table: str) -> int:
    """Allocate the next observation/summary id inside this machine's window (D4).

    Raises RuntimeError when machine.json is malformed so new rows wait in the
    queue instead of landing in window 0 by accident.
    """
    from . import settings
    settings.load()
    if settings.machine_json_malformed():
        raise RuntimeError(
            f"malformed machine.json at {settings.machine_path()}: "
            "refusing to allocate an observation id"
        )
    base = int(settings.get("id_base") or 0)
    lo, hi = base, base + WINDOW
    row = conn.execute(
        f"SELECT MAX(id) AS m FROM {table} WHERE id >= ? AND id < ?", (lo, hi)
    ).fetchone()
    current = row["m"] if row and row["m"] is not None else None
    if current is None:
        return base + 1
    return int(current) + 1


def _index(conn, kind: str, ref: int, body: str) -> None:
    if state.has_trigram(conn):
        conn.execute("INSERT INTO memory_trigram(kind, ref, body) VALUES (?, ?, ?)", (kind, ref, body))


def save_observations(conn, session, prompt_number: int, observations: List[Observation], *,
                      tokens: int, model: Optional[str], agent_id: Optional[str] = None,
                      agent_type: Optional[str] = None, created_at: Optional[int] = None) -> List[int]:
    """Insert new observations; bump occurrence_count for repeats. Returns new ids."""
    if not observations:
        return []
    with state.transaction(conn):
        return _save_observations(conn, session, prompt_number, observations, tokens=tokens, model=model,
                                  agent_id=agent_id, agent_type=agent_type, created_at=created_at)


def _save_observations(conn, session, prompt_number, observations, *, tokens, model, agent_id, agent_type,
                       created_at) -> List[int]:
    new_ids: List[int] = []
    share = tokens // max(1, len(observations))
    for obs in observations:
        digest = content_hash(obs)
        existing = conn.execute("SELECT id FROM observations WHERE project = ? AND content_hash = ?",
                                (session["project"], digest)).fetchone()
        if existing:
            conn.execute("UPDATE observations SET occurrence_count = occurrence_count + 1 WHERE id = ?",
                         (existing["id"],))
            continue
        oid = next_id(conn, "observations")
        cur = conn.execute(
            """INSERT INTO observations (id, content_session_id, project, prompt_number, type, title, subtitle,
                   facts, narrative, concepts, files_read, files_modified, content_hash, discovery_tokens,
                   generated_by_model, agent_id, agent_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (oid, session["content_session_id"], session["project"], prompt_number, obs.type, obs.title,
             obs.subtitle, state.dumps(obs.facts), obs.narrative, state.dumps(obs.concepts),
             state.dumps(obs.files_read), state.dumps(obs.files_modified), digest, share, model,
             agent_id, agent_type, created_at or state.now_ms()))
        new_ids.append(oid)
        conn.execute("INSERT INTO observations_fts(rowid, title, subtitle, narrative, facts, concepts) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (oid, obs.title, obs.subtitle, obs.narrative, " ".join(obs.facts), " ".join(obs.concepts)))
        _index(conn, "observation", oid, " ".join([obs.title, obs.subtitle, obs.narrative, *obs.facts]))
        conn.executemany("INSERT OR IGNORE INTO observation_files(observation_id, path, kind) VALUES (?, ?, ?)",
                         [(oid, p, "read") for p in obs.files_read] +
                         [(oid, p, "modified") for p in obs.files_modified])
    return new_ids


def files_for_prompt(conn, content_session_id: str, prompt_number: int) -> Dict[str, List[str]]:
    rows = conn.execute("""SELECT f.path, f.kind FROM observation_files f JOIN observations o
                           ON o.id = f.observation_id WHERE o.content_session_id = ? AND o.prompt_number = ?
                           ORDER BY f.path""", (content_session_id, prompt_number)).fetchall()
    out = {"read": [], "modified": []}
    for r in rows:
        if r["path"] not in out[r["kind"]]:
            out[r["kind"]].append(r["path"])
    return out


def save_summary(conn, session, prompt_number: int, summary: Summary, *, tokens: int,
                 model: Optional[str], created_at: Optional[int] = None) -> int:
    with state.transaction(conn):
        return _save_summary(conn, session, prompt_number, summary, tokens=tokens, model=model,
                             created_at=created_at)


def _save_summary(conn, session, prompt_number, summary, *, tokens, model, created_at) -> int:
    files = files_for_prompt(conn, session["content_session_id"], prompt_number)
    conn.execute("DELETE FROM summaries_fts WHERE rowid IN (SELECT id FROM session_summaries "
                 "WHERE content_session_id = ? AND prompt_number = ?)",
                 (session["content_session_id"], prompt_number))
    # REPLACE path: keep existing id when updating the same (session, prompt)
    existing = conn.execute(
        "SELECT id FROM session_summaries WHERE content_session_id = ? AND prompt_number = ?",
        (session["content_session_id"], prompt_number)).fetchone()
    sid = int(existing["id"]) if existing else next_id(conn, "session_summaries")
    conn.execute("DELETE FROM session_summaries WHERE content_session_id = ? AND prompt_number = ?",
                 (session["content_session_id"], prompt_number))
    cur = conn.execute(
        """INSERT INTO session_summaries (id, content_session_id, project, prompt_number, request,
               investigated, learned, completed, next_steps, notes, files_read, files_edited,
               discovery_tokens, generated_by_model, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sid, session["content_session_id"], session["project"], prompt_number, summary.request,
         summary.investigated, summary.learned, summary.completed, summary.next_steps, summary.notes,
         state.dumps(files["read"]), state.dumps(files["modified"]), tokens, model,
         created_at or state.now_ms()))
    conn.execute("INSERT INTO summaries_fts(rowid, request, investigated, learned, completed, next_steps, notes) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (sid, summary.request, summary.investigated, summary.learned, summary.completed,
                  summary.next_steps, summary.notes))
    _index(conn, "summary", sid, " ".join([summary.request, summary.investigated, summary.learned,
                                           summary.completed, summary.next_steps, summary.notes]))
    return sid
