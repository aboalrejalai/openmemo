# Memo (Cursor plugin)

Automatic session memory for Cursor — hooks, MCP, and `memo-search`, matching the Claude Code plugin.

## Install (this Mac)

```bash
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo install-cursor
```

That symlinks this folder into `~/.cursor/plugins/local/memo` and migrates user-level Memo hooks into the plugin. Then **Developer: Reload Window**.

## What you get

| Piece | Role |
| --- | --- |
| Hooks | Capture prompts, tools, edits; inject prior context into `.cursor/rules/memo-context.mdc` |
| MCP `memo` | Vault search / timeline / dossier / write tools |
| Skill `memo-search` | Three-step recall over recorded sessions |
| Commands | `/memo-status`, `/memo-doctor` |

## Layout

Canonical source lives in the Memo vault at `plugin/`. Claude Code reads `.claude-plugin/`; Cursor reads `.cursor-plugin/` and `hooks/cursor-plugin-hooks.json`.
