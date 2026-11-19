"""Where Memo keeps its working state: <vault>/.memo, so it moves with the vault.

Only the observer's working folder stays outside the vault (~/.memo), because
a Claude started inside the vault would load the vault's CLAUDE.md.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path


def memo_home() -> Path:
    raw = os.environ.get("MEMO_HOME")
    return Path(raw).expanduser() if raw else vault_dir() / ".memo"


def legacy_home() -> Path:
    """Where state lived before it moved into the vault (2026-11-01)."""
    return Path.home() / ".memo"


def observer_home() -> Path:
    home = memo_home().resolve()
    return legacy_home() if vault_dir().resolve() in home.parents else home


def vault_dir() -> Path:
    raw = os.environ.get("MEMO_VAULT")
    return Path(raw).expanduser().resolve() if raw else Path.home() / "Desktop" / "Project" / "memo"


def logs_dir() -> Path:
    path = memo_home() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_line(name: str, message: str) -> None:
    """Append one line to <vault>/.memo/logs/<name>.log. Never raises."""
    try:
        from .privacy import redact_for_log
        message = redact_for_log(message)
    except Exception:  # privacy module missing or broken: still log, truncated
        message = message[:2000]
    try:
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(logs_dir() / f"{name}.log", "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass
