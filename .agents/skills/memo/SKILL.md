---
name: memo
description: Recall context and record decisions using the Memo MCP server. Implements the three-step session memory flow (search -> session_timeline -> get_observations) and records facts or decisions via memo_save_memory and memo_append_fact. Use when searching project memory, resuming previous tasks, asking what changed or where we stopped, or saving milestone decisions.
disable-model-invocation: true
---

# Memo: Day-to-Day Session Memory & Recall

Guide for using OpenMemo Model Context Protocol (MCP) tools during day-to-day coding sessions to search past context, recall previous decisions, and record new discoveries.

> **Scope Note**: 
> - For initial onboarding, agent wiring, or VPS setup, see `memo-setup`.
> - For authoring or modifying curated vault dossiers (clients, projects, stack notes, graph entities), see `memo-vault`.

---

## The Three-Step Session Memory Flow

When answering questions such as *"where did we stop?"*, *"what changed in the last session?"*, or *"why did we make this architecture choice?"*, always follow the three-step recall pattern:

```
Step 1: memo_search ──> Step 2: memo_session_timeline ──> Step 3: memo_get_observations
   (Compact Index)           (Context Around Anchor)            (Full Details On Demand)
```

### Step 1: Search the Compact Index (`memo_search`)

Run a keyword search across the session memory index and vault.

```json
{
  "name": "memo_search",
  "arguments": {
    "query": "authentication provider Supabase",
    "project": "my-app",
    "limit": 5
  }
}
```

- Multi-word queries evaluate through a fallback ladder: `phrase` → `all-terms` → `any-term` → `prefix` → `substring` (trigram).
- Returns high-level matches with observation IDs (e.g. `142`) and summary IDs (e.g. `S12`).
- Inspect the returned hits to identify relevant anchors.

### Step 2: Inspect Context Around an Anchor (`memo_session_timeline`)

Once a relevant observation or summary ID is identified, examine what happened immediately before and after it across sessions:

```json
{
  "name": "memo_session_timeline",
  "arguments": {
    "anchor": "142",
    "before": 5,
    "after": 5,
    "project": "my-app"
  }
}
```

- `anchor` can be an observation ID (integer or string), an `S`-prefixed summary ID (e.g. `"S12"`), or an ISO date (`"YYYY-MM-DD"`).
- Returns a chronological, oldest-first index showing adjacent steps and interactions.

### Step 3: Retrieve Full Details On Demand (`memo_get_observations`)

To conserve context window tokens, fetch detailed narratives, diffs, and touched file lists only for the specific IDs needed for the immediate task:

```json
{
  "name": "memo_get_observations",
  "arguments": {
    "ids": [142, 145]
  }
}
```

- Returns the complete recorded narrative, standalone facts, and touched files.
- Fetch only what is necessary to answer the user request.

---

## Recording Decisions & Memories

When a concrete decision, breakthrough, or external detail is agreed upon during a conversation, record it using the appropriate tool.

### 1. Saving Session Decisions (`memo_save_memory`)

Use `memo_save_memory` for key decisions, architectural findings, or context that automatic file capture might miss:

```json
{
  "name": "memo_save_memory",
  "arguments": {
    "title": "Decided to adopt PostgreSQL RLS for multi-tenant isolation",
    "text": "Evaluated application-level tenant filtering vs Postgres row-level security. Chose RLS for consistent enforcement across direct queries and background workers.",
    "project": "my-app",
    "type": "decision",
    "facts": [
      "Tenant isolation enforced via PostgreSQL RLS",
      "Bypass role reserved for superadmin migrations"
    ]
  }
}
```

**Parameters:**
- `title`: Short summary starting with an active past-tense verb (`Decided`, `Found`, `Implemented`, `Fixed`).
- `text`: Concise explanation of what was decided and why it matters.
- `project`: Target project slug.
- `type`: Category (`decision`, `feature`, `bugfix`, `discovery`, `refactor`, `change`, `security_note`).
- `facts`: Optional list of discrete, standalone factual bullet points.

*Safety Rule*: Never include secrets, API tokens, credentials, or private passwords.

### 2. Logging Durable Milestones (`memo_append_fact`)

To record a major milestone into the permanent chronological history in `timeline.md`:

```json
{
  "name": "memo_append_fact",
  "arguments": {
    "summary": "Completed migration from Auth0 to Supabase Auth with zero downtime",
    "category": "Tech",
    "link": "[[projects/my-app]]"
  }
}
```

- Inserts the entry into the correct month section of `timeline.md` in reverse-chronological order.
- Includes built-in near-duplicate detection to prevent recording duplicate milestones.

---

## Summary Checklist

| Task | Recommended Tool |
| :--- | :--- |
| Find past session context or discussions | `memo_search` |
| View chronological order of events around a session event | `memo_session_timeline` |
| Read full facts & narrative of specific events | `memo_get_observations` |
| Record an important decision or architecture choice | `memo_save_memory` |
| Record a high-level project or company milestone | `memo_append_fact` |
| Query structured properties across notes without text | `memo_query_props` |
| Create or update curated files in `projects/` or `clients/` | Refer to `memo-vault` |
