---
name: memo-setup
description: Wire OpenMemo for a new user and connect AI coding agents. Guides local stdio vs remote Docker MCP selection, generates config snippets for Claude Code, Gemini, Cursor, and Codex, details hook installation via the memo plugin launcher, and outlines Hostinger VPS deployment. Use when setting up OpenMemo, connecting an agent to Memo, configuring MCP servers, or deploying Memo on a VPS.
disable-model-invocation: true
---

# OpenMemo Setup & Agent Integration

Guide for onboarding a new user to OpenMemo and connecting AI coding agents to the Memo Model Context Protocol (MCP) server and capture hooks.

## Onboarding Workflow

Follow this sequence when onboarding a user:

```
1. Run `memo init` (Automated) → 2. Launch `memo ui` (Visual Studio) → 3. Or Configure Remote VPS
```

### Fastest Path: One-Command Install

If OpenMemo is not on the machine yet, this single command downloads it into `~/openmemo`, adds `memo` to `~/.local/bin`, and runs `memo init --yes`:

```bash
curl -fsSL https://raw.githubusercontent.com/aboalrejalai/openmemo/main/install.sh | sh
```

Running it again updates the install. Set `OPENMEMO_HOME` first to install elsewhere.

### Fast Path: Automated Setup on an Existing Clone (`memo init`)

For local installations, run the built-in onboarding wizard first. It automatically detects installed agent CLIs/IDEs (`Claude Code`, `Cursor`, `Google Antigravity`, `Gemini CLI`, `OpenAI Codex CLI`, `Windsurf`, `OpenCode`), wires their capture hooks and MCP entries, symlinks `.agents/skills/*` into `~/.agents/skills/` and `~/.claude/skills/`, and runs `memo doctor`:

```bash
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo init --yes
```

To open the interactive Knowledge Graph & Hybrid Search Web Studio after setup:

```bash
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo ui
```

### 1. Collect Configuration (Manual / Custom Mode)

Gather the following settings from the user (or confirm defaults):

- **Vault Path**: Path to the OpenMemo repository/vault (default: `${OPENMEMO_HOME:-$HOME/openmemo}`).
- **Deployment Mode**: 
  - **Local stdio**: Agent executes Python MCP server directly on the local machine.
  - **Remote Docker (HTTP/SSE)**: Agent connects over HTTPS to an OpenMemo server running in Docker (e.g. on a VPS).
- **Agents to Enable**: Select from Claude Code, Gemini CLI, Cursor, and Codex CLI (or additional adapters).
- **Credentials (Remote Mode)**: Prompt the user to provide their server endpoint (`https://<memo-domain>/mcp`). Never invent or generate passwords; remind the user to configure `PASSWORD` (at least 20 characters) in their server `.env`.

---

## Deployment Options

### Option A: Local stdio (Offline & Instant)

No network or containers required. Agents run `python3 scripts/mcp_server.py` directly against the local vault.

- Vault location: `${OPENMEMO_HOME:-$HOME/openmemo}`
- Database location: `<vault>/.memo/state.db` (created automatically on first launch)
- Background worker launcher: `${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo`

### Option B: Remote Docker VPS (Hostinger Recommended)

For hosting OpenMemo as an always-on remote MCP endpoint with automatic sync:

- Recommended VPS provider: **[Hostinger KVM VPS](https://www.hostinger.com/ua?REFERRALCODE=aboalrejalai)** (KVM plan with 2 GB+ RAM).
- Detailed deployment guide: See [docs/DEPLOY_HOSTINGER.md](../../../docs/DEPLOY_HOSTINGER.md).
- Quick deploy outline:
  1. Copy `deploy/memo-mcp/` files to `/docker/memo-mcp` on the VPS.
  2. Create `/docker/memo-mcp/.env` from `.env.example` (`chmod 600 .env`).
  3. **Security note**: The user must generate and insert their own strong `PASSWORD` (>= 20 characters) into `.env`. Never generate or hardcode passwords in agent prompts.
  4. Launch via Docker: `docker compose up -d`.
  5. Put Cloudflare Tunnel or a reverse proxy in front of `127.0.0.1:8765` for HTTPS.

---

## Agent Configuration (All Four Core Agents)

### 1. Claude Code

**MCP Server Configuration:**

- *Local stdio*:
  ```bash
  claude mcp add --scope user memo python3 ${OPENMEMO_HOME:-$HOME/openmemo}/scripts/mcp_server.py
  ```
- *Remote HTTP/SSE*:
  ```bash
  claude mcp add --scope user memo https://<your-memo-domain>/mcp
  ```

**Plugin & Hooks Installation:**

```bash
claude plugin marketplace add ${OPENMEMO_HOME:-$HOME/openmemo}
claude plugin install memo@memo-local
```

---

### 2. Gemini CLI

**Automated Setup:**

Run the automated installer:
```bash
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo install-gemini
```

**Manual Configuration:**

Add to `~/.gemini/settings.json`:

*Local stdio:*
```json
{
  "mcpServers": {
    "memo": {
      "command": "python3",
      "args": ["${OPENMEMO_HOME:-$HOME/openmemo}/scripts/mcp_server.py"],
      "timeout": 30000
    }
  }
}
```

*Remote HTTP/SSE:*
```json
{
  "mcpServers": {
    "memo": {
      "url": "https://<your-memo-domain>/mcp"
    }
  }
}
```

---

### 3. Cursor

**Automated Setup:**

Run the automated installer to symlink the plugin into `~/.cursor/plugins/local/memo`:
```bash
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo install-cursor
```
Then reload Cursor via the Command Palette: **Developer: Reload Window**.

**Manual MCP Configuration:**

In `.cursor/mcp.json` or Cursor settings:

*Local stdio:*
```json
{
  "mcpServers": {
    "memo": {
      "command": "python3",
      "args": ["${OPENMEMO_HOME:-$HOME/openmemo}/scripts/mcp_server.py"]
    }
  }
}
```

*Remote HTTP/SSE:*
```json
{
  "mcpServers": {
    "memo": {
      "url": "https://<your-memo-domain>/mcp"
    }
  }
}
```

---

### 4. Codex CLI

**Automated Setup:**

Run the automated installer to register the plugin marketplace and enable hooks:
```bash
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo install-codex
```

**Manual Configuration:**

In `~/.codex/config.toml`:
```toml
[features]
hooks = true

[mcp.memo]
command = "python3"
args = ["${OPENMEMO_HOME:-$HOME/openmemo}/scripts/mcp_server.py"]
```

Or for remote:
```toml
[features]
hooks = true

[mcp.memo]
url = "https://<your-memo-domain>/mcp"
```

---

## Additional Live Adapters

OpenMemo includes live adapters for additional AI development environments:

- **Antigravity**: Run `${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo install-antigravity` to configure `~/.gemini/config/hooks.json` and MCP config.
- **OpenCode**: Run `${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo install-opencode` to install `~/.config/opencode/plugins/memo.js` and register MCP in `opencode.json`.
- **Windsurf**: Run `${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo install-windsurf` to configure Claude-style hooks in `~/.config/devin/config.json`.
- **ZadLoop**: Supported via `plugin/zadloop/` hooks and engine adapters.

---

## Health Check and Verification

After installation, verify the setup using the built-in diagnostic tools:

```bash
# Check worker, queue, and capture status
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo status

# Run full system diagnostics
${OPENMEMO_HOME:-$HOME/openmemo}/plugin/bin/memo doctor
```

Ask the agent to call `memo_status` via MCP to ensure the connection is active and reporting index metrics.
