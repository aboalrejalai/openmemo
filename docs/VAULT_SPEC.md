---
title: Memo Vault Spec
aliases:
  - VAULT_SPEC
  - vault-spec
type: core
status: active
tags:
  - type/core
  - status/active
created: 2026-09-06
updated: 2026-10-27
---
# Memo Vault Spec

> Governing document for the Memo sovereign memory vault. Agents and humans must follow this before inventing structure.

Human home: [[HOME]] · Agent contract: [[README]] · Graph schema: [[graph/schema]]

---

## 0. Root Policy (six hubs only)

The vault root may contain **exactly these six living hubs**:

| File | Audience | Role |
|---|---|---|
| `README.md` | Agents | Dense identity + operating contract |
| `HOME.md` | Humans | Visual dashboard (Bases / Canvas links) |
| `timeline.md` | Both | Chronological living history (MCP path is fixed here) |
| `decisions.md` | Both | Locked decisions and rationales |
| `stack.md` | Both | Tooling MOC pointing into `stack/` |
| `subscriptions.md` | Both | Billing MOC pointing into `subscriptions/` |

Everything else descriptive, historical, or one-shot operational belongs under `docs/`:

- `docs/VAULT_SPEC.md` · `docs/MCP_SERVER.md`
- `docs/_open-questions.md` · `docs/_prompt-fill-gaps.md` · `docs/_CLAUDE-template.md`

**Forbidden at root:** duplicate stubs that only redirect to an existing `projects/` or `clients/` file (example: deleted root `demo-project.md` → use `[[projects/demo-project]]`).

---

## 1. Folder Meanings

| Folder | Meaning | `type` |
|---|---|---|
| `clients/` | Commercial accounts & ventures serviced | `client` |
| `projects/` | Products, repos, hackathon builds | `project` |
| `stack/` | Tools the owner actually uses (dossiers + paid/daily tools) | `stack` |
| `bookmarks/` | Browser bookmark archive, one subfolder per browser folder; kept for lookup, not memory | `bookmark` |
| `subscriptions/` | Paid plans, billing cycles, renewals | `subscription` |
| `expertise/` | Cross-cutting disciplines | `expertise` |
| `hackathons/` | Competition events | `hackathon` |
| `profiles/` | Public presence channels | `profile` |
| `agent/` | DemoApp directives & heuristics | `agent` |
| `sessions/` | Ephemeral working memory | `session` |
| `bases/` | Obsidian Bases (Notion-class views) | — |
| `canvas/` | Spatial maps of the graph | — |
| `graph/` | Machine-readable entity network | — |
| `docs/` | Specs, MCP docs, historical meta | `core` |
| `_templates/` | New-note templates (not indexed) | — |
| `_attachments/` | Binary assets (not indexed) | — |
| `life/` | Daily life OS (areas, tasks, goals, habits, study, library) | `area` / `goal` / `task` / `habit` / `study` / `library` |
| `repo-library/` | Curated open-source repos & architecture references | `repo` |
| `Export/` | Raw Notion dump (staging only; ignored by lint & Obsidian) | — |

**Hard rule:** Project ≠ Client ≠ Hackathon ≠ Stack ≠ Subscription ≠ Repo Library. Never merge independent scopes into one file. Stack = capability inventory; Subscription = billing reality for a paid plan. Filename of a subscription note matches the tool slug (e.g. `subscriptions/anthropic-claude.md` ↔ `stack/anthropic-claude.md`); plan tier lives inside the note because plans change. Repo Library = external open-source repos curated for future evaluation/adoption (never mix with personal `projects/`).

**Life OS rule:** Life-OS notes live under `life/`. Do not dump export pages into `projects/` or `clients/`. Link existing Memo dossiers via wikilinks instead of duplicating them.

---

## 2. Frontmatter Schema

Every markdown note (except templates) starts with YAML:

```yaml
---
title: Human title
aliases: []
type: client|project|stack|...
status: active|paused|completed|planned|archived|empty
tags: [type/..., status/...]
entity_id: graph-id   # when a graph entity exists
created: YYYY-MM-DD
updated: YYYY-MM-DD
curated: true         # optional — protects fields from add_frontmatter overwrite
---
```

### Type-specific fields

