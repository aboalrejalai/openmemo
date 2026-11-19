"""`memo reindex`: rebuild <vault>/.memo/state.db from the vault's session notes.

The vault is the source of truth: each session note
sessions/<project>/<date>/<session>.md has its exact data beside it in
<session>.json, which this reads back, keeping the original ids so links
such as "obs-12" stay valid. (Notes from before the sidecar carried the
same data in `<!-- memo:... -->` comments; those are still read.) The queue (pending_messages), raw tool backups and spool
bookkeeping are machine state and are left alone.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import settings, state, store
from .paths import vault_dir

_COMMENT = re.compile(r"<!-- memo:(session|prompt|obs|summary) (\{.*?\}) -->")
_TABLES = ("observation_files", "observations", "session_summaries", "user_prompts", "sessions",
           "file_context_seen")


def parse_note(text: str) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {"session": [], "prompt": [], "obs": [], "summary": []}
    for kind, raw in _COMMENT.findall(text):
        try:
            out[kind].append(json.loads(raw))
        except ValueError:
            continue
    return out


def session_notes(vault: Path) -> List[Path]:
    """Every recorded session as its note path, whether or not the .md exists.

    A sidecar without its note (a note moved out of the vault by hand) still
    counts: the .json holds every field, so the session is never dropped.
    """
    root = vault / "sessions"
    paths = {p for p in root.rglob("*.md")}
    for sidecar in root.rglob("*.json"):
        try:
            with open(sidecar, encoding="utf-8") as fh:
                head = fh.read(64)
        except OSError:
            continue
        if '"memo_session_data"' in head:
            paths.add(sidecar.with_suffix(".md"))
    return sorted(paths)


def load_note(md_path: Path) -> Dict[str, List[dict]]:
    sidecar = md_path.with_suffix(".json")
    if sidecar.exists():
        try:
            d = json.loads(sidecar.read_text(encoding="utf-8"))
            return {"session": [d["session"]], "prompt": d.get("prompts", []),
                    "obs": d.get("observations", []), "summary": d.get("summaries", [])}
        except (ValueError, KeyError):
            pass
    if not md_path.exists():
        return {"session": [], "prompt": [], "obs": [], "summary": []}
    return parse_note(md_path.read_text(encoding="utf-8"))


def insert_prompt(conn, sid: str, p: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO user_prompts (content_session_id, prompt_number, prompt_text, "
                 "private, created_at) VALUES (?, ?, ?, 0, ?)", (sid, p["n"], p["text"], p.get("at") or 0))


def insert_observation(conn, sid: str, project: str, rel: str, o: dict, keep_id: bool = True,
                       force_id: Optional[int] = None) -> int:
    """One observation from a sidecar, with its search rows. Returns its id."""
    if force_id is not None:
        oid = force_id
    elif keep_id:
        oid = o["id"]
    else:
        oid = store.next_id(conn, "observations")
    cur = conn.execute(
        """INSERT OR REPLACE INTO observations (id, content_session_id, project, prompt_number, type, title,
               subtitle, facts, narrative, concepts, files_read, files_modified, content_hash,
               occurrence_count, discovery_tokens, generated_by_model, agent_id, agent_type,
               markdown_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (oid, sid, project, o.get("prompt"), o["type"], o["title"], o.get("subtitle"),
         state.dumps(o.get("facts")), o.get("narrative"), state.dumps(o.get("concepts")),
         state.dumps(o.get("files_read")), state.dumps(o.get("files_modified")), o["hash"],
         o.get("count") or 1, o.get("tokens") or 0, o.get("model"), o.get("agent_id"),
         o.get("agent_type"), rel, o["at"]))
    conn.execute("INSERT INTO observations_fts(rowid, title, subtitle, narrative, facts, concepts) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (oid, o["title"], o.get("subtitle") or "", o.get("narrative") or "",
                  " ".join(o.get("facts") or []), " ".join(o.get("concepts") or [])))
    if state.has_trigram(conn):
        conn.execute("INSERT INTO memory_trigram(kind, ref, body) VALUES ('observation', ?, ?)",
                     (oid, " ".join([o["title"], o.get("subtitle") or "", o.get("narrative") or "",
                                     *(o.get("facts") or [])])))
    conn.executemany("INSERT OR IGNORE INTO observation_files(observation_id, path, kind) VALUES (?, ?, ?)",
                     [(oid, f, "read") for f in o.get("files_read") or []] +
                     [(oid, f, "modified") for f in o.get("files_modified") or []])
    return oid


