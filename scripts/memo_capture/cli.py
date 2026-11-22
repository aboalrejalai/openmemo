#!/usr/bin/env python3
"""Memo command line, reached through the plugin launcher `bin/memo`.

  memo --version             print the OpenMemo version
  memo hook <host> <event>   run one hook (stdin: host payload, stdout: JSON)
  memo init [--yes]          auto-detect & wire installed AI agents, skills, and MCP
  memo ui [--port 8766]      launch Memo Sovereign Studio (Knowledge Graph & Hybrid Search UI)
  memo start                 start the background worker if it is not running
  memo stop                  stop the worker
  memo worker                run the worker in the foreground
  memo port                  print the worker port
  memo status                worker, queue and capture summary
  memo reindex               rebuild <vault>/.memo/state.db from the vault's Markdown
  memo doctor                check the installation and print fixes
  memo sync status|now|resolve   sync with GitHub (conflict, holds, push)
  memo install-<host>        wire another agent: codex | cursor | gemini | antigravity |
                             windsurf | opencode
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "memo_capture"

from memo_capture import hooks  # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if argv[0] in ("-V", "--version", "version"):
        from memo_capture import __version__
        print(f"openmemo {__version__}")
        return 0
    command, rest = argv[0], argv[1:]
    if command == "hook":
        return hooks.main(rest)
    commands = {}
    try:
        from memo_capture import commands as _commands
        commands = _commands.TABLE
    except ImportError:
        pass
    if command in commands:
        return commands[command](rest) or 0
    if command == "start":
        return 0  # nothing to start before the worker exists
    print(f"memo: unknown command '{command}'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
