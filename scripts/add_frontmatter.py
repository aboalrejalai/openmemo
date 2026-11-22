#!/usr/bin/env python3
"""Add / refresh Obsidian YAML frontmatter across the Memo vault.

Idempotent: re-running merges known keys without duplicating the --- block.
Derives values only from file content + graph/entities.json (no invention).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VAULT = Path(__file__).resolve().parent.parent
GRAPH = VAULT / "graph" / "entities.json"
TODAY = date.today().isoformat()

SKIP_DIRS = {
    ".git", ".obsidian", ".cache", ".gemini", ".claude",
    "venv", ".venv", "node_modules", "__pycache__", ".trash",
    "_templates", "_attachments",
}

FOLDER_TYPE = {
    "clients": "client",
    "projects": "project",
    "stack": "stack",
    "expertise": "expertise",
    "hackathons": "hackathon",
    "profiles": "profile",
    "agent": "agent",
    "sessions": "session",
    "docs": "core",
    "graph": "core",
    "subscriptions": "subscription",
    "bookmarks": "bookmark",
}

CORE_ROOT = {
    "README.md", "decisions.md", "timeline.md", "stack.md", "HOME.md", "subscriptions.md",
}

ALLOWED_TAGS = {
    "type/core", "type/client", "type/project", "type/stack",
    "type/expertise", "type/hackathon", "type/profile", "type/agent", "type/session",
    "type/subscription", "type/bookmark",

    "status/active", "status/paused", "status/completed", "status/planned",
    "status/archived", "status/empty", "status/saved",
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
}

FLAGSHIP = {
    "demo-project", "my-project",
}

DOMAIN_HINTS = [
    (r"salla|woocommerce|e-?commerce|store", "domain/ecommerce"),
    (r"lms|education|edtech|madrasati|university", "domain/edtech"),
    (r"bank|fintech|sama|payment|energy budget", "domain/fintech"),
    (r"health|clinical|radiolog|pharmacy|medication|dicom|fhir|patient", "domain/healthtech"),
    (r"dga|govtech|nds|ministry|sovereign", "domain/govtech"),
    (r"security|pentest|kali|cyber", "domain/security"),
    (r"n8n|automation|workflow|whatsapp agent", "domain/automation"),
    (r"design|figma|decor|ui\b", "domain/design"),
    (r"interior|ديكور|decor", "domain/interior-design"),
    (r"\bai\b|agent|llm|mlx|rag|prompt", "domain/ai"),
]

TECH_HINTS = [
    (r"\breact\b|next\.js|tanstack", "tech/react"),
    (r"\bpython\b|mlx\b", "tech/python"),
    (r"supabase", "tech/supabase"),
    (r"\bn8n\b", "tech/n8n"),
    (r"wordpress", "tech/wordpress"),
    (r"woocommerce", "tech/woocommerce"),
    (r"\bsalla\b|سلة", "tech/salla"),
    (r"flutter|\bdart\b", "tech/flutter"),
    (r"fastapi", "tech/fastapi"),
    (r"sqlite|fts5", "tech/sqlite"),
    (r"\bdocker\b", "tech/docker"),
    (r"cloudflare", "tech/cloudflare"),
    (r"tailwind", "tech/tailwind"),
]


def load_entities() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    data = json.loads(GRAPH.read_text(encoding="utf-8"))
    by_id = {e["id"]: e for e in data["entities"]}
    by_file: Dict[str, Dict[str, Any]] = {}
    for e in data["entities"]:
        f = e.get("file")
        if not f:
            continue
        # Prefer Client/Project/Technology over Person when same file
        existing = by_file.get(f)
        if existing is None:
            by_file[f] = e
            continue
        rank = {"Client": 3, "Project": 3, "Technology": 3, "Organization": 2,
                "Hackathon": 3, "Discipline": 3, "Profile": 3, "Person": 1}
        if rank.get(e["type"], 0) >= rank.get(existing["type"], 0):
            by_file[f] = e
    return by_id, by_file


def split_frontmatter(text: str) -> Tuple[Optional[str], str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return text[4:end], text[end + 5:]


def parse_simple_yaml(block: str) -> Dict[str, Any]:
    """Minimal YAML subset parser sufficient for our frontmatter."""
    result: Dict[str, Any] = {}
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if not val:
            result[key] = None
            continue
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                items = []
                for part in re.split(r",\s*", inner):
                    part = part.strip().strip("'\"")
                    if part:
                        items.append(part)
                result[key] = items
        elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            result[key] = val[1:-1]
        else:
            result[key] = val
    return result


def dump_yaml(data: Dict[str, Any]) -> str:
    lines = ["---"]
    for key, val in data.items():
        if val is None or val == "" or val == []:
            continue
        if isinstance(val, list):
            rendered = ", ".join(_yaml_scalar(v) for v in val)
            lines.append(f"{key}: [{rendered}]")
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_scalar(val: Any) -> str:
    s = str(val)
    if re.search(r'[:#\[\]{},\n"]', s) or s.startswith(" ") or s.endswith(" "):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def h1_title(body: str) -> Optional[str]:
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else None


def extract_field(body: str, labels: List[str]) -> Optional[str]:
    for label in labels:
        pat = rf"\*\*{re.escape(label)}\*\*:\s*(.+?)(?:\n|$)"
        m = re.search(pat, body, re.IGNORECASE)
        if m:
            return re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", m.group(1)).strip()
    return None


def extract_url(body: str) -> Optional[str]:
    for label in ("Official Website", "Website", "Domain / Website", "Homepage",
                  "Live demo", "Official Portal", "event_url", "URL"):
        val = extract_field(body, [label])
        if val:
            m = re.search(r"`(https?://[^`]+)`", val) or re.search(r"\((https?://[^)]+)\)", val) \
                or re.search(r"(https?://\S+)", val)
            if m:
                return m.group(1).rstrip(").,")
            if val.startswith("http"):
                return val.split()[0].rstrip(").,")
    m = re.search(r"`(https?://[^`]+)`", body)
    return m.group(1) if m else None


def extract_repo(body: str) -> Optional[str]:
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", body)
    if m:
        return f"https://github.com/{m.group(1)}/{m.group(2)}"
    m = re.search(r"`([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)`", body)
    if m:
        return f"https://github.com/{m.group(1)}/{m.group(2)}"
    return None


def infer_status(body: str, note_type: str) -> str:
    if note_type == "bookmark":
        return "saved"
    if note_type in ("stack", "expertise", "profile", "core", "agent", "session"):
        return "active"
    blob = body.lower()
    if "empty repository" in blob and ("pending content" in blob or "size=0" in blob or "size:** 0" in blob):
        return "empty"
    if re.search(r"\*\*status:\*\*[^*\n]{0,80}empty repository", blob):
        return "empty"
    if re.search(r"\*\*status:\*\*[^*\n]{0,80}paused|inactive template stage", blob):
        return "paused"
    if re.search(r"\*\*status:\*\*[^*\n]{0,100}(completed|fully submitted)", blob):
        return "completed"
    if re.search(r"\*\*status:\*\*[^*\n]{0,80}(planned|scheduled target)", blob):
        return "planned"
    if re.search(r"\*\*status:\*\*[^*\n]{0,40}archived", blob):
        return "archived"
    if re.search(r"\*\*status:\*\*[^*\n]{0,80}active", blob):
        return "active"
    if note_type == "hackathon":
        if "champion" in blob or "1st place" in blob:
            return "completed"
        if "scheduled target" in blob:
            return "planned"
        return "completed"
    return "active"


def infer_platform(body: str) -> Optional[str]:
    blob = body.lower()
    if "salla" in blob or "سلة" in body:
        return "salla"
    if "woocommerce" in blob:
        return "woocommerce"
    if "wordpress" in blob:
        return "wordpress"
    if "lovable" in blob:
        return "lovable"
    return None


def git_dates(path: Path) -> Tuple[Optional[str], Optional[str]]:
    try:
        created = subprocess.check_output(
            ["git", "log", "--diff-filter=A", "--follow", "--format=%cs", "--", str(path)],
            cwd=VAULT, text=True, stderr=subprocess.DEVNULL,
        ).strip().splitlines()
        updated = subprocess.check_output(
            ["git", "log", "-1", "--format=%cs", "--", str(path)],
            cwd=VAULT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        c = created[-1] if created else None
        u = updated or None
        return c, u
    except Exception:
        return None, None


def note_type_for(rel: str) -> str:
    if rel in CORE_ROOT or rel.startswith("_"):
        return "core"
    parts = Path(rel).parts
    if parts and parts[0] in FOLDER_TYPE:
        return FOLDER_TYPE[parts[0]]
    return "core"


def build_tags(note_type: str, status: str, body: str, rel: str, entity: Optional[Dict]) -> List[str]:
    tags = [f"type/{note_type}", f"status/{status}"]
    # Core / agent / session notes stay lightly tagged (avoid whole-vault keyword bleed)
    if note_type in ("core", "agent", "session", "docs"):
        out = [t for t in tags if t in ALLOWED_TAGS]
        return out

    blob = body.lower()
    stem = Path(rel).stem
    # Prefer the Overview / header zone for tech/domain hints to reduce noise
    head = blob[:2500]

    for pat, tag in DOMAIN_HINTS:
        if re.search(pat, head) and tag not in tags:
            tags.append(tag)
    for pat, tag in TECH_HINTS:
        if re.search(pat, head) and tag not in tags:
            tags.append(tag)

    if note_type == "project":
        if stem in FLAGSHIP or (entity and entity["id"] in FLAGSHIP) or entity and entity.get("id") == "project-acme-corp":
            tags.append("tier/flagship")
        elif status == "empty":
            tags.append("tier/experiment")
        else:
            tags.append("tier/side")
        vis = None
        if re.search(r"\(public\)|public repository|license:\s*mit", head):
            vis = "visibility/public"
        if re.search(r"\(private\)", head):
            vis = "visibility/private"
        if vis:
            tags.append(vis)
    if note_type == "client":
        tags.append("tier/client-work")
        if "seo" in head:
            tags.append("service/seo")
        if "whatsapp" in head:
            tags.append("service/whatsapp")
        if "n8n" in head or "automation" in head:
            tags.append("service/automation")
    if note_type == "stack" and entity and entity.get("category"):
        cat = entity["category"].lower()
        for pat, tag in TECH_HINTS:
            if entity["id"].replace("tech-", "") in tag or re.search(pat, entity["name"].lower()):
                if tag not in tags:
                    tags.append(tag)

    out: List[str] = []
    for t in tags:
        if t in ALLOWED_TAGS and t not in out:
            out.append(t)
    return out


def stack_links(body: str) -> List[str]:
    found = re.findall(r"\[\[(stack/[^\]|#]+)(?:\|[^\]]+)?\]\]", body)
    out: List[str] = []
    for f in found:
        link = f"[[{f}]]"
        if link not in out:
            out.append(link)
    return out[:12]


def build_frontmatter(rel: str, body: str, by_file: Dict[str, Dict], existing: Dict[str, Any]) -> Dict[str, Any]:
    # Preserve human-curated frontmatter wholesale
    if existing.get("curated") in (True, "true", "True", "yes", "1"):
        preserved = dict(existing)
        preserved["updated"] = TODAY
        preserved["curated"] = True
        return {k: v for k, v in preserved.items() if v not in (None, "", [])}

    note_type = note_type_for(rel)
    entity = by_file.get(rel)
    # also try without preferring person collision for project-acme-corp sharing file
    if entity is None:
        for e in by_file.values():
            if e.get("file") == rel:
                entity = e
                break

    title = existing.get("title") or (entity.get("name") if entity else None) or h1_title(body) or Path(rel).stem
    # Always recompute derived fields; preserve curated extras below
    status = infer_status(body, note_type)

    aliases: List[str] = []
    if entity:
        if entity.get("name_ar"):
            aliases.append(entity["name_ar"])
        for a in entity.get("aliases") or []:
            if a not in aliases:
                aliases.append(a)
        if entity["id"] not in aliases and entity["id"] != Path(rel).stem:
            aliases.append(entity["id"])
    # keep prior aliases
    for a in existing.get("aliases") or []:
        if a not in aliases:
            aliases.append(a)

    created, updated = git_dates(VAULT / rel)
    fm: Dict[str, Any] = {
        "title": title,
        "aliases": aliases,
        "type": note_type,
        "status": status,
        "tags": build_tags(note_type, status, body, rel, entity),
        "entity_id": (entity["id"] if entity else existing.get("entity_id")),
        "created": existing.get("created") or created or TODAY,
        "updated": TODAY,
    }

    if note_type == "client":
        website = existing.get("website") or extract_url(body)
        platform = existing.get("platform") or infer_platform(body)
        if website:
            fm["website"] = website
        if platform:
            fm["platform"] = platform
        since = extract_field(body, ["Status"])
        if since and "since" in since.lower():
            m = re.search(r"(\d{2}/\d{4}|\d{4}-\d{2})", since)
            if m:
                fm["client_since"] = m.group(1)
        if "demo-partner" in body.lower() or "الشريك" in body:
            fm["referred_by"] = "[[clients/demo-partner|Demo Partner]]" if "demo-partner" not in rel else None
            if fm.get("referred_by") is None and "demo-partner" not in rel:
                fm["referred_by"] = "Demo Partner"

    if note_type == "project":
        repo = existing.get("repo") or extract_repo(body)
        if repo:
            fm["repo"] = repo
        live = existing.get("live_url") or extract_url(body)
        # avoid treating github as live_url
        if live and "github.com" not in live:
            fm["live_url"] = live
        if "visibility/public" in fm["tags"]:
            fm["visibility"] = "public"
        elif "visibility/private" in fm["tags"]:
            fm["visibility"] = "private"
        elif re.search(r"\(private\)", body):
            fm["visibility"] = "private"
        elif re.search(r"\(public\)|MIT License|License:\s*MIT", body):
            fm["visibility"] = "public"
        stacks = existing.get("stack") or stack_links(body)
        if stacks:
            fm["stack"] = stacks
        hm = re.search(r"\[\[(hackathons/[^\]|#]+)(?:\|[^\]]+)?\]\]", body)
        if hm:
            fm["hackathon"] = f"[[{hm.group(1)}]]"

    if note_type == "stack" and entity:
        if entity.get("category"):
            fm["category"] = entity["category"]

    if note_type == "hackathon":
        url = existing.get("event_url") or (entity.get("url") if entity else None) or extract_url(body)
        if url:
            fm["event_url"] = url
        if entity and entity.get("status"):
            fm["result"] = entity["status"]
        org = extract_field(body, ["Organizers", "Organizer", "Platform"])
        if org:
            fm["organizer"] = re.sub(r"[*_`]", "", org)[:120]

    if note_type == "profile":
        url = existing.get("url") or (entity.get("url") if entity else None) or extract_url(body)
        if url:
            fm["url"] = url
        handle = extract_field(body, ["Handle"])
        if handle:
            fm["handle"] = handle.strip("`")

    if note_type == "expertise" and entity and entity.get("category"):
        fm["category"] = entity["category"]

    # Drop empty
    return {k: v for k, v in fm.items() if v not in (None, "", [])}


def process_file(path: Path, by_file: Dict[str, Dict], dry_run: bool = False) -> str:
    rel = path.relative_to(VAULT).as_posix()
    text = path.read_text(encoding="utf-8")
    old_fm_raw, body = split_frontmatter(text)
    existing = parse_simple_yaml(old_fm_raw) if old_fm_raw is not None else {}
    # If body still starts with --- somehow broken, leave alone
    fm = build_frontmatter(rel, body, by_file, existing)
    new_text = dump_yaml(fm) + body.lstrip("\n")
    if new_text != text:
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
        return "updated"
    return "unchanged"


def iter_md() -> List[Path]:
    out: List[Path] = []
    for root, dirs, files in __import__("os").walk(VAULT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                out.append(Path(root) / f)
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    _, by_file = load_entities()
    counts = {"updated": 0, "unchanged": 0}
    for p in iter_md():
        # skip template/attachment dirs already filtered
        status = process_file(p, by_file, dry_run=args.dry_run)
        counts[status] += 1
        print(f"{status:10} {p.relative_to(VAULT)}")
    print(f"\nDone: {counts}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
