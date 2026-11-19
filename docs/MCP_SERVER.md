---
title: Memo Memory MCP Server
type: core
status: active
tags:
  - type/core
  - status/active
created: 2026-09-06
updated: 2026-10-27
---
# Memo Memory MCP Server

> Local, offline Model Context Protocol server exposing the Memo sovereign
> memory vault to any MCP-capable agent (Claude Code, Claude Desktop, Gemini,
> Cursor, DemoApp). No network, no external dependencies — Python 3.9+ stdlib and
> SQLite FTS5 only.

---

## Architecture

```
scripts/
  memo_core.py     single source of truth: index, search, timeline, graph, safety
  mcp_server.py     JSON-RPC/stdio protocol layer  ─┐
  memory_engine.py  command-line interface          ─┴─ both import memo_core
  test_mcp.py       54-assertion regression suite
```

Every behavioural fix lands once, in `memo_core.py`. The MCP server and the CLI
are thin surfaces over it and cannot drift apart.

**stdout is reserved for JSON-RPC.** All logging goes to stderr via
`MEMO_LOG_LEVEL`; nothing else may print to stdout.

---

## Configuration

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `MEMO_VAULT` | `${OPENMEMO_HOME:-$HOME/openmemo}` | Vault root. Lets the global skill reach the vault from any project. |
| `MEMO_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`, written to stderr. |
| `MEMO_GIT_SNAPSHOT` | `1` | Set `0` to disable automatic git commits around mutations. |

### Registering the server

Claude Code, user scope (matches the global `memo` skill):

```bash
claude mcp add --scope user memo python3 ${OPENMEMO_HOME:-$HOME/openmemo}/scripts/mcp_server.py
```

Other hosts: point them at [`mcp_config.json`](../mcp_config.json).

---

## Tools

Thirteen tools. All declare `annotations` and an `outputSchema`, and return
`structuredContent` alongside human-readable text.

### Read (`readOnlyHint: true`)

#### `memo_search`
Full-text search over the whole vault. Multi-word queries run through a
**fallback ladder**, stopping at the first strategy that matches; the winning
strategy is reported so you can judge match quality.

Optional filters: `type`, `status`, `tag` (exact frontmatter / tag match).

Historical meta under `docs/_open-questions.md` and `docs/_prompt-fill-gaps.md`
is **not FTS-indexed** (readable via `memo_get_dossier` only).

| Order | Strategy | Behaviour |
| :-- | :--- | :--- |
| 0 | `phrase` | Query wrapped in `"…"` → exact phrase, nothing else tried |
| 1 | `phrase` | All terms adjacent |
| 2 | `all-terms` | Every term present (AND) |
| 3 | `any-term` | Any term present (OR) |
| 4 | `prefix` | Term prefixes (`salla*`) |
| 5 | `substring` | Trigram index — matches inside words, needed for Arabic morphology (`المكتبة` ↔ `مكتبة`) |

```json
{"name": "memo_search", "arguments": {"query": "Salla Supabase", "limit": 5, "type": "project"}}
```

#### `memo_query_props`
Inventory notes by frontmatter **without a text query**. Use when the agent
needs “all active clients” or “active Salla clients” rather than inventing a
keyword for `memo_search`.

```json
{"name": "memo_query_props", "arguments": {"type": "client", "status": "active", "tag": "tech/salla"}}
```

#### `memo_get_entity`
Knowledge-graph lookup with incoming and outgoing relationships. Matches on id,
English name, or Arabic name.

#### `memo_get_timeline`
Structured events from `timeline.md`, each with `date`, `category`, `summary`,
`link`, and `section` (`upcoming` from the milestones table, `history` from the
Living History blocks). Optional `since` / `until` ISO filters.

#### `memo_get_dossier`
Full markdown of one vault file. Paths resolve strictly inside the vault.

#### `memo_list_files`
Every markdown file with its title, optionally scoped to a subdirectory. Use it
instead of guessing paths.

#### `memo_status`
File count, index freshness, graph size, timeline event count, git-snapshot
state, SQLite version.

#### `memo_lint`
Vault integrity gate: required frontmatter, closed tag taxonomy, `entity_id` ↔
graph sync, broken wikilinks. Same checks as `scripts/vault_lint.py`.

### Write

#### `memo_append_fact` — `destructiveHint: false`
Appends to `timeline.md` or `sessions/current.md`. Timeline entries are inserted
into the correct `### Month YYYY` block of `## Living History`, in descending
date order — never at end of file.

