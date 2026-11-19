#!/usr/bin/env python3
"""Export Codex CLI transcripts into sessions/_raw-codex/ as Markdown.

Codex CLI stores session history, transcripts, and event payloads under ~/.codex/.
This extractor inspects known storage locations (sessions, transcripts, history, projects)
and converts JSON / JSONL transcripts into clean Markdown dossiers for the Memo vault.

If no durable transcripts are found on disk, it documents the inspected paths and exits 0 cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VAULT = Path(
    os.environ.get("MEMO_VAULT")
    or os.environ.get("OPENMEMO_HOME")
    or str(Path(__file__).resolve().parents[4])
)
OUT = VAULT / "sessions" / "_raw-codex"
CANDIDATE_DIRS = [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "transcripts",
    Path.home() / ".codex" / "history",
    Path.home() / ".codex" / "projects",
]


def english_slug(title: str, max_len: int = 72) -> str:
    s = re.sub(r"[\u0600-\u06FF]+", " ", title or "")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len].rstrip("-") or "session")


def project_slug(cwd_or_path: str) -> str:
    s = cwd_or_path.replace("\\", "/").rstrip("/")
    if "/" in s:
        s = s.split("/")[-1]
    s = re.sub(r"^Users-[^/-]+-(?:Desktop-)?(?:Project-)?", "", s)
    return s or "general"


def extract_from_jsonl(path: Path) -> Tuple[str, str]:
    blocks: List[str] = []
    first_prompt = ""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue

            role = obj.get("role") or obj.get("type") or "event"
            prompt = obj.get("prompt") or obj.get("input") or ""
            assistant_msg = obj.get("last_assistant_message") or obj.get("response") or obj.get("content") or ""
            tool_name = obj.get("tool_name")
            tool_input = obj.get("tool_input")
            tool_response = obj.get("tool_response")

            if prompt:
                if not first_prompt:
                    first_prompt = str(prompt).strip().splitlines()[0][:120]
                blocks.append(f"### user\n\n{prompt}")

            if assistant_msg:
                if isinstance(assistant_msg, list):
                    text_parts = [p.get("text", "") for p in assistant_msg if isinstance(p, dict)]
                    assistant_msg = "\n".join(text_parts)
                blocks.append(f"### assistant\n\n{assistant_msg}")

            if tool_name:
                call_info = f"**Tool**: `{tool_name}`"
                if tool_input:
                    call_info += f"\n```json\n{json.dumps(tool_input, indent=2, ensure_ascii=False)}\n```"
                if tool_response:
                    call_info += f"\n**Response**:\n```\n{str(tool_response)[:2000]}\n```"
                blocks.append(f"### tool · {tool_name}\n\n{call_info}")

    title = first_prompt or f"codex-{path.stem[:8]}"
    md = [
        "---",
        f'title: "Codex raw · {title[:80]}"',
        "type: session",
        "status: archived",
        "tags:",
        "  - type/session",
        "  - status/archived",
        'host: "codex-raw"',
        f'source_path: "{path}"',
        "memo_raw_extract: true",
        "---",
        "",
        f"# Codex raw · {title[:80]}",
        "",
        f"Source: `{path}`",
        "",
        "\n\n---\n\n".join(blocks) if blocks else "_empty_",
        "",
    ]
    return "\n".join(md), title


def extract_from_json(path: Path) -> Tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return "", ""

    blocks: List[str] = []
    first_prompt = ""

    items = data if isinstance(data, list) else data.get("messages") or data.get("turns") or data.get("history") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role") or "event"
            content = item.get("content") or item.get("prompt") or item.get("text") or ""
            if isinstance(content, list):
                content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            if content:
                if role == "user" and not first_prompt:
                    first_prompt = str(content).strip().splitlines()[0][:120]
                blocks.append(f"### {role}\n\n{content}")
    else:
        # Fallback to general object dump
        blocks.append(f"```json\n{json.dumps(data, indent=2, ensure_ascii=False)[:4000]}\n```")

    title = first_prompt or f"codex-{path.stem[:8]}"
    md = [
        "---",
        f'title: "Codex raw · {title[:80]}"',
        "type: session",
        "status: archived",
        "tags:",
        "  - type/session",
        "  - status/archived",
        'host: "codex-raw"',
        f'source_path: "{path}"',
        "memo_raw_extract: true",
        "---",
        "",
        f"# Codex raw · {title[:80]}",
        "",
        f"Source: `{path}`",
        "",
        "\n\n---\n\n".join(blocks) if blocks else "_empty_",
        "",
    ]
    return "\n".join(md), title


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    candidates: List[Path] = []
    for d in CANDIDATE_DIRS:
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file() and f.suffix in (".json", ".jsonl"):
                    candidates.append(f)

    if not candidates:
        checked = ", ".join(str(p) for p in CANDIDATE_DIRS)
        print(f"codex raw: no transcripts found in ~/.codex/ (checked: {checked}). Exiting cleanly.")
        return 0

    if args.limit:
        candidates = candidates[:args.limit]

    n = 0
    for file_path in candidates:
        if file_path.suffix == ".jsonl":
            md, title = extract_from_jsonl(file_path)
        else:
            md, title = extract_from_json(file_path)

        if not md or md.rstrip().endswith("_empty_"):
            continue

        proj = project_slug(file_path.parent.name)
        dest_dir = OUT / proj
        dest = dest_dir / f"{english_slug(title)}.md"
        n += 1
        if args.dry_run:
            print(" would", dest.relative_to(OUT))
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        i = 2
        while dest.exists():
            dest = dest_dir / f"{english_slug(title)}-{i}.md"
            i += 1
            if i > 30:
                dest = dest_dir / f"{file_path.stem[:8]}.md"
                break
        dest.write_text(md, encoding="utf-8")

    print(f"codex raw: wrote {n} files to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
