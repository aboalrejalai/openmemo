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


_REPO_ROOT = Path(__file__).resolve().parents[2]


def vault_dir() -> Path:
    """MEMO_VAULT, else the checkout this code runs from, else $OPENMEMO_HOME or ~/openmemo
    (the same order as memo_core and the plugin launcher)."""
    raw = os.environ.get("MEMO_VAULT")
    if raw:
        return Path(raw).expanduser().resolve()
    if (_REPO_ROOT / "timeline.md").exists() and (_REPO_ROOT / "scripts" / "memo_core.py").exists():
        return _REPO_ROOT
    home = os.environ.get("OPENMEMO_HOME")
    return Path(home).expanduser().resolve() if home else Path.home() / "openmemo"


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
