#!/usr/bin/env python3
"""Export Cursor agent-transcripts into sessions/_raw-cursor/ as Markdown."""
from __future__ import annotations
import argparse, json, os, re
from datetime import datetime, timezone
from pathlib import Path

VAULT = Path(
    os.environ.get("MEMO_VAULT")
    or os.environ.get("OPENMEMO_HOME")
    or str(Path(__file__).resolve().parents[4])
)
OUT = VAULT / "sessions" / "_raw-cursor"
SRC = Path.home() / ".cursor" / "projects"
QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)

def slug_project(cursor_slug: str) -> str:
    s = cursor_slug
    s = re.sub(r"^Users-[^/-]+-(?:Desktop-)?(?:Project-)?", "", s)
    return s or "unknown"

def english_slug(title: str, max_len: int = 72) -> str:
    s = re.sub(r"[\u0600-\u06FF]+", " ", title or "")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len].rstrip("-") or "session")

def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text") or "")
        return "\n".join(parts)
    return ""

def extract(path: Path) -> tuple[str, str]:
    blocks, first = [], "cursor-session"
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            role = o.get("role") or o.get("type")
            msg = o.get("message") or {}
            body = text_of(msg.get("content") if isinstance(msg, dict) else o.get("content")).strip()
            if not body:
                continue
            if role == "user":
                m = QUERY.search(body)
                prompt = (m.group(1) if m else body).strip()
                if first == "cursor-session" and prompt:
                    first = prompt.splitlines()[0][:120]
                blocks.append(f"### user\n\n{prompt}")
            elif role == "assistant":
                blocks.append(f"### assistant\n\n{body}")
    title = first
    md = [
        "---",
        f'title: "Cursor raw · {title[:80]}"',
        "type: session",
        "status: archived",
        "tags:",
        "  - type/session",
        "  - status/archived",
        'host: "cursor-raw"',
        f'source_path: "{path}"',
        "memo_raw_extract: true",
        "---",
        "",
        f"# Cursor raw · {title[:80]}",
        "",
        f"Source: `{path}`",
        "",
        "\n\n---\n\n".join(blocks) if blocks else "_empty_",
        "",
    ]
    return "\n".join(md), title

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"cursor raw: {SRC} does not exist. Exiting cleanly.")
        return 0

    jsonl_files = [
        jl for jl in SRC.rglob("*.jsonl")
        if "agent-transcripts" in jl.parts
        and "subagents" not in jl.parts
        and jl.parent.name == jl.stem
    ]

    if not jsonl_files:
        print(f"cursor raw: no transcripts found in {SRC}. Exiting cleanly.")
        return 0

    n = 0
    for jl in jsonl_files:
        proj = slug_project(jl.parts[jl.parts.index("projects") + 1])
        md, title = extract(jl)
        dest_dir = OUT / proj
        dest = dest_dir / f"{english_slug(title)}.md"
        n += 1
        if args.dry_run:
            print(" would", dest.relative_to(OUT))
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        # disambiguate
        i = 2
        while dest.exists() and dest.read_text(encoding="utf-8", errors="ignore")[:200] != md[:200]:
            dest = dest_dir / f"{english_slug(title)}-{i}.md"
            i += 1
            if i > 30:
                dest = dest_dir / f"{jl.stem[:8]}.md"
                break
        dest.write_text(md, encoding="utf-8")
    print(f"cursor raw: wrote {n} files to {OUT}")
    return 0

if __name__ == "__main__":
    main()
