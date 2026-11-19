"""PreToolUse: past observations about the file a tool is about to touch.

Before Read, Edit, MultiEdit, Write or NotebookEdit on a file (1,500 bytes or
more) that earlier sessions recorded, the agent gets a short index of that
history, once per session per file (again only when newer observations
exist). Subagents get nothing, as in claude-mem.

The full-read gate (off by default, see settings) denies a whole-file Read
of a large known file and points to Grep -n, a ranged Read and
memo_get_observations instead.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import List, Optional, Tuple

from . import project as projects
from . import prompts, settings, state
from .adapters import HookResult, NormalizedInput

FILE_TOOLS = {"Read", "Edit", "MultiEdit", "Write", "NotebookEdit"}
MIN_BYTES = 1_500
DISPLAY_LIMIT = 15
UNGATED_SUFFIXES = {".md", ".markdown", ".yaml", ".yml", ".toml", ".txt", ".json"}


def _target(inp: NormalizedInput) -> Optional[str]:
    if inp.tool_name not in FILE_TOOLS or not isinstance(inp.tool_input, dict):
        return None
    path = inp.tool_input.get("file_path") or inp.tool_input.get("notebook_path")
    return path if isinstance(path, str) and path else None


def _candidates(abs_path: Path, root: str) -> List[str]:
    """Ways the observer may have written this path."""
    out = [str(abs_path)]
    try:
        out.insert(0, abs_path.relative_to(Path(root).resolve()).as_posix())
    except ValueError:
        pass
    return out


def history(conn, project_names: List[str], paths: List[str], limit: int = DISPLAY_LIMIT):
    marks = ",".join("?" * len(paths))
    pmarks = ",".join("?" * len(project_names))
    return conn.execute(
        f"""SELECT DISTINCT o.* FROM observations o JOIN observation_files f ON f.observation_id = o.id
            WHERE f.path IN ({marks}) AND o.project IN ({pmarks})
            ORDER BY o.created_at DESC, o.id DESC LIMIT ?""", (*paths, *project_names, limit)).fetchall()


def _format(path: str, rows) -> str:
    lines = [f"Memo: earlier sessions recorded work on {path}. These are past observations, "
             "not the file's current contents:"]
    for r in rows:
        when = datetime.datetime.fromtimestamp(int(r["created_at"]) / 1000).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{r['id']} {when} {prompts.type_emoji(r['type'])} {r['title']}")
    lines.append(f"Details: memo_get_observations([{', '.join(str(r['id']) for r in rows)}])")
    return "<memo-context>\n" + "\n".join(lines) + "\n</memo-context>"


def _is_full_read(inp: NormalizedInput, abs_path: Path) -> bool:
    ti = inp.tool_input if isinstance(inp.tool_input, dict) else {}
    try:
        offset = int(ti.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    if offset > 1:
        return False
    limit = ti.get("limit")
    if limit in (None, ""):
        return True
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return True
    with open(abs_path, "rb") as fh:
        for i, _ in enumerate(fh):
            if i >= limit:
                return False
    return True


def _gate(inp: NormalizedInput, abs_path: Path, size: int, rows, root: str) -> Optional[str]:
    if not settings.get("file_read_gate_enabled") or inp.tool_name != "Read" or inp.host != "claude-code":
        return None
    if size < int(settings.get("file_read_gate_min_bytes")) or abs_path.suffix.lower() in UNGATED_SUFFIXES:
        return None
    try:
        abs_path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return None  # outside the workspace: never blocked
    if not _is_full_read(inp, abs_path):
        return None
    ids = ", ".join(str(r["id"]) for r in rows)
    return (f"Full-file Read held back by Memo: {abs_path} is large and earlier sessions recorded work on it. "
            f"Find the lines you need with Grep (-n) and Read them with offset and limit; past work is in "
            f"memo_get_observations([{ids}]). A ranged Read is allowed and satisfies Edit's read rule.\n"
            + _format(str(abs_path), rows))


def pre_tool_use(inp: NormalizedInput) -> Optional[HookResult]:
    target = _target(inp)
    if not target or not inp.session_id or (inp.agent_id and inp.agent_type):
        return None
    abs_path = Path(target if os.path.isabs(target) else os.path.join(inp.cwd or "", target)).resolve()
    try:
        size = abs_path.stat().st_size
    except OSError:
        return None
    if size < MIN_BYTES:
        return None
    proj = projects.resolve(inp.cwd)
    names = [proj.name] + ([proj.name.split("/")[0]] if "/" in proj.name else [])
    conn = state.connect()
    rows = history(conn, names, _candidates(abs_path, proj.root))
    if not rows:
        return None
    deny = _gate(inp, abs_path, size, rows, proj.root)
    if deny:
        return HookResult(permission_decision="deny", permission_reason=deny)
    newest = int(rows[0]["id"])
    seen = conn.execute("SELECT last_observation_id FROM file_context_seen WHERE content_session_id = ? AND path = ?",
                        (inp.session_id, str(abs_path))).fetchone()
    if seen and int(seen[0]) >= newest:
        return None
    conn.execute("INSERT OR REPLACE INTO file_context_seen(content_session_id, path, last_observation_id) "
                 "VALUES (?, ?, ?)", (inp.session_id, str(abs_path), newest))
    conn.commit()
    return HookResult(additional_context=_format(_candidates(abs_path, proj.root)[0], rows))
