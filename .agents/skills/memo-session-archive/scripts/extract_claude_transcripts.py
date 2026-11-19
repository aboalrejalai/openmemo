#!/usr/bin/env python3
"""Extract every text string from Claude Code ~/.claude/projects JSONL into Markdown.

Keeps every text character found in transcript payloads (user/assistant text blocks,
tool_result strings that are plain text, thinking, prompts). Skips binary/base64
image payloads and raw api-request-blob bodies.

Default output:
  <vault>/sessions/_raw-claude/<project-slug>/<session>.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VAULT_DIR = Path(
    os.environ.get("MEMO_VAULT")
    or os.environ.get("OPENMEMO_HOME")
    or str(Path(__file__).resolve().parents[4])
)
DEFAULT_SRC = Path.home() / ".claude" / "projects"
DEFAULT_OUT = VAULT_DIR / "sessions" / "_raw-claude"

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def project_slug(folder_name: str) -> str:
    name = folder_name.lstrip("-")
    name = re.sub(r"^Users-[^/-]+-(?:Desktop-)?(?:Project-)?", "", name)
    name = name.replace("--claude-worktrees-", "/wt/")
    return name or "unknown"


def custom_title_for(jsonl: Path) -> Optional[str]:
    """Claude Code writes <session-uuid>/custom-title.json beside the jsonl."""
    sidecar = jsonl.parent / jsonl.stem / "custom-title.json"
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    title = data.get("customTitle") or data.get("title") or ""
    return str(title).strip() or None


def english_slug(title: str, max_len: int = 72) -> str:
    """ASCII kebab slug for Obsidian Graph."""
    s = (title or "").strip().splitlines()[0]
    s = re.sub(r"[\u0600-\u06FF]+", " ", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len].rstrip("-") or "session")


def human_stem(path: Path, topic: Optional[str] = None) -> str:
    title = topic or custom_title_for(path)
    if title:
        return english_slug(title)
    return session_stem(path)


def session_stem(path: Path) -> str:
    stem = path.stem
    if stem.startswith("agent-"):
        return stem
    if UUID_RE.match(stem):
        return stem[:8]
    return re.sub(r"[^\w.-]+", "-", stem)[:80] or "session"


def looks_binary_or_b64(s: str) -> bool:
    if not s:
        return False
    if s.startswith("data:image") or "base64," in s[:80]:
        return True
    if len(s) > 4000 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s[:2000] or ""):
        return True
    return False


def collect_text(node: Any, out: List[str]) -> None:
    if node is None:
        return
    if isinstance(node, str):
        if node and not looks_binary_or_b64(node):
            out.append(node)
        return
    if isinstance(node, list):
        for item in node:
            collect_text(item, out)
        return
    if isinstance(node, dict):
        typ = str(node.get("type") or "")
        if typ in ("image", "image_url", "input_image", "api-request-blob"):
            return
        if "base64" in node or node.get("media_type") in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            if "text" in node and isinstance(node["text"], str):
                collect_text(node["text"], out)
            return
        for key in ("text", "content", "prompt", "thinking", "summary", "result", "output"):
            if key in node:
                collect_text(node[key], out)
        for key, val in node.items():
            if key in ("text", "content", "prompt", "thinking", "summary", "result", "output",
                       "base64", "source", "data", "blob", "bytes"):
                continue
            if isinstance(val, (dict, list)):
                collect_text(val, out)


def line_role(obj: dict) -> str:
    if obj.get("type") in ("user", "assistant", "system"):
        return str(obj.get("type"))
    msg = obj.get("message")
    if isinstance(msg, dict) and msg.get("role"):
        return str(msg.get("role"))
    return str(obj.get("type") or obj.get("role") or "event")


def extract_file(path: Path) -> Tuple[str, Dict[str, Any]]:
    blocks: List[str] = []
    chars = 0
    lines_in = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            lines_in += 1
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") in ("api-request-blob", "api-request-shape", "file-history-snapshot",
                                   "atis-latch", "frame-link", "mode", "agent-name"):
                continue
            texts: List[str] = []
            msg = obj.get("message")
            if isinstance(msg, dict):
                collect_text(msg.get("content"), texts)
                if msg.get("thinking"):
                    collect_text(msg.get("thinking"), texts)
            if "toolUseResult" in obj:
                collect_text(obj.get("toolUseResult"), texts)
            if obj.get("type") == "user" and not msg:
                collect_text(obj.get("content"), texts)
            if obj.get("type") in ("custom-title", "last-prompt"):
                collect_text(obj.get("title") or obj.get("prompt") or obj.get("content"), texts)

            cleaned = []
            for t in texts:
                t = t.replace("\x00", "")
                if not t.strip():
                    continue
                if cleaned and cleaned[-1] == t:
                    continue
                cleaned.append(t)
            if not cleaned:
                continue
            role = line_role(obj)
            ts = obj.get("timestamp") or ""
            body = "\n\n".join(cleaned)
            chars += len(body)
            header = f"### {role}"
            if ts:
                header += f" · {ts}"
            blocks.append(header + "\n\n" + body)

    meta = {
        "source": str(path),
        "lines_in": lines_in,
        "blocks": len(blocks),
        "text_chars": chars,
        "source_bytes": path.stat().st_size,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    ct = custom_title_for(path)
    title = ct or (path.stem[:8] if UUID_RE.match(path.stem) else path.stem)
    md = [
        "---",
        f'title: "Claude raw · {title}"',
        "type: session",
        "status: archived",
        "tags:",
        "  - type/session",
        "  - status/archived",
        'host: "claude-code-raw"',
        f'source_path: "{path}"',
        f"source_bytes: {meta['source_bytes']}",
        f"text_chars: {chars}",
        "memo_raw_extract: true",
        "---",
        "",
        f"# Claude raw transcript · `{title}`",
        "",
        f"Source: `{path}`",
        "",
        "<!-- Extracted text only. Original JSONL kept in ~/.claude/projects. -->",
        "",
    ]
    md.append("\n\n---\n\n".join(blocks) if blocks else "_No extractable text._\n")
    return "\n".join(md) + "\n", meta


def iter_transcripts(src: Path, project_filter: Optional[str], include_agents: bool = False) -> Iterable[Path]:
    if not src.exists():
        return
    for folder in sorted(src.iterdir()):
        if not folder.is_dir() or "memo-observer" in folder.name:
            continue
        slug = project_slug(folder.name)
        if project_filter and project_filter not in slug and project_filter not in folder.name:
            continue
        for path in sorted(folder.glob("*.jsonl")):
            yield path
        if include_agents:
            for path in sorted(folder.rglob("subagents/**/*.jsonl")):
                yield path


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--project", default="", help="Substring filter on project slug")
    ap.add_argument("--limit", type=int, default=0, help="Max files to write (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-agents", action="store_true",
                    help="Also extract agent-*.jsonl subagent transcripts")
    args = ap.parse_args(argv)

    if not args.src.exists():
        print(f"claude raw: directory {args.src} does not exist. Exiting cleanly.")
        return 0

    files = list(iter_transcripts(args.src, args.project or None, include_agents=args.include_agents))
    if not files:
        print(f"claude raw: no jsonl files found in {args.src}. Exiting cleanly.")
        return 0

    total_src = sum(p.stat().st_size for p in files)
    print(f"candidates: {len(files)} jsonl files, {total_src/1e9:.2f} GB raw")

    written = 0
    text_chars = 0
    out_bytes = 0
    for path in files:
        if args.limit and written >= args.limit:
            break
        try:
            rel = path.relative_to(args.src)
            proj_folder = rel.parts[0]
        except ValueError:
            proj_folder = path.parent.name
        slug = project_slug(proj_folder)
        if "subagents" in path.parts:
            parts = path.parts
            i = parts.index("subagents")
            parent_sid = parts[i - 1] if i > 0 else "unknown"
            parent8 = parent_sid[:8] if UUID_RE.match(parent_sid) else parent_sid[:24]
            dest_dir = args.out / slug / parent8 / "subagents"
        else:
            dest_dir = args.out / slug
        dest = dest_dir / f"{human_stem(path)}.md"
        md, meta = extract_file(path)
        text_chars += meta["text_chars"]
        out_bytes += len(md.encode("utf-8"))
        written += 1
        if args.dry_run:
            if written <= 8 or written % 500 == 0:
                print(f"  would write {dest.relative_to(args.out)}")
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(md, encoding="utf-8")
        dest.with_suffix(".extract.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(f"{'dry-run ' if args.dry_run else ''}claude raw: wrote {written} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
