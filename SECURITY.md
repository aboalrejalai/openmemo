# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report it privately through GitHub:
[Security → Report a vulnerability](https://github.com/aboalrejalai/openmemo/security/advisories/new).

Include what you found, how to reproduce it, and the version (`memo --version`).
You should get a first reply within 7 days. Once a fix is released, the advisory
is published with credit to the reporter unless you ask otherwise.

## What OpenMemo stores and where

- Everything stays on your machine by default: Markdown notes in your vault and
  an SQLite index in `<vault>/.memo/`. No telemetry, no cloud calls from search.
- Captured sessions pass through a redaction step that removes API keys, tokens,
  private keys and password assignments before anything is written
  (`scripts/memo_capture/privacy.py`, tested in `tests/capture/test_privacy.py`).
- The optional remote MCP container is protected by a single password. Use a long
  one (20+ characters), serve it over HTTPS only, and keep `.env` out of git
  (`chmod 600 .env`).
- `memo sync` pushes your vault to the Git remote you configure. Use a private
  repository for a personal vault.
