"""`memo <command>` implementations other than `hook`."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from typing import List

from . import settings, state, worker_client


def cmd_start(args: List[str]) -> int:
    settings.ensure_file()
    ok = worker_client.ensure_started(wait=3.0)
    print("worker running" if ok else "worker starting", file=sys.stderr)
    return 0


def cmd_stop(args: List[str]) -> int:
    from .worker import pid_file
    if worker_client.request("/api/shutdown", {}, timeout=2) is not None:
        for _ in range(30):
            if not worker_client.healthy(0.2):
                break
            time.sleep(0.1)
    try:
        pid = json.loads(pid_file().read_text()).get("pid")
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError, TypeError):
        pass
    print("worker stopped")
    return 0


def cmd_worker(args: List[str]) -> int:
    from . import worker
    return worker.main()


def cmd_port(args: List[str]) -> int:
    print(settings.worker_port())
    return 0


def cmd_status(args: List[str]) -> int:
    from . import status
    return status.main(args)


def cmd_reindex(args: List[str]) -> int:
    from . import reindex
    return reindex.main(args)


def cmd_backfill(args: List[str]) -> int:
    from . import backfill
    return backfill.main(args)


def cmd_rerender(args: List[str]) -> int:
    """Rewrite every session note from the index and its sidecar (compact form).
    Adds only: anything a sidecar holds that the index lacks is put back first.
    Foreign sessions (other machine) are skipped and not counted."""
    from . import reindex, writer
    conn = state.connect()
    core = writer._core()
    paths = []
    for row in conn.execute("SELECT content_session_id, machine FROM sessions ORDER BY started_at").fetchall():
        stamp = row["machine"] if "machine" in row.keys() else None
        if not reindex.is_own(stamp):
            continue
        path = writer.write_session(conn, row[0])
        if path is not None:
            paths += [path, path.with_suffix(".json")]
    if paths and "--no-commit" not in args:
        core.git_commit_change("memo: session notes in compact form (details stay in each .json)", paths)
    print(f"Rewrote {len(paths) // 2} session note(s).")
    return 0


def cmd_doctor(args: List[str]) -> int:
    from . import doctor
    return doctor.main(args)


def _backup(path) -> None:
    import shutil
    from .paths import memo_home
    if path.exists():
        dest = memo_home() / "backups" / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def cmd_install_codex(args: List[str]) -> int:
    """Add the vault as a Codex marketplace, install memo, and turn Codex hooks on."""
    import re
    import subprocess
    from pathlib import Path
    from .paths import vault_dir
    vault = str(vault_dir())
    for cmd in (["codex", "plugin", "marketplace", "add", vault], ["codex", "plugin", "add", "memo@memo-local"]):
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(f"$ {' '.join(cmd)}\n{(res.stdout + res.stderr).strip()}")
    config = Path.home() / ".codex" / "config.toml"
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    if not re.search(r"(?m)^\[features\][^\[]*^hooks\s*=\s*true", text):
        _backup(config)
        if re.search(r"(?m)^\[features\]\s*$", text):
            text = re.sub(r"(?m)^\[features\]\s*$", "[features]\nhooks = true", text, count=1)
        else:
            text = text.rstrip() + "\n\n[features]\nhooks = true\n"
        config.write_text(text, encoding="utf-8")
        print("Enabled [features] hooks = true in ~/.codex/config.toml")
    return 0


def cmd_install_cursor(args: List[str]) -> int:
    """Install Memo as a Cursor local plugin (hooks + MCP + skills), Claude-parity.

    Symlinks vault/plugin → ~/.cursor/plugins/local/memo and removes Memo entries
    from ~/.cursor/hooks.json so capture is owned by the plugin (no double-fire).
    """
    from pathlib import Path
    from .paths import vault_dir

    plugin_src = vault_dir() / "plugin"
    local_root = Path.home() / ".cursor" / "plugins" / "local"
    local_root.mkdir(parents=True, exist_ok=True)
    target = local_root / "memo"
    if target.is_symlink() or target.exists():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            # Refuse to delete a real directory that is not our symlink.
            print(f"Refusing to replace non-symlink {target}; move it aside and re-run.")
            return 1
    import oslock
    oslock.link_dir(target, plugin_src)
    print(f"Linked Cursor plugin: {target} → {plugin_src}")

    # Migrate: strip Memo hooks from the user hooks file so only the plugin runs them.
    hooks_path = Path.home() / ".cursor" / "hooks.json"
    if hooks_path.exists():
        current = _load_json(hooks_path)
        if current is not None:
            hooks = current.get("hooks") or {}
            changed = False
            for event, entries in list(hooks.items()):
                kept = [e for e in entries if isinstance(e, dict) and not _is_memo(e.get("command", ""))]
                if len(kept) != len(entries):
                    changed = True
                if kept:
                    hooks[event] = kept
                else:
                    hooks.pop(event, None)
                    changed = True
            if changed:
                current["hooks"] = hooks
                current.setdefault("version", 1)
                _save_json(hooks_path, current)
                print(f"Removed Memo entries from {hooks_path} (plugin owns hooks now)")

    print("Reload Cursor (Developer: Reload Window) to load the Memo plugin.")
    return 0


def _python() -> str:
    """The interpreter the launcher would pick, for MCP entries in other hosts' configs."""
    import shutil
    for py in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.access(py, os.X_OK):
            return py
    return shutil.which("python3") or sys.executable


