"""Hook dispatcher: `memo hook <host> <event>`.

Contract with the host, the same as claude-mem's hook-command:
- read one JSON payload from stdin, normalise it through the host adapter;
- print exactly one JSON object on stdout;
- exit 0 always, even on errors, so a Memo failure never blocks a session;
- keep stderr out of the model's context (it is captured and logged).
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from typing import Any, Dict, List

from .adapters import HookResult, get_adapter
from .paths import log_line

EVENTS = (
    "context", "session-init", "file-context", "observation",
    "summarize", "subagent-stop", "session-end",
)


def _handler(event: str):
    from . import handlers
    return {
        "context": handlers.context,
        "session-init": handlers.session_init,
        "file-context": handlers.file_context,
        "observation": handlers.observation,
        "summarize": handlers.summarize,
        "subagent-stop": handlers.subagent_stop,
        "session-end": handlers.session_end,
    }[event]


def run(host: str, event: str, stdin_text: str) -> Dict[str, Any]:
    """Run one hook and return the JSON object to print. Never raises."""
    if os.environ.get("MEMO_OBSERVER") == "1" or os.environ.get("MEMO_DISABLED") == "1":
        return {"continue": True} if host == "cursor" else {}
    try:
        if event not in EVENTS:
            raise ValueError(f"unknown event '{event}'")
        adapter = get_adapter(host)
        raw = json.loads(stdin_text) if stdin_text.strip() else {}
        if os.environ.get("MEMO_DEBUG_HOOKS") == "1":   # diagnose a host's payload shape
            log_line("hook-payloads", f"{host} {event} keys={sorted(raw)[:40]} {stdin_text[:1500]}")
        if not isinstance(raw, dict):
            raise ValueError("hook payload is not a JSON object")
        inp = adapter.normalize(raw, event)
        result = _handler(event)(inp) or HookResult()
        return adapter.format_output(result, event, inp)
    except Exception:
        log_line("hooks", f"{host} {event} failed:\n{traceback.format_exc()}")
        return {"continue": True} if host == "cursor" else {}


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print("{}")
        return 0
    host, event = argv
    stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    real_stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        out = run(host, event, stdin_text)
    finally:
        captured, sys.stderr = sys.stderr.getvalue(), real_stderr
    if captured.strip():
        log_line("hooks", f"{host} {event} stderr: {captured.strip()}")
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0
