#!/usr/bin/env python3
"""Strip bloat from raw transcript Markdown without deleting substantive text.

Removes: ANSI escapes, bidi/RTL marks, NUL/control chars, huge base64 blobs,
repeated box-drawing-only lines. Keeps all normal prose (Arabic or English).
"""
from __future__ import annotations
import argparse, re
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BIDI = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
B64 = re.compile(r"(?:data:image\/[a-zA-Z0-9.+-]+;base64,)?[A-Za-z0-9+/]{400,}={0,2}")
LINKS_LINE = re.compile(r"^Links:\s*\[.{200,}\]\s*$")
BOX_ONLY = re.compile(r"^[\s\u2500-\u257F\u2580-\u259F─│┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬]+$")

def clean(text: str) -> str:
    text = ANSI.sub("", text)
    text = BIDI.sub("", text)
    text = CTRL.sub("", text)
    text = B64.sub("[omitted-binary-or-base64]", text)
    # Collapse huge search-result dumps embedded as single lines
    text = "\n".join(
        ("Links: [omitted-long-link-dump]" if LINKS_LINE.match(ln) else ln)
        for ln in text.splitlines()
    )
    lines = []
    prev = None
    for line in text.splitlines():
        if BOX_ONLY.match(line):
            continue
        if line == prev and len(line) > 120:
            continue  # drop exact long duplicate lines
        lines.append(line)
        prev = line
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    files = list(args.root.rglob("*.md")) if args.root.exists() else []
    saved = 0
    before = after = 0
    for p in files:
        raw = p.read_text(encoding="utf-8", errors="replace")
        out = clean(raw)
        before += len(raw.encode())
        after += len(out.encode())
        if out != raw and not args.dry_run:
            p.write_text(out, encoding="utf-8")
            saved += 1
    print(f"files={len(files)} rewritten={saved} bytes {before} → {after} ({(before-after)/1e6:.1f} MB saved)")

if __name__ == "__main__":
    main()
