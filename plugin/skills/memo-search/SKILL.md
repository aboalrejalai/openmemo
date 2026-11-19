---
name: memo-search
description: Find what earlier sessions did, decided or left unfinished, from Memo's recorded session memory. Use when the user asks "where did we stop", "what did we change", "why did we do X", "وين وقفنا", "وش عدّلنا", "ليش سوينا", or refers to past work in this or another project, and when the injected Memo context names an observation you need in full.
---

# Memo search

Memo records every agent session as typed observations and per-request
summaries, and keeps them in the vault under `sessions/<project>/`. Read them
in three steps, fetching details only for what the task needs:

1. **Index:** `memo_search(query, project?, scope="sessions")` returns
   `ID DATE [PROJECT] TYPE · TITLE` rows. Summary ids start with `S`.
   Filters: `obs_type` (bugfix, feature, refactor, change, discovery, decision,
   security_alert, security_note, sensitive), `concept` (how-it-works,
   why-it-exists, what-changed, problem-solution, gotcha, pattern, trade-off),
   `since` / `until` (YYYY-MM-DD). Without `scope`, vault notes are searched too.
2. **Around one item:** `memo_session_timeline(anchor=<id | S<id> | date>)`
   shows what happened before and after it.
3. **Details:** `memo_get_observations(ids=[...])` returns facts, narrative,
   files and the vault note for each id.

To record something capture cannot see (a decision made on a call, a client
detail), use `memo_save_memory(title, text, project, type)`. Never save
passwords, keys or tokens.

The project name is the git repository's folder name (a worktree is
`repo/worktree`). The life and business timeline in `timeline.md` is a
different tool: `memo_get_timeline`.
