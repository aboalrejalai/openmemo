# Changelog

All notable changes to OpenMemo are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.1] - 2026-11-23

### Fixed
- The observer burned through model usage limits. It kept one resumed conversation per session, so every tool event re-read the whole session history: one small event cost 50–90k tokens, and the cost kept growing with the session. The 400k-character cap never applied, because its counter reset whenever the conversation was resumed.
- Recorded `discovery_tokens` were cumulative per conversation, so token stats were heavily overstated.

### Changed
- Every observer call is now a fresh conversation (no `--resume` / `--conversation`). It carries only the rules, the user's request, the last summary, the titles already recorded and the new events, so a call costs about the same at event 500 as at event 5.
- Events are sent once a session has been quiet for `observer_quiet_sec` (default 60), as soon as `observer_batch_size` events are queued (default 15, was 1), or when the session ends. A busy session makes about one call a minute instead of one per tool.
- The extra "new request" priming call is gone: the request travels with the first batch.
- A request that recorded nothing and got only a short reply no longer triggers a summary call.

## [0.1.0] - 2026-11-22

First public release.

### Added
- One-command install: `curl -fsSL https://raw.githubusercontent.com/aboalrejalai/openmemo/main/install.sh | sh`.
- `memo --version`.
- Hybrid search (`hybrid-rrf`) that fuses vault FTS5, session BM25, an Arabic/substring trigram index and knowledge-graph aliases, all local.
- `memo ui`: local web Studio with the knowledge graph, memory stream and dossier reader.
- `memo init`: detects and wires Claude Code, Cursor, Codex CLI, Gemini CLI, Antigravity, Windsurf and OpenCode.
- MCP server over stdio, plus a password-protected remote MCP container (`docker compose up -d`).
- Demo vault with templates, a demo project note and a graph entity.
- GitHub Actions CI on Linux, macOS and Windows (Python 3.9, 3.12 and 3.13), plus installer and Docker server jobs.
- `SECURITY.md` and this changelog.
- Windows support: `irm .../install.ps1 | iex`, a `memo.cmd` launcher, and portable file locks (`scripts/oslock.py`).

### Fixed
- Two sessions whose ids shared their first 8 characters could overwrite each other's note when they had the same topic on the same day.
- Note templates carried a fixed Second Brain sync id, so every new note shared it.
- The Claude, Codex and Cursor plugin manifests pointed at a private repository.
- The worker and `memo ui` could hang on start on machines with slow DNS: Python's HTTP server did a reverse DNS lookup when binding.
- Two identical prompts sent in the same millisecond were stored as one.
- Without `MEMO_VAULT`, the capture code fell back to a personal path instead of the checkout or `~/openmemo`.
- The server image defaulted to the Asia/Riyadh time zone; it is UTC now (set `TZ` to change it).
- `memo init` now links the skills into `~/.claude/skills/` too, so Claude Code sees them.
- The `memo` and `memo-vault` skills can be invoked by the agent on its own again.

### Changed
- All components now carry version 0.1.0 (they were 2.1.0, 2.0.0 and 1.0.0).
- Python 3.9 is the minimum supported version (macOS ships with it).
- Personal names and private data were removed from docs, templates, tests and tool descriptions.

[Unreleased]: https://github.com/aboalrejalai/openmemo/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/aboalrejalai/openmemo/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aboalrejalai/openmemo/releases/tag/v0.1.0
