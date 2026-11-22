---
name: memo-vault
description: Author and update curated Markdown dossiers and knowledge graph entities in the Memo vault using MCP write tools (memo_create_note, memo_append_section, memo_append_fact, memo_update_fact, memo_add_entity). Follows the six core root hubs policy, folder taxonomy, and schema constraints in docs/VAULT_SPEC.md. Use when authoring client or project dossiers, modifying stack notes, adding knowledge graph entities, or validating vault integrity.
---

# Memo Vault: Curated Dossier & Graph Management

Guide for authoring and maintaining structured Markdown dossiers and knowledge graph records in the OpenMemo sovereign vault using Model Context Protocol (MCP) write tools.

All vault structures, frontmatter schemas, and taxonomy rules are governed by [docs/VAULT_SPEC.md](../../../docs/VAULT_SPEC.md).

---

## Core Vault Rules

1. **English Vault Prose**: Write all curated dossier content and frontmatter values in clear, factual English (unless preserving a proper noun or verbatim client transcript).
2. **Six Root Hubs Only**: The root of the vault is strictly limited to exactly six living hubs:
   - `README.md` (Agent contract & operating persona)
   - `HOME.md` (Human dashboard)
   - `timeline.md` (Living chronological history)
   - `decisions.md` (Locked decisions & rationales)
   - `stack.md` (Tooling map of content in `stack/`)
   - `subscriptions.md` (Billing & renewal map of content in `subscriptions/`)
   *Never create new root-level markdown files.* All other notes belong in their designated subdirectories.
3. **Zero Invented Facts**: Under-write rather than guess. Omit optional fields if verified data is unavailable.
4. **Never Write Secrets**: API tokens, passwords, private keys, and session cookies are strictly forbidden in vault files.

---

## Vault Folder Taxonomy

Each subdirectory has a strict semantic purpose and corresponding `type`:

| Subdirectory | Role | `type` Tag |
| :--- | :--- | :--- |
| `projects/` | Products, repositories, and technical builds | `type/project` |
| `clients/` | Commercial accounts and client ventures | `type/client` |
| `stack/` | Software tools, libraries, and platforms actually in use | `type/stack` |
| `subscriptions/`| Paid plans, renewal schedules, and billing cycles | `type/subscription` |
| `expertise/` | Cross-cutting disciplines and capability maps | `type/expertise` |
| `hackathons/` | Competitions and hackathon submissions | `type/hackathon` |
| `profiles/` | Public channels, social handles, and personal profiles | `type/profile` |
| `repo-library/` | Curated third-party open-source reference repositories | `type/repo` |
| `life/` | Personal productivity system (areas, goals, tasks, habits) | `type/area`, `type/task` |
| `sessions/` | Automatically recorded agent session logs and raw archives | `type/session` |

---

## MCP Authoring Tools & Workflows

Always use the dedicated MCP tools to modify the vault. The server enforces integrity checks, atomic writes, and path-scoped git snapshots automatically.

### 1. Creating a New Dossier (`memo_create_note`)

Use `memo_create_note` to instantiate a note from `_templates/<type>.md` with lint-clean frontmatter:

```json
{
  "name": "memo_create_note",
  "arguments": {
    "path": "projects/cloud-sync.md",
    "type": "project",
    "title": "Cloud Sync Service",
    "status": "active",
    "tags": ["tech/python", "tech/docker", "domain/automation"],
    "entity_id": "cloud-sync"
  }
}
```

- **Path constraint**: Must be lowercase `kebab-case.md` inside an approved directory (e.g. `projects/`, `clients/`, `stack/`).
- **Validation**: Automatically validates tags against the closed taxonomy defined in `scripts/vault_lint.py`.

### 2. Appending Content to a Dossier (`memo_append_section`)

To add information to an existing dossier without rewriting the entire document:

```json
{
  "name": "memo_append_section",
  "arguments": {
    "path": "projects/cloud-sync.md",
    "heading": "Architecture",
    "content": "The sync engine uses SQLite WAL mode for local state and batches mutations over HTTPS to the remote worker.",
    "create_missing": true
  }
}
```

- Inserts content at the end of the specified section before the next heading of equivalent level.
- Skips duplicate content if already present.

### 3. Correcting an Existing Fact (`memo_update_fact`)

For precise, in-place corrections to existing text:

```json
{
  "name": "memo_update_fact",
  "arguments": {
    "path": "projects/cloud-sync.md",
    "old_text": "status: planned",
    "new_text": "status: active"
  }
}
```

- **Safety check**: Refuses execution unless `old_text` matches **exactly once** in the target file, preventing unintended or ambiguous replacements.

### 4. Adding Knowledge Graph Entities (`memo_add_entity`)

To register a node or edge in `graph/entities.json`:

```json
{
  "name": "memo_add_entity",
  "arguments": {
    "id": "cloud-sync",
    "type": "project",
    "name": "Cloud Sync Service",
    "relations": [
      {
        "target": "tech-python",
        "relation": "built_with"
      }
    ]
  }
}
```

- Validates entity IDs, relationship names, and target existence before writing.

### 5. Validating Vault Integrity (`memo_lint`)

Before completing any batch of edits, run `memo_lint`:

```json
{
  "name": "memo_lint",
  "arguments": {}
}
```

- Checks for missing required frontmatter, invalid tags outside taxonomy, broken wikilinks, and mismatched `entity_id` mappings.
- Can also be run directly via CLI: `python3 scripts/vault_lint.py`.