def insert_summary(conn, sid: str, project: str, m: dict, keep_id: bool = True,
                   force_id: Optional[int] = None) -> int:
    if force_id is not None:
        mid = force_id
    elif keep_id:
        mid = m["id"]
    else:
        mid = store.next_id(conn, "session_summaries")
    conn.execute("DELETE FROM session_summaries WHERE id = ?", (mid,))
    conn.execute(
        """INSERT INTO session_summaries (id, content_session_id, project, prompt_number, request,
               investigated, learned, completed, next_steps, notes, files_read, files_edited,
               discovery_tokens, generated_by_model, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mid, sid, project, m.get("prompt"), m.get("request"), m.get("investigated"),
         m.get("learned"), m.get("completed"), m.get("next_steps"), m.get("notes"),
         state.dumps(m.get("files_read")), state.dumps(m.get("files_edited")), m.get("tokens") or 0,
         m.get("model"), m["at"]))
    conn.execute("INSERT INTO summaries_fts(rowid, request, investigated, learned, completed, next_steps, "
                 "notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (mid, m.get("request") or "", m.get("investigated") or "", m.get("learned") or "",
                  m.get("completed") or "", m.get("next_steps") or "", m.get("notes") or ""))
    if state.has_trigram(conn):
        conn.execute("INSERT INTO memory_trigram(kind, ref, body) VALUES ('summary', ?, ?)",
                     (mid, " ".join(str(m.get(k) or "") for k in
                                    ("request", "investigated", "learned", "completed", "next_steps", "notes"))))
    return mid


@dataclass
class AdoptResult:
    added: int = 0
    collisions: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


def is_own(stamp: Optional[str]) -> bool:
    """True when this machine owns the session stamp (D5 / Terms)."""
    name = settings.effective_name()
    if stamp:
        return stamp == name
    return bool(settings.get("owns_unstamped"))


def adopt_data(conn, data: dict, rel: str) -> AdoptResult:
    """Adopt parsed note data into the index (D5). One id rule for sync and rebuild."""
    result = AdoptResult()
    if not data or not data.get("session"):
        return result
    s = data["session"][0] if isinstance(data["session"], list) else data["session"]
    sid = s.get("session_id") or s.get("id")
    if not sid:
        result.error = "missing session_id"
        return result
    project = s.get("project") or ""
    stamp = s.get("machine")
    own = is_own(stamp)
    prompts = data.get("prompt") or data.get("prompts") or []
    observations = data.get("obs") or data.get("observations") or []
    summaries = data.get("summary") or data.get("summaries") or []

    existing = state.get_session(conn, sid)
    if existing is None:
        counter = max(
            [p.get("n") or p.get("prompt_number") or 0 for p in prompts]
            + [o.get("prompt") or 1 for o in observations]
            + [0]
        )
        status = "ended" if not own else (s.get("status") or "ended")
        if not own:
            status = "ended"
        conn.execute(
            """INSERT INTO sessions (content_session_id, host, project, project_key_source, cwd,
                   observer_session_id, observed_model, prompt_counter, status, markdown_path,
                   machine, started_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, s.get("host") or "claude-code", project, s.get("project_key_source"),
             s.get("cwd"), s.get("observer_session_id"), s.get("model") or s.get("observed_model"),
             counter, status, rel, stamp, s["started_at"],
             s.get("updated_at") or s["started_at"]))
        result.added += 1
    else:
        if own:
            counter = max([p.get("n") or 0 for p in prompts] + [0])
            if counter:
                conn.execute(
                    "UPDATE sessions SET prompt_counter = MAX(prompt_counter, ?) WHERE content_session_id = ?",
                    (counter, sid))
        else:
            # Foreign: update from newer sidecar only
            new_updated = s.get("updated_at") or 0
            if new_updated > (existing["updated_at"] or 0):
                counter = max(
                    [p.get("n") or 0 for p in prompts]
                    + [o.get("prompt") or 1 for o in observations]
                    + [existing["prompt_counter"] or 0]
                )
                conn.execute(
                    """UPDATE sessions SET updated_at = ?, prompt_counter = ?, markdown_path = ?,
                           observed_model = COALESCE(?, observed_model)
                       WHERE content_session_id = ?""",
                    (new_updated, counter, rel, s.get("model") or s.get("observed_model"), sid))

    # Prompts
    have_prompts = {r[0] for r in conn.execute(
        "SELECT prompt_number FROM user_prompts WHERE content_session_id = ?", (sid,))}
    for p in prompts:
        n = p.get("n") if "n" in p else p.get("prompt_number")
        if n not in have_prompts:
            insert_prompt(conn, sid, {"n": n, "text": p.get("text") or p.get("prompt_text") or "",
                                      "at": p.get("at") or p.get("created_at") or 0})
            result.added += 1

    # Observations
    have_hash = {r[0]: r[1] for r in conn.execute(
        "SELECT content_hash, id FROM observations WHERE project = ?", (project,))}
    for o in observations:
        h = o.get("hash") or o.get("content_hash")
        if h and h in have_hash:
            continue  # same content already present
        oid = o.get("id")
        taken = conn.execute("SELECT id, content_hash FROM observations WHERE id = ?", (oid,)).fetchone()
        if taken is None:
            insert_observation(conn, sid, project, rel, o, keep_id=True)
            result.added += 1
            have_hash[h] = oid
            continue
        # id taken by a different row
        if taken["content_hash"] == h:
            continue
        if own:
            new_id = store.next_id(conn, "observations")
            insert_observation(conn, sid, project, rel, o, force_id=new_id)
            result.added += 1
            have_hash[h] = new_id
        else:
            result.collisions.append({
                "kind": "observation", "id": oid, "session": sid, "path": rel,
                "error": "id taken",
            })

    # Summaries keyed by (session, prompt)
    for m in summaries:
        prompt_n = m.get("prompt") if "prompt" in m else m.get("prompt_number")
        existing_sum = conn.execute(
            "SELECT id, created_at FROM session_summaries WHERE content_session_id = ? AND prompt_number = ?",
            (sid, prompt_n)).fetchone()
        at = m.get("at") or m.get("created_at") or 0
        if existing_sum is None:
            taken = conn.execute("SELECT 1 FROM session_summaries WHERE id = ?", (m["id"],)).fetchone()
            if taken and not own:
                result.collisions.append({
                    "kind": "summary", "id": m["id"], "session": sid, "path": rel, "error": "id taken",
                })
                continue
            insert_summary(conn, sid, project, m, keep_id=not bool(taken))
            result.added += 1
        elif at > (existing_sum["created_at"] or 0):
            # Replace with sidecar's row under sidecar's id
            conn.execute("DELETE FROM summaries_fts WHERE rowid = ?", (existing_sum["id"],))
            conn.execute("DELETE FROM session_summaries WHERE id = ?", (existing_sum["id"],))
            if state.has_trigram(conn):
                conn.execute("DELETE FROM memory_trigram WHERE kind = 'summary' AND ref = ?",
                             (existing_sum["id"],))
            taken = conn.execute("SELECT 1 FROM session_summaries WHERE id = ?", (m["id"],)).fetchone()
            if taken and m["id"] != existing_sum["id"]:
                if own:
                    insert_summary(conn, sid, project, m, keep_id=False)
                else:
                    result.collisions.append({
                        "kind": "summary", "id": m["id"], "session": sid, "path": rel, "error": "id taken",
                    })
                    continue
            else:
                insert_summary(conn, sid, project, m, keep_id=True)
            result.added += 1
    return result


