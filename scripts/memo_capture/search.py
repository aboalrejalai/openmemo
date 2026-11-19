"""Search session memory: the three-step, progressive-disclosure flow.

1. search()            → a compact index (ids, dates, types, titles) ranked via
                         Zero-Cost Hybrid Reciprocal Rank Fusion (FTS5 BM25 +
                         Arabic/Substring Trigram + Knowledge Graph Alias Expansion)
2. timeline()          → what happened around one id or date
3. get_observations()  → full facts, narrative and files for chosen ids
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import state
from .paths import vault_dir

_TERM = re.compile(r"[\w؀-ۿ][\w؀-ۿ.\-/]*", re.UNICODE)
_RRF_K = 60

_GRAPH_CACHE: Dict[str, Any] = {"mtime": 0.0, "path": "", "entities": []}


def _load_graph_entities() -> List[Dict[str, Any]]:
    """Load graph/entities.json with mtime caching for sub-millisecond alias expansion."""
    try:
        gpath = vault_dir() / "graph" / "entities.json"
        if not gpath.exists():
            return []
        st = gpath.stat()
        key_path = str(gpath)
        if _GRAPH_CACHE["path"] == key_path and _GRAPH_CACHE["mtime"] == st.st_mtime:
            return _GRAPH_CACHE["entities"]
        data = json.loads(gpath.read_text(encoding="utf-8"))
        entities = data.get("entities") or []
        _GRAPH_CACHE.update({"mtime": st.st_mtime, "path": key_path, "entities": entities})
        return entities
    except Exception:
        return []


def expand_query_with_graph(query: str) -> Tuple[List[str], Set[str]]:
    """Expand query terms using graph/entities.json (id, name, name_ar, aliases, file stem).

    Returns (expanded_terms, matched_project_or_entity_slugs).
    """
    raw_terms = _TERM.findall(query or "")
    if not raw_terms:
        return [], set()

    q_lower = (query or "").strip().lower()
    term_lowers = {t.lower() for t in raw_terms if len(t) >= 2}
    expanded: List[str] = list(raw_terms)
    seen_lower: Set[str] = {t.lower() for t in expanded}
    matched_slugs: Set[str] = set()

    for ent in _load_graph_entities():
        eid = str(ent.get("id") or "").strip()
        name = str(ent.get("name") or "").strip()
        name_ar = str(ent.get("name_ar") or "").strip()
        aliases = [str(a).strip() for a in (ent.get("aliases") or []) if a]
        file_rel = str(ent.get("file") or "").strip()
        file_stem = Path(file_rel).stem if file_rel else ""

        candidates = [c for c in (eid, name, name_ar, file_stem, *aliases) if c]
        cand_lowers = [c.lower() for c in candidates]

        hit = False
        for cl in cand_lowers:
            if cl in term_lowers or (len(cl) >= 3 and cl in q_lower):
                hit = True
                break
            for tl in term_lowers:
                if len(tl) >= 3 and tl in cl:
                    hit = True
                    break
            if hit:
                break

        if hit:
            if eid:
                matched_slugs.add(eid.lower())
            if file_stem:
                matched_slugs.add(file_stem.lower())
            for cand in candidates:
                for sub_t in _TERM.findall(cand):
                    if sub_t.lower() not in seen_lower:
                        seen_lower.add(sub_t.lower())
                        expanded.append(sub_t)

    return expanded[:18], matched_slugs


def _fts_query(query: str, expanded_terms: Optional[List[str]] = None) -> Optional[str]:
    terms = expanded_terms if expanded_terms is not None else _TERM.findall(query or "")
    if not terms:
        return None
    quoted = ['"' + t.replace('"', "") + '"' for t in terms[:16] if t.strip()]
    return " OR ".join(quoted) if quoted else None


def _date_ms(value: Optional[str], end: bool = False) -> Optional[int]:
    if not value:
        return None
    day = datetime.date.fromisoformat(value[:10])
    moment = datetime.datetime.combine(day, datetime.time.max if end else datetime.time.min)
    return int(moment.timestamp() * 1000)


def _when(ms: int) -> str:
    return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")


def _filters(project, obs_type, since, until, alias=""):
    where, params = [], []
    if project:
        where.append(f"({alias}project = ? OR {alias}project LIKE ?)")
        params += [project, f"{project}/%"]
    if obs_type:
        where.append(f"{alias}type = ?")
        params.append(obs_type)
    if since:
        where.append(f"{alias}created_at >= ?")
        params.append(_date_ms(since))
    if until:
        where.append(f"{alias}created_at <= ?")
        params.append(_date_ms(until, end=True))
    return (" AND " + " AND ".join(where)) if where else "", params


def search(conn, query: str, *, project: Optional[str] = None, obs_type: Optional[str] = None,
           concept: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None,
           limit: int = 20) -> List[Dict[str, Any]]:
    """Zero-Cost Hybrid Search using Reciprocal Rank Fusion (RRF).

    Fuses 3 local rankings:
      1. FTS5 BM25 rank (with Knowledge Graph bilingual alias expansion)
      2. Trigram substring rank (Arabic roots & partial tokens)
      3. Knowledge Graph entity/project affinity boost
    """
    expanded_terms, matched_slugs = expand_query_with_graph(query)
    fts = _fts_query(query, expanded_terms)
    hits: Dict[str, Dict[str, Any]] = {}
    fts_ranks: Dict[str, int] = {}
    tri_ranks: Dict[str, int] = {}
    graph_ranks: Dict[str, int] = {}

    where, params = _filters(project, obs_type, since, until, "o.")
    if fts:
        try:
            rows = conn.execute(
                f"""SELECT o.*, bm25(observations_fts) AS score FROM observations_fts
                    JOIN observations o ON o.id = observations_fts.rowid
                    WHERE observations_fts MATCH ?{where} ORDER BY score LIMIT ?""",
                (fts, *params, limit * 3),
            ).fetchall()
            for rank_idx, r in enumerate(rows, 1):
                key = f"o{r['id']}"
                hits[key] = _obs_hit(r, r["score"])
                fts_ranks[key] = rank_idx
        except Exception:
            pass
        if not obs_type:
            swhere, sparams = _filters(project, None, since, until, "s.")
            try:
                srows = conn.execute(
                    f"""SELECT s.*, bm25(summaries_fts) AS score FROM summaries_fts
                        JOIN session_summaries s ON s.id = summaries_fts.rowid
                        WHERE summaries_fts MATCH ?{swhere} ORDER BY score LIMIT ?""",
                    (fts, *sparams, limit * 2),
                ).fetchall()
                for rank_idx, r in enumerate(srows, 1):
                    key = f"s{r['id']}"
                    hits[key] = _summary_hit(r, r["score"])
                    fts_ranks[key] = rank_idx
            except Exception:
                pass

    # Signal 2: Trigram substring index (always queried when len >= 3 for true RRF fusion)
    q_clean = (query or "").strip().replace('"', "")
    tri_candidates = [q_clean] if len(q_clean) >= 3 else []
    for t in expanded_terms:
        tc = t.strip().replace('"', "")
        if len(tc) >= 3 and tc not in tri_candidates:
            tri_candidates.append(tc)

    if tri_candidates and state.has_trigram(conn):
        tri_expr = " OR ".join(f'"{c}"' for c in tri_candidates[:8])
        try:
            refs = conn.execute(
                "SELECT kind, ref FROM memory_trigram WHERE memory_trigram MATCH ? LIMIT ?",
                (tri_expr, limit * 3),
            ).fetchall()
        except Exception:
            refs = []
        for rank_idx, ref in enumerate(refs, 1):
            key = ("o" if ref["kind"] == "observation" else "s") + str(ref["ref"])
            if key not in tri_ranks:
                tri_ranks[key] = rank_idx
            if key in hits:
                continue
            if ref["kind"] != "observation" and obs_type:
                continue
            table = "observations" if ref["kind"] == "observation" else "session_summaries"
            alias_where, alias_params = _filters(
                project, obs_type if table == "observations" else None, since, until
            )
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?{alias_where}",
                (ref["ref"], *alias_params),
            ).fetchone()
            if row is not None:
                hits[key] = _obs_hit(row, 0.0) if table == "observations" else _summary_hit(row, 0.0)

    # Signal 3: Knowledge Graph entity/project match boost
    if matched_slugs:
        slug_items: List[Tuple[int, str]] = []
        for key, item in hits.items():
            proj_low = str(item.get("project") or "").lower()
            title_low = str(item.get("title") or "").lower()
            if any(s == proj_low or proj_low.startswith(f"{s}/") or s in title_low for s in matched_slugs):
                slug_items.append((item["at"], key))
        slug_items.sort(reverse=True)
        for rank_idx, (_, key) in enumerate(slug_items, 1):
            graph_ranks[key] = rank_idx

    # Compute Reciprocal Rank Fusion (RRF) score
    out: List[Dict[str, Any]] = []
    for key, item in hits.items():
        if concept and item["kind"] == "observation" and concept not in item.get("concepts", []):
            continue
        rrf = 0.0
        if key in fts_ranks:
            rrf += 1.0 / (_RRF_K + fts_ranks[key])
        if key in tri_ranks:
            rrf += 1.0 / (_RRF_K + tri_ranks[key])
        if key in graph_ranks:
            rrf += 1.0 / (_RRF_K + graph_ranks[key])
        # Store negative RRF in score so existing ascending sort `h["score"]` puts highest RRF first
        item["score"] = -round(rrf, 6) if rrf > 0 else float(item.get("score") or 0.0)
        out.append(item)

    out.sort(key=lambda h: (h["score"], -h["at"]))
    return out[:limit]


def _obs_hit(r, score) -> Dict[str, Any]:
    return {"kind": "observation", "id": r["id"], "project": r["project"], "type": r["type"],
            "title": r["title"], "subtitle": r["subtitle"] or "", "date": _when(r["created_at"]),
            "at": int(r["created_at"]), "session": r["content_session_id"],
            "concepts": state.loads(r["concepts"]), "score": float(score or 0)}


def _summary_hit(r, score) -> Dict[str, Any]:
    return {"kind": "session", "id": f"S{r['id']}", "project": r["project"], "type": "session",
            "title": r["request"] or "session", "subtitle": (r["completed"] or "")[:200],
            "date": _when(r["created_at"]), "at": int(r["created_at"]), "session": r["content_session_id"],
            "score": float(score or 0)}


def format_index(hits: List[Dict[str, Any]]) -> str:
    lines = []
    for h in hits:
        icon = "🎯" if h["kind"] == "session" else ""
        lines.append(f"{h['id']} {h['date']} [{h['project']}] {icon}{h['type']} · {h['title']}")
    return "\n".join(lines)


def _anchor_ms(conn, anchor: str) -> Optional[int]:
    anchor = str(anchor or "").strip()
    if re.fullmatch(r"\d+", anchor):
        row = conn.execute("SELECT created_at FROM observations WHERE id = ?", (int(anchor),)).fetchone()
        return int(row[0]) if row else None
    if re.fullmatch(r"[Ss]\d+", anchor):
        row = conn.execute("SELECT created_at FROM session_summaries WHERE id = ?", (int(anchor[1:]),)).fetchone()
        return int(row[0]) if row else None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", anchor):
        return _date_ms(anchor)
    return None


def timeline(conn, anchor: str, *, before: int = 10, after: int = 10,
             project: Optional[str] = None) -> List[Dict[str, Any]]:
    at = _anchor_ms(conn, anchor)
    if at is None:
        return []
    where, params = _filters(project, None, None, None)
    items: List[Dict[str, Any]] = []
    for op, order, n in (("<", "DESC", before), (">=", "ASC", after + 1)):
        items += [_obs_hit(r, 0) for r in conn.execute(
            f"SELECT * FROM observations WHERE created_at {op} ?{where} ORDER BY created_at {order} LIMIT ?",
            (at, *params, n))]
        items += [_summary_hit(r, 0) for r in conn.execute(
            f"SELECT * FROM session_summaries WHERE created_at {op} ?{where} ORDER BY created_at {order} LIMIT ?",
            (at, *params, n))]
    items.sort(key=lambda h: h["at"])
    seen, out = set(), []
    for h in items:
        if (h["kind"], h["id"]) not in seen:
            seen.add((h["kind"], h["id"]))
            out.append(h)
    return out


def get_observations(conn, ids: List[Any]) -> List[Dict[str, Any]]:
    out = []
    for raw in ids[:50]:
        text = str(raw).strip()
        if re.fullmatch(r"[Ss]\d+", text):
            r = conn.execute("SELECT * FROM session_summaries WHERE id = ?", (int(text[1:]),)).fetchone()
            if r:
                out.append({"id": f"S{r['id']}", "kind": "session", "project": r["project"],
                            "date": _when(r["created_at"]), "request": r["request"],
                            "investigated": r["investigated"], "learned": r["learned"],
                            "completed": r["completed"], "next_steps": r["next_steps"], "notes": r["notes"],
                            "files_read": state.loads(r["files_read"]),
                            "files_edited": state.loads(r["files_edited"]), "session": r["content_session_id"],
                            "markdown": _markdown(conn, r["content_session_id"])})
            continue
        if not re.fullmatch(r"\d+", text):
            continue
        r = conn.execute("SELECT * FROM observations WHERE id = ?", (int(text),)).fetchone()
        if r:
            out.append({"id": r["id"], "kind": "observation", "project": r["project"], "type": r["type"],
                        "title": r["title"], "subtitle": r["subtitle"], "date": _when(r["created_at"]),
                        "facts": state.loads(r["facts"]), "narrative": r["narrative"],
                        "concepts": state.loads(r["concepts"]), "files_read": state.loads(r["files_read"]),
                        "files_modified": state.loads(r["files_modified"]), "session": r["content_session_id"],
                        "markdown": _markdown(conn, r["content_session_id"])})
    return out


def _markdown(conn, sid: str) -> Optional[str]:
    row = conn.execute("SELECT markdown_path FROM sessions WHERE content_session_id = ?", (sid,)).fetchone()
    return row[0] if row else None


def semantic_block(conn, project: str, prompt: str, limit: int = 5) -> str:
    """Prompt-time context (semantic_inject): the best matches for the new prompt."""
    hits = [h for h in search(conn, prompt, project=project, limit=limit) if h["kind"] == "observation"]
    if not hits:
        return ""
    return ("<memo-context>\nMemo: past observations related to this request "
            "(details: memo_get_observations):\n" + format_index(hits) + "\n</memo-context>")


# ----------------------------------------------------------------------
# Observation Hygiene: Deletion & Duplicate Compaction
# ----------------------------------------------------------------------

def delete_observation(conn, obs_id: int) -> bool:
    """Delete a single observation from SQLite and rebuild FTS5 cleanly."""
    row = conn.execute("SELECT id FROM observations WHERE id = ?", (int(obs_id),)).fetchone()
    if not row:
        return False
    with state.transaction(conn):
        conn.execute("DELETE FROM observation_files WHERE observation_id = ?", (int(obs_id),))
        if state.has_trigram(conn):
            conn.execute("DELETE FROM memory_trigram WHERE kind = 'observation' AND ref = ?", (int(obs_id),))
        conn.execute("DELETE FROM observations WHERE id = ?", (int(obs_id),))
        conn.execute("INSERT INTO observations_fts(observations_fts) VALUES ('rebuild')")
    return True


def find_duplicate_observations(conn, limit: int = 100) -> List[Dict[str, Any]]:
    """Find observations with identical normalized (project, type, title)."""
    rows = conn.execute(
        """SELECT id, project, type, title, occurrence_count, created_at
           FROM observations ORDER BY created_at ASC, id ASC"""
    ).fetchall()
    groups: Dict[Tuple[str, str, str], List[Any]] = {}
    for r in rows:
        norm_title = re.sub(r"\s+", " ", (r["title"] or "").strip().lower())
        if not norm_title:
            continue
        key = ((r["project"] or "").strip().lower(), (r["type"] or "").strip().lower(), norm_title)
        groups.setdefault(key, []).append(r)

    dups: List[Dict[str, Any]] = []
    for (proj, otype, _), items in groups.items():
        if len(items) > 1:
            keep = items[0]
            drops = [int(x["id"]) for x in items[1:]]
            dups.append({
                "project": keep["project"],
                "type": keep["type"],
                "title": keep["title"],
                "keep_id": int(keep["id"]),
                "drop_ids": drops,
                "count": len(items),
            })
    dups.sort(key=lambda d: d["count"], reverse=True)
    return dups[:limit]


def deduplicate_observations(conn) -> Dict[str, Any]:
    """Merge duplicate observations by summing occurrence_count into keep_id and deleting drop_ids."""
    dups = find_duplicate_observations(conn, limit=5000)
    if not dups:
        return {"groups_merged": 0, "observations_removed": 0}
    removed = 0
    with state.transaction(conn):
        for grp in dups:
            keep_id = grp["keep_id"]
            drop_ids = grp["drop_ids"]
            conn.execute(
                "UPDATE observations SET occurrence_count = occurrence_count + ? WHERE id = ?",
                (len(drop_ids), keep_id),
            )
            for did in drop_ids:
                conn.execute("DELETE FROM observation_files WHERE observation_id = ?", (did,))
                if state.has_trigram(conn):
                    conn.execute("DELETE FROM memory_trigram WHERE kind = 'observation' AND ref = ?", (did,))
                conn.execute("DELETE FROM observations WHERE id = ?", (did,))
                removed += 1
        conn.execute("INSERT INTO observations_fts(observations_fts) VALUES ('rebuild')")
    return {"groups_merged": len(dups), "observations_removed": removed}
