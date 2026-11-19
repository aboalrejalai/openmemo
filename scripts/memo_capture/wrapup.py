"""After every request: the session's timeline line and the project's "Latest state".

Sessions stay open for days and Claude Code rarely ends them, so nothing here
waits for the end of a session (2026-11-05). After each saved summary:

- timeline.md has one event per session per day: written the first time a
  request of that day finishes (through the vault's append_fact gateway, so
  its placement and commit rules apply), then rewritten in place as later
  requests of the same day finish.
- The project note (projects/<slug>.md) gets its block between
  <!-- memo:latest --> and <!-- /memo:latest --> replaced with the newest
  summary. The block is created under "## Latest state (Memo)" the first
  time. Frontmatter is never touched.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional

from . import state
from .paths import log_line
from .writer import _core, _project_note

BLOCK_START, BLOCK_END = "<!-- memo:latest -->", "<!-- /memo:latest -->"
HEADING = "## Latest state (Memo)"


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _summaries(conn, sid: str):
    """The session's summaries, each with `asked_at`: when its request was made
    (the prompt's time), which a summary written hours later must not replace."""
    return conn.execute("""SELECT s.*, COALESCE(p.created_at, s.created_at) AS asked_at
                           FROM session_summaries s LEFT JOIN user_prompts p
                           ON p.content_session_id = s.content_session_id AND p.prompt_number = s.prompt_number
                           WHERE s.content_session_id = ? ORDER BY s.prompt_number""", (sid,)).fetchall()


def timeline_line(session, summaries) -> Optional[str]:
    if not summaries:
        return None
    first, last = summaries[0], summaries[-1]
    asked = _clip(first["request"], 140)
    done = _clip(last["completed"] or last["learned"] or "", 160)
    turns = f"{len(summaries)} request(s)"
    text = f"{session['project']}: {asked}"
    if done:
        text += f" — {done}"
    return f"{text} ({turns}, {session['host']}, session {session['content_session_id'][:8]})"


def latest_block(session, summaries) -> str:
    last = summaries[-1]
    when = datetime.datetime.fromtimestamp(int(last["asked_at"]) / 1000).strftime("%Y-%m-%d %H:%M")
    link = session["markdown_path"][:-3] if session["markdown_path"] else ""
    lines = [BLOCK_START, f"<!-- memo:asked_at {int(last['asked_at'])} -->",
             f"*Request of {when}, session [[{link}|{session['content_session_id'][:8]}]] "
             f"({session['host']}).*", ""]
    for label, key in (("Last request", "request"), ("Completed", "completed"), ("Learned", "learned"),
                       ("Next steps", "next_steps"), ("Notes", "notes")):
        if last[key]:
            lines.append(f"- **{label}:** {_clip(last[key], 600)}")
    lines.append(BLOCK_END)
    return "\n".join(lines)


def replace_block(text: str, block: str) -> str:
    pattern = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    return text.rstrip() + f"\n\n{HEADING}\n\n{block}\n"


def update_project_note(conn, session, summaries) -> Optional[str]:
    note = _project_note(session)
    if not note:
        return None
    core = _core()
    path = core.VAULT_DIR / note
    with core.vault_lock(timeout=30):
        text = path.read_text(encoding="utf-8")
        shown = re.search(r"<!-- memo:asked_at (\d+) -->", text)
        if shown and int(shown.group(1)) > int(summaries[-1]["asked_at"]):
            return note          # the block already shows a newer request (an older one was processed late)
        new = replace_block(text, latest_block(session, summaries))
        if new == text:
            return note
        core.write_mutation(path, new, snapshot="memo_session_wrapup")
        core.git_commit_change(f"memo: latest state {note} from session {session['content_session_id'][:8]}",
                               [path])
    return note


def _day_of(ms: int) -> str:
    return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d")


def _find_day_line(lines, day: str, marker: str) -> Optional[int]:
    """Index of the line under `- **day**` that carries `marker`, if any."""
    start = next((i for i, l in enumerate(lines) if l.strip() == f"- **{day}**"), None)
    if start is None:
        return None
    for i in range(start + 1, len(lines)):
        if not lines[i].startswith("  "):
            return None
        if marker in lines[i]:
            return i
    return None


def update_timeline(session, summaries) -> None:
    """Write or rewrite this session's line for the day its latest request was made."""
    core = _core()
    day = _day_of(summaries[-1]["asked_at"])
    todays = [s for s in summaries if _day_of(s["asked_at"]) == day]
    line = timeline_line(session, todays)
    if not line:
        return
    link = session["markdown_path"][:-3] if session["markdown_path"] else None
    marker = f"[[{link}]]" if link else f"session {session['content_session_id'][:8]})"
    with core.vault_lock(timeout=30):
        text = core.TIMELINE_PATH.read_text(encoding="utf-8")
        lines = text.split("\n")
        at = _find_day_line(lines, day, marker)
        if at is not None:
            new_line = core._child_line("Session", line, link)
            if lines[at] == new_line:
                return
            lines[at] = new_line
            core.write_mutation(core.TIMELINE_PATH, "\n".join(lines), snapshot="memo_session_checkpoint")
            core.git_commit_change(f"memo: timeline {day} — Session: {line[:60]}", [core.TIMELINE_PATH])
            return
    core.append_fact("timeline", "Session", line, date_str=day, link=link, force=True,
                     reason="memo_session_checkpoint")


def checkpoint(conn, session) -> None:
    """Called after every saved summary and at session end; safe to repeat."""
    if session is None:
        return
    summaries = _summaries(conn, session["content_session_id"])
    if not summaries:
        return
    try:
        update_timeline(session, summaries)
    except Exception as exc:
        log_line("writer", f"timeline event failed for {session['content_session_id']}: {exc}")
    try:
        update_project_note(conn, session, summaries)
    except Exception as exc:
        log_line("writer", f"project note update failed for {session['content_session_id']}: {exc}")


def session_ended(conn, session) -> None:
    checkpoint(conn, session)