def adopt_sidecar(conn, *args) -> AdoptResult:  # noqa: F811
    """Adopt a sidecar. Accepts (conn, sidecar_path, rel) or legacy (conn, sid, sidecar, rel)."""
    if len(args) == 3 and isinstance(args[0], str):
        sid, sidecar, rel = args
    elif len(args) == 2:
        sidecar, rel = args
        sid = ""
    elif len(args) == 1:
        sidecar, rel, sid = args[0], "", ""
    else:
        raise TypeError(f"adopt_sidecar: bad args {args!r}")
    sidecar = Path(sidecar)
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return AdoptResult(error=str(exc))
    if data.get("memo_session_data") != 1:
        return AdoptResult(error="not a memo session sidecar")
    if not rel:
        try:
            rel = sidecar.resolve().relative_to(vault_dir()).with_suffix(".md").as_posix()
        except ValueError:
            rel = sidecar.with_suffix(".md").as_posix()
    sess = dict(data["session"])
    if "session_id" not in sess:
        sess["session_id"] = sid or sidecar.stem
    payload = {
        "session": [sess],
        "prompt": data.get("prompts") or [],
        "obs": data.get("observations") or [],
        "summary": data.get("summaries") or [],
    }
    with state.transaction(conn):
        return adopt_data(conn, payload, rel)


