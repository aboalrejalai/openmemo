#!/usr/bin/env python3
"""Export Antigravity conversation SQLite DBs into sessions/_raw-antigravity/ Markdown.

Antigravity stores step_payload as protobuf (not JSON). We harvest readable UTF-8
string islands from those blobs. Original DBs are never deleted.
"""
from __future__ import annotations
import argparse, os, re, sqlite3, shutil, tempfile
from pathlib import Path

VAULT = Path(
    os.environ.get("MEMO_VAULT")
    or os.environ.get("OPENMEMO_HOME")
    or str(Path(__file__).resolve().parents[4])
)
OUT = VAULT / "sessions" / "_raw-antigravity"
SOURCES = [
    Path.home() / ".gemini" / "antigravity" / "conversations",
    Path.home() / ".gemini" / "antigravity-cli" / "conversations",
]

STEP_LABEL = {0: "step", 1: "user", 2: "assistant", 3: "tool", 4: "system"}
# Printable runs: Latin + Arabic + common punctuation, length >= 40
RUN = re.compile(
    r"(?:[\u0600-\u06FF]|[A-Za-z0-9])"
    r"(?:[\u0600-\u06FFA-Za-z0-9 \t\n.,;:!?\"'()\[\]{}/_#@%&*+=\-<>`~|\\]){39,}"
)

def english_slug(title: str, max_len: int = 72) -> str:
    s = re.sub(r"[\u0600-\u06FF]+", " ", title or "")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len].rstrip("-") or "session")

def harvest(blob) -> list[str]:
    if blob is None:
        return []
    if isinstance(blob, str):
        data = blob.encode("utf-8", errors="ignore")
    elif isinstance(blob, (bytes, bytearray)):
        data = bytes(blob)
    else:
        return []
    txt = data.decode("utf-8", errors="ignore")
    out = []
    seen = set()
    for m in RUN.finditer(txt):
        s = m.group().strip()
        # drop pure noise / paths that dominate
        if s.count("\x00"):
            continue
        key = s[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:6000])
        if len(out) >= 40:
            break
    return out

def extract_db(db_path: Path) -> tuple[str, str]:
    td = Path(tempfile.mkdtemp(prefix="agy-raw-"))
    tmp = td / "c.db"
    try:
        shutil.copy2(db_path, tmp)
        for side in (db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
            if side.exists():
                try:
                    shutil.copy2(side, td / side.name)
                except Exception:
                    pass
        blocks = []
        first = f"antigravity-{db_path.stem[:8]}"
        con = sqlite3.connect(str(tmp))
        rows = con.execute(
            "SELECT idx, step_type, step_payload FROM steps ORDER BY idx"
        ).fetchall()
        for idx, step_type, payload in rows:
            parts = harvest(payload)
            if not parts:
                continue
            body = "\n\n".join(parts).strip()
            label = STEP_LABEL.get(step_type, f"type-{step_type}")
            if first.startswith("antigravity-") and body:
                skip_pfx = (
                    "The following system", "JThe following", "IThe following",
                    "SessionStart", "system message was injected",
                )
                for line in body.splitlines():
                    line = line.strip().lstrip("JIji")
                    if len(line) < 24:
                        continue
                    if any(line.startswith(s) or s.lower() in line[:80].lower() for s in skip_pfx):
                        continue
                    if line.startswith("/") or line.startswith("http"):
                        continue
                    first = line[:120]
                    break
            blocks.append(f"### {label} · {idx}\n\n{body}")
        con.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)
    title = first
    md = "\n".join([
        "---",
        f'title: "Antigravity raw · {title[:80]}"',
        "type: session",
        "status: archived",
        "tags:",
        "  - type/session",
        "  - status/archived",
        'host: "antigravity-raw"',
        f'source_path: "{db_path}"',
        "memo_raw_extract: true",
        "---",
        "",
        f"# Antigravity raw · {title[:80]}",
        "",
        f"Source: `{db_path}`",
        "",
        "\n\n---\n\n".join(blocks) if blocks else "_empty_",
        "",
    ])
    return md, title

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    dbs = []
    for src in SOURCES:
        if src.exists():
            dbs.extend(sorted(src.glob("*.db")))
    if not dbs:
        print("antigravity raw: no conversation databases found. Exiting cleanly.")
        return 0
    if args.limit:
        dbs = dbs[: args.limit]
    n = skipped = 0
    for db in dbs:
        if db.stat().st_size < 1024:
            skipped += 1
            continue
        try:
            md, title = extract_db(db)
        except Exception as e:
            print("skip", db.name, e)
            skipped += 1
            continue
        if md.rstrip().endswith("_empty_"):
            skipped += 1
            continue
        host = db.parent.parent.name
        dest_dir = OUT / host
        dest = dest_dir / f"{english_slug(title)}.md"
        n += 1
        if args.dry_run:
            print(" would", host, dest.name, "bytes", len(md.encode()))
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        i = 2
        while dest.exists():
            dest = dest_dir / f"{english_slug(title)}-{i}.md"
            i += 1
            if i > 40:
                dest = dest_dir / f"{db.stem[:8]}.md"
                break
        dest.write_text(md, encoding="utf-8")
    print(f"antigravity raw: wrote={n} skipped={skipped} → {OUT}")
    return 0

if __name__ == "__main__":
    main()
