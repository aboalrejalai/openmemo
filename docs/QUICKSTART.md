---
title: OpenMemo Quickstart
type: core
status: active
tags:
  - type/core
  - status/active
created: 2026-11-16
updated: 2026-11-22
---
# OpenMemo Quickstart

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/aboalrejalai/openmemo/main/install.sh | sh
memo ui
```

Or clone and verify by hand:

```bash
git clone https://github.com/aboalrejalai/openmemo.git
cd openmemo
python3 scripts/test_mcp.py
python3 scripts/vault_lint.py
```

This repository root is the demo vault.

## 2. Run local MCP (stdio)

Point your agent at:

```bash
python3 /path/to/openmemo/scripts/mcp_server.py
```

Set `MEMO_VAULT` to the OpenMemo repo path if the process does not start inside it.

## 3. Wire an agent

Use the `memo-setup` skill (`.agents/skills/memo-setup/`) and choose Claude, Gemini, Cursor, and/or Codex.

## 4. Optional: remote MCP on a VPS

See [DEPLOY_HOSTINGER.md](DEPLOY_HOSTINGER.md).