def rebuild(conn, vault: Path) -> Dict[str, Any]:
    with state.transaction(conn):
        return _rebuild(conn, vault)


def _rebuild(conn, vault: Path) -> Dict[str, Any]:
    for table in _TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.execute("INSERT INTO observations_fts(observations_fts) VALUES ('delete-all')")
    conn.execute("INSERT INTO summaries_fts(summaries_fts) VALUES ('delete-all')")
    if state.has_trigram(conn):
        conn.execute("DELETE FROM memory_trigram")
    counts: Dict[str, Any] = {"notes": 0, "sessions": 0, "observations": 0, "summaries": 0,
                              "collisions": []}
    for path in session_notes(vault):
        rel = path.relative_to(vault).as_posix()
        sidecar = path.with_suffix(".json")
        data = None
        if sidecar.exists():
            try:
                raw = json.loads(sidecar.read_text(encoding="utf-8"))
                if raw.get("memo_session_data") == 1:
                    data = {
                        "session": [raw["session"]],
                        "prompt": raw.get("prompts") or [],
                        "obs": raw.get("observations") or [],
                        "summary": raw.get("summaries") or [],
                    }
                    if "session_id" not in data["session"][0]:
                        data["session"][0]["session_id"] = raw["session"].get("session_id") or path.stem
            except (OSError, ValueError, KeyError):
                data = None
        if data is None:
            data = load_note(path)
        if not data["session"]:
            continue
        counts["notes"] += 1
        result = adopt_data(conn, data, rel)
        if result.collisions:
            counts["collisions"].extend(result.collisions)
        # recount roughly
        s = data["session"][0]
        sid = s.get("session_id")
        if state.get_session(conn, sid):
            counts["sessions"] += 1
        counts["observations"] += len(data.get("obs") or [])
        counts["summaries"] += len(data.get("summary") or [])
    return counts


def main(args: List[str]) -> int:
    counts = rebuild(state.connect(), vault_dir())
    collisions = counts.get("collisions") or []
    print(f"Rebuilt state from {counts['notes']} session note(s): {counts['sessions']} session(s), "
          f"{counts['observations']} observation(s), {counts['summaries']} summary(ies).")
    if collisions:
        print(f"{len(collisions)} collision(s)")
        for c in collisions:
            print(f"  {c.get('kind')} id={c.get('id')} session={c.get('session')} path={c.get('path')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
