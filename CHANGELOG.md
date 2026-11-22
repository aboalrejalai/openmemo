# Changelog

All notable changes to OpenMemo are listed here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
- GitHub Actions CI on Linux and macOS, Python 3.9, 3.12 and 3.13.
- `SECURITY.md` and this changelog.

### Fixed
- Two sessions whose ids shared their first 8 characters could overwrite each other's note when they had the same topic on the same day.
- Note templates carried a fixed Second Brain sync id, so every new note shared it.
- The Claude, Codex and Cursor plugin manifests pointed at a private repository.

### Changed
- All components now carry version 0.1.0 (they were 2.1.0, 2.0.0 and 1.0.0).
- Python 3.9 is the minimum supported version (macOS ships with it).
- Personal names and private data were removed from docs, templates, tests and tool descriptions.

[Unreleased]: https://github.com/aboalrejalai/openmemo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aboalrejalai/openmemo/releases/tag/v0.1.0
