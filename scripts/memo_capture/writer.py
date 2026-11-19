"""Write session memory into the vault as Markdown.

One file per session: sessions/<project>/<YYYY-MM-DD>/<session8>.md

The file is regenerated from state.db after every observer reply, so it is
always complete and ordered. Its exact data sits next to it in
<session8>.json, which `memo reindex` reads to rebuild state.db; keeping it
out of the note keeps the note (and its Second Brain copy) readable. Writes
take the vault lock; one git commit per finished turn (when its summary
lands) and one at session end.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import project as projects
from . import prompts, settings, state
from .paths import log_line, vault_dir


def _core():
    scripts = str(Path(__file__).resolve().parent.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import memo_core
    return memo_core


def _day(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _time(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def _iso(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def file_stem(content_session_id: str) -> str:
    """Fallback stem: first 8 of a UUID, else a safe id. Prefer topic_slug for new notes."""
    if _UUID.match(content_session_id):
        return content_session_id[:8]
    return re.sub(r"[^\w.-]+", "-", content_session_id).strip("-.")[:80] or "session"


def topic_slug(topic: str, max_len: int = 72) -> str:
    """Human filename for Obsidian Graph: readable title, no UUID suffix."""
    text = (topic or "session").strip().splitlines()[0]
    text = re.sub(r"<redacted type='[^']*'/>", "redacted", text)
    text = re.sub(r"\s+", "-", text)
    # Keep letters/digits/Arabic/dash/dot/underscore
    text = re.sub(r"[^\w\u0600-\u06FF.-]+", "", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip(".-") or "session"
    return text[:max_len].rstrip(".-") or "session"


def session_rel_path(session, topic: Optional[str] = None) -> str:
    """sessions/<project>/<YYYY-MM-DD>/<readable-slug>.md — Graph shows the slug, not a UUID."""
    key = projects.folder_key(session["project"])
    day = _day(int(session["started_at"]))
    slug = topic_slug(topic) if topic else file_stem(session["content_session_id"])
    return f"sessions/{key}/{day}/{slug}.md"



def _session_topic(conn, sid: str) -> str:
    """First user prompt or summary request — drives the Obsidian filename."""
    row = conn.execute(
        "SELECT prompt_text FROM user_prompts WHERE content_session_id = ? AND private = 0 "
        "AND prompt_text IS NOT NULL AND prompt_text != '' ORDER BY prompt_number LIMIT 1",
        (sid,)).fetchone()
    if row and row["prompt_text"]:
        return row["prompt_text"].strip().splitlines()[0]
    row = conn.execute(
        "SELECT request FROM session_summaries WHERE content_session_id = ? "
        "AND request IS NOT NULL AND request != '' ORDER BY prompt_number LIMIT 1",
        (sid,)).fetchone()
    if row and row["request"]:
        return row["request"].strip().splitlines()[0]
    return "session"


def _unique_session_rel(session, topic: str) -> str:
    """Readable slug path; on same-day collision append -2, -3… (never a UUID in the name)."""
    base = session_rel_path(session, topic)
    vault = vault_dir()
    sid = session["content_session_id"]
    current = session["markdown_path"] if "markdown_path" in session.keys() else None
    if current:
        # Keep a stable human path once chosen, even if the topic drifts.
        cur = vault / current
        if cur.exists() or (vault / Path(current).with_suffix(".json")).exists():
            # If already human-named (not an 8-char hex stem), keep it.
            stem = Path(current).stem
            if not (len(stem) == 8 and all(c in "0123456789abcdef" for c in stem.lower())):
                return current
    path = Path(base)
    if not (vault / path).exists() and not (vault / path.with_suffix(".json")).exists():
        return base
    # Exists: same session already owns it?
    try:
        existing = (vault / path).read_text(encoding="utf-8")[:800]
        if f'session_id: "{sid}"' in existing or f"session_id: '{sid}'" in existing:
            return base
        if f'session_id: "{sid[:8]}' in existing:
            return base
    except OSError:
        pass
    stem, parent, suffix = path.stem, path.parent, path.suffix
    for n in range(2, 50):
        candidate = parent / f"{stem}-{n}{suffix}"
        if not (vault / candidate).exists():
            return candidate.as_posix()
    # Last resort: short disambiguator from id (still not the bare UUID filename)
    return (parent / f"{stem}-{sid[:4]}{suffix}").as_posix()


def _project_note(session) -> Optional[str]:
    try:
        return projects.vault_note(projects.Project(session["project"], session["project_key_source"] or "", ""),
                                   vault_dir())
    except Exception:
        return None


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def obs_row_data(row) -> Dict:
    return {"id": row["id"], "prompt": row["prompt_number"], "type": row["type"], "title": row["title"],
            "subtitle": row["subtitle"], "facts": state.loads(row["facts"]), "narrative": row["narrative"],
            "concepts": state.loads(row["concepts"]), "files_read": state.loads(row["files_read"]),
            "files_modified": state.loads(row["files_modified"]), "hash": row["content_hash"],
            "count": row["occurrence_count"], "tokens": row["discovery_tokens"],
            "model": row["generated_by_model"], "agent_id": row["agent_id"], "agent_type": row["agent_type"],
            "at": row["created_at"]}


def summary_row_data(row) -> Dict:
    return {"id": row["id"], "prompt": row["prompt_number"], "request": row["request"],
            "investigated": row["investigated"], "learned": row["learned"], "completed": row["completed"],
            "next_steps": row["next_steps"], "notes": row["notes"],
            "files_read": state.loads(row["files_read"]), "files_edited": state.loads(row["files_edited"]),
            "tokens": row["discovery_tokens"], "model": row["generated_by_model"], "at": row["created_at"]}


def render_session(conn, sid: str) -> Optional[str]:
    session = state.get_session(conn, sid)
    if not session:
        return None
    obs = conn.execute("SELECT * FROM observations WHERE content_session_id = ? ORDER BY prompt_number, id",
                       (sid,)).fetchall()
    sums = {r["prompt_number"]: r for r in conn.execute(
        "SELECT * FROM session_summaries WHERE content_session_id = ? ORDER BY prompt_number", (sid,))}
    prompts_rows = {r["prompt_number"]: r for r in conn.execute(
        "SELECT * FROM user_prompts WHERE content_session_id = ? ORDER BY prompt_number", (sid,))}
    numbers = sorted({*(r["prompt_number"] or 1 for r in obs), *sums.keys(),
                      *(n for n, p in prompts_rows.items() if not p["private"])})
    status = "completed" if session["status"] == "ended" else "active"
    first_prompt = next((prompts_rows[n]["prompt_text"] for n in numbers
                         if n in prompts_rows and prompts_rows[n]["prompt_text"]), "")
    first_request = next((sums[n]["request"] for n in numbers if n in sums and sums[n]["request"]), "")
    topic = (first_prompt or first_request or "session").strip().splitlines()[0]
    topic = re.sub(r"<redacted type='[^']*'/>", "[redacted]", topic)
    if len(topic) > 80:
        topic = topic[:79].rstrip() + "…"
    title = f"{session['project']} · {_day(int(session['started_at']))} · {topic}"
    note = _project_note(session)
    # Always link a project dossier so Obsidian Graph groups sessions under the project.
    if note:
        project_link = note[:-3]  # projects/foo.md → projects/foo
    else:
        project_link = f"projects/{projects.folder_key(session['project']).split('/')[0]}"
    # Graph shows the filename; keep H1 = human topic (no UUID).
    display = topic if topic and topic != "session" else title
    lines = [
        "---",
        f"title: {_yaml(display)}",
        "type: session",
        f"status: {status}",
        "tags:",
        "  - type/session",
        f"  - status/{status}",
        f"project: {_yaml(session['project'])}",
        f"session_id: {_yaml(sid)}",
        f"host: {_yaml(session['host'])}",
        f"model: {_yaml(session['observed_model'] or settings.get('observer_model'))}",
        f"cwd: {_yaml(session['cwd'] or '')}",
        f"started: {_yaml(_iso(int(session['started_at'])))}",
        f"updated: {_yaml(_iso(int(session['updated_at'])))}",
        f"created: {_day(int(session['started_at']))}",
        "memo_session: true",
        f"project_note: {_yaml('[[' + project_link + ']]')}",
    ]
    lines += ["---", "", f"# {display}", ""]
    meta = (f"**Project:** [[{project_link}|{session['project']}]]"
            f" · **Host:** {session['host']}"
            f" · **Started:** {_iso(int(session['started_at'])).replace('T', ' ')}"
            f" · **Session:** `{sid[:8]}`")
    lines += [meta, ""]
    by_prompt: Dict[int, List] = {}
    for row in obs:
        by_prompt.setdefault(row["prompt_number"] or 1, []).append(row)
    for n in numbers:
        p = prompts_rows.get(n)
        if p is not None and p["private"]:
            continue
        lines.append(f"## Request {n}")
        lines.append("")
        if p is not None and p["prompt_text"]:
            quoted = "\n".join("> " + l for l in p["prompt_text"].strip().splitlines()[:40])
            lines += [quoted, ""]
        s = sums.get(n)
        if s is not None:
            lines.append("### Summary")
            lines.append("")
            for label, key in (("Request", "request"), ("Investigated", "investigated"), ("Learned", "learned"),
                               ("Completed", "completed"), ("Next steps", "next_steps"), ("Notes", "notes")):
                if s[key]:
                    lines.append(f"- **{label}:** {s[key]}")
            edited = state.loads(s["files_edited"])
            if edited:
                lines.append("- **Files edited:** " + ", ".join(f"`{f}`" for f in edited))
            lines.append("")
        rows = by_prompt.get(n, [])
        if rows:
            lines += [f"### Observations ({len(rows)})", ""]
        for row in rows:
            lines.append(_obs_line(row))
        if rows:
            lines.append("")
    lines += ["---", "",
              "*Each observation's facts, narrative and files are in the `.json` beside this note, "
              "and through `memo_get_observations([ids])`.*"]
    return "\n".join(lines).rstrip() + "\n"


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _obs_line(row) -> str:
    """One short line per observation, so a long session stays a note Obsidian
    can open (a 5.4 MB note with 40,000-character lines crashed it, 2026-11-01)."""
    line = (f"- obs-{row['id']} · {prompts.type_emoji(row['type'])} {row['type']} · {_clip(row['title'], 200)}"
            f" *({_time(int(row['created_at']))})*")
    modified = state.loads(row["files_modified"])
    if modified:
        shown = ", ".join(f"`{_clip(f, 80)}`" for f in modified[:3])
        line += f" — {shown}" + (f" +{len(modified) - 3}" if len(modified) > 3 else "")
    return line


def session_data(conn, sid: str) -> Dict:
    """Everything reindex needs to rebuild this session exactly."""
    from . import settings as _settings
    s = state.get_session(conn, sid)
    machine = None
    if s is not None and "machine" in s.keys() and s["machine"]:
        machine = s["machine"]
    else:
        machine = _settings.effective_name()
    return {
        "memo_session_data": 1,
        "session": {"session_id": sid, "project": s["project"], "project_key_source": s["project_key_source"],
                    "host": s["host"], "cwd": s["cwd"], "started_at": s["started_at"],
                    "updated_at": s["updated_at"], "status": s["status"],
                    "observer_session_id": s["observer_session_id"], "model": s["observed_model"],
                    "machine": machine},
        "prompts": [{"n": r["prompt_number"], "text": r["prompt_text"], "at": r["created_at"]} for r in conn.execute(
            "SELECT * FROM user_prompts WHERE content_session_id = ? AND private = 0 ORDER BY prompt_number",
            (sid,))],
        "observations": [obs_row_data(r) for r in conn.execute(
            "SELECT * FROM observations WHERE content_session_id = ? ORDER BY id", (sid,))],
        "summaries": [summary_row_data(r) for r in conn.execute(
            "SELECT * FROM session_summaries WHERE content_session_id = ? ORDER BY prompt_number", (sid,))],
    }


_STAMP_KEYS = ("second-brain-id", "second-brain-synced")


def keep_sync_stamps(old: str, new: str) -> str:
    """Carry the Second Brain sync plugin's frontmatter keys into a regenerated note.

    The plugin updates the cloud memories listed in second-brain-id; dropping
    them would make its next sync create duplicates.
    """
    if not old.startswith("---"):
        return new
    end = old.find("\n---", 3)
    lines = old[3:end].splitlines() if end != -1 else []
    kept, keep = [], False
    for line in lines:
        if any(line.startswith(k + ":") for k in _STAMP_KEYS):
            keep = True
            kept.append(line)
        elif keep and line.startswith("  - "):
            kept.append(line)
        else:
            keep = False
    if not kept or not new.startswith("---"):
        return new
    close = new.find("\n---", 3)
    return new[:close] + "\n" + "\n".join(kept) + new[close:]


def _create_then_fill(core, path: Path, text: str) -> None:
    """A new note is first written as a short stub and filled a moment later.

    The Second Brain sync plugin reacts to Obsidian's modify events, and a file
    created outside Obsidian only raises a create event, so a note written in
    one go would stay out of the cloud until its next change. Checked live:
    a same-bytes rewrite does not count as a change; a content change does.
    """
    delay = float(os.environ.get("MEMO_CREATE_FILL_DELAY", "1.5"))
    if delay <= 0:
        core.atomic_write(path, text)
        return
    head_end = text.find("\n---", 3)
    stub = (text[: head_end + 4] if head_end != -1 else "") + "\n\n_Memo is writing this session…_\n"
    core.atomic_write(path, stub)
    time.sleep(delay)          # let Obsidian index the new file first
    core.atomic_write(path, text)


def write_session(conn, sid: str, commit_subject: Optional[str] = None) -> Optional[Path]:
    session = state.get_session(conn, sid)
    if session is None:
        return None
    from . import reindex, settings as _settings
    stamp = session["machine"] if "machine" in session.keys() else None
    if stamp is None:
        # treat as absent stamp
        if not _settings.get("owns_unstamped"):
            # foreign / unnamed without owns_unstamped: still allow rewrite of own unnamed stamp
            pass
    if not reindex.is_own(stamp):
        return None  # foreign session: one writer
    # Prefer an existing human path; otherwise build from the first prompt/summary topic.
    topic = _session_topic(conn, sid)
    rel = _unique_session_rel(session, topic)
    sidecar = vault_dir() / Path(rel).with_suffix(".json")
    if sidecar.exists():
        # Anything the vault already holds and the index lost comes back first,
        # so this rewrite can only add (owner rule: no session memory is ever lost).
        back = reindex.adopt_sidecar(conn, sid, sidecar, rel)
        added = back.added if hasattr(back, "added") else int(back or 0)
        if added:
            log_line("writer", f"restored {added} item(s) of {rel} from its sidecar into the index")
    has_memory = conn.execute(
        "SELECT (SELECT COUNT(*) FROM observations WHERE content_session_id = ?) + "
        "(SELECT COUNT(*) FROM session_summaries WHERE content_session_id = ?)", (sid, sid)).fetchone()[0]
    if not has_memory:
        return None  # nothing worth a file yet
    text = render_session(conn, sid)
    if text is None:
        return None
    session = state.get_session(conn, sid)
    core = _core()
    path = core.VAULT_DIR / rel
    data_obj = session_data(conn, sid)
    sidecar = path.with_suffix(".json")
    try:
        with core.vault_lock(timeout=30):
            kept = _would_shrink(sidecar, data_obj)
            if kept:
                # Never replace a richer record with a poorer one (a lost or
                # partly rebuilt index): keep the files, say so, try again later.
                log_line("writer", f"kept {rel}: the vault copy has more ({kept}) than the index; not overwritten")
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                _create_then_fill(core, path, text)
            else:
                current = path.read_text(encoding="utf-8")
                text = keep_sync_stamps(current, text)
                if current != text:
                    core.atomic_write(path, text)
            data = json.dumps(data_obj, ensure_ascii=False, indent=1) + "\n"
            if not sidecar.exists() or sidecar.read_text(encoding="utf-8") != data:
                core.atomic_write(sidecar, data)
            if session["markdown_path"] != rel:
                # Not update_session: recording the path is not session activity.
                conn.execute("UPDATE sessions SET markdown_path = ? WHERE content_session_id = ?", (rel, sid))
                conn.commit()
            if commit_subject:
                core.git_commit_change(commit_subject, [path, sidecar])
    except Exception as exc:
        log_line("writer", f"could not write {rel}: {exc}")
        raise
    return path


def _would_shrink(sidecar: Path, new: Dict) -> str:
    """'' when writing `new` loses nothing the sidecar already holds; otherwise
    what the sidecar has more of, e.g. 'observations 2636 > 12'."""
    if not sidecar.exists():
        return ""
    try:
        old = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    more = []
    for key in ("prompts", "observations", "summaries"):
        before, after = len(old.get(key) or []), len(new.get(key) or [])
        if before > after:
            more.append(f"{key} {before} > {after}")
    return ", ".join(more)


def _subject(session, what: str) -> str:
    return f"memo: session {session['project']} {session['content_session_id'][:8]} — {what}"[:120]


# Called by processor.py --------------------------------------------------

OBS_WRITE_EVERY_SEC = 60
_last_obs_write: Dict[str, float] = {}


def observations_saved(conn, session, ids: List[int]) -> None:
    """Rewrite the note at most once a minute while observations arrive; the
    summary and session end always write. Nothing waits only in memory: every
    observation is already in the index, and the next write includes it."""
    sid = session["content_session_id"]
    now = time.time()
    if now - _last_obs_write.get(sid, 0) < OBS_WRITE_EVERY_SEC:
        return
    _last_obs_write[sid] = now
    write_session(conn, sid)


def summary_saved(conn, session, summary_id: int) -> None:
    row = conn.execute("SELECT prompt_number, request FROM session_summaries WHERE id = ?", (summary_id,)).fetchone()
    what = f"request {row['prompt_number']}: {row['request'] or 'summary'}" if row else "summary"
    write_session(conn, session["content_session_id"], commit_subject=_subject(session, what))
    try:
        from . import wrapup
        wrapup.checkpoint(conn, state.get_session(conn, session["content_session_id"]))
    except ImportError:
        pass


def session_end(conn, session) -> None:
    state.update_session(conn, session["content_session_id"], status="ended")
    write_session(conn, session["content_session_id"], commit_subject=_subject(session, "ended"))
    try:
        from . import wrapup
        wrapup.session_ended(conn, state.get_session(conn, session["content_session_id"]))
    except ImportError:
        pass
