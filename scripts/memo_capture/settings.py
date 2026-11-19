"""Memo capture settings: <vault>/.memo/settings.json, overridable per key by env.

A key `observer_model` can be overridden by the env var MEMO_OBSERVER_MODEL.
Defaults follow claude-mem 15.3.0 except where the owner decided otherwise:
the observer runs Opus 5.5 at medium effort, and secret redaction is on.

Machine identity (D4) lives in the untracked `.memo/machine.json` and is
overlaid after settings.json and before env. Those keys never travel in the
tracked settings file.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Dict

from .paths import legacy_home, memo_home

# Keys that belong only to machine.json / env, never settings.json.
MACHINE_KEYS = (
    "machine_name",
    "id_base",
    "machine_host",
    "owns_unstamped",
    "accept_incoming_code",
)

# machine.json field → settings key
_MACHINE_JSON_MAP = {
    "name": "machine_name",
    "id_base": "id_base",
    "host": "machine_host",
    "owns_unstamped": "owns_unstamped",
    "accept_incoming_code": "accept_incoming_code",
}

DEFAULTS: Dict[str, Any] = {
    # Where the vault lives (the launcher also reads this key).
    "vault": "",
    # Observer: the background Claude that turns tool events into memory.
    "observer_provider": "claude",           # claude | agy (Antigravity/Gemini)
    "observer_model": "claude-opus-5-5",
    "observer_effort": "medium",
    "observer_max_concurrent": 2,
    "observer_max_conversation_chars": 400_000,
    "observer_language": "en",            # language of narratives: en | ar
    "observer_batch_size": 1,             # tool events per observer message (claude-mem: 1)
    "observer_timeout_sec": 600,
    "observer_idle_close_sec": 900,
    "claude_cli": "",                      # empty: find `claude` on PATH
    "observer_cli": "",                    # empty: find `agy` when provider=agy
    "agy_cli": "",                         # alias of observer_cli
    # Capture filters.
    "skip_tools": "ListMcpResourcesTool,SlashCommand,Skill,TodoWrite,AskUserQuestion",
    "skip_bash_patterns": "",
    "skip_subagent_observations": False,
    "skip_agent_types": "",
    "excluded_projects": "",
    "project_name_source": "git-toplevel",   # git-toplevel | git-remote
    "project_environments": {},             # {"/abs/path": "project-name"}
    # Privacy.
    "redact_enabled": True,
    "redact_generic_enabled": True,
    "redact_disabled_builtins": "",
    "redact_custom_patterns": [],           # [{"name": "...", "pattern": "..."}]
    # SessionStart context.
    "context_observations": 50,
    "context_session_count": 10,
    "context_full_count": 0,
    "context_full_field": "narrative",
    "context_show_last_summary": True,
    "context_show_last_message": False,
    "context_show_read_tokens": False,
    "context_show_work_tokens": True,
    "context_show_savings_percent": True,
    "context_observation_types": "",
    "context_observation_concepts": "",
    "context_projects": "current",          # current | all
    "context_main_agent_only": True,
    "context_budget_chars": 10_000,
    # Other hooks.
    "file_read_gate_enabled": False,
    "file_read_gate_min_bytes": 32 * 1024,
    "semantic_inject": False,
    "semantic_inject_limit": 5,
    "folder_claudemd_enabled": False,
    # Worker.
    "worker_host": "127.0.0.1",
    "worker_port": 0,                       # 0: 37800 + uid % 100
    "worker_idle_exit_sec": 0,
    "spool_expire_days": 0,               # ignored: spooled events never expire (owner rule)
    "sync_interval_minutes": 0,           # 0 until TASK-006 sets tracked default to 3
    # Machine identity (D4) — defaults when machine.json is missing.
    "machine_name": "",
    "id_base": 0,
    "machine_host": "",
    "owns_unstamped": False,
    "accept_incoming_code": False,
}

_cache: Dict[str, Any] = {}
_sources: Dict[str, str] = {}  # key → machine.json | env | settings.json | default
_machine_malformed = False


def settings_path():
    path = memo_home() / "settings.json"
    legacy = legacy_home() / "settings.json"
    if not path.exists() and not os.environ.get("MEMO_HOME") and legacy.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            return legacy
    return path


def machine_path():
    return memo_home() / "machine.json"


def _coerce(default: Any, raw: str) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, (dict, list)):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def _read_machine_json() -> tuple[Dict[str, Any], bool]:
    """Return (mapped settings overlay, malformed). Malformed → empty overlay."""
    global _machine_malformed
    path = machine_path()
    if not path.exists():
        _machine_malformed = False
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            _machine_malformed = True
            return {}, True
        id_base = data.get("id_base", 0)
        if not isinstance(id_base, int) or isinstance(id_base, bool) or id_base < 0:
            _machine_malformed = True
            return {}, True
        out: Dict[str, Any] = {}
        for jk, sk in _MACHINE_JSON_MAP.items():
            if jk in data:
                out[sk] = data[jk]
        _machine_malformed = False
        return out, False
    except (OSError, ValueError, TypeError):
        _machine_malformed = True
        return {}, True


def machine_json_malformed() -> bool:
    load()
    return _machine_malformed


def load(refresh: bool = False) -> Dict[str, Any]:
    if _cache and not refresh:
        return _cache
    values = dict(DEFAULTS)
    sources = {k: "default" for k in DEFAULTS}

    path = settings_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if k in DEFAULTS and k not in MACHINE_KEYS:
                        # Also ignore raw machine.json spellings if someone put them in settings.
                        if k in ("name", "host"):
                            continue
                        values[k] = v
                        sources[k] = "settings.json"
        except (OSError, ValueError):
            pass
        # Explicitly ignore machine keys if present under either spelling
        try:
            stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if isinstance(stored, dict):
                for junk in MACHINE_KEYS + ("name", "host"):
                    if junk in stored and junk in MACHINE_KEYS:
                        pass  # already skipped above
        except (OSError, ValueError):
            pass

    overlay, _malformed = _read_machine_json()
    for k, v in overlay.items():
        values[k] = v
        sources[k] = "machine.json"

    for key, default in DEFAULTS.items():
        env = os.environ.get(f"MEMO_{key.upper()}")
        if env is not None:
            values[key] = _coerce(default, env)
            sources[key] = "env"

    # Refresh in place so a concurrent get() never sees a missing key.
    _cache.update(values)
    for stale in list(_cache):
        if stale not in values:
            del _cache[stale]
    _sources.clear()
    _sources.update(sources)
    return _cache


def get(key: str) -> Any:
    return load()[key]


def source(key: str) -> str:
    load()
    return _sources.get(key, "default")


def effective_name() -> str:
    """Machine name for stamps and sync refs; unnamed-<hostname> when unset."""
    name = str(get("machine_name") or "").strip()
    if name:
        return name
    return f"unnamed-{socket.gethostname()}"


def csv(key: str) -> list:
    return [x.strip() for x in str(get(key) or "").split(",") if x.strip()]


def worker_port() -> int:
    port = int(get("worker_port") or 0)
    return port or 37800 + (os.getuid() % 100)


def ensure_file() -> None:
    """Write the defaults once so the owner can see and edit every key."""
    path = settings_path()
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never write machine identity keys into the tracked settings shape.
    payload = {k: v for k, v in DEFAULTS.items() if k not in MACHINE_KEYS}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
