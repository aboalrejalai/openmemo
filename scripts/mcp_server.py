#!/usr/bin/env python3
"""
Memo Memory MCP Server

Model Context Protocol server exposing the owner's sovereign memory
vault over stdio JSON-RPC. This module is a thin protocol layer: every piece of
behaviour lives in `memo_core`, which the CLI (`memory_engine.py`) shares.

100% offline, zero external dependencies (Python 3.9+ stdlib & SQLite FTS5).

stdout carries JSON-RPC only. All diagnostics go to stderr.
"""

from __future__ import annotations

import os
import re
import sys
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memo_core as core
from memo_core import MemoError, log
from memo_capture import search as capture_search

SERVER_NAME = "memo-mcp"
SERVER_INSTRUCTIONS = (
    "Memo is the owner's memory: curated vault notes (projects, clients, stack, "
    "timeline, decisions) plus the automatically recorded memory of every agent session. "
    "To answer 'where did we stop', 'what changed' or 'why' about a project, use three steps: "
    "1) memo_search(query, project?) for a compact index, 2) memo_session_timeline(anchor) for "
    "what happened around one item, 3) memo_get_observations([ids]) for full details. "
    "Fetch details only for what the task needs."
)
SERVER_VERSION = "0.1.1"
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
PARSE_ERROR = -32700


# ----------------------------------------------------------------------
# Tool handlers -> (human_text, structured_payload)
# ----------------------------------------------------------------------

def h_search(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    query = (args.get("query") or "").strip()
    if not query:
        raise MemoError("Search query cannot be empty.")
    limit = max(1, min(int(args.get("limit", 5)), 50))
    note_type = (args.get("type") or "").strip() or None
    status = (args.get("status") or "").strip() or None
    tag = (args.get("tag") or "").strip() or None

    scope = (args.get("scope") or "all").strip()
    project = (args.get("project") or "").strip() or None

    strategy, results = (None, [])
    if scope in ("all", "vault"):
        strategy, results = core.search(
            query, limit, type=note_type, status=status, tag=tag,
        )
    sessions: List[Dict[str, Any]] = []
    if scope in ("all", "sessions"):
        sessions = capture_search.search(_capture_db(), query, project=project,
                                         obs_type=(args.get("obs_type") or "").strip() or None,
                                         concept=(args.get("concept") or "").strip() or None,
                                         since=args.get("since"), until=args.get("until"),
                                         limit=max(limit, 10))
    # Unified Reciprocal Rank Fusion (RRF) combining Vault Dossiers + Session Memory
    ranked_fusion: List[Dict[str, Any]] = []
    for idx, r in enumerate(results, 1):
        ranked_fusion.append({
            "source": "vault",
            "id": r["path"],
            "title": r["title"],
            "path": r["path"],
            "wikilink": r["wikilink"],
            "snippet": r["snippet"],
            "rrf_score": round(1.0 / (60 + idx), 6),
        })
    for idx, s in enumerate(sessions, 1):
        base_rrf = abs(float(s.get("score") or 0.0)) or (1.0 / (60 + idx))
        ranked_fusion.append({
            "source": "session",
            "id": str(s["id"]),
            "kind": s["kind"],
            "type": s["type"],
            "project": s["project"],
            "title": s["title"],
            "date": s["date"],
            "rrf_score": round(base_rrf, 6),
        })
    ranked_fusion.sort(key=lambda x: x["rrf_score"], reverse=True)

    payload = {
        "query": query,
        "search_mode": "hybrid-rrf",
        "strategy": strategy or ("hybrid-rrf" if sessions else "none"),
        "count": len(results),
        "results": results,
        "sessions": [{k: v for k, v in h.items() if k != "score"} for h in sessions],
        "ranked_fusion": ranked_fusion[: max(limit * 2, 10)],
        "filters": {
            "type": note_type,
            "status": status,
            "tag": tag,
            "project": project,
        },
    }

    if not results and not sessions:
        return (f"No results in the Memo vault or session memory for '{query}'. "
                "Try fewer or broader terms, or memo_list_files to browse.", payload)

    lines = []
    if results:
        lines += [f"Found {len(results)} vault note(s) for '{query}' "
                  f"(match strategy: {strategy}):", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r['title']}] ({r['path']})")
            lines.append(f"   Snippet: {r['snippet']}")
            lines.append(f"   Wikilink: [[{r['wikilink']}]]")
            lines.append("")
    if sessions:
        lines += [f"Session memory ({len(sessions)} item(s); ID DATE [PROJECT] TYPE · TITLE):",
                  capture_search.format_index(sessions), "",
                  "Next: memo_session_timeline(anchor=<id>) for what happened around one item, "
                  "memo_get_observations([ids]) for full details."]
    return "\n".join(lines), payload


def _capture_db():
    from memo_capture import state as capture_state
    return capture_state.connect()


