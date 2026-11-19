"""Is capture keeping up? Cheap checks a hook can run on every session start.

2026-11-02 → 05: recording was stuck for three days and nothing said so. Now
the spool's oldest waiting event and the worker's own health record decide a
one-line warning that the next session shows.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from typing import Dict, Optional, Tuple

from .paths import memo_home

STUCK_AFTER_SEC = 600            # an event waiting longer than this means recording is behind


def health_file():
    return memo_home() / "health.json"


def spool_backlog() -> Tuple[int, Optional[float]]:
    """(waiting events, epoch seconds of the oldest), from file times only."""
    folder = memo_home() / "spool"
    count, oldest = 0, None
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if not entry.is_file() or not entry.name.endswith(".json") or entry.name.startswith(".tmp-"):
                    continue
                count += 1
                mtime = entry.stat().st_mtime
                oldest = mtime if oldest is None or mtime < oldest else oldest
    except OSError:
        pass
    return count, oldest


def write(record: Dict) -> None:
    try:
        tmp = health_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(record))
        os.replace(tmp, health_file())
    except OSError:
        pass


def read() -> Dict:
    try:
        return json.loads(health_file().read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _when(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def warning(now: Optional[float] = None, db_error: str = "") -> Optional[str]:
    """One line for the owner when recording is behind or broken, else None."""
    now = now or time.time()
    parts = []
    count, oldest = spool_backlog()
    if count and oldest is not None and now - oldest > STUCK_AFTER_SEC:
        parts.append(f"{count} event(s) waiting since {_when(oldest)}")
    record = read()
    failing = record.get("failing_since") if _alive(record.get("pid")) else None   # a dead worker's record is stale
    if failing and now - failing / 1000 > STUCK_AFTER_SEC:
        parts.append(f"the worker cannot write since {_when(failing / 1000)} ({record.get('last_error', '')[:80]})")
    if db_error:
        parts.append(f"the memory index cannot be opened ({db_error[:80]})")
    if not parts:
        return None
    return ("⚠️ Memo is not recording: " + "; ".join(parts) +
            ". Nothing is lost (events stay in .memo/spool). Run `${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo doctor`.")



FAILING_WARN_AFTER_MS = 30 * 60 * 1000  # 30 minutes


def sync_warning(now_ms: Optional[int] = None) -> Optional[str]:
    """One ≤300-char line about sync trouble for session start / status (REQ-003)."""
    from . import settings, sync as sync_mod
    import socket
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    triggers = []

    st = sync_mod.read_state()
    status = st.get("status")
    reason = st.get("reason") or ""
    paths = st.get("conflict_paths") or []
    stuck = st.get("stuck_machines") or []
    held = st.get("held_sha")
    adopt_errors = st.get("adopt_errors") or []
    failing_since = st.get("failing_since")
    last_attempt = st.get("last_attempt") or now_ms

    if status == "conflict" or st.get("resolving"):
        shown = ", ".join(paths[:3]) if paths else reason or "conflict"
        triggers.append((
            f"conflict at {shown}",
            "commits are kept here and on sync/" + settings.effective_name(),
            "run memo sync resolve",
        ))
    if stuck:
        triggers.append((
            f"stuck machine(s): {', '.join(stuck[:3])}",
            "their commits are on GitHub",
            f"run memo sync resolve on {stuck[0]}",
        ))
    if status == "skipped" and reason.startswith("code-change"):
        triggers.append((
            "incoming code change held",
            "nothing was merged",
            f"review {held}, then memo sync now --accept-code {held}" if held else "review held commit",
        ))
    if adopt_errors:
        path = ""
        for e in adopt_errors:
            if isinstance(e, dict) and e.get("path"):
                path = e["path"]
                break
        triggers.append((
            "adopt error(s) in the index",
            "the rows stay readable in their session notes",
            f"check machine.json on the machine that wrote {path}" if path else "check machine.json",
        ))

    # machine.json issues
    mj = settings.machine_path()
    malformed = settings.machine_json_malformed()
    missing = not mj.exists()
    host_mismatch = False
    if mj.exists() and not malformed:
        host = settings.get("machine_host") or ""
        if host and host != socket.gethostname():
            host_mismatch = True
    if missing or malformed or host_mismatch:
        what = "machine.json missing" if missing else (
            "machine.json malformed" if malformed else "machine.json host mismatch")
        triggers.append((what, "", "write .memo/machine.json (docs/MEMO.md, Sync)"))

    if failing_since and status in ("retry", "error", "skipped") and not reason.startswith("code-change"):
        if reason not in ("no-remote", "no-git", "busy") and (last_attempt - failing_since) >= FAILING_WARN_AFTER_MS:
            triggers.append((
                "sync keeps failing",
                "commits are kept here",
                "run memo sync status",
            ))

    if not triggers:
        return None
    what, why, fix = triggers[0]
    more = len(triggers) - 1
    line = f"⚠️ Memo sync: {what}."
    if why:
        line += f" {why}."
    line += f" {fix}"
    if more:
        line += f" (+{more} more: memo sync status)"
    if len(line) > 300:
        line = line[:297] + "..."
    return line
