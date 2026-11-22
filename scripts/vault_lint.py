#!/usr/bin/env python3
"""Memo vault linter — enforce frontmatter schema, tag taxonomy, graph sync, wikilinks.

Usage:
  python3 scripts/vault_lint.py
  python3 scripts/vault_lint.py --json
  python3 scripts/vault_lint.py --fix   # safe fixes only (currently: none destructive)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VAULT = Path(__file__).resolve().parent.parent
GRAPH = VAULT / "graph" / "entities.json"

SKIP_DIRS = {
    ".git", ".obsidian", ".cache", ".gemini", ".claude", ".agents", ".agy-briefs",
    "venv", ".venv", "node_modules", "__pycache__", ".trash",
    "_templates", "_attachments", "Export",
    "plugin", "tests",          # plugin package and test code, not notes
    "deploy",                   # container/deploy files, not notes
    "docs/assets",              # brand assets READMEs, not vault notes
}

# Agent instruction files at the vault root are not vault notes and carry no
# note frontmatter. Matched without case (this Mac's filesystem ignores case).
ROOT_INSTRUCTION_FILES = {"claude.md", "gemini.md", "agents.md"}

# Product / OSS packaging files at the vault root (GitHub-facing), not dossiers.
ROOT_PRODUCT_FILES = {"readme.md", "contributing.md", "license.md", "changelog.md", "security.md"}

REQUIRED = {"title", "type", "status", "tags"}
ALLOWED_TYPES = {
    "core", "client", "project", "stack", "expertise",
    "hackathon", "profile", "agent", "session", "subscription",
    "area", "goal", "task", "habit", "study", "library", "repo", "bookmark",
}
ALLOWED_STATUS = {"active", "paused", "completed", "planned", "archived", "empty", "evaluating", "saved"}
ALLOWED_TAGS = {
    "type/core", "type/client", "type/project", "type/stack",
    "type/expertise", "type/hackathon", "type/profile", "type/agent", "type/session",
    "type/subscription",
    "type/area", "type/goal", "type/task", "type/habit", "type/study", "type/library", "type/repo",
    "type/bookmark",

    "status/active", "status/paused", "status/completed", "status/planned",
    "status/archived", "status/empty", "status/evaluating", "status/saved",
    "domain/ai", "domain/ecommerce", "domain/edtech", "domain/fintech",
    "domain/healthtech", "domain/govtech", "domain/security", "domain/automation",
    "domain/design", "domain/education", "domain/interior-design", "domain/construction",
    "tech/react", "tech/python", "tech/supabase", "tech/n8n", "tech/wordpress",
    "tech/woocommerce", "tech/salla", "tech/flutter", "tech/fastapi",
    "tech/sqlite", "tech/docker", "tech/cloudflare", "tech/tailwind",
    "tech/nextjs", "tech/capacitor",
    "visibility/public", "visibility/private",
    "tier/flagship", "tier/side", "tier/experiment", "tier/client-work",
    "service/seo", "service/whatsapp", "service/automation", "service/web-development",
    "area/developer", "area/study", "area/education", "area/finance", "area/personal",
    "study/course", "study/book", "study/exam", "study/note", "study/video", "study/assignment",
    "library/prompt", "library/code", "library/link", "library/idea", "library/note", "library/resource",
}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
PLACEHOLDER_LINKS = {"اسم-الملف", "...", "affected-file"}


def split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5:]


def parse_simple_yaml(block: str) -> Dict[str, Any]:
    """Minimal YAML for Memo frontmatter — supports inline and block lists.

    Obsidian / Second Brain Sync rewrite `tags: [a, b]` into:
      tags:
        - a
        - b
    The old line-only parser dropped those tags and failed the lint.
    """
    result: Dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line.lstrip().startswith("- "):
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if not val:
            items: List[str] = []
            while i < len(lines):
                nxt = lines[i]
                m = re.match(r"^\s+-\s+(.*)$", nxt)
                if m:
                    items.append(m.group(1).strip().strip("'\""))
                    i += 1
                    continue
                if not nxt.strip():
                    i += 1
                    continue
                break
            result[key] = items
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            result[key] = [] if not inner else [
                p.strip().strip("'\"") for p in re.split(r",\s*", inner) if p.strip()
            ]
        elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            result[key] = val[1:-1]
        elif val.lower() in ("true", "false"):
            result[key] = val.lower() == "true"
        else:
            result[key] = val
    return result


def iter_md() -> List[Path]:
    out: List[Path] = []
    for root, dirs, files in __import__("os").walk(VAULT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        at_root = Path(root) == VAULT
        for f in files:
            if at_root and f.lower() in ROOT_INSTRUCTION_FILES:
                continue
            if at_root and f.lower() in ROOT_PRODUCT_FILES:
                continue
            # Brand / packaging notes under docs/assets are not vault dossiers.
            if "docs" in Path(root).parts and "assets" in Path(root).parts:
                continue
            if f.endswith(".md"):
                out.append(Path(root) / f)
    return sorted(out)


def resolve_wikilink(target: str) -> bool:
    if target in PLACEHOLDER_LINKS:
        return True
    cand = [
        VAULT / f"{target}.md",
        VAULT / target,
        VAULT / f"{target}.base",
        VAULT / f"{target}.canvas",
    ]
    if any(c.exists() for c in cand):
        return True
    stem = Path(target).name
    matches = [
        p for p in VAULT.rglob(f"{stem}.md")
        if not any(s in p.parts for s in SKIP_DIRS | {".obsidian", ".git"})
    ]
    if matches:
        return True
    # also .base / .canvas
    for ext in (".base", ".canvas"):
        matches = [
            p for p in VAULT.rglob(f"{stem}{ext}")
            if not any(s in p.parts for s in SKIP_DIRS | {".obsidian", ".git"})
        ]
        if matches:
            return True
    return False


def strip_code_for_links(text: str) -> str:
    """Remove fenced/inline code so example wikilinks do not fail the lint."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"`[^`]+`", "", text)
    return text


def lint() -> Dict[str, Any]:
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []

    graph = json.loads(GRAPH.read_text(encoding="utf-8"))
    entities = graph["entities"]
    by_file = {}
    for e in entities:
        f = e.get("file")
        if f:
            by_file.setdefault(f, []).append(e["id"])
    entity_ids = {e["id"] for e in entities}

    files = iter_md()
    fm_entity_ids = set()

    for path in files:
        rel = path.relative_to(VAULT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        fm_raw, body = split_frontmatter(text)
        if fm_raw is None:
            errors.append({"file": rel, "code": "no_frontmatter", "msg": "Missing YAML frontmatter"})
            continue
        fm = parse_simple_yaml(fm_raw)
        for key in REQUIRED:
            if key not in fm or fm[key] in (None, "", []):
                errors.append({"file": rel, "code": "missing_field", "msg": f"Missing required field '{key}'"})

        ntype = fm.get("type")
        if ntype and ntype not in ALLOWED_TYPES:
            errors.append({"file": rel, "code": "bad_type", "msg": f"Unknown type '{ntype}'"})
        status = fm.get("status")
        if status and status not in ALLOWED_STATUS:
            errors.append({"file": rel, "code": "bad_status", "msg": f"Unknown status '{status}'"})

        tags = fm.get("tags") or []
        if not isinstance(tags, list):
            errors.append({"file": rel, "code": "bad_tags", "msg": "tags must be a list"})
            tags = []
        for t in tags:
            if t not in ALLOWED_TAGS:
                errors.append({"file": rel, "code": "bad_tag", "msg": f"Tag not in taxonomy: {t}"})
        if ntype and f"type/{ntype}" not in tags:
            warnings.append({"file": rel, "code": "tag_type_mismatch", "msg": f"Expected tag type/{ntype}"})
        if status and f"status/{status}" not in tags:
            warnings.append({"file": rel, "code": "tag_status_mismatch", "msg": f"Expected tag status/{status}"})

        eid = fm.get("entity_id")
        if eid:
            fm_entity_ids.add(eid)
            if eid not in entity_ids:
                errors.append({"file": rel, "code": "orphan_entity_id", "msg": f"entity_id '{eid}' not in graph"})

        # Memo session notes: the fields capture writes, and the data sidecar reindex needs.
        if rel.startswith("sessions/") and str(fm.get("memo_session", "")).lower() == "true":
            for key in ("project", "session_id", "started"):
                if not fm.get(key):
                    errors.append({"file": rel, "code": "session_field",
                                   "msg": f"Memo session note missing '{key}'"})
            if not path.with_suffix(".json").exists():
                warnings.append({"file": rel, "code": "session_sidecar",
                                 "msg": "Memo session note has no .json data sidecar (memo reindex cannot rebuild it)"})

        # Folder ↔ type consistency
        top = Path(rel).parts[0] if "/" in rel else ""
        expected = {
            "clients": "client", "projects": "project", "stack": "stack",
            "expertise": "expertise", "hackathons": "hackathon", "profiles": "profile",
            "agent": "agent", "sessions": "session", "subscriptions": "subscription",
            "bookmarks": "bookmark",
        }.get(top)
        # life/ hosts multiple Life OS types — folder↔type checked by subpath below
        if top == "life":
            parts = Path(rel).parts
            life_expected = {
                "areas": "area", "goals": "goal", "tasks": "task", "habits": "habit",
            }.get(parts[1] if len(parts) > 1 else "")
            if life_expected and ntype and ntype != life_expected:
                warnings.append({
                    "file": rel, "code": "folder_type",
                    "msg": f"Folder suggests type={life_expected}, got {ntype}",
                })
            elif len(parts) > 1 and parts[1] == "study" and ntype and ntype != "study":
                warnings.append({
                    "file": rel, "code": "folder_type",
                    "msg": f"Folder suggests type=study, got {ntype}",
                })
            elif len(parts) > 1 and parts[1] == "library" and ntype and ntype != "library":
                warnings.append({
                    "file": rel, "code": "folder_type",
                    "msg": f"Folder suggests type=library, got {ntype}",
                })
        elif expected and ntype and ntype != expected:
            warnings.append({"file": rel, "code": "folder_type", "msg": f"Folder suggests type={expected}, got {ntype}"})

        link_scan = strip_code_for_links(text)
        for m in WIKILINK_RE.finditer(link_scan):
            target = m.group(1).strip()
            if not resolve_wikilink(target):
                errors.append({"file": rel, "code": "broken_wikilink", "msg": f"Broken wikilink [[{target}]]"})

    # Graph files that should exist
    for e in entities:
        f = e.get("file")
        if f and not (VAULT / f).exists():
            errors.append({"file": f, "code": "missing_entity_file", "msg": f"Entity {e['id']} points to missing file"})

    # clients/projects/stack should have an entity
    for folder, etypes in (("clients", {"Client", "Organization", "Person"}),
                           ("projects", {"Project", "Organization"}),
                           ("stack", {"Technology"})):
        for path in sorted((VAULT / folder).glob("*.md")):
            rel = path.relative_to(VAULT).as_posix()
            if rel not in by_file:
                warnings.append({"file": rel, "code": "no_graph_entity", "msg": f"No entity references this file"})

    return {
        "ok": len(errors) == 0,
        "files": len(files),
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "error_codes": dict(Counter(e["code"] for e in errors)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix", action="store_true", help="Reserved for safe auto-fixes")
    args = ap.parse_args()
    result = lint()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Memo vault lint — {result['files']} files")
        print(f"  errors:   {result['error_count']}")
        print(f"  warnings: {result['warning_count']}")
        if result["error_codes"]:
            print(f"  codes:    {result['error_codes']}")
        for e in result["errors"][:40]:
            print(f"  ERROR [{e['code']}] {e['file']}: {e['msg']}")
        if result["error_count"] > 40:
            print(f"  ... and {result['error_count'] - 40} more errors")
        for w in result["warnings"][:20]:
            print(f"  WARN  [{w['code']}] {w['file']}: {w['msg']}")
        print("PASS" if result["ok"] else "FAIL")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