def _launcher() -> str:
    from .paths import vault_dir
    return str(vault_dir() / "plugin" / "bin" / "memo")


def _mcp_args() -> List[str]:
    from .paths import vault_dir
    return [str(vault_dir() / "scripts" / "mcp_server.py")]


def _is_memo(command: str) -> bool:
    command = command.replace("\\", "/")     # Windows paths use backslashes
    return "memo_capture" in command or "/plugin/bin/memo" in command


def _load_json(path):
    """The file's JSON object, {} when missing, None when unreadable (then it is left alone)."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = json.loads(text)
    except ValueError:
        print(f"Skipped {path}: not plain JSON (comments?). Add Memo's entries by hand.")
        return None
    return data if isinstance(data, dict) else None


def _save_json(path, data) -> None:
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote Memo into {path}")


def _merge_matcher_hooks(hooks: dict, wanted: dict) -> None:
    """Claude-style {event: [{matcher, hooks: [...]}]}: drop Memo's old groups, add the new ones."""
    for event, groups in wanted.items():
        kept = [g for g in hooks.get(event, [])
                if not any(_is_memo(h.get("command", "")) for h in g.get("hooks", []) if isinstance(h, dict))]
        hooks[event] = kept + groups


def cmd_install_gemini(args: List[str]) -> int:
    """Gemini CLI: hooks and the memo MCP server in ~/.gemini/settings.json."""
    from pathlib import Path
    path = Path.home() / ".gemini" / "settings.json"
    data = _load_json(path)
    if data is None:
        return 1
    hook = lambda event: {"name": f"memo-{event}", "type": "command", "timeout": 20000,
                          "command": f'"{_launcher()}" hook gemini {event}'}
    _merge_matcher_hooks(data.setdefault("hooks", {}), {
        "SessionStart": [{"matcher": "*", "hooks": [hook("context")]}],
        "BeforeAgent": [{"matcher": "*", "hooks": [hook("session-init")]}],
        "AfterTool": [{"matcher": "*", "hooks": [hook("observation")]}],
        "AfterAgent": [{"matcher": "*", "hooks": [hook("summarize")]}],
        "SessionEnd": [{"matcher": "*", "hooks": [hook("session-end")]}],
    })
    data.setdefault("mcpServers", {})["memo"] = {"command": _python(), "args": _mcp_args(), "timeout": 30000}
    _save_json(path, data)
    return 0


def cmd_install_antigravity(args: List[str]) -> int:
    """Antigravity (App, CLI, IDE): a "memo" entry in ~/.gemini/config/hooks.json, plus MCP."""
    from pathlib import Path
    gemini = Path.home() / ".gemini"
    hook = lambda event: {"type": "command", "timeout": 20,
                          "command": f'"{_launcher()}" hook antigravity {event}'}
    path = gemini / "config" / "hooks.json"
    data = _load_json(path)
    if data is None:
        return 1
    data["memo"] = {
        "SessionStart": [hook("context")],
        "PreInvocation": [hook("session-init")],
        "PostToolUse": [{"matcher": "*", "hooks": [hook("observation")]}],
        "Stop": [hook("summarize")],
    }
    _save_json(path, data)
    # The CLI's docs name ~/.gemini/config/mcp_config.json; older builds read ~/.gemini/antigravity/.
    for mcp in (gemini / "config" / "mcp_config.json", gemini / "antigravity" / "mcp_config.json"):
        conf = _load_json(mcp)
        if conf is None:
            continue
        conf.setdefault("mcpServers", {})["memo"] = {"command": _python(), "args": _mcp_args()}
        _save_json(mcp, conf)
    return 0


