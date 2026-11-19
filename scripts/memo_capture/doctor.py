"""`memo doctor`: check every piece capture depends on, and say how to fix it."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

from . import settings, worker_client
from .paths import memo_home, vault_dir

REQUIRED_FLAGS = ("--input-format", "--output-format", "--effort", "--safe-mode", "--tools",
                  "--strict-mcp-config", "--permission-mode", "--resume")


def checks() -> List[Tuple[bool, str, str]]:
    out: List[Tuple[bool, str, str]] = []

    def add(ok: bool, what: str, fix: str = "") -> None:
        out.append((ok, what, fix))

    add(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "install Python 3.9 or newer")
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        add(True, f"SQLite {sqlite3.sqlite_version} with FTS5")
    except sqlite3.OperationalError:
        add(False, "SQLite FTS5", "use a Python whose SQLite has FTS5 (Homebrew python3)")
    try:
        probe.execute("CREATE VIRTUAL TABLE t2 USING fts5(a, tokenize='trigram')")
        add(True, "SQLite trigram tokenizer (Arabic and substring search)")
    except sqlite3.OperationalError:
        add(False, "SQLite trigram tokenizer", "upgrade SQLite to 3.34+; word search still works")

    vault = vault_dir()
    add((vault / "scripts" / "memo_capture" / "cli.py").exists(), f"Vault code at {vault}",
        "set MEMO_VAULT or \"vault\" in <vault>/.memo/settings.json")
    add((vault / ".git").exists(), "Vault is a git repository", "run git init in the vault")

    home = memo_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / ".write-test").write_text("ok")
        (home / ".write-test").unlink()
        add(True, f"State folder {home} is writable")
    except OSError as exc:
        add(False, f"State folder {home}", f"fix permissions: {exc}")

    try:
        from . import settings as _settings
        from .observer import provider, claude_cli, agy_cli
        prov = provider()
        add(True, f"Observer provider: {prov} (model {_settings.get('observer_model')})")
        if prov == "agy":
            cli = agy_cli()
            help_run = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=30)
            help_text = (help_run.stdout or "") + (help_run.stderr or "")
            add("stream-json" in help_text,
                f"agy CLI supports stream-json at {cli}",
                "install Antigravity CLI (agy) or set observer_cli")
        else:
            cli = claude_cli()
            version = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
            add(True, f"claude CLI {version} at {cli}")
            help_text = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=30).stdout
            missing = [f for f in REQUIRED_FLAGS if f not in help_text]
            add(not missing, "claude CLI supports the observer flags" + (f" (missing {missing})" if missing else ""),
                "update Claude Code: claude update")
    except Exception as exc:
        add(False, "observer CLI", f"fix observer_provider / claude_cli / observer_cli ({exc})")

    installed = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    entry = None
    try:
        data = json.loads(installed.read_text())
        plugins = data.get("plugins", data)
        entry = (plugins.get("memo@memo") or [None])[0]
    except (OSError, ValueError, AttributeError):
        pass
    add(bool(entry), f"Plugin memo@memo installed{' (' + entry.get('version', '?') + ')' if entry else ''}",
        f"claude plugin marketplace add {vault} && claude plugin install memo@memo --scope user")
    if entry:
        hooks = Path(entry.get("installPath", "")) / "hooks" / "hooks.json"
        try:
            events = sorted(json.loads(hooks.read_text())["hooks"])
            add(len(events) == 8, f"Plugin hooks: {', '.join(events)}", "claude plugin update memo@memo")
        except (OSError, ValueError, KeyError):
            add(False, "Plugin hooks.json readable", "claude plugin update memo@memo")

    try:
        claude_json = json.loads((Path.home() / ".claude.json").read_text())
        add("memo" in (claude_json.get("mcpServers") or {}), "Memo MCP server registered for Claude Code",
            f"claude mcp add memo -s user -- python3 {vault}/scripts/mcp_server.py")
    except (OSError, ValueError):
        add(False, "~/.claude.json readable", "start Claude Code once")

    # Cursor local plugin (hooks + MCP + skills), same capture stack as Claude Code.
    cursor_plugin = Path.home() / ".cursor" / "plugins" / "local" / "memo"
    cursor_manifest = cursor_plugin / ".cursor-plugin" / "plugin.json"
    cursor_hooks = cursor_plugin / "hooks" / "cursor-plugin-hooks.json"
    add(cursor_plugin.is_dir() or cursor_plugin.is_symlink(),
        "Cursor plugin linked at ~/.cursor/plugins/local/memo",
        "memo install-cursor  (then Developer: Reload Window)")
    if cursor_plugin.is_dir() or cursor_plugin.is_symlink():
        try:
            meta = json.loads(cursor_manifest.read_text())
            add(meta.get("name") == "memo",
                f"Cursor plugin manifest ({meta.get('version', '?')})",
                "memo install-cursor")
        except (OSError, ValueError):
            add(False, "Cursor plugin manifest readable", "memo install-cursor")
        try:
            events = sorted(json.loads(cursor_hooks.read_text()).get("hooks", {}))
            add(len(events) >= 8, f"Cursor plugin hooks: {', '.join(events)}",
                "memo install-cursor")
        except (OSError, ValueError, AttributeError):
            add(False, "Cursor plugin hooks readable", "memo install-cursor")
        add((cursor_plugin / "bin" / "memo-mcp").is_file(),
            "Cursor plugin MCP launcher (bin/memo-mcp)",
            "memo install-cursor")

    healthy = worker_client.healthy()
    add(healthy, f"Worker answering on port {settings.worker_port()}", "memo start (or open a Claude Code session)")
    from . import health
    warn = health.warning()
    add(warn is None, "Recording keeps up (no event waiting over 10 minutes)" if warn is None else warn,
        "memo stop, then memo start: a fresh worker drains the spool; nothing in it is lost")
    add(os.environ.get("MEMO_OBSERVER") != "1", "Not running inside the observer",
        "unset MEMO_OBSERVER in this shell")

    # Sync (plan 003 TASK-007)
    from . import sync as sync_mod
    import memo_core
    memo_core.ensure_git_config()
    st = sync_mod.read_state()
    sync_ok = True
    sync_fix = ""
    note = ""
    if st.get("status") == "conflict" or st.get("resolving"):
        sync_ok, sync_fix = False, "run memo sync resolve"
    elif st.get("status") == "skipped" and str(st.get("reason") or "").startswith("code-change"):
        sync_ok, sync_fix = False, "review held commit: memo sync now --accept-code <sha>"
    elif st.get("stuck_machines"):
        sync_ok, sync_fix = False, "run memo sync resolve on the stuck machine"
    elif settings.machine_json_malformed() or not settings.machine_path().exists():
        sync_ok, sync_fix = False, "write .memo/machine.json (docs/MEMO.md, Sync)"
    else:
        # no remote is OK
        remotes = subprocess.run(["git", "remote"], cwd=vault_dir(), capture_output=True, text=True)
        if "origin" not in (remotes.stdout or "").split():
            note = " (no remote)"
    add(sync_ok, "Sync" + note + ("" if sync_ok else f": {st.get('status')} {st.get('reason') or ''}"),
        sync_fix)
    for mode in ("timeline", "note"):
        name = f"memo-{mode}"
        r = subprocess.run(["git", "config", "--local", "--get", f"merge.{name}.driver"],
                           cwd=vault_dir(), capture_output=True, text=True)
        add(r.returncode == 0 and bool(r.stdout.strip()),
            f"merge.{name}.driver",
            "memo sync now (registers drivers via ensure_git_config)")
    add(True, f"machine_name={settings.effective_name()} (source={settings.source('machine_name')})")
    add(True, f"id_base={settings.get('id_base')} (source={settings.source('id_base')})")
    for key in ("machine_name", "id_base", "machine_host", "owns_unstamped", "accept_incoming_code",
                "sync_interval_minutes"):
        env = os.environ.get(f"MEMO_{key.upper()}")
        if env is not None:
            add(True, f"env MEMO_{key.upper()}={env}")
    return out


def main(args: List[str]) -> int:
    results = checks()
    for ok, what, fix in results:
        print(f"  {'OK ' if ok else 'FIX'}  {what}" + ("" if ok or not fix else f"\n        → {fix}"))
    bad = sum(1 for ok, _, _ in results if not ok)
    print(f"\n{len(results) - bad}/{len(results)} checks passed.")
    return 0 if bad == 0 else 1
