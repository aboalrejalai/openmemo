"""memo_save_memory: an agent saves one observation by hand.

Manual memories live in a per-project, per-day pseudo-session
(manual-<project>-<YYYYMMDD>[ -<name>[-<source>]]), so they appear in
context, search and the vault exactly like captured ones.
"""

from __future__ import annotations

import datetime
import re
from typing import Dict, List, Optional

from . import prompts, settings, state, store, writer
from .parser import Observation

_SOURCE_RE = re.compile(r"^[a-z0-9-]{0,16}$")
_NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")


def _manual_session_id(project: str, *, source: str = "") -> str:
    """Build the daily manual session id per D4."""
    proj = project.replace("/", "-")[:40]
    day = f"{datetime.date.today():%Y%m%d}"
    base = f"manual-{proj}-{day}"
    if settings.get("owns_unstamped"):
        return base
    name = settings.effective_name()
    if not _NAME_RE.fullmatch(name):
        # effective_name always produces a usable stamp; still guard
        name = re.sub(r"[^a-z0-9-]", "-", name.lower())[:32] or "unnamed"
    out = f"{base}-{name}"
    if source:
        out = f"{out}-{source}"
    return out


def save(title: str, text: str, *, project: str, obs_type: str = "discovery",
         facts: Optional[List[str]] = None, host: str = "mcp", conn=None,
         source: str = "") -> Dict:
    conn = conn or state.connect()
    title, text, project = (title or "").strip(), (text or "").strip(), (project or "").strip()
    if not title or not project:
        raise ValueError("'title' and 'project' are required")
    if source and not _SOURCE_RE.fullmatch(source):
        raise ValueError("source must match [a-z0-9-]{0,16}")
    if settings.get("owns_unstamped"):
        source = ""  # ignored on Mac / owns_unstamped machines
    if obs_type not in prompts.valid_types():
        obs_type = "discovery"
    sid = _manual_session_id(project, source=source)
    state.upsert_session(conn, sid, project=project, host=host, project_key_source="manual")
    if not state.get_prompt(conn, sid, 1):
        state.next_prompt_number(conn, sid)
        state.add_prompt(conn, sid, 1, "Memories saved by agents through memo_save_memory", private=False)
    session = state.get_session(conn, sid)
    obs = Observation(type=obs_type, title=title, narrative=text, facts=[f for f in (facts or []) if f])
    ids = store.save_observations(conn, session, 1, [obs], tokens=0, model="manual")
    if ids:
        writer.write_session(conn, sid, commit_subject=f"memo: saved memory for {project} — {title[:60]}")
    row = conn.execute("SELECT id FROM observations WHERE project = ? AND content_hash = ?",
                       (project, store.content_hash(obs))).fetchone()
    return {"id": row["id"] if row else None, "new": bool(ids), "session": sid,
            "markdown": state.get_session(conn, sid)["markdown_path"]}