def cmd_install_windsurf(args: List[str]) -> int:
    """Windsurf / Devin Local: Claude-style hooks in ~/.config/devin/config.json, plus MCP."""
    from pathlib import Path
    path = Path.home() / ".config" / "devin" / "config.json"
    data = _load_json(path)
    if data is None:
        return 1
    hook = lambda event: {"type": "command", "timeout": 20,
                          "command": f'"{_launcher()}" hook windsurf {event}'}
    _merge_matcher_hooks(data.setdefault("hooks", {}), {
        "SessionStart": [{"matcher": "", "hooks": [hook("context")]}],
        "UserPromptSubmit": [{"matcher": "", "hooks": [hook("session-init")]}],
        "PostToolUse": [{"matcher": "", "hooks": [hook("observation")]}],
        "Stop": [{"matcher": "", "hooks": [hook("summarize")]}],
        "SessionEnd": [{"matcher": "", "hooks": [hook("session-end")]}],
    })
    data.setdefault("mcpServers", {})["memo"] = {"command": _python(), "args": _mcp_args()}
    _save_json(path, data)
    return 0


def cmd_install_opencode(args: List[str]) -> int:
    """OpenCode: the plugin file in ~/.config/opencode/plugins/ and the MCP server in opencode.json."""
    from pathlib import Path
    from .paths import vault_dir
    root = Path.home() / ".config" / "opencode"
    source = (vault_dir() / "plugin" / "opencode" / "memo.js").read_text(encoding="utf-8")
    plugin = root / "plugins" / "memo.js"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    _backup(plugin)
    plugin.write_text(source.replace("__MEMO_LAUNCHER__", _launcher()), encoding="utf-8")
    print(f"Wrote Memo plugin {plugin}")
    path = root / "opencode.json"
    data = _load_json(path)
    if data is None:
        return 1
    data.setdefault("$schema", "https://opencode.ai/config.json")
    data.setdefault("mcp", {})["memo"] = {"type": "local", "command": [_python(), *_mcp_args()], "enabled": True}
    _save_json(path, data)
    return 0


