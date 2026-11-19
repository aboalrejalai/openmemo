#!/usr/bin/env python3
"""Export Gemini CLI transcripts into sessions/_raw-gemini/ as Markdown.

Gemini CLI hooks record session_id, transcript_path, prompt, and prompt_response.
Unlike streaming JSONL hosts, Gemini transcripts are stored as standalone JSON documents
under ~/.gemini/ (e.g. transcripts, sessions, projects, history).

This extractor parses those JSON documents and formats them into clean Markdown files
for the Memo vault. If no transcript files are present, it documents the inspected
paths and exits 0 cleanly.
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
OUT = VAULT / "sessions" / "_raw-gemini"
CANDIDATE_DIRS = [
    Path.home() / ".gemini" / "transcripts",
    Path.home() / ".gemini" / "sessions",
    Path.home() / ".gemini" / "projects",
    Path.home() / ".gemini" / "history",
]


def english_slug(title: str, max_len: int = 72) -> str:
    s = re.sub(r"[\u0600-\u06FF]+", " ", title or "")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len].rstrip("-") or "session")


def project_slug(folder_or_name: str) -> str:
    s = folder_or_name.replace("\\", "/").rstrip("/")
    if "/" in s:
        s = s.split("/")[-1]
    s = re.sub(r"^Users-[^/-]+-(?:Desktop-)?(?:Project-)?", "", s)
    return s or "general"


def extract_gemini_json(path: Path) -> Tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return "", ""

    blocks: List[str] = []
    first_prompt = ""

    # Check for array of turns or object containing messages/history/turns
    items: List[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("turns") or data.get("messages") or data.get("history") or data.get("events") or []
        if not items and ("prompt" in data or "prompt_response" in data):
            items = [data]

    for item in items:
        if not isinstance(item, dict):
            continue

        prompt = item.get("prompt") or item.get("user") or ""
        response = item.get("prompt_response") or item.get("response") or item.get("model") or ""
        role = item.get("role") or item.get("type") or ""
        parts = item.get("parts") or item.get("content") or ""

        # Format user prompt
        if prompt:
            if not first_prompt:
                first_prompt = str(prompt).strip().splitlines()[0][:120]
            blocks.append(f"### user\n\n{prompt}")

        # Format model response
        if response:
            blocks.append(f"### assistant\n\n{response}")

        # If role-based parts exist (Google AI Chat format)
        if parts and role:
            text_val = ""
            if isinstance(parts, list):
                text_parts = []
                for p in parts:
                    if isinstance(p, dict) and "text" in p:
                        text_parts.append(p["text"])
                    elif isinstance(p, str):
                        text_parts.append(p)
                text_val = "\n".join(text_parts)
            elif isinstance(parts, str):
                text_val = parts

            if text_val:
                if role in ("user", "human"):
                    if not first_prompt:
                        first_prompt = text_val.strip().splitlines()[0][:120]
                    blocks.append(f"### user\n\n{text_val}")
                else:
                    blocks.append(f"### assistant\n\n{text_val}")

        # Tool calls if logged
        tool_name = item.get("tool_name")
        if tool_name:
            t_in = item.get("tool_input")
            t_out = item.get("tool_response")
            info = f"**Tool**: `{tool_name}`"
            if t_in:
                info += f"\n```json\n{json.dumps(t_in, indent=2, ensure_ascii=False)}\n```"
            if t_out:
                info += f"\n**Output**:\n```\n{str(t_out)[:2000]}\n```"
            blocks.append(f"### tool · {tool_name}\n\n{info}")

    title = first_prompt or f"gemini-{path.stem[:8]}"
    md = [
        "---",
        f'title: "Gemini raw · {title[:80]}"',
        "type: session",
        "status: archived",
        "tags:",
        "  - type/session",
        "  - status/archived",
        'host: "gemini-raw"',
        f'source_path: "{path}"',
        "memo_raw_extract: true",
        "---",
        "",
        f"# Gemini raw · {title[:80]}",
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
            for f in d.rglob("*.json"):
                if f.is_file():
                    candidates.append(f)

    if not candidates:
        checked = ", ".join(str(p) for p in CANDIDATE_DIRS)
        print(f"gemini raw: no transcripts found in ~/.gemini/ (checked: {checked}). Exiting cleanly.")
        return 0

    if args.limit:
        candidates = candidates[:args.limit]

    n = 0
    for file_path in candidates:
        md, title = extract_gemini_json(file_path)
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

    print(f"gemini raw: wrote {n} files to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
