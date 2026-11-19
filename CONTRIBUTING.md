# Contributing to OpenMemo

## Ground rules

- Keep the demo vault free of personal data and secrets.
- Do not commit `.env`, deploy keys, or API tokens.
- Skills stay in English and follow Cursor create-skill structure under `.agents/skills/`.
- Run gates before opening a change:

```bash
python3 scripts/test_mcp.py
python3 scripts/vault_lint.py
```

## Scope

Product code lives in `scripts/`, `plugin/`, `deploy/`, `docs/`, and `.agents/skills/`. The repo root is also a working demo vault.