- **client:** `website` · `platform` · `client_since` · `referred_by` · `contract`
- **project:** `repo` · `visibility` · `live_url` · `stack` · `hackathon` · `role`
- **repo:** `repo` · `category` · `intent` · `stars` · `license` · `status`
- **stack:** `category` · `vendor` · `cost`
- **bookmark:** `url` · `category` (leaf browser folder) · `browser_folder` (owner's browser folder code)
- **subscription:** `vendor` · `plan` · `payer` · `price` · `cycle` · `starts` · `ends` · `stack` (wikilink path to tool dossier)
- **hackathon:** `event_url` · `organizer` · `result` · `event_date`
- **profile:** `url` · `handle`
- **expertise:** `level` · `evidence` · `category`
- **area:** `area` (slug: developer|study|education|finance|personal)
- **goal:** `area` (wikilink) · `progress` · `total_steps` · `completed_steps`
- **task:** `area` (wikilink) · `priority` · `due` · `progress`
- **habit:** `area` (wikilink) · `category` · `cadence`
- **study:** `area` (wikilink) · `study_kind` (course|book|exam|note|video|assignment) · `category`
- **library:** `area` (wikilink) · `library_kind` (prompt|code|link|idea|note|resource) · `url`

**Zero invented facts.** Leave a field absent rather than guessing.

---

## 3. Tag Taxonomy (closed vocabulary)

- `type/` core · client · project · stack · subscription · expertise · hackathon · profile · agent · session · area · goal · task · habit · study · library · repo · bookmark
- `status/` active · paused · completed · planned · archived · empty · evaluating · saved (bookmarks only: kept for reference)
- `domain/` ai · ecommerce · edtech · fintech · healthtech · govtech · security · automation · design · education · interior-design · construction
- `tech/` react · python · supabase · n8n · wordpress · woocommerce · salla · flutter · fastapi · sqlite · docker · cloudflare · tailwind · nextjs · capacitor
- `visibility/` public · private
- `tier/` flagship · side · experiment · client-work
- `service/` seo · whatsapp · automation · web-development
- `area/` developer · study · education · finance · personal
- `study/` course · book · exam · note · video · assignment
- `library/` prompt · code · link · idea · note · resource

Any tag outside this list fails `scripts/vault_lint.py`.

---

## 4. Naming

- Filenames: `kebab-case.md` always
- Wikilinks: `[[path/without/extension]]` or `[[path|label]]`
- Attachments: `_attachments/<slug>-<short-description>.ext`
- Entity IDs: match graph ids (`demo-client`, `tech-react`, `hackathon-demo`)

---

## 5. Lifecycle

1. **Create** from `_templates/` (Templater folder templates auto-apply)
2. **Link** to related notes + set `entity_id` if graph-backed
3. **Flush** durable facts with `/memo` (not session scratch)
4. **Lint** with `python3 scripts/vault_lint.py`
5. **Archive** only when a year of timeline events is fully past → `archive/`

---

## 6. Tooling Map

| Tool | Role |
|---|---|
| Obsidian Bases (`bases/*.base`) | Human databases (Notion replacement) |
| Graph view + color groups | Visual clusters by `type/*` tags |
| Canvas (`canvas/*.canvas`) | Spatial ecosystem maps |
| `scripts/add_frontmatter.py` | Bulk property generation |
| `scripts/vault_lint.py` | Integrity gate |
| `scripts/memory_engine.py` / MCP | Agent retrieval + structured filters |
| `.gitattributes` | Merge drivers: `*.md` → `memo-note`, `sessions/current.md` → `union`, `timeline.md` → `memo-timeline` (via `scripts/memo_merge.py`) |

---

## 7. Agent Contract Addendum

- Prefer `memo_query_props` for structured inventories (e.g. all active Salla clients)
- Prefer `memo_search` with `type` / `status` / `tag` filters for keyword + property queries
- Create notes with `memo_create_note` (templated + lint-clean), never by hand — it enforces this spec
- Add content with `memo_append_section`; reserve `memo_update_fact` for corrections
- Historical meta under `docs/_open-questions.md` and `docs/_prompt-fill-gaps.md` is **not FTS-indexed** — read via `memo_get_dossier` only when needed
- Never write secrets into the vault
- Under-write rather than invent
- `HOME.md` is for humans; `README.md` is for agents — update both when identity facts change
