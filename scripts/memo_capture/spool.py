"""The hook spool: one JSON file per event in <vault>/.memo/spool/.

Hooks only write a file and exit, so a slow or stopped worker never slows a
session. A file is named <kind>-<key>.json, where the key is the tool_use_id
or a hash of the event, so a re-delivered event overwrites its own file
instead of duplicating. The worker drains files oldest first, exactly once
(state.spool_consumed), moving unreadable files to corrupt/. Nothing is ever
expired or deleted before it is ingested: a file waits as long as it takes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .paths import memo_home

KINDS = ("prompt", "observation", "summarize", "session_end", "subagent_stop")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def spool_dir() -> Path:
    path = memo_home() / "spool"
    path.mkdir(parents=True, exist_ok=True)
    return path


def event_key(kind: str, payload: Dict[str, Any]) -> str:
    tool_use_id = payload.get("tool_use_id")
    if tool_use_id:
        return _SAFE.sub("_", str(tool_use_id))[:96]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    session = str(payload.get("content_session_id", ""))
    return hashlib.sha256(f"{kind}\0{session}\0{canonical}".encode()).hexdigest()[:40]


_last_ns = 0


def write_event(kind: str, payload: Dict[str, Any], at_ns: Optional[int] = None) -> Path:
    """Atomically write one event. Returns the file path. `at_ns` places a
    recovered event at its original time in the drain order."""
    if kind not in KINDS:
        raise ValueError(f"unknown spool kind '{kind}'")
    key = event_key(kind, payload)
    global _last_ns
    if at_ns:
        now_ns = at_ns
    else:
        # Windows' clock ticks every ~15 ms, so two events in a row could share a
        # timestamp and drain out of order; keep this process's events strictly ordered.
        now_ns = max(time.time_ns(), _last_ns + 1)
        _last_ns = now_ns
    body = {"kind": kind, "key": f"{kind}-{key}", "enqueued_at_ms": now_ns // 1_000_000,
            "enqueued_at_ns": now_ns, "payload": payload}
    directory = spool_dir()
    target = directory / f"{kind}-{key}.json"
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, default=str)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def _move(path: Path, folder: str) -> None:
    dest = spool_dir() / folder
    dest.mkdir(exist_ok=True)
    try:
        os.replace(path, dest / path.name)
    except OSError:
        pass


def pending_files() -> List[Tuple[int, Path, Optional[Dict[str, Any]]]]:
    """(enqueued_at_ns, path, body or None if unreadable), oldest first."""
    out = []
    for path in spool_dir().glob("*.json"):
        if path.name.startswith(".tmp-"):
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(body, dict) or "payload" not in body:
                raise ValueError("not a spool body")
            ns = body.get("enqueued_at_ns") or int(body.get("enqueued_at_ms", 0)) * 1_000_000
            out.append((int(ns), path, body))
        except (OSError, ValueError):
            out.append((0, path, None))
    # Same millisecond: a prompt comes before the tool events and the summary of its turn.
    order = {kind: i for i, kind in enumerate(KINDS)}
    out.sort(key=lambda item: (item[0], order.get(item[1].name.split("-", 1)[0], len(KINDS)), item[1].name))
    return out


def drain(conn, ingest: Callable[[str, Dict[str, Any]], None], expire_days: int = 0) -> Dict[str, int]:
    """Feed every spooled event to `ingest(kind, payload)` exactly once.

    `expire_days` is accepted for old callers and ignored: an event is kept
    until it is ingested, however old it is (owner rule: never lose memory).
    """
    from . import state
    stats = {"ingested": 0, "duplicate": 0, "corrupt": 0, "expired": 0, "failed": 0}
    for enqueued, path, body in pending_files():
        if body is None:
            _move(path, "corrupt")
            stats["corrupt"] += 1
            continue
        key = str(body.get("key"))
        if state.spool_seen(conn, key):
            path.unlink(missing_ok=True)
            stats["duplicate"] += 1
            continue
        try:
            with state.transaction(conn):     # queued and marked together, or neither
                ingest(body.get("kind", ""), body["payload"])
                state.mark_spool_consumed(conn, key)
        except Exception as exc:
            stats["failed"] += 1   # stays on disk; the next drain retries it
            stats.setdefault("error", f"{type(exc).__name__}: {exc}"[:300])
            continue
        path.unlink(missing_ok=True)   # only after the event is safely in the queue
        stats["ingested"] += 1
    return stats
