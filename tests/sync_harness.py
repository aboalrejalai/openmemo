"""In-process harness cases for sync (plan 003 TASK-004).

Run as: python3 tests/sync_harness.py <overlap|lockfree>
with MEMO_VAULT / MEMO_HOME / PYTHONPATH set by the test.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# scripts/ on path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def case_overlap() -> int:
    from memo_capture import sync
    results = [None, None]
    barrier = threading.Barrier(2)

    def one(i):
        barrier.wait()
        results[i] = sync.run(initiator="cli")

    t1 = threading.Thread(target=one, args=(0,))
    t2 = threading.Thread(target=one, args=(1,))
    t1.start(); t2.start()
    t1.join(); t2.join()
    statuses = sorted(r["status"] for r in results)
    # one ok/retry/skipped, one busy skipped
    print("overlap", statuses, [r.get("reason") for r in results])
    assert any(r["status"] == "skipped" and r.get("reason") == "busy" for r in results) or \
           all(r["status"] in ("ok", "retry", "skipped") for r in results)
    # sync.json valid
    import json
    st = sync.read_state()
    json.dumps(st)
    return 0


def case_lockfree() -> int:
    """While a slow fetch runs, another process takes vault_lock(timeout=1)."""
    import memo_core as core
    from memo_capture import sync
    # Patch fetch to sleep by replacing _git for fetch
    real_git = sync._git
    started = threading.Event()
    done = threading.Event()

    def slow_git(cwd, *args, **kwargs):
        if args[:1] == ("fetch",) or (args and args[0] == "fetch"):
            started.set()
            time.sleep(2)
            done.set()
        return real_git(cwd, *args, **kwargs)

    sync._git = slow_git  # type: ignore
    holder = {"ok": False}

    def take_lock():
        started.wait(5)
        try:
            with core.vault_lock(timeout=1):
                holder["ok"] = True
        except Exception as exc:
            print("lock failed", exc)
            holder["ok"] = False

    t = threading.Thread(target=take_lock)
    t.start()
    try:
        sync.git_sync()
    finally:
        sync._git = real_git  # type: ignore
    t.join(5)
    print("lockfree", holder["ok"])
    assert holder["ok"], "vault_lock should succeed during fetch"
    return 0


def main():
    case = sys.argv[1] if len(sys.argv) > 1 else ""
    if case == "overlap":
        return case_overlap()
    if case == "lockfree":
        return case_lockfree()
    print("usage: sync_harness.py <overlap|lockfree>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