Two dedup layers: a byte-identical summary is refused, and a **near-duplicate**
(token-overlap ≥ 0.6, or sequence ratio ≥ 0.8, against every existing timeline
event) is held back unwritten, returning the matching events with their
similarity scores. Pass `force: true` to override once the candidates have been
reviewed. This is what stops the same fact entering the vault twice in two
different wordings.

#### `memo_append_section` — `destructiveHint: false`
Appends a markdown block at the end of a named section — the correct tool for
*adding* content to a client, project, or stack note. Finds the heading, walks
to the next heading of the same or higher level, and inserts before it, so no
anchor text is needed. An unknown heading is refused **with the list of real
headings**; `create_missing: true` adds the section instead. Identical text
already present is skipped.

#### `memo_create_note` — `destructiveHint: false`
Creates a new note from `_templates/<type>.md` with lint-clean frontmatter
(`title`, `type`, `status`, `tags`, `created`, `updated`), auto-adding the
`type/<type>` and `status/<status>` tags. Taxonomy is read from
`scripts/vault_lint.py`, so a note cannot be born failing the linter that
validates it.

Refusals: an existing path, a new file at the vault root (six hubs only), an absolute path, a `..` or dot segment, a note named `claude.md` / `gemini.md` / `agents.md`, a
folder/type mismatch (`clients/` requires `type: client`), a tag outside the
taxonomy, or an `entity_id` that is not in the graph. The response reports the
lint result for the created file.

#### `memo_update_fact` — `destructiveHint: true`
Replaces an exact snippet in a vault file. **Refused unless `old_text` occurs
exactly once**, so an ambiguous edit can never hit the wrong record. Intended
only for confirmed corrections.

#### `memo_add_entity` — `destructiveHint: false`
Adds an entity and/or relationships to `graph/entities.json`. Validates entity
type, relation name, duplicate ids, and dangling endpoints before writing.

---

## Safety guarantees

| Guarantee | Mechanism |
| :--- | :--- |
| No access outside the vault | `safe_vault_path` resolves symlinks and `..`, then asserts containment |
| No partial writes | `atomic_write` → temp file + `os.replace` |
| No interleaved concurrent writes | `flock` on `.cache/memo.lock` |
| Every mutation revertable | path-scoped git snapshot of the touched files immediately before the write (only if they were dirty), commit of those paths only after; other dirty or staged files stay out; hooks run |
| No commit without a change | every refusal, duplicate, held-back or unchanged call returns before the snapshot, so it never commits a hand edit |
| No ambiguous edits | `update_fact` requires a unique match |
| No stale search results | deleted files are purged from the index on every sync |
| No silent protocol corruption | logging is stderr-only |

---

## Protocol support

- Versions: `2025-06-18` (default), `2025-03-26`, `2024-11-05` — negotiated
  against the client's request.
- Methods: `initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`,
  `resources/templates/list`, `resources/read`, `prompts/list`, `prompts/get`,
  and `notifications/*`.
- JSON-RPC **batch arrays** are supported.
- Unknown tool / prompt / resource → `-32602 Invalid params` (not a misleading
  `-32601`).

### Resources

Hubs (root policy — five living hubs + vault spec):

- `memo://core/readme` → `README.md`
- `memo://core/home` → `HOME.md`
- `memo://core/timeline` → `timeline.md`
- `memo://core/decisions` → `decisions.md`
- `memo://core/stack` → `stack.md`
- `memo://core/vault-spec` → `docs/VAULT_SPEC.md`

Plus graph / agent / session:

- `memo://core/graph`, `memo://core/schema`
- `memo://agent/directives`, `memo://agent/heuristics`
- `memo://session/current`

`bases/` and `canvas/` stay Obsidian-human surfaces and are **not** MCP resources.

### Prompts

`owner_context` — operating persona and rules, loaded from the vault README.

---

## CLI

```bash
python3 scripts/memory_engine.py search "Acme Corp budget"
python3 scripts/memory_engine.py timeline --since 2026-09-01
python3 scripts/memory_engine.py entity demo-partner
python3 scripts/memory_engine.py files --subdir clients
python3 scripts/memory_engine.py add-event --category System --summary "..."
python3 scripts/memory_engine.py status
python3 scripts/memory_engine.py index
```

---

## Tests

```bash
python3 scripts/test_mcp.py
```

Runs against a throwaway git-initialised copy of the vault via `MEMO_VAULT`, so
real memory is never touched. Each assertion is tied to a defect reproduced
against the previous implementation: phrase-only search returning zero hits,
appended facts being unreadable, writes landing past the ingestion template,
path traversal, and stale index rows.
