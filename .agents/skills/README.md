# OpenMemo Agent Skills

A collection of Agent Skills for AI coding assistants (Claude Code, Gemini CLI, Cursor, Codex, and Antigravity) working with the OpenMemo sovereign memory vault.

Each skill conforms to the Cursor Agent Skill specification with YAML frontmatter, focused progressive disclosure, and explicit trigger scenarios.

---

## Skills Overview

| Skill | Role | Primary Use Cases |
| :--- | :--- | :--- |
| **[`memo-setup`](memo-setup/SKILL.md)** | Primary Onboarding | Wire OpenMemo MCP server and hooks for Claude Code, Gemini CLI, Cursor, and Codex CLI. Guides local stdio vs. remote Docker deployment on Hostinger VPS. |
| **[`memo`](memo/SKILL.md)** | Day-to-Day Recall | Recall past session context using the three-step flow (`memo_search` → `memo_session_timeline` → `memo_get_observations`) and record decisions via `memo_save_memory` and `memo_append_fact`. |
| **[`memo-vault`](memo-vault/SKILL.md)** | Curated Dossier Writes | Create and update structured dossiers (`projects/`, `clients/`, `stack/`) and knowledge graph entities using MCP write tools while enforcing `docs/VAULT_SPEC.md`. |
| **[`memo-session-archive`](memo-session-archive/SKILL.md)** | Raw Session Archiving | Batch extract raw agent session transcripts into `sessions/_raw-<host>/` across Claude Code, Cursor, Codex, Gemini, and Antigravity, followed by bloat cleanup. |

---

## Installation & Symlinking

### 1. Global User Skills (`~/.agents/skills/`)

To make these skills available across all projects on your machine:

```bash
OPENMEMO_HOME="${OPENMEMO_HOME:-$HOME/openmemo}"
mkdir -p ~/.agents/skills

for skill in memo-setup memo memo-vault memo-session-archive; do
  ln -sfn "$OPENMEMO_HOME/.agents/skills/$skill" "$HOME/.agents/skills/$skill"
done
```

### 2. Cursor Project Skills (`.cursor/skills/`)

To enable these skills specifically inside a target project:

```bash
OPENMEMO_HOME="${OPENMEMO_HOME:-$HOME/openmemo}"
mkdir -p .cursor/skills

for skill in memo-setup memo memo-vault memo-session-archive; do
  ln -sfn "$OPENMEMO_HOME/.agents/skills/$skill" ".cursor/skills/$skill"
done
```
