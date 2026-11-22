"""Talk to the worker from a hook: is it up, start it, nudge it.

Every call is bounded and silent: a hook never fails because of the worker.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from . import settings
from .paths import logs_dir, memo_home

CLI = Path(__file__).resolve().parent / "cli.py"


def base_url() -> str:
    return f"http://{settings.get('worker_host')}:{settings.worker_port()}"


def request(path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(base_url() + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except Exception:
        return None


def healthy(timeout: float = 0.5) -> bool:
    res = request("/health", timeout=timeout)
    return bool(res and res.get("ok"))


def spawn() -> None:
    """Start `memo worker` detached from the hook's process group."""
    log = open(logs_dir() / "worker.out", "ab")
    env = dict(os.environ)
    env.pop("MEMO_OBSERVER", None)
    extra = {}
    if os.name == "nt":
        # Outlive the hook's console without opening a window of its own.
        extra["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                  | subprocess.CREATE_NO_WINDOW)
    else:
        extra["start_new_session"] = True
    subprocess.Popen([sys.executable, str(CLI), "worker"], stdin=subprocess.DEVNULL, stdout=log,
                     stderr=log, close_fds=True, env=env, cwd=str(memo_home()), **extra)


def ensure_started(wait: float = 0.0) -> bool:
    """Start the worker if it is down. With wait > 0, wait for it to answer."""
    if healthy():
        return True
    lock = memo_home() / "worker.starting"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime < 10:
            pass  # another hook just started it
        else:
            memo_home().mkdir(parents=True, exist_ok=True)
            lock.write_text(str(os.getpid()))
            spawn()
    except OSError:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if healthy():
            return True
        time.sleep(0.1)
    return False


def nudge() -> None:
    if request("/api/spool/nudge", {}, timeout=0.25) is None:
        ensure_started()