def h_session_timeline(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    anchor = str(args.get("anchor") or "").strip()
    if not anchor:
        raise MemoError("'anchor' is required: an observation id (123), a session summary id (S12) or a date.")
    before = max(0, min(int(args.get("before", 10)), 50))
    after = max(0, min(int(args.get("after", 10)), 50))
    items = capture_search.timeline(_capture_db(), anchor, before=before, after=after,
                                    project=(args.get("project") or "").strip() or None)
    payload = {"anchor": anchor, "count": len(items),
               "items": [{k: v for k, v in h.items() if k != "score"} for h in items]}
    if not items:
        return f"Nothing in session memory around '{anchor}'.", payload
    return (f"Session memory around {anchor} (oldest first):\n" + capture_search.format_index(items)
            + "\n\nNext: memo_get_observations([ids]) for full details.", payload)


def h_get_observations(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    ids = args.get("ids") or []
    if isinstance(ids, (str, int)):
        ids = [ids]
    if not ids:
        raise MemoError("'ids' is required: observation ids (123) and/or session summary ids (S12).")
    items = capture_search.get_observations(_capture_db(), list(ids))
    payload = {"count": len(items), "items": items}
    if not items:
        return "No observation or session summary matches those ids.", payload
    lines = []
    for it in items:
        if it["kind"] == "session":
            lines.append(f"## {it['id']} · session · {it['date']} · [{it['project']}]")
            for label, key in (("Request", "request"), ("Investigated", "investigated"),
                               ("Learned", "learned"), ("Completed", "completed"),
                               ("Next steps", "next_steps"), ("Notes", "notes")):
                if it.get(key):
                    lines.append(f"- {label}: {it[key]}")
        else:
            lines.append(f"## {it['id']} · {it['type']} · {it['date']} · [{it['project']}] {it['title']}")
            if it.get("subtitle"):
                lines.append(it["subtitle"])
            lines += [f"- {f}" for f in it.get("facts", [])]
            if it.get("narrative"):
                lines.append(it["narrative"])
            if it.get("files_modified"):
                lines.append("Modified: " + ", ".join(it["files_modified"]))
            if it.get("files_read"):
                lines.append("Read: " + ", ".join(it["files_read"]))
        if it.get("markdown"):
            lines.append(f"Vault note: [[{it['markdown'][:-3]}]]")
        lines.append("")
    return "\n".join(lines).rstrip(), payload


def h_save_memory(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    from memo_capture import manual
    try:
        res = manual.save(args.get("title") or "", args.get("text") or "",
                          project=args.get("project") or "", obs_type=(args.get("type") or "discovery"),
                          facts=args.get("facts") or [])
    except ValueError as exc:
        raise MemoError(str(exc))
    # Remote: nudge background sync so the save reaches GitHub quickly.
    try:
        import mcp_remote
        wake = getattr(mcp_remote, "SYNC_WAKE", None)
        if wake is not None:
            wake.set()
    except Exception:
        pass
    text = (f"Saved memory {res['id']} in {res['markdown']}." if res["new"]
            else f"Already remembered as {res['id']}; nothing new written.")
    return text, res


def h_get_entity(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    name = (args.get("name") or "").strip()
    if not name:
        raise MemoError("Entity name cannot be empty.")
    matched = core.graph_find(name)
    payload = {"query": name, "count": len(matched), "entities": matched}
    if not matched:
        return f"No entity matching '{name}' in the Memo knowledge graph.", payload

    lines: List[str] = []
    for ent in matched:
        lines.append(f"Entity: {ent.get('name')} ({ent.get('name_ar','')}) "
                     f"[Type: {ent.get('type')}]")
        if ent.get("file"):
            lines.append(f"Associated File: [[{ent['file'].replace('.md','')}]]")
        if ent.get("outgoing"):
            lines.append("Outgoing Connections:")
            for r in ent["outgoing"]:
                lines.append(f"  -> --[{r['relation']}]--> {r['target']} ({r.get('notes','')})")
        if ent.get("incoming"):
            lines.append("Incoming Connections:")
            for r in ent["incoming"]:
                lines.append(f"  <- --[{r['relation']}]-- {r['source']} ({r.get('notes','')})")
        lines.append("")
    return "\n".join(lines), payload


def h_get_timeline(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    since = args.get("since")
    until = args.get("until")
    events = core.parse_timeline()
    if since:
        events = [e for e in events if e["date"] >= since]
    if until:
        events = [e for e in events if e["date"] <= until]

    payload = {"count": len(events), "since": since or "", "until": until or "",
               "events": events}
    if not events:
        window = f" since {since}" if since else ""
        return f"No timeline events found{window}.", payload

    lines = [f"Memo Timeline ({len(events)} event(s)):", ""]
    for e in events:
        tag = "UPCOMING" if e["section"] == "upcoming" else "HISTORY"
        link = f" -> [[{e['link']}]]" if e["link"] else ""
        cat = f"{e['category']}: " if e["category"] else ""
        lines.append(f"- [{e['date']}] ({tag}) {cat}{e['summary']}{link}")
    return "\n".join(lines), payload


def h_get_dossier(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    rel, content = core.read_dossier(args.get("path") or "")
    return f"# Dossier: {rel}\n\n{content}", {"path": rel, "content": content}



def h_list_projects(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """List every project dossier under projects/ (canonical project set)."""
    status_filter = (args.get("status") or "").strip().lower()
    root = Path(core.VAULT_DIR) / "projects"
    projects: List[Dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.glob("*.md")):
            raw = path.read_text(encoding="utf-8", errors="replace")
            fm = ""
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                fm = raw[3:end] if end != -1 else ""

            def _fm(key: str, default: str = "") -> str:
                m = re.search(rf"^{key}:\s*[\"']?([^\"'\n]+)", fm, re.M)
                return (m.group(1).strip() if m else default)

            st = _fm("status", "unknown")
            title = _fm("title", path.stem)
            if status_filter and st.lower() != status_filter:
                continue
            projects.append({
                "id": path.stem,
                "path": f"projects/{path.name}",
                "title": title,
                "status": st,
            })
    by_status: Dict[str, int] = {}
    for p in projects:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    payload = {
        "count": len(projects),
        "by_status": by_status,
        "projects": projects,
        "note": (
            "Canonical project count comes from projects/*.md dossiers. "
            "sessions/ folders are only projects that had a captured chat, so they are fewer."
        ),
    }
    lines = [
        f"Memo has {len(projects)} project dossier(s) under projects/.",
        "By status: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())),
        "",
    ]
    for p in projects:
        lines.append(f"- {p['id']}  [{p['status']}]  {p['title']}  ({p['path']})")
    lines.append("")
    lines.append(payload["note"])
    return "\n".join(lines), payload


def h_list_files(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    files = core.list_files(args.get("subdir"))
    payload = {"count": len(files), "subdir": args.get("subdir") or "",
               "files": files}
    lines = [f"Memo vault contains {len(files)} markdown file(s):", ""]
    lines += [f"- {f['path']}  —  {f['title']}" for f in files]
    return "\n".join(lines), payload


def h_status(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    st = core.status()
    lines = [
        "Memo Memory Status",
        f"  Vault:          {st['vault']}",
        f"  Markdown files: {st['markdown_files']}",
        f"  Index:          +{st['index']['added']} new / {st['index']['updated']} updated "
        f"/ {st['index']['purged']} purged  ({st['index']['db_kb']} KB, "
        f"trigram={'on' if st['index']['trigram'] else 'off'})",
        f"  Graph:          {st['graph']['entities']} entities, "
        f"{st['graph']['relationships']} relationships",
        f"  Timeline:       {st['timeline_events']} events",
        f"  Git snapshots:  {'enabled' if st['git_snapshots'] else 'disabled'}",
        f"  SQLite:         {st['sqlite_version']}",
    ]
    return "\n".join(lines), st


def h_lint(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    payload = core.vault_lint()
    lines = [
        f"Memo vault lint — {payload['files']} files",
        f"  errors:   {payload['error_count']}",
        f"  warnings: {payload['warning_count']}",
        f"  codes:    {payload.get('error_codes') or {}}",
        "  PASS" if payload["ok"] else "  FAIL",
    ]
    for e in payload["errors"][:15]:
        lines.append(f"  - [{e['code']}] {e['file']}: {e['msg']}")
    return "\n".join(lines), payload


def h_query_props(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    note_type = (args.get("type") or "").strip() or None
    status = (args.get("status") or "").strip() or None
    tag = (args.get("tag") or "").strip() or None
    limit = max(1, min(int(args.get("limit", 50)), 200))

    results = core.query_props(type=note_type, status=status, tag=tag, limit=limit)
    payload = {
        "count": len(results),
        "filters": {"type": note_type, "status": status, "tag": tag},
        "results": results,
    }
    if not results:
        return ("No notes match the given property filters. "
                "Try broader type/status/tag values.", payload)

    lines = [f"Found {len(results)} note(s) matching property filters:", ""]
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. [{r['title']}] ({r['path']}) "
            f"type={r.get('type') or '-'} status={r.get('status') or '-'}"
        )
        lines.append(f"   Wikilink: [[{r['wikilink']}]]")
        if r.get("tags"):
            lines.append(f"   Tags: {', '.join(r['tags'])}")
        lines.append("")
    return "\n".join(lines), payload


def _held_back_text(candidates: List[Dict[str, Any]]) -> str:
    """Human text for a near-duplicate hold-back."""
    lines = ["Held back: this looks like a rephrased duplicate. "
             "Nothing was written.", ""]
    for c in candidates:
        lines.append(f"  - [{c['date']}] {c['category']}: {c['summary']} "
                     f"(similarity {c['similarity']})")
    lines.append("")
    lines.append("If it really is a distinct fact, call again with force=true.")
    return "\n".join(lines)


def h_append_fact(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    result = core.append_fact(
        args.get("target") or "", args.get("category") or "", args.get("summary") or "",
        date_str=args.get("date"), link=args.get("link"),
        force=bool(args.get("force")), reason="memo_append_fact",
    )
    if result.get("near_duplicate"):
        return _held_back_text(result["candidates"]), result
    if result.get("duplicate"):
        if result["target"] == "timeline":
            return ("Skipped: an identical summary already exists in timeline.md. "
                    "Nothing was written.", result)
        return "Skipped: that note is already in sessions/current.md.", result
    return (f"Appended to {result['file']} ({result.get('placement','session scratchpad')}):\n"
            f"{result['entry']}"), result


def h_append_section(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    path = args.get("path") or ""
    heading = args.get("heading") or ""
    text = args.get("text") or ""
    create_missing = bool(args.get("create_missing"))

    with core.vault_lock():
        rel = core.resolve_dossier(path)
        # The snapshot runs inside the core, right before the write, so a
        # refusal or a duplicate never commits a hand edit.
        result = core.append_section(rel, heading, text,
                                     create_missing=create_missing,
                                     snapshot="memo_append_section")
        if not result["duplicate"]:
            core.git_commit_change(
                f"memo: append to {result['file']} § {result['heading'][:40]}",
                [result["file"]])
        core.sync_index()

    if result["duplicate"]:
        return (f"Skipped: that text is already present in {result['file']}.", result)
    return (f"Appended to {result['file']} ({result['placement']}):\n"
            f"{result['entry']}"), result


def h_create_note(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    path = args.get("path") or ""
    title = args.get("title") or ""
    note_type = args.get("type") or ""
    if not path or not title or not note_type:
        raise MemoError("'path', 'title', and 'type' are all required.")

    with core.vault_lock():
        # No snapshot: create_note refuses any existing path, so a snapshot
        # could only commit a hand edit the call never touches.
        result = core.create_note(
            path, title, note_type,
            status=args.get("status") or "active",
            tags=args.get("tags") or None,
            body=args.get("body") or None,
            entity_id=args.get("entity_id") or None,
            aliases=args.get("aliases") or None,
        )
        core.git_commit_change(f"memo: create {result['file']}", [result["file"]])
        core.sync_index()

    lint = core.vault_lint()
    result["lint_ok"] = lint["ok"]
    result["lint_errors"] = [e for e in lint["errors"] if e["file"] == result["file"]]

    lines = [
        f"Created {result['file']}",
        f"  title:  {result['title']}",
        f"  type:   {result['type']}   status: {result['status']}",
        f"  tags:   {', '.join(result['tags'])}",
        f"  source: {'_templates/' + result['type'] + '.md' if result['templated'] else 'inline body'}",
        f"  lint:   {'clean' if not result['lint_errors'] else 'ERRORS'}",
    ]
    for e in result["lint_errors"]:
        lines.append(f"    - [{e['code']}] {e['msg']}")
    if not result["entity_id"]:
        lines.append("  note:   no entity_id yet — register it with memo_add_entity "
                     "if this is a client, project, or technology.")
    return "\n".join(lines), result


def h_update_fact(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    path = args.get("path") or ""
    old_text = args.get("old_text") or ""
    new_text = args.get("new_text")
    if new_text is None:
        raise MemoError("'new_text' is required (use an empty string to delete).")

    with core.vault_lock():
        rel = core.resolve_dossier(path)
        result = core.update_fact(rel, old_text, new_text, snapshot="memo_update_fact")
        if result["changed"]:
            core.git_commit_change(f"memo: update {result['file']}", [result["file"]])
            core.sync_index()

    if not result["changed"]:
        return (f"Skipped: 'new_text' is identical to 'old_text' in {result['file']}. "
                "Nothing was written."), result
    return (f"Updated {result['file']}.\n"
            f"  - was: {result['replaced']}\n"
            f"  + now: {result['with']}"), result


def h_add_entity(args: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    entity = args.get("entity")
    relationships = args.get("relationships") or []
    if not entity and not relationships:
        raise MemoError("Provide 'entity', 'relationships', or both.")

    with core.vault_lock():
        result = core.graph_add_entity(entity, relationships, snapshot="memo_add_entity")
        if result.get("entity") or result.get("relationships"):
            core.git_commit_change(
                f"memo: graph +{result.get('entity') or 'relationships'}",
                [core.GRAPH_PATH])
            core.sync_index()

    if result.get("note"):
        return result["note"], result
    parts = []
    if result.get("entity"):
        parts.append(f"entity '{result['entity']}'")
    if result.get("relationships"):
        parts.append(f"{len(result['relationships'])} relationship(s)")
    return (f"Added {' and '.join(parts)} to the knowledge graph "
            f"({result['entity_count']} entities, "
            f"{result['relationship_count']} relationships)."), result


# ----------------------------------------------------------------------
# Tool definitions
# ----------------------------------------------------------------------

def _obj(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


_RESULT_ITEM = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "title": {"type": "string"},
        "snippet": {"type": "string"},
        "wikilink": {"type": "string"},
    },
}

_EVENT_ITEM = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "category": {"type": "string"},
        "summary": {"type": "string"},
        "link": {"type": "string"},
        "section": {"type": "string", "enum": ["upcoming", "history"]},
    },
}

RO = {"readOnlyHint": True, "destructiveHint": False,
      "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False,
         "idempotentHint": False, "openWorldHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True,
               "idempotentHint": False, "openWorldHint": False}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "memo_search",
        "title": "Search Memo vault",
        "description": (
            "Full-text search across the Memo memory vault (projects, clients, stack, "
            "hackathons, timeline) and the recorded session memory of every agent session "
            "(observations and per-request summaries). Step 1 of 3: it returns a compact index; "
            "use memo_session_timeline and memo_get_observations for details. Multi-word queries are matched by a "
            "fallback ladder: exact phrase, then all-terms, then any-term, then prefix, "
            "then substring (Arabic-friendly). Wrap the query in double quotes to force "
            "an exact-phrase search. Optional type/status/tag filters constrain results "
            "via frontmatter. Returns excerpts with Obsidian wikilinks. For inventories "
            "without a keyword, use memo_query_props instead."
        ),
        "inputSchema": _obj({
            "query": {"type": "string", "description":
                      "Keyword, name, or question (e.g. 'Acme Corp budget', 'Salla "
                      "Supabase', 'شركة تجريبية'). Quote it to force a phrase match."},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50,
                      "description": "Maximum snippets to return (default 5)."},
            "type": {"type": "string",
                     "description": "Optional note type filter (client, project, stack, ...)."},
            "status": {"type": "string",
                       "description": "Optional status filter (active, paused, completed, ...)."},
            "tag": {"type": "string",
                    "description": "Optional exact tag filter (e.g. 'tech/salla', 'tier/flagship')."},
            "scope": {"type": "string", "enum": ["all", "vault", "sessions"], "default": "all",
                      "description": "Where to search: vault notes, recorded session memory, or both."},
            "project": {"type": "string",
                        "description": "Session memory only: a project name such as 'my-project'."},
            "obs_type": {"type": "string", "description":
                         "Session memory only: bugfix, feature, refactor, change, discovery, decision, "
                         "security_alert, security_note or sensitive."},
            "concept": {"type": "string", "description":
                        "Session memory only: how-it-works, why-it-exists, what-changed, "
                        "problem-solution, gotcha, pattern or trade-off."},
            "since": {"type": "string", "description": "Session memory only: YYYY-MM-DD."},
            "until": {"type": "string", "description": "Session memory only: YYYY-MM-DD."},
        }, ["query"]),
        "outputSchema": _obj({
            "query": {"type": "string"},
            "strategy": {"type": "string"},
            "count": {"type": "integer"},
            "results": {"type": "array", "items": _RESULT_ITEM},
            "sessions": {"type": "array", "items": {"type": "object"}},
        }, ["query", "strategy", "count", "results"]),
        "annotations": RO,
        "handler": h_search,
    },
    {
        "name": "memo_get_entity",
        "title": "Look up knowledge-graph entity",
        "description": (
            "Query the knowledge graph for a Person, Organization, Client, Project, "
            "Hackathon, Technology, Discipline, or Profile. Returns the entity plus its "
            "incoming and outgoing semantic relationships."
        ),
        "inputSchema": _obj({
            "name": {"type": "string", "description":
                     "Entity id or name substring, Arabic or English "
                     "(e.g. 'Demo Partner', 'DemoApp', 'شركة تجريبية')."},
        }, ["name"]),
        "outputSchema": _obj({
            "query": {"type": "string"},
            "count": {"type": "integer"},
            "entities": {"type": "array", "items": {"type": "object"}},
        }, ["query", "count", "entities"]),
        "annotations": RO,
        "handler": h_get_entity,
    },
    {
        "name": "memo_get_timeline",
        "title": "Read chronological timeline",
        "description": (
            "Retrieve dated milestones, compensation changes, and upcoming deadlines from "
            "timeline.md. Each event carries its date, category, summary, wikilink, and "
            "whether it is an upcoming milestone or completed history."
        ),
        "inputSchema": _obj({
            "since": {"type": "string", "description":
                      "Optional ISO date (YYYY-MM-DD); return events on or after it."},
            "until": {"type": "string", "description":
                      "Optional ISO date (YYYY-MM-DD); return events on or before it."},
        }),
        "outputSchema": _obj({
            "count": {"type": "integer"},
            "since": {"type": "string"},
            "until": {"type": "string"},
            "events": {"type": "array", "items": _EVENT_ITEM},
        }, ["count", "events"]),
        "annotations": RO,
        "handler": h_get_timeline,
    },
    {
        "name": "memo_get_dossier",
        "title": "Read a vault file",
        "description": (
            "Return the full markdown of one vault file. Paths are resolved strictly "
            "inside the vault; anything escaping it is refused. Use memo_list_files "
            "first if you do not know the exact path."
        ),
        "inputSchema": _obj({
            "path": {"type": "string", "description":
                     "Vault-relative path, '.md' optional "
                     "(e.g. 'projects/demo-project.md', 'clients/acme-corp')."},
        }, ["path"]),
        "outputSchema": _obj({
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, ["path", "content"]),
        "annotations": RO,
        "handler": h_get_dossier,
    },
    {
        "name": "memo_list_projects",
        "title": "List all projects",
        "description": (
            "Return the canonical list of the owner's projects from projects/*.md "
            "(id, title, status, path) plus total count and by_status. "
            "Use this whenever asked how many projects exist or to enumerate them. "
            "Do NOT use sessions/ folder counts — those are only projects with captured chats."
        ),
        "inputSchema": _obj({
            "status": {"type": "string", "description":
                       "Optional status filter: active, completed, archived, empty."},
        }),
        "outputSchema": _obj({
            "count": {"type": "integer"},
            "by_status": {"type": "object"},
            "projects": {"type": "array"},
            "note": {"type": "string"},
        }, ["count", "projects"]),
        "annotations": RO,
        "handler": h_list_projects,
    },
    {
        "name": "memo_list_files",
        "title": "List vault files",
        "description": (
            "Enumerate every markdown file in the vault with its title, optionally "
            "scoped to a subdirectory such as 'clients' or 'projects'. Use this to "
            "discover the vault structure instead of guessing paths."
        ),
        "inputSchema": _obj({
            "subdir": {"type": "string", "description":
                       "Optional subdirectory (e.g. 'clients', 'projects', 'stack')."},
        }),
        "outputSchema": _obj({
            "count": {"type": "integer"},
            "subdir": {"type": "string"},
            "files": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "title": {"type": "string"},
                "bytes": {"type": "integer"}}}},
        }, ["count", "files"]),
        "annotations": RO,
        "handler": h_list_files,
    },
    {
        "name": "memo_status",
        "title": "Vault health status",
        "description": (
            "Report vault health: file count, index freshness (added/updated/purged), "
            "graph size, timeline event count, whether git snapshots are active, and "
            "the SQLite version."
        ),
        "inputSchema": _obj({}),
        "outputSchema": _obj({
            "vault": {"type": "string"},
            "markdown_files": {"type": "integer"},
            "index": {"type": "object"},
            "graph": {"type": "object"},
            "timeline_events": {"type": "integer"},
            "git_snapshots": {"type": "boolean"},
            "sqlite_version": {"type": "string"},
        }, ["vault", "markdown_files"]),
        "annotations": RO,
        "handler": h_status,
    },
    {
        "name": "memo_lint",
        "title": "Lint vault integrity",
        "description": (
            "Run the Memo vault integrity gate: required frontmatter fields, closed tag "
            "taxonomy, entity_id ↔ graph sync, and broken wikilink detection. Returns "
            "error/warning counts and sample findings."
        ),
        "inputSchema": _obj({}),
        "outputSchema": _obj({
            "ok": {"type": "boolean"},
            "files": {"type": "integer"},
            "error_count": {"type": "integer"},
            "warning_count": {"type": "integer"},
            "errors": {"type": "array", "items": {"type": "object"}},
            "warnings": {"type": "array", "items": {"type": "object"}},
        }, ["ok", "files", "error_count", "warning_count"]),
        "annotations": RO,
        "handler": h_lint,
    },
    {
        "name": "memo_query_props",
        "title": "Query notes by frontmatter",
        "description": (
            "List vault notes by structured frontmatter properties without a text query. "
            "Use for inventories like 'all active clients' or 'active Salla clients' via "
            "type=client, status=active, tag=tech/salla. Prefer this over inventing a "
            "keyword for memo_search when you only have property filters."
        ),
        "inputSchema": _obj({
            "type": {"type": "string",
                     "description": "Optional note type (client, project, stack, ...)."},
            "status": {"type": "string",
                       "description": "Optional status (active, paused, completed, ...)."},
            "tag": {"type": "string",
                    "description": "Optional exact tag (e.g. 'tech/salla', 'tier/flagship')."},
            "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200,
                      "description": "Maximum notes to return (default 50)."},
        }),
        "outputSchema": _obj({
            "count": {"type": "integer"},
            "filters": {"type": "object"},
            "results": {"type": "array", "items": {"type": "object", "properties": {
                "path": {"type": "string"}, "title": {"type": "string"},
                "type": {"type": "string"}, "status": {"type": "string"},
                "entity_id": {"type": "string"}, "tags": {"type": "array"},
                "wikilink": {"type": "string"}}}},
        }, ["count", "results"]),
        "annotations": RO,
        "handler": h_query_props,
    },
    {
        "name": "memo_append_fact",
        "title": "Append a verified fact",
        "description": (
            "Append a new verified fact to timeline.md (inserted into the correct month "
            "block of Living History) or to sessions/current.md. Refuses to write a "
            "summary that already exists, so calling it twice is safe, and holds back a "
            "rephrased near-duplicate. One call writes one event: line breaks in "
            "category, summary or link collapse to spaces. The date must be exactly "
            "YYYY-MM-DD. Git commits only the touched file: a snapshot right before the "
            "write if it held uncommitted edits, then the change; hooks run. A refused, "
            "duplicate or held-back call makes no commit."
        ),
        "inputSchema": _obj({
            "target": {"type": "string", "enum": ["timeline", "session"],
                       "description": "'timeline' for durable dated facts, "
                                      "'session' for in-flight scratchpad notes."},
            "category": {"type": "string", "description":
                         "Category label (e.g. 'Compensation', 'Client Agreement', "
                         "'Infrastructure', 'Hackathon')."},
            "summary": {"type": "string", "description":
                        "Token-dense English summary of the fact."},
            "date": {"type": "string", "description":
                     "ISO date, exactly YYYY-MM-DD (e.g. '2026-09-05'). Defaults to "
                     "today. Other spellings ('20260905', '2026-9-5') are refused."},
            "link": {"type": "string", "description":
                     "Optional wikilink target (e.g. 'projects/demo-project')."},
            "force": {"type": "boolean", "default": False, "description":
                      "Write even if a near-duplicate timeline event exists. Only set "
                      "this after reading the reported candidates and confirming the "
                      "fact is genuinely distinct."},
        }, ["target", "category", "summary"]),
        "outputSchema": _obj({
            "file": {"type": "string"},
            "target": {"type": "string"},
            "entry": {"type": "string"},
            "placement": {"type": "string"},
            "duplicate": {"type": "boolean"},
            "near_duplicate": {"type": "boolean"},
            "candidates": {"type": "array", "items": {"type": "object"}},
        }, ["target", "duplicate"]),
        "annotations": WRITE,
        "handler": h_append_fact,
    },
    {
        "name": "memo_append_section",
        "title": "Append under a heading",
        "description": (
            "Append a block of markdown at the end of a named section of a vault file "
            "(e.g. add a deliverable under '## Scope of Work & Deliverables' in a client "
            "note). Use this — not memo_update_fact — whenever you are ADDING content "
            "rather than correcting it: no fragile anchor text is required. Identical "
            "text already present is skipped. Sets the note's existing 'updated:' "
            "frontmatter value to today. Git commits only that file: a snapshot right "
            "before the write if it held uncommitted edits, then the change; hooks run. "
            "A refused or duplicate call makes no commit."
        ),
        "inputSchema": _obj({
            "path": {"type": "string", "description":
                     "Vault-relative file (e.g. 'clients/acme-corp.md')."},
            "heading": {"type": "string", "description":
                        "Heading text to append under, with or without '#' "
                        "(e.g. 'Scope of Work & Deliverables'). Unknown headings are "
                        "refused with the list of real ones."},
            "text": {"type": "string", "description":
                     "Markdown block to append (e.g. '- WhatsApp cart recovery live')."},
            "create_missing": {"type": "boolean", "default": False, "description":
                               "Create the section at end of file if the heading does "
                               "not exist."},
        }, ["path", "heading", "text"]),
        "outputSchema": _obj({
            "file": {"type": "string"},
            "heading": {"type": "string"},
            "entry": {"type": "string"},
            "placement": {"type": "string"},
            "duplicate": {"type": "boolean"},
        }, ["file", "duplicate"]),
        "annotations": WRITE,
        "handler": h_append_section,
    },
    {
        "name": "memo_create_note",
        "title": "Create a new vault note",
        "description": (
            "Create a new note (new client, project, stack tool, subscription, hackathon, profile) "
            "from its '_templates/<type>.md' template with lint-clean frontmatter "
            "(the template's second-brain-id / second-brain-synced stamp is never "
            "copied; title and aliases are YAML-quoted). Refuses an absolute path such "
            "as '/clients/x.md', any '..' segment, any dot segment ('.' or a hidden "
            "name such as '.obsidian'), and an agent instruction-file name in any case "
            "(CLAUDE.md, GEMINI.md, AGENTS.md: use a vendor-prefixed name such as "
            "'stack/anthropic-claude.md'). Also refuses to overwrite an existing file, "
            "to create a file at the vault root (six hubs only), to use a "
            "type/status/tag outside the taxonomy, or to reference an entity_id that is "
            "not in the graph. Git commits only the new file; hooks run; a refused call "
            "makes no commit. Reports the lint result for the created file."
        ),
        "inputSchema": _obj({
            "path": {"type": "string", "description":
                     "Vault-relative path in a domain folder (e.g. 'clients/globex.md'). "
                     "No leading '/', no '..' or dot segments, and not named "
                     "claude.md, gemini.md or agents.md."},
            "title": {"type": "string", "description": "Human title, e.g. 'Globex Studio'."},
            "type": {"type": "string", "description":
                     "Note type: client, project, stack, subscription, expertise, hackathon, "
                     "profile, core. Must match the folder."},
            "status": {"type": "string", "default": "active", "description":
                       "active, paused, completed, planned, archived, empty."},
            "tags": {"type": "array", "items": {"type": "string"}, "description":
                     "Extra taxonomy tags (e.g. ['tech/salla', 'domain/ecommerce']). "
                     "type/ and status/ tags are added automatically."},
            "body": {"type": "string", "description":
                     "Optional markdown body. Omit to use the type's template."},
            "entity_id": {"type": "string", "description":
                          "Optional graph entity id; must already exist."},
            "aliases": {"type": "array", "items": {"type": "string"},
                        "description": "Optional Obsidian aliases."},
        }, ["path", "title", "type"]),
        "outputSchema": _obj({
            "file": {"type": "string"},
            "title": {"type": "string"},
            "type": {"type": "string"},
            "status": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "templated": {"type": "boolean"},
            "lint_ok": {"type": "boolean"},
            "lint_errors": {"type": "array", "items": {"type": "object"}},
        }, ["file", "title", "type"]),
        "annotations": WRITE,
        "handler": h_create_note,
    },
    {
        "name": "memo_update_fact",
        "title": "Update an existing fact",
        "description": (
            "Replace an exact snippet inside a vault file — for correcting a record that "
            "has changed (a rate, a salary, a status). DESTRUCTIVE: it overwrites "
            "existing memory, so only call it after the user has explicitly confirmed "
            "the change. The edit is refused unless 'old_text' occurs exactly once. Sets "
            "the note's existing 'updated:' frontmatter value to today. Git commits only "
            "that file: a snapshot right before the write if it held uncommitted edits, "
            "then the change; hooks run. A refused call, or a new_text identical to "
            "old_text (reported as changed=false), makes no commit."
        ),
        "inputSchema": _obj({
            "path": {"type": "string", "description":
                     "Vault-relative file to edit (e.g. 'clients/acme-corp.md')."},
            "old_text": {"type": "string", "description":
                         "Exact text to replace, copied verbatim from the file. Must "
                         "occur exactly once; add surrounding context to disambiguate."},
            "new_text": {"type": "string", "description":
                         "Replacement text. Empty string deletes the snippet."},
        }, ["path", "old_text", "new_text"]),
        "outputSchema": _obj({
            "file": {"type": "string"},
            "replaced": {"type": "string"},
            "with": {"type": "string"},
            "changed": {"type": "boolean"},
        }, ["file"]),
        "annotations": DESTRUCTIVE,
        "handler": h_update_fact,
    },
    {
        "name": "memo_add_entity",
        "title": "Add graph entity or relationship",
        "description": (
            "Register a new person, organization, client, project, hackathon, or "
            "technology in graph/entities.json, and/or link existing entities with a "
            "semantic relationship. Entity types, relation names, duplicate ids, and "
            "dangling endpoints are all validated before anything is written. Git "
            "commits only graph/entities.json; hooks run. A refused call, or one with "
            "nothing new to add, makes no commit."
        ),
        "inputSchema": _obj({
            "entity": {"type": "object", "description":
                       "New entity: {id, type, name, name_ar?, roles?, file?}.",
                       "properties": {
                           "id": {"type": "string"},
                           "type": {"type": "string"},
                           "name": {"type": "string"},
                           "name_ar": {"type": "string"},
                           "file": {"type": "string"},
                       }},
            "relationships": {"type": "array", "description":
                              "Triplets {source, relation, target, notes?}. Both "
                              "endpoints must be known entity ids.",
                              "items": {"type": "object", "properties": {
                                  "source": {"type": "string"},
                                  "relation": {"type": "string"},
                                  "target": {"type": "string"},
                                  "notes": {"type": "string"}}}},
        }),
        "outputSchema": _obj({
            "entity": {"type": ["string", "null"]},
            "relationships": {"type": "array", "items": {"type": "object"}},
            "entity_count": {"type": "integer"},
            "relationship_count": {"type": "integer"},
            "note": {"type": "string"},
        }),
        "annotations": WRITE,
        "handler": h_add_entity,
    },
]

TOOLS += [
    {
        "name": "memo_session_timeline",
        "title": "Session memory around one item",
        "description": (
            "Step 2 of 3. What happened before and after one recorded item: an observation id "
            "(e.g. 123), a session summary id (e.g. S12) or a date (YYYY-MM-DD). Returns a compact, "
            "oldest-first index across sessions. This is session memory; for the life and business "
            "timeline in timeline.md use memo_get_timeline."
        ),
        "inputSchema": _obj({
            "anchor": {"type": "string", "description": "Observation id, S<summary id> or YYYY-MM-DD."},
            "before": {"type": "integer", "default": 10, "minimum": 0, "maximum": 50},
            "after": {"type": "integer", "default": 10, "minimum": 0, "maximum": 50},
            "project": {"type": "string", "description": "Optional project name filter."},
        }, ["anchor"]),
        "outputSchema": _obj({"anchor": {"type": "string"}, "count": {"type": "integer"},
                              "items": {"type": "array", "items": {"type": "object"}}},
                             ["anchor", "count", "items"]),
        "annotations": RO,
        "handler": h_session_timeline,
    },
    {
        "name": "memo_get_observations",
        "title": "Full details of recorded items",
        "description": (
            "Step 3 of 3. Full facts, narrative, files and vault note for observation ids (123) "
            "and session summary ids (S12) found with memo_search or memo_session_timeline. "
            "Fetch only the ids the task needs."
        ),
        "inputSchema": _obj({
            "ids": {"type": "array", "items": {"type": ["integer", "string"]}, "maxItems": 50,
                    "description": "Observation ids and/or S-prefixed summary ids."},
        }, ["ids"]),
        "outputSchema": _obj({"count": {"type": "integer"},
                              "items": {"type": "array", "items": {"type": "object"}}}, ["count", "items"]),
        "annotations": RO,
        "handler": h_get_observations,
    },
    {
        "name": "memo_save_memory",
        "title": "Save one memory by hand",
        "description": (
            "Record one observation that capture would not see: a decision, a fact from a call, "
            "a client detail. It is stored in session memory and written to the vault under "
            "sessions/<project>/. Never put passwords, keys or tokens in it."
        ),
        "inputSchema": _obj({
            "title": {"type": "string", "description": "Short title starting with a verb (decided, found, set)."},
            "text": {"type": "string", "description": "What happened and why it matters."},
            "project": {"type": "string", "description": "Project name, e.g. 'my-project'."},
            "type": {"type": "string", "default": "discovery", "description":
                     "bugfix, feature, refactor, change, discovery, decision, security_alert, "
                     "security_note or sensitive."},
            "facts": {"type": "array", "items": {"type": "string"},
                      "description": "Optional standalone facts."},
        }, ["title", "text", "project"]),
        "outputSchema": _obj({"id": {"type": ["integer", "null"]}, "new": {"type": "boolean"},
                              "session": {"type": "string"}, "markdown": {"type": ["string", "null"]}},
                             ["id", "new", "session"]),
        "annotations": WRITE,
        "handler": h_save_memory,
    },
]

TOOLS_DEFINITION = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]
HANDLERS = {t["name"]: t["handler"] for t in TOOLS}

RESOURCES = [
    ("memo://core/readme", "Memo Core Identity & Operational Directives",
     "Master entry point: owner background, hard rules, active projects, daily rhythm.",
     "README.md", "text/markdown"),
    ("memo://core/home", "Memo Human Dashboard",
     "Human-facing home board: Bases, Canvas, and quick vault navigation.",
     "HOME.md", "text/markdown"),
    ("memo://core/timeline", "Chronological Living History",
     "Verified record of milestones, compensation transitions, and upcoming dates.",
     "timeline.md", "text/markdown"),
    ("memo://core/decisions", "Locked Decisions & Rationales",
     "Settled decisions that agents must treat as binding context.",
     "decisions.md", "text/markdown"),
    ("memo://core/stack", "Stack Map of Contents",
     "Tooling MOC pointing into the stack/ domain folder.",
     "stack.md", "text/markdown"),
    ("memo://core/subscriptions", "Subscriptions Map of Contents",
     "Billing MOC pointing into the subscriptions/ domain folder.",
     "subscriptions.md", "text/markdown"),
    ("memo://core/vault-spec", "Vault Structure Specification",
     "Folder meanings, root policy, frontmatter, and agent contract for the vault.",
     "docs/VAULT_SPEC.md", "text/markdown"),
    ("memo://core/graph", "Memo Knowledge Graph (Entities & Relations)",
     "Structured registry of entities and semantic triplets.",
     "graph/entities.json", "application/json"),
    ("memo://core/schema", "Knowledge Graph Ontology",
     "Entity and relationship type definitions for the graph.",
     "graph/schema.md", "text/markdown"),
    ("memo://agent/directives", "DemoApp Operational Directives & Core Persona",
     "Protocols, latency budgets, and boundaries for the DemoApp agent.",
     "agent/directives.md", "text/markdown"),
    ("memo://agent/heuristics", "Learned Engineering Heuristics",
     "Accumulated failure modes and lessons to avoid repeating.",
     "agent/learned-heuristics.md", "text/markdown"),
    ("memo://session/current", "Active Session Scratchpad",
     "Live scratchpad for in-flight task context and candidate facts.",
     "sessions/current.md", "text/markdown"),
]

RESOURCES_DEFINITION = [
    {"uri": uri, "name": name, "description": desc, "mimeType": mime}
    for uri, name, desc, _, mime in RESOURCES
]
RESOURCE_FILES = {uri: (rel, mime) for uri, _, _, rel, mime in RESOURCES}

PROMPTS_DEFINITION = [{
    "name": "owner_context",
    "description": "The vault owner's identity, communication preferences, and "
                   "operating rules, loaded from the vault README.",
    "arguments": [],
}]


# ----------------------------------------------------------------------
# JSON-RPC dispatcher
# ----------------------------------------------------------------------

def _ok(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_error(msg_id: Any, text: str) -> Dict[str, Any]:
    return _ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": True})


def handle_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    msg_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    remote = None
    if (os.environ.get("MEMO_REMOTE") or "").strip() in {"1", "true", "TRUE", "yes", "YES"}:
        import mcp_remote as remote  # local import keeps non-remote path light
        early = remote.before(method or "", params, msg_id=msg_id)
        if early is not None:
            return early

    if method == "initialize":
        requested = params.get("protocolVersion")
        negotiated = requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
        log.info("initialize: client requested %s, negotiated %s", requested, negotiated)
        caps = {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
        }
        if remote is not None:
            caps.pop("resources", None)
            caps.pop("prompts", None)
        resp = _ok(msg_id, {
            "protocolVersion": negotiated,
            "capabilities": caps,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                           "title": "Memo Sovereign Memory"},
            "instructions": SERVER_INSTRUCTIONS,
        })
        if remote is not None:
            resp = remote.after(method, params, resp)
        return resp

    if method == "ping":
        return _ok(msg_id, {})

    if method is not None and method.startswith("notifications/"):
        return None

    if method == "tools/list":
        tools = TOOLS_DEFINITION
        if remote is not None:
            tools = remote.filter_tools_definition(tools)
        resp = _ok(msg_id, {"tools": tools})
        if remote is not None:
            resp = remote.after(method, params, resp)
        return resp

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(tool_name)
        if handler is None:
            resp = _err(msg_id, INVALID_PARAMS,
                        f"Unknown tool '{tool_name}'. Available: "
                        + ", ".join(sorted(HANDLERS)))
            return remote.after(method, params, resp) if remote is not None else resp
        try:
            text, structured = handler(args)
            resp = _ok(msg_id, {
                "content": [{"type": "text", "text": text}],
                "structuredContent": structured,
                "isError": False,
            })
        except MemoError as exc:
            log.warning("tool %s refused: %s", tool_name, exc)
            resp = _tool_error(msg_id, str(exc))
        except Exception as exc:  # unexpected: surface, don't crash the server
            log.exception("tool %s failed", tool_name)
            resp = _tool_error(msg_id, f"Error executing '{tool_name}': {exc}")
        if remote is not None:
            resp = remote.after(method, params, resp)
        return resp

    if method == "resources/list":
        return _ok(msg_id, {"resources": RESOURCES_DEFINITION})

    if method == "resources/templates/list":
        return _ok(msg_id, {"resourceTemplates": []})

    if method == "resources/read":
        uri = params.get("uri", "")
        entry = RESOURCE_FILES.get(uri)
        if entry is None:
            return _err(msg_id, INVALID_PARAMS,
                        f"Unknown resource '{uri}'. Available: "
                        + ", ".join(sorted(RESOURCE_FILES)))
        rel, mime = entry
        try:
            target = core.safe_vault_path(rel, must_exist=True)
        except MemoError as exc:
            return _err(msg_id, INVALID_PARAMS, str(exc))
        return _ok(msg_id, {"contents": [{
            "uri": uri, "mimeType": mime,
            "text": target.read_text(encoding="utf-8", errors="replace"),
        }]})

    if method == "prompts/list":
        return _ok(msg_id, {"prompts": PROMPTS_DEFINITION})

    if method == "prompts/get":
        name = params.get("name")
        if name not in ("owner_context", "founder_context"):
            return _err(msg_id, INVALID_PARAMS,
                        f"Unknown prompt '{name}'. Available: owner_context")
        try:
            readme = core.safe_vault_path("README.md", must_exist=True).read_text(
                encoding="utf-8", errors="replace")
        except MemoError as exc:
            return _err(msg_id, INVALID_PARAMS, str(exc))
        return _ok(msg_id, {
            "description": "Primary operating persona and rules for the vault owner",
            "messages": [{"role": "user", "content": {
                "type": "text",
                "text": f"Operational Owner Context from Memo:\n\n{readme}"}}],
        })

    if msg_id is not None:
        return _err(msg_id, METHOD_NOT_FOUND, f"Method not supported: '{method}'")
    return None


def handle_payload(payload: Any) -> Optional[Any]:
    """Handle a single request or a JSON-RPC batch array."""
    if isinstance(payload, list):
        if not payload:
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request: empty batch"}}
        responses = [r for r in (handle_request(m) for m in payload if isinstance(m, dict))
                     if r is not None]
        return responses or None
    if isinstance(payload, dict):
        return handle_request(payload)
    return {"jsonrpc": "2.0", "id": None,
            "error": {"code": -32600, "message": "Invalid Request"}}


def main() -> None:
    try:
        core.check_fts5()
    except MemoError as exc:
        log.error("%s", exc)
        sys.exit(1)

    log.info("Memo MCP %s starting on vault %s", SERVER_VERSION, core.VAULT_DIR)
    try:
        stats = core.sync_index()
        log.info("index ready: %d files (+%d new, %d updated, %d purged)",
                 stats["total"], stats["added"], stats["updated"], stats["purged"])
    except Exception:
        log.exception("initial indexing failed; continuing with a stale index")

    if (os.environ.get("MEMO_REMOTE") or "").strip() in {"1", "true", "TRUE", "yes", "YES"}:
        import mcp_remote
        mcp_remote.install()
        try:
            from memo_capture import sync as _memo_sync  # type: ignore
        except ImportError:
            log.warning("MEMO_REMOTE=1 but memo_capture.sync is missing; "
                        "skipping background sync (plan 004 TASK-001 / plan 003)")
        else:
            starter = getattr(_memo_sync, "start_background", None)
            if starter is None:
                log.warning("memo_capture.sync has no start_background(); skipping")
            else:
                def _on_scripts_changed() -> None:
                    log.warning("incoming scripts_merged change; exiting 75 for restart")
                    sys.stdout.flush()
                    os._exit(75)
                wake = starter(on_scripts_changed=_on_scripts_changed)
                # Expose for memo_save_memory wake-after-save (plan 004 TASK-002).
                mcp_remote.SYNC_WAKE = wake  # type: ignore[attr-defined]
                log.info("remote background sync started")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": PARSE_ERROR, "message": "Parse error: invalid JSON"},
            }) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_payload(payload)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