def cmd_sync(args: List[str]) -> int:
    """memo sync status | now [--accept-code <sha>] | resolve [--finish]."""
    from . import sync as sync_mod
    from . import health
    import datetime
    sub = args[0] if args else "status"
    rest = args[1:]
    if sub == "status":
        st = sync_mod.read_state()
        if st.get("status") == "conflict":
            paths = st.get("conflict_paths") or []
            print("conflict: " + (", ".join(paths) if paths else (st.get("reason") or "")))
        warn = health.sync_warning()
        if warn:
            print(warn)
        # live ahead/behind
        from .paths import vault_dir
        import subprocess
        cwd = vault_dir()
        ahead = behind = "?"
        try:
            subprocess.run(["git", "fetch", "origin"], cwd=cwd, capture_output=True, timeout=30)
            ab = subprocess.run(["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
                                cwd=cwd, capture_output=True, text=True, timeout=30)
            if ab.returncode == 0 and ab.stdout.strip():
                left, right = ab.stdout.strip().split()
                ahead, behind = left, right
        except Exception:
            pass
        def _iso(ms):
            if not ms:
                return ""
            return datetime.datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
        print(f"status: {st.get('status')}")
        print(f"reason: {st.get('reason') or ''}")
        print(f"last_ok: {_iso(st.get('last_ok'))}")
        print(f"ahead: {ahead}")
        print(f"behind: {behind}")
        print(f"conflict_paths: {st.get('conflict_paths') or []}")
        print(f"stuck_machines: {st.get('stuck_machines') or []}")
        print(f"adopt_errors: {st.get('adopt_errors') or []}")
        return 0
    if sub == "now":
        accept = None
        if rest[:1] == ["--accept-code"] and len(rest) >= 2:
            accept = rest[1]
        return sync_mod.main(["now"] + (["--accept-code", accept] if accept else []))
    if sub == "resolve":
        return sync_mod.main(["resolve"] + rest)
    print(f"memo sync: unknown subcommand '{sub}'", file=sys.stderr)
    return 2


def cmd_ui(args: List[str]) -> int:
    """Launch Memo Sovereign Studio local web server (`memo ui [--port 8766] [--no-browser]`)."""
    import memo_ui
    return memo_ui.main(args)


def cmd_init(args: List[str]) -> int:
    """Interactive/zero-friction onboarding wizard (`memo init [--yes] [--vault PATH] [--no-doctor]`)."""
    import shutil
    import subprocess
    from pathlib import Path
    from .paths import vault_dir

    auto_yes = "--yes" in args or "-y" in args or not sys.stdin.isatty()
    run_doc = "--no-doctor" not in args

    # Optional --vault path override
    custom_vault: Optional[Path] = None
    for i, a in enumerate(args):
        if a == "--vault" and i + 1 < len(args):
            custom_vault = Path(args[i + 1]).expanduser().resolve()
            os.environ["MEMO_VAULT"] = str(custom_vault)
            break

    vault = custom_vault or vault_dir()
    vault.mkdir(parents=True, exist_ok=True)
    for folder in ("graph", ".memo"):
        (vault / folder).mkdir(parents=True, exist_ok=True)
    graph_file = vault / "graph" / "entities.json"
    if not graph_file.exists():
        graph_file.write_text(
            json.dumps({"$schema": "./schema.md", "version": "2.2.0", "entities": [], "relationships": []}, indent=2) + "\n",
            encoding="utf-8",
        )

    # Persist vault location in ~/.memo/settings.json if missing or explicitly passed
    user_settings_path = Path.home() / ".memo" / "settings.json"
    user_settings = _load_json(user_settings_path) or {}
    if custom_vault or "vault" not in user_settings:
        user_settings["vault"] = str(vault)
        _save_json(user_settings_path, user_settings)

    home = Path.home()
    detected: List[tuple] = []
    if (home / ".cursor").exists():
        detected.append(("cursor", "Cursor IDE", cmd_install_cursor))
    if (home / ".gemini" / "config").exists() or (home / ".gemini" / "antigravity").exists() or shutil.which("agy"):
        detected.append(("antigravity", "Google Antigravity", cmd_install_antigravity))
    if (home / ".gemini").exists() or shutil.which("gemini"):
        detected.append(("gemini", "Gemini CLI", cmd_install_gemini))
    if (home / ".codex").exists() or shutil.which("codex"):
        detected.append(("codex", "OpenAI Codex CLI", cmd_install_codex))
    if (home / ".config" / "devin").exists():
        detected.append(("windsurf", "Windsurf / Devin", cmd_install_windsurf))
    if (home / ".config" / "opencode").exists() or shutil.which("opencode"):
        detected.append(("opencode", "OpenCode", cmd_install_opencode))

    print(f"Memo Init — Vault: {vault}")
    if detected:
        print("Detected AI Agent environments:")
        for key, label, _ in detected:
            print(f"  • {label} ({key})")
    else:
        print("No external agent directories detected yet; wiring core skills and vault index.")

    if not auto_yes and detected:
        try:
            ans = input("\nWire detected agents and symlink Memo skills now? [Y/n]: ").strip().lower()
        except EOFError:
            ans = "y"
        if ans and ans not in ("y", "yes"):
            print("Aborted by user.")
            return 0

    for key, label, fn in detected:
        try:
            print(f"\n→ Wiring {label}...")
            fn([])
        except Exception as exc:
            print(f"  Warning wiring {label}: {exc}", file=sys.stderr)

    # Symlink canonical skills from <vault>/.agents/skills/* into ~/.agents/skills/
    # (Codex, Cursor, Gemini, Antigravity) and ~/.claude/skills/ (Claude Code).
    src_skills = vault / ".agents" / "skills"
    if src_skills.is_dir():
        roots = [home / ".agents" / "skills"]
        if (home / ".claude").is_dir():
            roots.append(home / ".claude" / "skills")
        for dst_root in roots:
            dst_root.mkdir(parents=True, exist_ok=True)
            linked = []
            for sdir in sorted(src_skills.iterdir()):
                if not sdir.is_dir() or not (sdir / "SKILL.md").exists():
                    continue
                target = dst_root / sdir.name
                if not target.exists() and not target.is_symlink():
                    try:
                        import oslock
                        oslock.link_dir(target, sdir)
                        linked.append(sdir.name)
                    except OSError:
                        pass
            if linked:
                print(f"Linked agent skills into {dst_root}: {', '.join(linked)}")

    # Ensure git merge drivers and SQLite state index are ready
    try:
        import memo_core
        memo_core.ensure_git_config()
    except Exception:
        pass
    conn = state.connect()
    conn.close()

    if run_doc:
        print("\nRunning Memo Doctor diagnostics:")
        cmd_doctor([])

    print("\n✅ Memo initialization complete! Launch the visual studio anytime with: memo ui")
    return 0


TABLE = {
    "init": cmd_init,
    "ui": cmd_ui,
    "install-gemini": cmd_install_gemini,
    "install-antigravity": cmd_install_antigravity,
    "install-windsurf": cmd_install_windsurf,
    "install-opencode": cmd_install_opencode,
    "install-codex": cmd_install_codex,
    "install-cursor": cmd_install_cursor,
    "start": cmd_start,
    "stop": cmd_stop,
    "worker": cmd_worker,
    "port": cmd_port,
    "status": cmd_status,
    "reindex": cmd_reindex,
    "sync": cmd_sync,
    "doctor": cmd_doctor,
    "backfill": cmd_backfill,
    "rerender": cmd_rerender,
}
