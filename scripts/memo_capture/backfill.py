"""`memo backfill --since <time>`: recover prompts and summaries the hooks dropped.

From 2026-11-02 23:26 to 2026-11-05 the index was locked, so the prompt and
summary hooks (which then wrote to it directly) failed, while tool events
still reached the spool. Claude Code keeps every session's transcript, so the
missing prompts and each turn's reply can be read back and spooled at their
original times. Draining the spool oldest first then rebuilds each request
with its own tool events and summary, exactly as if nothing had failed.

Read-only towards the transcripts; it only writes spool files. Safe to run
twice: an event already spooled has the same file name, and prompts the index
already holds are skipped.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from . import privacy, project, settings, spool, state
from .adapters import NormalizedInput
from .paths import observer_home
from .transcript import _is_real_prompt, _message, _role, _text_of


def _ms(stamp: str) -> Optional[int]:
    try:
        return int(datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
    except (AttributeError, ValueError):
        return None


def turns(path: Path) -> Iterator[Dict]:
    """Each real prompt with its time, cwd, and the reply text and end time of its turn."""
    current: Optional[Dict] = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(line, dict) or line.get("isSidechain"):
                continue
            at = _ms(line.get("timestamp") or "")
            if line.get("type") == "user" and not line.get("isMeta") and _is_real_prompt(line):
                if current:
                    yield current
                current = {"at": at, "cwd": line.get("cwd") or "", "prompt": _text_of(_message(line)["content"]),
                           "texts": [], "end": at, "model": None}
            elif current is not None and _role(line) == "assistant":
                msg = _message(line)
                text = _text_of(msg.get("content"))
                if text:
                    current["texts"].append(text)
                current["model"] = current["model"] or msg.get("model")
                current["end"] = at or current["end"]
    if current:
        yield current


def _spooled_prompt_times(sid: str) -> List[int]:
    out = []
    for path in spool.spool_dir().glob("prompt-*.json"):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if body.get("payload", {}).get("content_session_id") == sid:
            out.append(int(body["payload"].get("submitted_at_ms") or body.get("enqueued_at_ms") or 0))
    return out


def backfill_transcript(conn, path: Path, since_ms: int, until_ms: int) -> Dict[str, int]:
    sid = path.stem
    row = conn.execute("SELECT MAX(created_at) FROM user_prompts WHERE content_session_id = ?", (sid,)).fetchone()
    start = max(since_ms, int(row[0] or 0) + 1)
    live = [t for t in _spooled_prompt_times(sid) if t >= since_ms]
    end = min([until_ms] + live)          # prompts from here on are already in the spool
    session = state.get_session(conn, sid)
    counts = {"prompts": 0, "summaries": 0}
    for turn in turns(path):
        if turn["at"] is None or not (start <= turn["at"] < end):
            continue
        text = turn["prompt"] or ""
        if privacy.is_task_notification_only(text):
            continue
        cwd = (session["cwd"] if session and session["cwd"] else "") or turn["cwd"]
        proj = project.resolve(cwd)
        if proj.name in settings.csv("excluded_projects"):
            continue
        inp = NormalizedInput(host="claude-code", event="session-init", session_id=sid, cwd=cwd, prompt=text,
                              transcript_path=str(path))
        from .session_init import prompt_payload
        spool.write_event("prompt", prompt_payload(inp, proj, turn["at"]), at_ns=turn["at"] * 1_000_000)
        counts["prompts"] += 1
        reply = "\n\n".join(turn["texts"]).strip()
        if reply:
            stop = max(turn["end"] or turn["at"], turn["at"] + 1)
            payload = {"content_session_id": sid, "host": "claude-code", "cwd": cwd, "project": proj.name,
                       "project_key_source": proj.source, "agent_id": None, "agent_type": None,
                       "transcript_path": str(path), "last_assistant_message": privacy.clean_text(reply, 32_000),
                       "observed_model": turn["model"], "stopped_at_ms": stop}
            spool.write_event("summarize", payload, at_ns=stop * 1_000_000)
            counts["summaries"] += 1
    return counts


def transcripts(since_ms: int, root: Optional[Path] = None) -> List[Path]:
    root = root or Path.home() / ".claude" / "projects"
    skip = str(observer_home()).replace("/", "-").replace(".", "-")
    out = []
    for folder in root.iterdir() if root.exists() else []:
        if not folder.is_dir() or "memo-observer" in folder.name or folder.name.startswith(skip):
            continue
        for path in folder.glob("*.jsonl"):
            if path.stat().st_mtime * 1000 >= since_ms:
                out.append(path)
    return sorted(out)


def main(args: List[str]) -> int:
    if "--since" not in args:
        print("usage: memo backfill --since 2026-11-02T23:26 [--until <time>] [--dry-run]")
        return 2
    since = int(datetime.datetime.fromisoformat(args[args.index("--since") + 1]).timestamp() * 1000)
    until = (int(datetime.datetime.fromisoformat(args[args.index("--until") + 1]).timestamp() * 1000)
             if "--until" in args else state.now_ms())
    conn = state.connect()
    total = {"transcripts": 0, "prompts": 0, "summaries": 0}
    for path in transcripts(since):
        if "--dry-run" in args:
            n = sum(1 for t in turns(path) if t["at"] and since <= t["at"] < until)
            print(f"  {path.parent.name}/{path.name}: {n} prompt(s) in range")
            continue
        counts = backfill_transcript(conn, path, since, until)
        if counts["prompts"]:
            total["transcripts"] += 1
            total["prompts"] += counts["prompts"]
            total["summaries"] += counts["summaries"]
            print(f"  {path.parent.name}/{path.stem[:8]}: {counts['prompts']} prompt(s), "
                  f"{counts['summaries']} summary(ies)")
    print(f"Spooled {total['prompts']} prompt(s) and {total['summaries']} summary(ies) "
          f"from {total['transcripts']} transcript(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
