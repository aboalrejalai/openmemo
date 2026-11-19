#!/usr/bin/env python3
"""
Memo Local Memory Engine — command-line interface.

A thin CLI over `memo_core`, which the MCP server shares. Every behavioural
fix therefore lands in both surfaces at once.

Usage:
  python3 scripts/memory_engine.py index [--purge]
  python3 scripts/memory_engine.py search "Acme Corp budget" [--limit 5]
  python3 scripts/memory_engine.py timeline [--since YYYY-MM-DD] [--until YYYY-MM-DD]
  python3 scripts/memory_engine.py entity "demo-partner"
  python3 scripts/memory_engine.py add-event --date 2026-09-05 --category System --summary "..." [--force]
  python3 scripts/memory_engine.py files [--subdir clients]
  python3 scripts/memory_engine.py status
"""

from __future__ import annotations

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memo_core as core
from memo_core import MemoError


def cmd_index(args: argparse.Namespace) -> None:
    stats = core.sync_index()
    print(f"Indexing complete: {stats['added']} new, {stats['updated']} updated, "
          f"{stats['purged']} purged, {stats['total']} total files.")


def cmd_search(args: argparse.Namespace) -> None:
    strategy, results = core.search(args.query, args.limit)
    if not results:
        print(f"No results for '{args.query}'.")
        return
    print(f"\nFound {len(results)} match(es) for '{args.query}' "
          f"(strategy: {strategy})\n")
    for i, r in enumerate(results, 1):
        print(f"{i}. [{r['title']}] ({r['path']})")
        print(f"   Excerpt: {r['snippet']}")
        print(f"   Link: [[{r['wikilink']}]]\n")


def cmd_timeline(args: argparse.Namespace) -> None:
    events = core.parse_timeline()
    if args.since:
        events = [e for e in events if e["date"] >= args.since]
    if args.until:
        events = [e for e in events if e["date"] <= args.until]
    print(f"\nChronological Events ({len(events)}):\n")
    for e in events:
        tag = "UPCOMING" if e["section"] == "upcoming" else "HISTORY"
        link = f" -> [[{e['link']}]]" if e["link"] else ""
        cat = f"{e['category']}: " if e["category"] else ""
        print(f"- [{e['date']}] ({tag}) {cat}{e['summary']}{link}")


def cmd_entity(args: argparse.Namespace) -> None:
    matched = core.graph_find(args.name)
    if not matched:
        print(f"No entity matching '{args.name}'.")
        return
    for ent in matched:
        print(f"\nEntity: {ent.get('name')} ({ent.get('name_ar','')}) [{ent.get('type')}]")
        if ent.get("file"):
            print(f"   Associated File: [[{ent['file'].replace('.md','')}]]")
        for label, key, arrow in (("Outgoing", "outgoing", "-->"),
                                  ("Incoming", "incoming", "<--")):
            if ent.get(key):
                print(f"   {label} Connections:")
                for r in ent[key]:
                    other = r["target"] if key == "outgoing" else r["source"]
                    print(f"     {arrow} [{r['relation']}] {other} ({r.get('notes','')})")


def cmd_add_event(args: argparse.Namespace) -> None:
    # Same gateway as the MCP memo_append_fact tool: lock, exact and
    # near-duplicate gates, path-scoped git commits, reindex.
    result = core.append_fact("timeline", args.category, args.summary,
                              date_str=args.date, link=args.link, force=args.force,
                              reason="memory_engine add-event")
    if result.get("near_duplicate"):
        print("Held back: this looks like a rephrased duplicate. Nothing was written.")
        for c in result["candidates"]:
            print(f"  - [{c['date']}] {c['category']}: {c['summary']} "
                  f"(similarity {c['similarity']})")
        print("If it really is a distinct fact, rerun with --force.")
        return
    if result.get("duplicate"):
        print("Skipped: an identical summary already exists in timeline.md.")
        return
    print(f"Appended to timeline.md ({result['placement']}):\n{result['entry']}")


def cmd_files(args: argparse.Namespace) -> None:
    files = core.list_files(args.subdir)
    print(f"\n{len(files)} markdown file(s):\n")
    for f in files:
        print(f"- {f['path']}  —  {f['title']}")


def cmd_status(args: argparse.Namespace) -> None:
    st = core.status()
    print("\nMemo Memory Engine Status:")
    print(f"   Vault:           {st['vault']}")
    print(f"   Markdown files:  {st['markdown_files']}")
    print(f"   Index:           +{st['index']['added']} new / {st['index']['updated']} "
          f"updated / {st['index']['purged']} purged "
          f"({st['index']['db_kb']} KB, trigram="
          f"{'on' if st['index']['trigram'] else 'off'})")
    print(f"   Knowledge graph: {st['graph']['entities']} entities, "
          f"{st['graph']['relationships']} relationships")
    print(f"   Timeline:        {st['timeline_events']} events")
    print(f"   Git snapshots:   {'enabled' if st['git_snapshots'] else 'disabled'}")
    print(f"   SQLite:          {st['sqlite_version']}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Memo Local Memory Engine")
    sp = parser.add_subparsers(dest="command", required=True)

    p = sp.add_parser("index", help="Index all markdown files (purges deleted ones)")
    p.add_argument("--purge", action="store_true",
                   help="Kept for compatibility; purging is always performed.")
    p.set_defaults(func=cmd_index)

    p = sp.add_parser("search", help="Full-text search across the vault")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_search)

    p = sp.add_parser("timeline", help="Inspect timeline events")
    p.add_argument("--since")
    p.add_argument("--until")
    p.set_defaults(func=cmd_timeline)

    p = sp.add_parser("entity", help="Look up an entity in the knowledge graph")
    p.add_argument("name")
    p.set_defaults(func=cmd_entity)

    p = sp.add_parser("add-event", help="Append an event to timeline.md")
    p.add_argument("--date")
    p.add_argument("--category", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--link")
    p.add_argument("--force", action="store_true",
                   help="Write even if a near-duplicate event exists.")
    p.set_defaults(func=cmd_add_event)

    p = sp.add_parser("files", help="List vault markdown files")
    p.add_argument("--subdir")
    p.set_defaults(func=cmd_files)

    p = sp.add_parser("status", help="Show memory engine status")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    try:
        args.func(args)
    except MemoError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
