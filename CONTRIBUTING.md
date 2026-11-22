# Contributing to OpenMemo

## Ground rules

- Keep the demo vault free of personal data and secrets.
- Do not commit `.env`, deploy keys, or API tokens.
- Skills stay in English and follow Cursor create-skill structure under `.agents/skills/`.
- Run gates before opening a change:

```bash
python3 scripts/test_mcp.py
python3 scripts/vault_lint.py
python3 -m unittest discover -s tests/capture -p "test_*.py"
```

CI runs the same three gates on Linux and macOS (Python 3.9, 3.12, 3.13). Add a line under `## [Unreleased]` in `CHANGELOG.md` for any user-visible change.

## Scope

Product code lives in `scripts/`, `plugin/`, `deploy/`, `docs/`, and `.agents/skills/`. The repo root is also a working demo vault.
