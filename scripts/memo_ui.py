#!/usr/bin/env python3
"""Memo Studio (`memo ui`): Built-in Zero-Build Local Web Viewer & Knowledge Graph Studio.

Binds strictly to 127.0.0.1 (default port 8766). Zero external pip/npm dependencies.
Provides:
  - Interactive 60fps HTML5 Canvas Force-Directed & Radial Knowledge Graph
  - Unified Hybrid RRF Search (Vault Dossiers + Session Observations + Graph Aliases)
  - Live Memory Stream with One-Click Observation Deletion & Duplicate Compaction
  - Slide-Over Markdown Dossier & Backlink Inspector
  - System, Worker, Sync, and Multi-Agent Adapter Health Cockpit
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memo_core as core
from memo_capture import health as capture_health
from memo_capture import search as capture_search
from memo_capture import settings, state, worker_client
from memo_capture.paths import vault_dir

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _when(ms: Any) -> str:
    if not ms:
        return "never"
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "never"


def _detect_agents() -> List[Dict[str, Any]]:
    home = Path.home()
    vault = vault_dir()
    agents = []

    # Claude Code
    claude_plugins = home / ".claude" / "plugins" / "installed_plugins.json"
    claude_ok = False
    try:
        if claude_plugins.exists():
            data = json.loads(claude_plugins.read_text(encoding="utf-8"))
            plugins = data.get("plugins", data)
            claude_ok = bool(plugins.get("memo@memo") or plugins.get("memo@memo-local"))
    except Exception:
        pass
    agents.append({"id": "claude", "name": "Claude Code", "connected": claude_ok, "path": "~/.claude"})

    # Cursor
    cursor_plugin = home / ".cursor" / "plugins" / "local" / "memo"
    cursor_ok = cursor_plugin.is_dir() or cursor_plugin.is_symlink()
    agents.append({"id": "cursor", "name": "Cursor IDE", "connected": cursor_ok, "path": "~/.cursor/plugins/local/memo"})

    # Antigravity
    agy_hooks = home / ".gemini" / "config" / "hooks.json"
    agy_ok = False
    try:
        if agy_hooks.exists():
            agy_ok = "memo" in json.loads(agy_hooks.read_text(encoding="utf-8"))
    except Exception:
        pass
    agents.append({"id": "antigravity", "name": "Google Antigravity", "connected": agy_ok, "path": "~/.gemini/config"})

    # Gemini CLI
    gem_settings = home / ".gemini" / "settings.json"
    gem_ok = False
    try:
        if gem_settings.exists():
            gdata = json.loads(gem_settings.read_text(encoding="utf-8"))
            gem_ok = "memo" in (gdata.get("mcpServers") or {})
    except Exception:
        pass
    agents.append({"id": "gemini", "name": "Gemini CLI", "connected": gem_ok, "path": "~/.gemini/settings.json"})

    # Codex
    codex_cfg = home / ".codex" / "config.toml"
    codex_ok = codex_cfg.exists() and "hooks = true" in codex_cfg.read_text(encoding="utf-8", errors="ignore")
    agents.append({"id": "codex", "name": "OpenAI Codex CLI", "connected": codex_ok, "path": "~/.codex/config.toml"})

    # Skills symlink
    skills_dir = home / ".agents" / "skills" / "memo"
    skills_ok = skills_dir.exists() or (vault / ".agents" / "skills" / "memo").exists()
    agents.append({"id": "skills", "name": "Memo Agent Skills", "connected": skills_ok, "path": ".agents/skills/*"})

    return agents


def build_status_payload() -> Dict[str, Any]:
    st = core.status()
    conn = state.connect()
    try:
        today = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp() * 1000)
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        total_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        total_summaries = conn.execute("SELECT COUNT(*) FROM session_summaries").fetchone()[0]
        total_tokens = conn.execute("SELECT COALESCE(SUM(discovery_tokens), 0) FROM observations").fetchone()[0]
        today_sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (today,)).fetchone()[0]
        today_obs = conn.execute("SELECT COUNT(*) FROM observations WHERE created_at >= ?", (today,)).fetchone()[0]
        last_obs = conn.execute("SELECT MAX(created_at) FROM observations").fetchone()[0]
        queue = state.queue_counts(conn)
        dups = capture_search.find_duplicate_observations(conn, limit=500)
        dup_removed_est = sum(len(d["drop_ids"]) for d in dups)

        proj_rows = conn.execute(
            """SELECT project, COUNT(*) AS n, MAX(created_at) AS last
               FROM observations GROUP BY project ORDER BY n DESC LIMIT 30"""
        ).fetchall()
        projects = [{"project": r["project"], "count": int(r["n"]), "last": _when(r["last"])} for r in proj_rows]

        type_rows = conn.execute(
            """SELECT type, COUNT(*) AS n FROM observations GROUP BY type ORDER BY n DESC"""
        ).fetchall()
        by_type = {r["type"]: int(r["n"]) for r in type_rows}
    finally:
        conn.close()

    worker_health = worker_client.request("/health", timeout=0.4)
    spool_count, _ = capture_health.spool_backlog()

    from memo_capture import sync as sync_mod
    sync_state = sync_mod.read_state()

    return {
        "vault": st,
        "search_mode": "hybrid-rrf",
        "capture": {
            "total_sessions": int(total_sessions),
            "total_observations": int(total_obs),
            "total_summaries": int(total_summaries),
            "total_tokens": int(total_tokens),
            "today_sessions": int(today_sessions),
            "today_observations": int(today_obs),
            "last_capture": _when(last_obs),
            "queue_pending": queue.get("pending", 0),
            "queue_processing": queue.get("processing", 0),
            "spool_waiting": spool_count,
            "duplicate_groups": len(dups),
            "duplicate_observations": dup_removed_est,
            "projects": projects,
            "by_type": by_type,
        },
        "worker": {
            "running": bool(worker_health),
            "pid": worker_health.get("pid") if worker_health else None,
            "port": settings.worker_port(),
            "observer_model": settings.get("observer_model"),
            "observer_provider": settings.get("observer_provider") or "claude",
        },
        "sync": {
            "status": sync_state.get("status") or "ok",
            "reason": sync_state.get("reason") or "",
            "last_ok": _when(sync_state.get("last_ok")),
        },
        "agents": _detect_agents(),
    }


def build_graph_payload() -> Dict[str, Any]:
    """Combine graph/entities.json, vault Markdown dossiers + [[wikilinks]], and session activity."""
    vault = vault_dir()
    graph_path = vault / "graph" / "entities.json"
    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    file_to_node_id: Dict[str, str] = {}
    edges: List[Dict[str, Any]] = []
    seen_edges = set()

    # Session counts by project slug
    obs_by_project: Dict[str, int] = {}
    conn = state.connect()
    try:
        for r in conn.execute("SELECT project, COUNT(*) AS n FROM observations GROUP BY project").fetchall():
            pname = str(r["project"] or "").strip()
            obs_by_project[pname.lower()] = int(r["n"])
            base = pname.split("/")[0].lower()
            obs_by_project[base] = obs_by_project.get(base, 0) + int(r["n"])
    except Exception:
        pass
    finally:
        conn.close()

    # 1. Load canonical entities & semantic relationships from graph/entities.json
    if graph_path.exists():
        try:
            gdata = json.loads(graph_path.read_text(encoding="utf-8"))
            for ent in gdata.get("entities") or []:
                eid = str(ent.get("id") or "").strip()
                if not eid:
                    continue
                frel = str(ent.get("file") or "").strip()
                stem = Path(frel).stem.lower() if frel else eid.lower()
                act = obs_by_project.get(eid.lower(), 0) or obs_by_project.get(stem, 0)
                node = {
                    "id": eid,
                    "label": ent.get("name") or eid,
                    "label_ar": ent.get("name_ar") or "",
                    "type": ent.get("type") or "Entity",
                    "category": ent.get("category") or "",
                    "status": ent.get("status") or "active",
                    "file": frel,
                    "aliases": ent.get("aliases") or [],
                    "roles": ent.get("roles") or [],
                    "obs_count": act,
                    "degree": 0,
                    "source": "graph",
                }
                nodes_by_id[eid] = node
                if frel:
                    file_to_node_id[frel] = eid
                    if frel.endswith(".md"):
                        file_to_node_id[frel[:-3]] = eid

            for rel in gdata.get("relationships") or []:
                src = str(rel.get("source") or "").strip()
                tgt = str(rel.get("target") or "").strip()
                rtype = str(rel.get("relation") or "RELATED").strip()
                if src and tgt and src in nodes_by_id and tgt in nodes_by_id:
                    ekey = (src, tgt, rtype)
                    if ekey not in seen_edges:
                        seen_edges.add(ekey)
                        edges.append({
                            "source": src,
                            "target": tgt,
                            "relation": rtype,
                            "notes": rel.get("notes") or "",
                            "kind": "semantic",
                        })
        except Exception:
            pass

    # 2. Scan curated vault dossiers (projects/, clients/, stack/, hackathons/, hubs)
    curated_folders = ("projects", "clients", "stack", "hackathons")
    hub_files = ("HOME.md", "README.md", "decisions.md", "stack.md", "timeline.md", "subscriptions.md")
    md_files: List[Path] = []
    for hf in hub_files:
        p = vault / hf
        if p.exists():
            md_files.append(p)
    for folder in curated_folders:
        d = vault / folder
        if d.is_dir():
            md_files.extend(sorted(d.glob("*.md")))

    type_map = {
        "projects": "Project",
        "clients": "Client",
        "stack": "Technology",
        "hackathons": "Hackathon",
    }

    file_links: List[Tuple[str, List[str]]] = []
    for path in md_files:
        rel = str(path.relative_to(vault)).replace(os.sep, "/")
        rel_no_ext = rel[:-3] if rel.endswith(".md") else rel
        text = path.read_text(encoding="utf-8", errors="replace")

        # Parse simple title & status from frontmatter
        title = path.stem
        status = "active"
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                fm = text[4:end]
                m_t = re.search(r"^title:\s*[\"']?([^\"'\n]+)", fm, re.M)
                if m_t:
                    title = m_t.group(1).strip()
                m_s = re.search(r"^status:\s*[\"']?([^\"'\n]+)", fm, re.M)
                if m_s:
                    status = m_s.group(1).strip()

        node_id = file_to_node_id.get(rel) or file_to_node_id.get(rel_no_ext)
        if not node_id:
            node_id = f"doc:{rel_no_ext}"
            folder_name = rel.split("/")[0] if "/" in rel else "Core"
            ntype = type_map.get(folder_name, "Core")
            stem_low = path.stem.lower()
            act = obs_by_project.get(stem_low, 0)
            nodes_by_id[node_id] = {
                "id": node_id,
                "label": title,
                "label_ar": "",
                "type": ntype,
                "category": folder_name,
                "status": status,
                "file": rel,
                "aliases": [],
                "roles": [],
                "obs_count": act,
                "degree": 0,
                "source": "dossier",
            }
            file_to_node_id[rel] = node_id
            file_to_node_id[rel_no_ext] = node_id
            file_to_node_id[path.stem] = node_id
        else:
            if not nodes_by_id[node_id].get("file"):
                nodes_by_id[node_id]["file"] = rel
            file_to_node_id[path.stem] = node_id

        links = WIKILINK_RE.findall(text)
        file_links.append((node_id, links))

    # 3. Build wikilink edges between nodes
    for src_id, links in file_links:
        for raw_target in links:
            target = raw_target.strip().lstrip("/")
            if target.endswith(".md"):
                target = target[:-3]
            if target.startswith("sessions/"):
                continue
            tgt_id = (
                file_to_node_id.get(target)
                or file_to_node_id.get(f"{target}.md")
                or file_to_node_id.get(Path(target).name)
                or (target if target in nodes_by_id else None)
            )
            if tgt_id and tgt_id != src_id and tgt_id in nodes_by_id:
                ekey = (src_id, tgt_id, "WIKILINK")
                rev_key = (tgt_id, src_id, "WIKILINK")
                if ekey not in seen_edges and rev_key not in seen_edges:
                    # Check if semantic edge already connects them
                    has_sem = any(
                        (e["source"] == src_id and e["target"] == tgt_id)
                        or (e["source"] == tgt_id and e["target"] == src_id)
                        for e in edges
                    )
                    if not has_sem:
                        seen_edges.add(ekey)
                        edges.append({
                            "source": src_id,
                            "target": tgt_id,
                            "relation": "LINKS_TO",
                            "notes": "Markdown [[wikilink]]",
                            "kind": "wikilink",
                        })

    # Compute node degrees
    for e in edges:
        if e["source"] in nodes_by_id:
            nodes_by_id[e["source"]]["degree"] += 1
        if e["target"] in nodes_by_id:
            nodes_by_id[e["target"]]["degree"] += 1

    nodes_list = list(nodes_by_id.values())
    nodes_list.sort(key=lambda n: (n["degree"] + min(n["obs_count"], 50)), reverse=True)

    type_counts: Dict[str, int] = {}
    for n in nodes_list:
        t = n["type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    return {
        "node_count": len(nodes_list),
        "edge_count": len(edges),
        "type_counts": type_counts,
        "nodes": nodes_list,
        "edges": edges,
    }


def build_feed_payload(limit: int = 60, project: str = "", obs_type: str = "") -> Dict[str, Any]:
    conn = state.connect()
    try:
        where, params = [], []
        if project:
            where.append("(project = ? OR project LIKE ?)")
            params += [project, f"{project}/%"]
        if obs_type and obs_type != "session":
            where.append("type = ?")
            params.append(obs_type)
        w_sql = (" WHERE " + " AND ".join(where)) if where else ""

        items: List[Dict[str, Any]] = []
        if obs_type != "session":
            rows = conn.execute(
                f"SELECT * FROM observations{w_sql} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            for r in rows:
                items.append({
                    "kind": "observation",
                    "id": int(r["id"]),
                    "project": r["project"],
                    "type": r["type"],
                    "title": r["title"],
                    "subtitle": r["subtitle"] or "",
                    "facts": state.loads(r["facts"]),
                    "narrative": r["narrative"] or "",
                    "concepts": state.loads(r["concepts"]),
                    "files_read": state.loads(r["files_read"]),
                    "files_modified": state.loads(r["files_modified"]),
                    "occurrence_count": int(r["occurrence_count"] or 1),
                    "tokens": int(r["discovery_tokens"] or 0),
                    "model": r["generated_by_model"] or "",
                    "date": _when(r["created_at"]),
                    "at": int(r["created_at"]),
                    "session": r["content_session_id"],
                })

        if not obs_type or obs_type == "session":
            swhere, sparams = [], []
            if project:
                swhere.append("(project = ? OR project LIKE ?)")
                sparams += [project, f"{project}/%"]
            sw_sql = (" WHERE " + " AND ".join(swhere)) if swhere else ""
            srows = conn.execute(
                f"SELECT * FROM session_summaries{sw_sql} ORDER BY created_at DESC LIMIT ?",
                (*sparams, max(15, limit // 2)),
            ).fetchall()
            for r in srows:
                items.append({
                    "kind": "session",
                    "id": f"S{r['id']}",
                    "project": r["project"],
                    "type": "session",
                    "title": r["request"] or "Coding Session Summary",
                    "subtitle": (r["completed"] or "")[:240],
                    "facts": [x for x in (r["investigated"], r["learned"], r["completed"], r["next_steps"]) if x],
                    "narrative": r["notes"] or "",
                    "concepts": ["session-summary"],
                    "files_read": state.loads(r["files_read"]),
                    "files_modified": state.loads(r["files_edited"]),
                    "occurrence_count": 1,
                    "tokens": int(r["discovery_tokens"] or 0),
                    "model": r["generated_by_model"] or "",
                    "date": _when(r["created_at"]),
                    "at": int(r["created_at"]),
                    "session": r["content_session_id"],
                })

        items.sort(key=lambda x: x["at"], reverse=True)
        return {"count": len(items[:limit]), "items": items[:limit]}
    finally:
        conn.close()


def build_search_payload(query: str, project: str = "", obs_type: str = "", limit: int = 25) -> Dict[str, Any]:
    query = (query or "").strip()
    if not query:
        return {"query": "", "search_mode": "hybrid-rrf", "vault_results": [], "session_results": [], "ranked_fusion": []}

    strategy, vault_results = core.search(query, limit=min(limit, 20))
    conn = state.connect()
    try:
        session_hits = capture_search.search(
            conn, query, project=project or None, obs_type=obs_type or None, limit=limit
        )
        expanded_terms, matched_slugs = capture_search.expand_query_with_graph(query)
    finally:
        conn.close()

    ranked_fusion: List[Dict[str, Any]] = []
    for idx, r in enumerate(vault_results, 1):
        rrf = 1.0 / (60 + idx)
        if any(s in r["path"].lower() or s in r["title"].lower() for s in matched_slugs):
            rrf += 1.0 / (60 + 1)
        ranked_fusion.append({
            "source": "vault",
            "id": r["path"],
            "title": r["title"],
            "subtitle": r["path"],
            "snippet": r["snippet"],
            "path": r["path"],
            "wikilink": r["wikilink"],
            "rrf_score": round(rrf, 6),
        })
    for idx, s in enumerate(session_hits, 1):
        base_rrf = abs(float(s.get("score") or 0.0)) or (1.0 / (60 + idx))
        ranked_fusion.append({
            "source": "session",
            "id": str(s["id"]),
            "kind": s["kind"],
            "type": s["type"],
            "project": s["project"],
            "title": s["title"],
            "subtitle": s.get("subtitle") or "",
            "date": s["date"],
            "rrf_score": round(base_rrf, 6),
        })
    ranked_fusion.sort(key=lambda x: x["rrf_score"], reverse=True)

    return {
        "query": query,
        "search_mode": "hybrid-rrf",
        "vault_strategy": strategy or "none",
        "expanded_terms": expanded_terms,
        "matched_entities": sorted(matched_slugs),
        "vault_results": vault_results,
        "session_results": session_hits,
        "ranked_fusion": ranked_fusion[:limit],
    }


def build_dossier_payload(rel_path: str) -> Dict[str, Any]:
    rel, content = core.read_dossier(rel_path)
    links = sorted(set(WIKILINK_RE.findall(content)))
    stem = Path(rel).stem

    # Recent observations for this dossier/project
    conn = state.connect()
    try:
        rows = conn.execute(
            """SELECT id, type, title, subtitle, created_at FROM observations
               WHERE lower(project) = ? OR lower(project) LIKE ?
               ORDER BY created_at DESC LIMIT 15""",
            (stem.lower(), f"{stem.lower()}/%"),
        ).fetchall()
        related_obs = [
            {"id": int(r["id"]), "type": r["type"], "title": r["title"],
             "subtitle": r["subtitle"] or "", "date": _when(r["created_at"])}
            for r in rows
        ]
    except Exception:
        related_obs = []
    finally:
        conn.close()

    return {
        "path": rel,
        "stem": stem,
        "content": content,
        "wikilinks": links,
        "related_observations": related_obs,
    }


# ----------------------------------------------------------------------
# Embedded Single-Page Web Application (HTML5 Canvas Graph + Glass UI)
# ----------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl" class="dark">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Memo Studio — Sovereign Memory & Knowledge Graph</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@300;400;500;600;700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg-deep: #050811;
  --bg-panel: rgba(13, 19, 36, 0.78);
  --bg-card: rgba(19, 28, 52, 0.65);
  --bg-card-hover: rgba(27, 39, 71, 0.85);
  --border-glass: rgba(255, 255, 255, 0.08);
  --border-glow: rgba(16, 185, 129, 0.38);
  --text-main: #F8FAFC;
  --text-muted: #94A3B8;
  --text-dim: #64748B;
  --emerald: #10B981;
  --violet: #8B5CF6;
  --cyan: #06B6D4;
  --amber: #F59E0B;
  --rose: #F43F5E;
  --blue: #3B82F6;
  --font-sans: 'IBM Plex Sans Arabic', 'Inter', -apple-system, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: radial-gradient(circle at 15% 15%, rgba(16, 185, 129, 0.08), transparent 38%),
              radial-gradient(circle at 85% 20%, rgba(139, 92, 246, 0.09), transparent 40%),
              var(--bg-deep);
  color: var(--text-main);
  font-family: var(--font-sans);
  min-height: 100vh;
  overflow-x: hidden;
}
/* Top Navigation Bar */
.topbar {
  position: sticky; top: 0; z-index: 50;
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 24px;
  background: rgba(5, 8, 17, 0.84);
  backdrop-filter: blur(18px);
  border-bottom: 1px solid var(--border-glass);
}
.brand {
  display: flex; align-items: center; gap: 12px;
}
.brand-orb {
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, #10B981, #059669 50%, #8B5CF6);
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 18px; color: #fff;
  box-shadow: 0 0 20px rgba(16, 185, 129, 0.4);
}
.brand-title { font-weight: 700; font-size: 17px; letter-spacing: -0.01em; }
.brand-sub { font-size: 11px; color: var(--emerald); font-family: var(--font-mono); }

.nav-tabs {
  display: flex; gap: 6px;
  background: rgba(15, 23, 42, 0.75);
  padding: 4px; border-radius: 12px;
  border: 1px solid var(--border-glass);
}
.nav-tab {
  border: none; background: transparent; color: var(--text-muted);
  padding: 8px 16px; border-radius: 9px; cursor: pointer;
  font-family: var(--font-sans); font-size: 13.5px; font-weight: 500;
  display: flex; align-items: center; gap: 8px;
  transition: all 0.2s ease;
}
.nav-tab:hover { color: var(--text-main); background: rgba(255,255,255,0.04); }
.nav-tab.active {
  background: linear-gradient(135deg, rgba(16, 185, 129, 0.22), rgba(139, 92, 246, 0.18));
  color: #fff; border: 1px solid rgba(16, 185, 129, 0.35);
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.25);
}

.top-actions { display: flex; align-items: center; gap: 10px; }
.pill-badge {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 5px 11px; border-radius: 999px; font-size: 12px;
  background: rgba(16, 185, 129, 0.12); color: #34D399;
  border: 1px solid rgba(16, 185, 129, 0.25);
  font-family: var(--font-mono);
}
.pill-badge.warn {
  background: rgba(245, 158, 11, 0.12); color: #FBBF24;
  border-color: rgba(245, 158, 11, 0.3);
}
.dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; box-shadow: 0 0 8px currentColor; }
.btn {
  border: 1px solid var(--border-glass);
  background: rgba(30, 41, 59, 0.7);
  color: var(--text-main);
  padding: 7px 13px; border-radius: 9px;
  font-family: var(--font-sans); font-size: 12.5px; font-weight: 500;
  cursor: pointer; transition: all 0.18s ease;
  display: inline-flex; align-items: center; gap: 6px;
}
.btn:hover { background: rgba(51, 65, 85, 0.85); border-color: rgba(255,255,255,0.18); }
.btn-emerald {
  background: linear-gradient(135deg, #10B981, #059669);
  border-color: #34D399; color: #fff; font-weight: 600;
}
.btn-emerald:hover { filter: brightness(1.1); }
.btn-danger {
  background: rgba(244, 63, 94, 0.14); border-color: rgba(244, 63, 94, 0.35); color: #FDA4AF;
}
.btn-danger:hover { background: rgba(244, 63, 94, 0.28); }

/* Views */
.view-section { display: none; }
.view-section.active { display: block; }

/* Graph Studio View */
.graph-workspace {
  position: relative;
  height: calc(100vh - 63px);
  width: 100vw;
  overflow: hidden;
  background: radial-gradient(circle at 50% 50%, #0B132B 0%, #050811 85%);
}
#graphCanvas {
  width: 100%; height: 100%; display: block; cursor: grab;
}
#graphCanvas:active { cursor: grabbing; }

.graph-hud-top {
  position: absolute; top: 16px; left: 16px; right: 16px;
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
  gap: 12px; pointer-events: none;
}
.graph-hud-box {
  pointer-events: auto;
  background: rgba(9, 14, 28, 0.82);
  backdrop-filter: blur(16px);
  border: 1px solid var(--border-glass);
  border-radius: 14px;
  padding: 10px 14px;
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
}
.graph-search-input {
  background: rgba(15, 23, 42, 0.9);
  border: 1px solid var(--border-glass);
  color: var(--text-main);
  padding: 7px 12px; border-radius: 8px;
  font-family: var(--font-sans); font-size: 13px; width: 220px;
  outline: none;
}
.graph-search-input:focus { border-color: var(--emerald); }

.filter-chip {
  border: 1px solid rgba(255,255,255,0.08);
  background: rgba(30, 41, 59, 0.55);
  color: var(--text-muted);
  padding: 5px 11px; border-radius: 999px;
  font-size: 12px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: all 0.15s ease;
}
.filter-chip:hover { color: #fff; border-color: rgba(255,255,255,0.2); }
.filter-chip.active {
  background: rgba(16, 185, 129, 0.18);
  color: #fff; border-color: var(--emerald);
}

.graph-hud-bottom {
  position: absolute; bottom: 18px; left: 18px; right: 18px;
  display: flex; align-items: flex-end; justify-content: space-between;
  pointer-events: none;
}
.graph-legend {
  display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--text-muted);
}
.legend-item { display: flex; align-items: center; gap: 6px; }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; }

/* Container for non-fullscreen views */
.container { max-width: 1400px; margin: 0 auto; padding: 24px; }

/* KPI Grid */
.kpi-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 16px; margin-bottom: 24px;
}
.kpi-card {
  background: var(--bg-card);
  backdrop-filter: blur(14px);
  border: 1px solid var(--border-glass);
  border-radius: 16px;
  padding: 18px 20px;
  transition: transform 0.2s ease, border-color 0.2s ease;
}
.kpi-card:hover { transform: translateY(-2px); border-color: rgba(16, 185, 129, 0.3); }
.kpi-label { font-size: 12.5px; color: var(--text-muted); margin-bottom: 8px; display: flex; justify-content: space-between; }
.kpi-val { font-size: 28px; font-weight: 700; font-family: var(--font-mono); letter-spacing: -0.02em; }
.kpi-sub { font-size: 12px; color: var(--emerald); margin-top: 6px; }

/* Feed & Search Layout */
.toolbar-row {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between;
  background: var(--bg-panel);
  border: 1px solid var(--border-glass);
  padding: 14px 18px; border-radius: 14px; margin-bottom: 18px;
}
.input-box, .select-box {
  background: rgba(15, 23, 42, 0.9);
  border: 1px solid var(--border-glass);
  color: var(--text-main);
  padding: 9px 14px; border-radius: 10px;
  font-family: var(--font-sans); font-size: 13.5px;
  outline: none;
}
.input-box:focus, .select-box:focus { border-color: var(--emerald); }

.dedup-banner {
  background: linear-gradient(135deg, rgba(245, 158, 11, 0.14), rgba(139, 92, 246, 0.12));
  border: 1px solid rgba(245, 158, 11, 0.35);
  border-radius: 14px; padding: 14px 20px; margin-bottom: 18px;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}

.cards-list { display: flex; flex-direction: column; gap: 12px; }
.obs-card {
  background: var(--bg-card);
  border: 1px solid var(--border-glass);
  border-radius: 14px; padding: 16px 20px;
  transition: all 0.18s ease;
}
.obs-card:hover { background: var(--bg-card-hover); border-color: rgba(255,255,255,0.15); }
.obs-header {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 8px;
}
.obs-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }
.badge {
  padding: 3px 9px; border-radius: 6px; font-size: 11.5px; font-weight: 600;
  font-family: var(--font-mono);
}
.badge-project { background: rgba(16, 185, 129, 0.15); color: #34D399; border: 1px solid rgba(16, 185, 129, 0.3); }
.badge-type { background: rgba(139, 92, 246, 0.16); color: #C4B5FD; border: 1px solid rgba(139, 92, 246, 0.3); }
.badge-rrf { background: rgba(6, 182, 212, 0.16); color: #67E8F9; border: 1px solid rgba(6, 182, 212, 0.3); }
.obs-title { font-size: 15.5px; font-weight: 600; color: #fff; line-height: 1.45; }
.obs-sub { font-size: 13.5px; color: var(--text-muted); margin-top: 4px; line-height: 1.55; }
.obs-facts {
  margin-top: 10px; padding-right: 18px; padding-left: 18px;
  color: #CBD5E1; font-size: 13px; line-height: 1.65;
}
.obs-files {
  margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px;
}
.file-chip {
  font-family: var(--font-mono); font-size: 11px;
  padding: 2px 8px; border-radius: 5px;
  background: rgba(15, 23, 42, 0.8); color: #93C5FD;
  border: 1px solid rgba(59, 130, 246, 0.25);
}

/* Slide-Over Inspector Drawer */
.drawer-backdrop {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(4px); z-index: 90;
  display: none;
}
.drawer-backdrop.open { display: block; }
.drawer {
  position: fixed; top: 0; bottom: 0; left: 0;
  width: min(620px, 92vw);
  background: rgba(10, 15, 30, 0.96);
  backdrop-filter: blur(24px);
  border-right: 1px solid var(--border-glass);
  z-index: 100;
  transform: translateX(-105%);
  transition: transform 0.28s cubic-bezier(0.16, 1, 0.3, 1);
  display: flex; flex-direction: column;
  box-shadow: 20px 0 50px rgba(0, 0, 0, 0.7);
}
.drawer.open { transform: translateX(0); }
.drawer-header {
  padding: 18px 22px; border-bottom: 1px solid var(--border-glass);
  display: flex; align-items: center; justify-content: space-between;
}
.drawer-body {
  padding: 22px; overflow-y: auto; flex: 1;
  line-height: 1.7; font-size: 14px;
}
.md-pre {
  background: rgba(5, 8, 17, 0.85);
  border: 1px solid var(--border-glass);
  border-radius: 12px; padding: 16px;
  white-space: pre-wrap; word-break: break-word;
  font-family: var(--font-sans); font-size: 13.5px; color: #E2E8F0;
  line-height: 1.75;
}
.section-title {
  font-size: 14px; font-weight: 600; color: var(--emerald);
  margin: 18px 0 10px; text-transform: uppercase; letter-spacing: 0.04em;
}
.grid-2 {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 18px;
}
.agent-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 11px 14px; border-radius: 10px;
  background: rgba(15, 23, 42, 0.65); border: 1px solid var(--border-glass);
  margin-bottom: 8px;
}
</style>
</head>
<body>

<!-- Top Navigation -->
<header class="topbar">
  <div class="brand">
    <div class="brand-orb">M</div>
    <div>
      <div class="brand-title">Memo Sovereign Studio</div>
      <div class="brand-sub" id="topVaultPath">Hybrid RRF + Live Knowledge Graph</div>
    </div>
  </div>

  <nav class="nav-tabs">
    <button class="nav-tab active" onclick="switchView('graph')" id="tab-graph">
      <span>🕸️</span> <span data-i18n="nav_graph">الشبكة المعرفية (Graph View)</span>
    </button>
    <button class="nav-tab" onclick="switchView('feed')" id="tab-feed">
      <span>⚡</span> <span data-i18n="nav_feed">سجل الذاكرة الحي</span>
    </button>
    <button class="nav-tab" onclick="switchView('search')" id="tab-search">
      <span>🔍</span> <span data-i18n="nav_search">البحث الهجين (Hybrid RRF)</span>
    </button>
    <button class="nav-tab" onclick="switchView('cockpit')" id="tab-cockpit">
      <span>🛡️</span> <span data-i18n="nav_cockpit">لوحة القيادة والأنظمة</span>
    </button>
  </nav>

  <div class="top-actions">
    <div class="pill-badge" id="workerBadge">
      <span class="dot"></span> <span id="workerBadgeText">Worker</span>
    </div>
    <button class="btn" onclick="toggleLang()" id="langBtn">EN / عربي</button>
    <button class="btn" onclick="refreshAll()" title="Refresh">🔄</button>
  </div>
</header>

<!-- VIEW 1: CREATIVE KNOWLEDGE GRAPH STUDIO -->
<section class="view-section active" id="view-graph">
  <div class="graph-workspace">
    <canvas id="graphCanvas"></canvas>

    <!-- Top Floating HUD -->
    <div class="graph-hud-top">
      <div class="graph-hud-box">
        <input type="text" id="graphSearch" class="graph-search-input"
               placeholder="ابحث عن مشروع، عميل، شخصية... (Search node)"
               oninput="onGraphSearch(this.value)" />
        <div id="graphTypeFilters" style="display:flex; flex-wrap:wrap; gap:6px;"></div>
      </div>

      <div class="graph-hud-box">
        <button class="btn" onclick="setGraphLayout('physics')" id="layout-physics">🌌 فيزيائي حر</button>
        <button class="btn" onclick="setGraphLayout('radial')" id="layout-radial">🪐 مداري ذكي</button>
        <button class="btn" onclick="setGraphLayout('activity')" id="layout-activity">🔥 حسب النشاط</button>
        <button class="btn" onclick="toggleArabicLabels()" id="btnArabicLabels">ع/A الأسماء</button>
        <button class="btn" onclick="resetCamera()">🎯 توسيط</button>
      </div>
    </div>

    <!-- Bottom Floating HUD -->
    <div class="graph-hud-bottom">
      <div class="graph-hud-box graph-legend" id="graphLegend"></div>
      <div class="graph-hud-box" style="font-family:var(--font-mono); font-size:12px; color:var(--text-muted);">
        <span id="graphStatsText">Loading graph...</span>
      </div>
    </div>
  </div>
</section>

<!-- VIEW 2: LIVE MEMORY FEED & OBSERVATION CLEANER -->
<section class="view-section" id="view-feed">
  <div class="container">
    <div id="dedupBanner" class="dedup-banner" style="display:none;">
      <div>
        <strong style="color:#FBBF24; font-size:14.5px;">✨ منظف الملاحظات المتكررة (Smart Observation Cleaner)</strong>
        <div id="dedupBannerText" style="font-size:13px; color:#E2E8F0; margin-top:3px;"></div>
      </div>
      <button class="btn btn-emerald" onclick="runDeduplicate()">دمج وتنظيف المكرر بضغطة زر</button>
    </div>

    <div class="toolbar-row">
      <div style="display:flex; flex-wrap:wrap; gap:10px; align-items:center;">
        <select id="feedProjectSelect" class="select-box" onchange="loadFeed()">
          <option value="">كل المشاريع (All Projects)</option>
        </select>
        <select id="feedTypeSelect" class="select-box" onchange="loadFeed()">
          <option value="">كل الأنواع (All Types)</option>
          <option value="session">🎯 ملخصات الجلسات (Sessions)</option>
          <option value="decision">⚖ القرارات (Decisions)</option>
          <option value="feature">◆ المميزات (Features)</option>
          <option value="bugfix">● الإصلاحات (Bugfixes)</option>
          <option value="discovery">◇ الاكتشافات (Discoveries)</option>
          <option value="refactor">↻ إعادة الهيكلة (Refactors)</option>
          <option value="change">✎ التعديلات (Changes)</option>
        </select>
        <input type="text" id="feedQuickFilter" class="input-box" placeholder="فلترة سريعة في القائمة..." oninput="renderFeedList()" style="width:240px;" />
      </div>
      <div style="font-size:13px; color:var(--text-muted);" id="feedCountLabel"></div>
    </div>

    <div class="cards-list" id="feedList"></div>
  </div>
</section>

<!-- VIEW 3: UNIFIED HYBRID RRF SEARCH STUDIO -->
<section class="view-section" id="view-search">
  <div class="container">
    <div class="toolbar-row" style="padding:20px;">
      <div style="display:flex; width:100%; gap:12px; align-items:center;">
        <input type="text" id="hybridSearchInput" class="input-box"
               style="flex:1; font-size:16px; padding:13px 18px;"
               placeholder="ابحث بالعربي أو الإنجليزي في الملفات + الجلسات + الكيانات (مثال: Acme Corp، Supabase، شركة تجريبية)..."
               onkeydown="if(event.key==='Enter') runHybridSearch()" />
        <button class="btn btn-emerald" style="padding:13px 24px; font-size:14.5px;" onclick="runHybridSearch()">
          بحث هجين (Hybrid RRF)
        </button>
      </div>
      <div id="searchExpansionBar" style="margin-top:10px; font-size:12.5px; color:var(--text-muted); width:100%;"></div>
    </div>

    <div class="cards-list" id="searchResultsList"></div>
  </div>
</section>

<!-- VIEW 4: SYSTEM COCKPIT & AGENT HEALTH -->
<section class="view-section" id="view-cockpit">
  <div class="container">
    <div class="kpi-grid" id="cockpitKpis"></div>

    <div class="grid-2">
      <div class="kpi-card">
        <div class="section-title" style="margin-top:0;">🤖 حالة ربط الأيجنتات (Connected AI Agents)</div>
        <div id="agentsStatusList"></div>
      </div>

      <div class="kpi-card">
        <div class="section-title" style="margin-top:0;">📁 أنشط المشاريع في الذاكرة (Top Active Projects)</div>
        <div id="topProjectsList" style="max-height:340px; overflow-y:auto;"></div>
      </div>
    </div>
  </div>
</section>

<!-- SLIDE-OVER DOSSIER & NODE INSPECTOR DRAWER -->
<div class="drawer-backdrop" id="drawerBackdrop" onclick="closeDrawer()"></div>
<aside class="drawer" id="inspectorDrawer">
  <div class="drawer-header">
    <div>
      <div id="drawerTypeBadge" class="badge badge-project" style="margin-bottom:4px; display:inline-block;">ENTITY</div>
      <h2 id="drawerTitle" style="font-size:18px; font-weight:700;">Details</h2>
    </div>
    <button class="btn" onclick="closeDrawer()">✕ إغلاق</button>
  </div>
  <div class="drawer-body" id="drawerBody"></div>
</aside>

<script>
// ----------------------------------------------------------------------
// State & Colors
// ----------------------------------------------------------------------
let STATUS_DATA = null;
let GRAPH_DATA = null;
let FEED_ITEMS = [];
let CURRENT_LANG = 'ar';
let SHOW_ARABIC_LABELS = true;

const TYPE_COLORS = {
  Project: '#10B981',      // Emerald
  Client: '#F59E0B',       // Amber Gold
  Person: '#06B6D4',       // Cyan
  Organization: '#8B5CF6', // Violet
  Technology: '#3B82F6',   // Blue
  Stack: '#3B82F6',
  Hackathon: '#EC4899',    // Pink
  Core: '#F43F5E',         // Rose
  Discipline: '#14B8A6',   // Teal
  Profile: '#A855F7',
  Entity: '#94A3B8'
};

const TYPE_ICONS = {
  session: '🎯', bugfix: '●', feature: '◆', refactor: '↻',
  change: '✎', discovery: '◇', decision: '⚖', security_alert: '⚠',
  security_note: '🛡', sensitive: '◐'
};

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function switchView(name) {
  document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'graph') resizeCanvas();
  if (name === 'feed' && !FEED_ITEMS.length) loadFeed();
}

function toggleLang() {
  CURRENT_LANG = CURRENT_LANG === 'ar' ? 'en' : 'ar';
  document.documentElement.dir = CURRENT_LANG === 'ar' ? 'rtl' : 'ltr';
  document.documentElement.lang = CURRENT_LANG;
}

// ----------------------------------------------------------------------
// Data Loaders
// ----------------------------------------------------------------------
async function loadStatus() {
  const res = await fetch('/api/status');
  STATUS_DATA = await res.json();
  renderTopBar();
  renderCockpit();
}

function renderTopBar() {
  if (!STATUS_DATA) return;
  const w = STATUS_DATA.worker;
  const c = STATUS_DATA.capture;
  const badge = document.getElementById('workerBadge');
  const txt = document.getElementById('workerBadgeText');
  if (w.running) {
    badge.className = 'pill-badge';
    txt.textContent = `Worker: ON (${c.total_observations.toLocaleString()} obs)`;
  } else {
    badge.className = 'pill-badge warn';
    txt.textContent = `Worker: Idle (${c.total_observations.toLocaleString()} obs)`;
  }

  // Populate project select dropdown
  const sel = document.getElementById('feedProjectSelect');
  const currentVal = sel.value;
  sel.innerHTML = '<option value="">كل المشاريع (All Projects)</option>';
  (c.projects || []).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.project;
    opt.textContent = `${p.project} (${p.count})`;
    sel.appendChild(opt);
  });
  sel.value = currentVal;

  // Duplicate cleaner banner
  const banner = document.getElementById('dedupBanner');
  if (c.duplicate_observations > 0) {
    banner.style.display = 'flex';
    document.getElementById('dedupBannerText').textContent =
      `تم اكتشاف ${c.duplicate_observations} ملاحظة مكررة موزعة على ${c.duplicate_groups} مجموعة في قاعدة البيانات.`;
  } else {
    banner.style.display = 'none';
  }
}

function renderCockpit() {
  if (!STATUS_DATA) return;
  const v = STATUS_DATA.vault;
  const c = STATUS_DATA.capture;
  const w = STATUS_DATA.worker;
  const s = STATUS_DATA.sync;

  const kpis = [
    { label: 'ملفات الـ Vault (Markdown)', val: v.markdown_files.toLocaleString(), sub: `حجم الفهرس: ${v.index.db_kb} KB (Trigram: ${v.index.trigram ? 'ON' : 'OFF'})` },
    { label: 'الملاحظات والجلسات الملتقطة', val: c.total_observations.toLocaleString(), sub: `${c.total_sessions.toLocaleString()} جلسة · اليوم: +${c.today_observations} ملاحظة` },
    { label: 'الشبكة المعرفية (Knowledge Graph)', val: `${v.graph.entities} كيان`, sub: `${v.graph.relationships} علاقة دلالية · ${v.timeline_events} حدث تاريخي` },
    { label: 'التوفير في التوكنات (Tokens)', val: `${(c.total_tokens / 1000000).toFixed(1)}M`, sub: `الموديل المراقب: ${w.observer_model}` },
    { label: 'حالة المزامنة (GitHub Sync)', val: s.status.toUpperCase(), sub: `آخر مزامنة ناجحة: ${s.last_ok}` }
  ];

  document.getElementById('cockpitKpis').innerHTML = kpis.map(k => `
    <div class="kpi-card">
      <div class="kpi-label"><span>${escapeHtml(k.label)}</span></div>
      <div class="kpi-val">${escapeHtml(k.val)}</div>
      <div class="kpi-sub">${escapeHtml(k.sub)}</div>
    </div>
  `).join('');

  document.getElementById('agentsStatusList').innerHTML = (STATUS_DATA.agents || []).map(a => `
    <div class="agent-row">
      <div>
        <div style="font-weight:600; font-size:14px;">${escapeHtml(a.name)}</div>
        <div style="font-size:11.5px; color:var(--text-muted); font-family:var(--font-mono);">${escapeHtml(a.path)}</div>
      </div>
      <span class="pill-badge ${a.connected ? '' : 'warn'}">
        <span class="dot"></span> ${a.connected ? 'متصل (Connected)' : 'غير مفعل'}
      </span>
    </div>
  `).join('');

  document.getElementById('topProjectsList').innerHTML = (c.projects || []).map(p => `
    <div class="agent-row" style="cursor:pointer;" onclick="openProjectFromCockpit('${escapeHtml(p.project)}')">
      <div>
        <div style="font-weight:600; font-size:13.5px; color:#34D399;">${escapeHtml(p.project)}</div>
        <div style="font-size:11.5px; color:var(--text-muted);">آخر التقاط: ${escapeHtml(p.last)}</div>
      </div>
      <span class="badge badge-project">${p.count} ملاحظة</span>
    </div>
  `).join('');
}

function openProjectFromCockpit(proj) {
  switchView('feed');
  document.getElementById('feedProjectSelect').value = proj;
  loadFeed();
}

// ----------------------------------------------------------------------
// Interactive 60fps HTML5 Canvas Knowledge Graph Engine
// ----------------------------------------------------------------------
const canvas = document.getElementById('graphCanvas');
const ctx = canvas.getContext('2d');
let G_NODES = [];
let G_EDGES = [];
let G_FILTER_TYPE = 'ALL';
let G_SEARCH_TEXT = '';
let G_LAYOUT = 'physics'; // 'physics' | 'radial' | 'activity'

let camX = 0, camY = 0, camZoom = 1.0;
let isDraggingCanvas = false;
let draggedNode = null;
let hoveredNode = null;
let selectedNode = null;
let dragStartX = 0, dragStartY = 0;
let pointerDownX = 0, pointerDownY = 0;
let neighborSet = new Set();

function resizeCanvas() {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener('resize', resizeCanvas);

async function loadGraph() {
  const res = await fetch('/api/graph');
  GRAPH_DATA = await res.json();
  initGraphSimulation();
}

function initGraphSimulation() {
  if (!GRAPH_DATA) return;
  resizeCanvas();
  const w = canvas.clientWidth || 1200;
  const h = canvas.clientHeight || 800;
  camX = w / 2;
  camY = h / 2;
  camZoom = 0.88;

  const total = GRAPH_DATA.nodes.length;
  G_NODES = GRAPH_DATA.nodes.map((n, i) => {
    const angle = (i / Math.max(1, total)) * Math.PI * 2 * 3;
    const radius = 60 + Math.sqrt(i) * 34;
    const baseR = 6 + Math.min(16, Math.sqrt(n.degree * 2.5 + Math.min(n.obs_count, 80) * 0.4));
    return {
      ...n,
      x: Math.cos(angle) * radius,
      y: Math.sin(angle) * radius,
      vx: 0, vy: 0,
      r: baseR,
      color: TYPE_COLORS[n.type] || '#10B981',
      visible: true
    };
  });

  const nodeMap = new Map(G_NODES.map(n => [n.id, n]));
  G_EDGES = [];
  (GRAPH_DATA.edges || []).forEach(e => {
    const s = nodeMap.get(e.source);
    const t = nodeMap.get(e.target);
    if (s && t) {
      G_EDGES.push({ ...e, sNode: s, tNode: t });
    }
  });

  renderGraphHUD();
}

function renderGraphHUD() {
  if (!GRAPH_DATA) return;
  const tc = GRAPH_DATA.type_counts || {};
  const types = ['ALL', ...Object.keys(tc)];
  document.getElementById('graphTypeFilters').innerHTML = types.map(t => {
    const count = t === 'ALL' ? GRAPH_DATA.node_count : tc[t];
    const active = G_FILTER_TYPE === t ? 'active' : '';
    const dot = t !== 'ALL' ? `<span class="legend-dot" style="background:${TYPE_COLORS[t] || '#10B981'}"></span>` : '';
    return `<button class="filter-chip ${active}" onclick="setGraphTypeFilter('${t}')">${dot}${t} (${count})</button>`;
  }).join('');

  document.getElementById('graphLegend').innerHTML = Object.keys(tc).map(t => `
    <div class="legend-item">
      <span class="legend-dot" style="background:${TYPE_COLORS[t] || '#10B981'}"></span>
      <span>${t}</span>
    </div>
  `).join('');

  document.getElementById('graphStatsText').textContent =
    `${GRAPH_DATA.node_count} Nodes · ${GRAPH_DATA.edge_count} Connections`;
}

function setGraphTypeFilter(type) {
  G_FILTER_TYPE = type;
  G_NODES.forEach(n => {
    const matchType = (type === 'ALL' || n.type === type);
    const matchSearch = !G_SEARCH_TEXT || nodeMatchesQuery(n, G_SEARCH_TEXT);
    n.visible = matchType && matchSearch;
  });
  renderGraphHUD();
}

function nodeMatchesQuery(n, q) {
  const ql = q.toLowerCase();
  return (n.label && n.label.toLowerCase().includes(ql)) ||
         (n.label_ar && n.label_ar.toLowerCase().includes(ql)) ||
         (n.id && n.id.toLowerCase().includes(ql)) ||
         (n.aliases && n.aliases.some(a => a.toLowerCase().includes(ql)));
}

function onGraphSearch(val) {
  G_SEARCH_TEXT = (val || '').trim();
  G_NODES.forEach(n => {
    const matchType = (G_FILTER_TYPE === 'ALL' || n.type === G_FILTER_TYPE);
    const matchSearch = !G_SEARCH_TEXT || nodeMatchesQuery(n, G_SEARCH_TEXT);
    n.visible = matchType && matchSearch;
  });
  if (G_SEARCH_TEXT.length >= 2) {
    const first = G_NODES.find(n => n.visible);
    if (first) {
      selectedNode = first;
      updateNeighborSet(first);
    }
  } else {
    selectedNode = null;
    updateNeighborSet(hoveredNode);
  }
}

function setGraphLayout(mode) {
  G_LAYOUT = mode;
}

function toggleArabicLabels() {
  SHOW_ARABIC_LABELS = !SHOW_ARABIC_LABELS;
}

function resetCamera() {
  camX = (canvas.clientWidth || 1200) / 2;
  camY = (canvas.clientHeight || 800) / 2;
  camZoom = 0.88;
  selectedNode = null;
  updateNeighborSet(null);
}

function updateNeighborSet(focusNode) {
  neighborSet.clear();
  if (!focusNode) return;
  neighborSet.add(focusNode.id);
  G_EDGES.forEach(e => {
    if (e.sNode.id === focusNode.id) neighborSet.add(e.tNode.id);
    if (e.tNode.id === focusNode.id) neighborSet.add(e.sNode.id);
  });
}

function stepPhysics() {
  const visNodes = G_NODES.filter(n => n.visible);
  const N = visNodes.length;
  if (N === 0) return;

  if (G_LAYOUT === 'radial') {
    const types = Array.from(new Set(visNodes.map(n => n.type)));
    visNodes.forEach((n, idx) => {
      if (n === draggedNode) return;
      const tIdx = types.indexOf(n.type);
      const ringRadius = 110 + tIdx * 95;
      const angle = (idx / N) * Math.PI * 2 + tIdx * 0.5;
      const tx = Math.cos(angle) * ringRadius;
      const ty = Math.sin(angle) * ringRadius;
      n.x += (tx - n.x) * 0.08;
      n.y += (ty - n.y) * 0.08;
    });
    return;
  }

  if (G_LAYOUT === 'activity') {
    visNodes.forEach((n, idx) => {
      if (n === draggedNode) return;
      const actScore = n.obs_count + n.degree * 2;
      const ringRadius = actScore > 30 ? 80 : actScore > 10 ? 220 : actScore > 2 ? 360 : 490;
      const angle = (idx / N) * Math.PI * 2 * 2;
      const tx = Math.cos(angle) * ringRadius;
      const ty = Math.sin(angle) * ringRadius;
      n.x += (tx - n.x) * 0.08;
      n.y += (ty - n.y) * 0.08;
    });
    return;
  }

  // Standard Force-Directed Physics
  const repulsion = 2400;
  for (let i = 0; i < N; i++) {
    const a = visNodes[i];
    for (let j = i + 1; j < N; j++) {
      const b = visNodes[j];
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let distSq = dx * dx + dy * dy;
      if (distSq < 16) distSq = 16;
      if (distSq > 360000) continue;
      const dist = Math.sqrt(distSq);
      const force = repulsion / distSq;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx -= fx; a.vy -= fy;
      b.vx += fx; b.vy += fy;
    }
  }

  // Spring Edges
  for (let i = 0; i < G_EDGES.length; i++) {
    const e = G_EDGES[i];
    const s = e.sNode, t = e.tNode;
    if (!s.visible || !t.visible) continue;
    const dx = t.x - s.x;
    const dy = t.y - s.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const targetLen = e.kind === 'semantic' ? 125 : 165;
    const force = (dist - targetLen) * 0.025;
    const fx = (dx / dist) * force;
    const fy = (dy / dist) * force;
    s.vx += fx; s.vy += fy;
    t.vx -= fx; t.vy -= fy;
  }

  // Center Gravity & Velocity Damping
  for (let i = 0; i < N; i++) {
    const n = visNodes[i];
    if (n === draggedNode) {
      n.vx = 0; n.vy = 0;
      continue;
    }
    n.vx -= n.x * 0.0045;
    n.vy -= n.y * 0.0045;
    n.vx *= 0.82;
    n.vy *= 0.82;
    n.x += Math.max(-18, Math.min(18, n.vx));
    n.y += Math.max(-18, Math.min(18, n.vy));
  }
}

function drawGraph() {
  const w = canvas.clientWidth || 1200;
  const h = canvas.clientHeight || 800;
  ctx.clearRect(0, 0, w, h);

  ctx.save();
  ctx.translate(camX, camY);
  ctx.scale(camZoom, camZoom);

  const focus = hoveredNode || selectedNode;

  // Draw Edges
  for (let i = 0; i < G_EDGES.length; i++) {
    const e = G_EDGES[i];
    const s = e.sNode, t = e.tNode;
    if (!s.visible || !t.visible) continue;

    const isHighlighted = focus && (s.id === focus.id || t.id === focus.id);
    if (focus && !isHighlighted) {
      ctx.strokeStyle = 'rgba(148, 163, 184, 0.04)';
      ctx.lineWidth = 0.7;
    } else if (isHighlighted) {
      ctx.strokeStyle = e.kind === 'semantic' ? 'rgba(16, 185, 129, 0.88)' : 'rgba(139, 92, 246, 0.75)';
      ctx.lineWidth = 2.0;
    } else {
      ctx.strokeStyle = e.kind === 'semantic' ? 'rgba(148, 163, 184, 0.24)' : 'rgba(148, 163, 184, 0.11)';
      ctx.lineWidth = e.kind === 'semantic' ? 1.15 : 0.8;
    }

    ctx.beginPath();
    ctx.moveTo(s.x, s.y);
    ctx.lineTo(t.x, t.y);
    ctx.stroke();

    // Draw relationship label on highlighted edges
    if (isHighlighted && e.relation && camZoom > 0.55) {
      const mx = (s.x + t.x) / 2;
      const my = (s.y + t.y) / 2;
      ctx.font = '500 10px "JetBrains Mono", sans-serif';
      ctx.fillStyle = '#6EE7B7';
      ctx.textAlign = 'center';
      ctx.fillText(e.relation, mx, my - 4);
    }
  }

  // Draw Nodes
  for (let i = 0; i < G_NODES.length; i++) {
    const n = G_NODES[i];
    if (!n.visible) continue;

    const isFocused = focus && n.id === focus.id;
    const isNeighbor = focus && neighborSet.has(n.id);
    const dimmed = focus && !isNeighbor;

    ctx.globalAlpha = dimmed ? 0.16 : 1.0;

    // Outer Halo for active or focused nodes
    if (isFocused || n.obs_count > 10 || n.degree > 6) {
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r + (isFocused ? 8 : 4), 0, Math.PI * 2);
      ctx.fillStyle = isFocused ? 'rgba(16, 185, 129, 0.32)' : (n.color + '24');
      ctx.fill();
    }

    // Main Node Circle
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
    ctx.fillStyle = n.color;
    ctx.fill();
    ctx.lineWidth = isFocused ? 2.5 : 1.2;
    ctx.strokeStyle = isFocused ? '#FFFFFF' : 'rgba(255,255,255,0.28)';
    ctx.stroke();

    // Node Label
    if (isFocused || isNeighbor || camZoom > 0.72 || n.degree >= 3 || n.obs_count >= 5) {
      const displayLabel = (SHOW_ARABIC_LABELS && n.label_ar) ? `${n.label_ar} (${n.label})` : n.label;
      ctx.font = `${isFocused ? '700 12.5px' : '500 11px'} "IBM Plex Sans Arabic", "Inter", sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillStyle = isFocused ? '#FFFFFF' : '#E2E8F0';
      ctx.fillText(displayLabel, n.x, n.y + n.r + 14);
    }
  }

  ctx.globalAlpha = 1.0;
  ctx.restore();
}

function animateGraph() {
  if (document.getElementById('view-graph').classList.contains('active')) {
    stepPhysics();
    drawGraph();
  }
  requestAnimationFrame(animateGraph);
}

// Canvas Pointer & Zoom Interactions
function screenToWorld(sx, sy) {
  return {
    x: (sx - camX) / camZoom,
    y: (sy - camY) / camZoom
  };
}

function findNodeAt(wx, wy) {
  for (let i = G_NODES.length - 1; i >= 0; i--) {
    const n = G_NODES[i];
    if (!n.visible) continue;
    const dx = n.x - wx, dy = n.y - wy;
    if (dx * dx + dy * dy <= (n.r + 5) * (n.r + 5)) return n;
  }
  return null;
}

canvas.addEventListener('mousedown', e => {
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  pointerDownX = sx; pointerDownY = sy;
  const wpt = screenToWorld(sx, sy);
  const hit = findNodeAt(wpt.x, wpt.y);
  if (hit) {
    draggedNode = hit;
  } else {
    isDraggingCanvas = true;
    dragStartX = sx - camX;
    dragStartY = sy - camY;
  }
});

window.addEventListener('mousemove', e => {
  if (!document.getElementById('view-graph').classList.contains('active')) return;
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const wpt = screenToWorld(sx, sy);

  if (draggedNode) {
    draggedNode.x = wpt.x;
    draggedNode.y = wpt.y;
    return;
  }
  if (isDraggingCanvas) {
    camX = sx - dragStartX;
    camY = sy - dragStartY;
    return;
  }
  const hit = findNodeAt(wpt.x, wpt.y);
  if (hit !== hoveredNode) {
    hoveredNode = hit;
    canvas.style.cursor = hit ? 'pointer' : 'grab';
    updateNeighborSet(hoveredNode || selectedNode);
  }
});

window.addEventListener('mouseup', e => {
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const moved = Math.hypot(sx - pointerDownX, sy - pointerDownY);

  if (draggedNode && moved < 6) {
    selectedNode = draggedNode;
    updateNeighborSet(selectedNode);
    openNodeInspector(selectedNode);
  } else if (!draggedNode && isDraggingCanvas && moved < 5) {
    selectedNode = null;
    updateNeighborSet(null);
  }
  draggedNode = null;
  isDraggingCanvas = false;
});

canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const sx = e.clientX - rect.left;
  const sy = e.clientY - rect.top;
  const zoomFactor = e.deltaY < 0 ? 1.12 : 0.89;
  const nextZoom = Math.max(0.2, Math.min(3.5, camZoom * zoomFactor));
  camX = sx - ((sx - camX) / camZoom) * nextZoom;
  camY = sy - ((sy - camY) / camZoom) * nextZoom;
  camZoom = nextZoom;
}, { passive: false });

// ----------------------------------------------------------------------
// Slide-Over Node & Dossier Inspector
// ----------------------------------------------------------------------
async function openNodeInspector(node) {
  document.getElementById('drawerTypeBadge').textContent = `${node.type} · ${node.status || 'active'}`;
  document.getElementById('drawerTitle').textContent =
    node.label_ar ? `${node.label_ar} — ${node.label}` : node.label;

  const conns = G_EDGES.filter(e => e.sNode.id === node.id || e.tNode.id === node.id);
  let html = `
    <div style="display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px;">
      <span class="badge badge-project">ID: ${escapeHtml(node.id)}</span>
      <span class="badge badge-type">الروابط: ${conns.length}</span>
      <span class="badge badge-rrf">ملاحظات الجلسات: ${node.obs_count || 0}</span>
    </div>
  `;

  if (node.aliases && node.aliases.length) {
    html += `<div style="font-size:12.5px; color:var(--text-muted); margin-bottom:10px;">
      <strong>المرادفات (Aliases):</strong> ${escapeHtml(node.aliases.join(' ، '))}
    </div>`;
  }

  if (conns.length) {
    html += `<div class="section-title">🔗 العلاقات والروابط (${conns.length})</div><div style="display:flex; flex-direction:column; gap:6px;">`;
    conns.slice(0, 25).forEach(e => {
      const isOut = e.sNode.id === node.id;
      const other = isOut ? e.tNode : e.sNode;
      const arrow = isOut ? '←' : '→';
      html += `
        <div class="agent-row" style="margin:0; padding:8px 12px; cursor:pointer;" onclick="focusGraphNode('${escapeHtml(other.id)}')">
          <div>
            <span style="color:var(--emerald); font-family:var(--font-mono); font-size:11.5px;">[${escapeHtml(e.relation)}]</span>
            <strong style="margin:0 6px;">${escapeHtml(other.label_ar || other.label)}</strong>
            <span style="font-size:11.5px; color:var(--text-muted);">(${escapeHtml(other.type)})</span>
          </div>
          <span style="font-size:12px; color:var(--text-muted);">${arrow}</span>
        </div>
      `;
    });
    html += `</div>`;
  }

  document.getElementById('drawerBody').innerHTML = html + `<div style="margin-top:16px; color:var(--text-muted);">جاري تحميل محتوى الملف...</div>`;
  document.getElementById('drawerBackdrop').classList.add('open');
  document.getElementById('inspectorDrawer').classList.add('open');

  if (node.file) {
    try {
      const res = await fetch('/api/dossier?path=' + encodeURIComponent(node.file));
      if (res.ok) {
        const d = await res.json();
        let dHtml = html;
        if (d.related_observations && d.related_observations.length) {
          dHtml += `<div class="section-title">⚡ آخر ملاحظات الجلسات لهذا المشروع</div>`;
          d.related_observations.forEach(o => {
            dHtml += `
              <div style="padding:8px 12px; background:rgba(15,23,42,0.7); border:1px solid var(--border-glass); border-radius:8px; margin-bottom:6px;">
                <div style="font-size:11.5px; color:var(--emerald); font-family:var(--font-mono);">#${o.id} · ${escapeHtml(o.type)} · ${escapeHtml(o.date)}</div>
                <div style="font-weight:600; font-size:13px;">${escapeHtml(o.title)}</div>
              </div>
            `;
          });
        }
        dHtml += `<div class="section-title">📄 ملف الـ Dossier (${escapeHtml(d.path)})</div>`;
        dHtml += `<div class="md-pre">${escapeHtml(d.content)}</div>`;
        document.getElementById('drawerBody').innerHTML = dHtml;
        return;
      }
    } catch (err) {}
  }
  document.getElementById('drawerBody').innerHTML = html;
}

async function openDossierByPath(path) {
  document.getElementById('drawerTypeBadge').textContent = 'VAULT DOSSIER';
  document.getElementById('drawerTitle').textContent = path;
  document.getElementById('drawerBody').innerHTML = 'جاري تحميل الملف...';
  document.getElementById('drawerBackdrop').classList.add('open');
  document.getElementById('inspectorDrawer').classList.add('open');

  const res = await fetch('/api/dossier?path=' + encodeURIComponent(path));
  if (res.ok) {
    const d = await res.json();
    document.getElementById('drawerBody').innerHTML = `<div class="md-pre">${escapeHtml(d.content)}</div>`;
  }
}

function focusGraphNode(nodeId) {
  const target = G_NODES.find(n => n.id === nodeId);
  if (!target) return;
  selectedNode = target;
  updateNeighborSet(target);
  openNodeInspector(target);
}

function closeDrawer() {
  document.getElementById('drawerBackdrop').classList.remove('open');
  document.getElementById('inspectorDrawer').classList.remove('open');
}

// ----------------------------------------------------------------------
// Live Memory Feed & Observation Deletion / Compaction
// ----------------------------------------------------------------------
async function loadFeed() {
  const proj = document.getElementById('feedProjectSelect').value;
  const otype = document.getElementById('feedTypeSelect').value;
  const res = await fetch(`/api/feed?limit=80&project=${encodeURIComponent(proj)}&type=${encodeURIComponent(otype)}`);
  const data = await res.json();
  FEED_ITEMS = data.items || [];
  renderFeedList();
}

function renderFeedList() {
  const q = (document.getElementById('feedQuickFilter').value || '').trim().toLowerCase();
  const filtered = FEED_ITEMS.filter(it => {
    if (!q) return true;
    return (it.title || '').toLowerCase().includes(q) ||
           (it.subtitle || '').toLowerCase().includes(q) ||
           (it.project || '').toLowerCase().includes(q) ||
           (it.narrative || '').toLowerCase().includes(q);
  });

  document.getElementById('feedCountLabel').textContent = `يعرض ${filtered.length} عنصر من الذاكرة الحية`;
  document.getElementById('feedList').innerHTML = filtered.map(it => {
    const icon = TYPE_ICONS[it.type] || '◇';
    const factsHtml = (it.facts && it.facts.length)
      ? `<ul class="obs-facts">${it.facts.slice(0, 6).map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul>`
      : '';
    const filesHtml = (it.files_modified && it.files_modified.length)
      ? `<div class="obs-files">${it.files_modified.slice(0, 8).map(f => `<span class="file-chip">✎ ${escapeHtml(f)}</span>`).join('')}</div>`
      : '';
    const occBadge = it.occurrence_count > 1
      ? `<span class="badge badge-rrf">تكرر ×${it.occurrence_count}</span>`
      : '';
    const delBtn = it.kind === 'observation'
      ? `<button class="btn btn-danger" onclick="deleteObs(${it.id})" title="حذف هذه الملاحظة">🗑 حذف</button>`
      : '';

    return `
      <div class="obs-card" id="obs-card-${it.id}">
        <div class="obs-header">
          <div>
            <div class="obs-meta" style="margin-bottom:6px;">
              <span class="badge badge-project">${escapeHtml(it.project)}</span>
              <span class="badge badge-type">${icon} ${escapeHtml(it.type)}</span>
              ${occBadge}
              <span style="color:var(--text-muted); font-family:var(--font-mono);">#${escapeHtml(it.id)} · ${escapeHtml(it.date)}</span>
            </div>
            <div class="obs-title">${escapeHtml(it.title)}</div>
            ${it.subtitle ? `<div class="obs-sub">${escapeHtml(it.subtitle)}</div>` : ''}
          </div>
          <div>${delBtn}</div>
        </div>
        ${factsHtml}
        ${filesHtml}
      </div>
    `;
  }).join('');
}

async function deleteObs(id) {
  if (!confirm(`هل أنت متأكد من حذف الملاحظة رقم #${id} نهائياً من قاعدة البيانات؟`)) return;
  const res = await fetch(`/api/observation/${id}`, { method: 'DELETE' });
  if (res.ok) {
    FEED_ITEMS = FEED_ITEMS.filter(x => x.id !== id);
    renderFeedList();
    loadStatus();
  }
}

async function runDeduplicate() {
  if (!confirm('سيتم دمج الملاحظات المكررة ذات نفس العنوان والمشروع في ملاحظة واحدة نظيفة. متابعة؟')) return;
  const res = await fetch('/api/observations/deduplicate', { method: 'POST' });
  if (res.ok) {
    const out = await res.json();
    alert(`تم تنظيف ودمج ${out.observations_removed} ملاحظة مكررة بنجاح!`);
    await loadStatus();
    await loadFeed();
  }
}

// ----------------------------------------------------------------------
// Unified Hybrid RRF Search
// ----------------------------------------------------------------------
async function runHybridSearch() {
  const q = document.getElementById('hybridSearchInput').value.trim();
  if (!q) return;
  const res = await fetch('/api/search?q=' + encodeURIComponent(q));
  const data = await res.json();

  const expBar = document.getElementById('searchExpansionBar');
  const ents = (data.matched_entities || []).join(' ، ');
  expBar.innerHTML = `وضع البحث: <strong style="color:var(--emerald);">Hybrid RRF (FTS5 BM25 + Arabic Trigram + Graph Aliases)</strong>` +
    (ents ? ` · الكيانات المرتبطة تلقائياً: <strong style="color:#67E8F9;">${escapeHtml(ents)}</strong>` : '');

  const list = data.ranked_fusion || [];
  if (!list.length) {
    document.getElementById('searchResultsList').innerHTML =
      `<div class="obs-card" style="text-align:center; color:var(--text-muted);">لا توجد نتائج مطابقة لـ "${escapeHtml(q)}"</div>`;
    return;
  }

  document.getElementById('searchResultsList').innerHTML = list.map((item, idx) => {
    const isVault = item.source === 'vault';
    const srcBadge = isVault
      ? `<span class="badge badge-project">📄 Vault Dossier</span>`
      : `<span class="badge badge-type">⚡ Session Memory (${escapeHtml(item.type || 'obs')})</span>`;
    const rrfPct = Math.min(99, Math.round(item.rrf_score * 3200));
    const clickAttr = isVault
      ? `style="cursor:pointer;" onclick="openDossierByPath('${escapeHtml(item.path)}')"`
      : '';

    return `
      <div class="obs-card" ${clickAttr}>
        <div class="obs-header">
          <div>
            <div class="obs-meta" style="margin-bottom:6px;">
              <span class="badge badge-rrf">#${idx + 1} · RRF ${rrfPct}%</span>
              ${srcBadge}
              ${item.project ? `<span class="badge badge-project">${escapeHtml(item.project)}</span>` : ''}
              <span style="color:var(--text-muted); font-family:var(--font-mono);">${escapeHtml(item.date || item.path || '')}</span>
            </div>
            <div class="obs-title">${escapeHtml(item.title)}</div>
            <div class="obs-sub">${escapeHtml(item.snippet || item.subtitle || '')}</div>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function refreshAll() {
  await Promise.all([loadStatus(), loadGraph()]);
  if (document.getElementById('view-feed').classList.contains('active')) {
    await loadFeed();
  }
}

// Keyboard Shortcut Cmd+K / Ctrl+K for Hybrid Search
window.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    switchView('search');
    document.getElementById('hybridSearchInput').focus();
  }
  if (e.key === 'Escape') closeDrawer();
});

// Boot
(async function boot() {
  await Promise.all([loadStatus(), loadGraph()]);
  requestAnimationFrame(animateGraph);
})();
</script>
</body>
</html>
"""


class MemoUIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # Silent clean local server

    def _send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, html: str, status: int = 200) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path in ("/", "/index.html"):
                self._send_html(HTML_PAGE)
                return
            if path == "/api/status":
                self._send_json(build_status_payload())
                return
            if path == "/api/graph":
                self._send_json(build_graph_payload())
                return
            if path == "/api/feed":
                limit = max(1, min(int((qs.get("limit") or ["60"])[0]), 200))
                project = (qs.get("project") or [""])[0].strip()
                obs_type = (qs.get("type") or [""])[0].strip()
                self._send_json(build_feed_payload(limit=limit, project=project, obs_type=obs_type))
                return
            if path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                project = (qs.get("project") or [""])[0].strip()
                obs_type = (qs.get("type") or [""])[0].strip()
                limit = max(1, min(int((qs.get("limit") or ["25"])[0]), 100))
                self._send_json(build_search_payload(q, project=project, obs_type=obs_type, limit=limit))
                return
            if path == "/api/dossier":
                rel = (qs.get("path") or [""])[0].strip()
                self._send_json(build_dossier_payload(rel))
                return
            if path == "/api/duplicates":
                conn = state.connect()
                try:
                    dups = capture_search.find_duplicate_observations(conn, limit=100)
                finally:
                    conn.close()
                self._send_json({"count": len(dups), "duplicates": dups})
                return
            self._send_json({"error": "Not found"}, status=404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=400)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/observations/deduplicate":
                conn = state.connect()
                try:
                    res = capture_search.deduplicate_observations(conn)
                finally:
                    conn.close()
                self._send_json(res)
                return
            self._send_json({"error": "Not found"}, status=404)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=400)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        m = re.fullmatch(r"/api/observation/(\d+)", parsed.path)
        if not m:
            self._send_json({"error": "Not found"}, status=404)
            return
        obs_id = int(m.group(1))
        conn = state.connect()
        try:
            ok = capture_search.delete_observation(conn, obs_id)
        finally:
            conn.close()
        self._send_json({"deleted": ok, "id": obs_id}, status=200 if ok else 404)


def run_server(port: int = 8766, open_browser: bool = True) -> int:
    import oslock
    server = oslock.local_http_server(("127.0.0.1", port), MemoUIHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Memo Sovereign Studio running at {url} (vault: {vault_dir()})")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMemo Studio stopped.")
    finally:
        server.server_close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Memo Sovereign Studio Local Web Viewer")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MEMO_UI_PORT", "8766")))
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args(argv)
    return run_server(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
