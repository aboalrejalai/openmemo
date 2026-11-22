# OpenMemo

[![CI](https://github.com/aboalrejalai/openmemo/actions/workflows/ci.yml/badge.svg)](https://github.com/aboalrejalai/openmemo/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-0.1.0-blue)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB)](pyproject.toml)

Open-source sovereign memory vault & zero-cost hybrid search engine for AI coding agents: curated Markdown dossiers, Reciprocal Rank Fusion (FTS5 BM25 + Arabic/Substring Trigram + Knowledge Graph Aliases), built-in interactive Web Studio (`memo ui`), MCP server, and multi-agent capture hooks.

<p align="center">
  <a href="https://www.hostinger.com/ua?REFERRALCODE=aboalrejalai">
    <img src="docs/assets/hostinger/hostinger-icon.svg" alt="Deploy on Hostinger VPS" width="64" height="64">
  </a>
</p>

<p align="center">
  <a href="https://www.hostinger.com/ua?REFERRALCODE=aboalrejalai"><strong>🚀 Deploy OpenMemo on Hostinger KVM VPS</strong></a>
  · Official OpenMemo Partner Link
</p>

---

## Install (one command)

**macOS / Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/aboalrejalai/openmemo/main/install.sh | sh
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/aboalrejalai/openmemo/main/install.ps1 | iex
```

That's it. The installer:

1. checks for `git` and Python 3.9+ (macOS already has both; on Windows: `winget install Git.Git Python.Python.3.12`),
2. downloads OpenMemo into `~/openmemo`,
3. adds the `memo` command to your PATH (`~/.local/bin`, or `openmemo\\plugin\\bin` on Windows),
4. runs `memo init`, which finds Claude Code, Cursor, Codex, Gemini CLI, Antigravity, Windsurf and OpenCode and wires them up.

Then open the Studio:

```bash
memo ui
```

Run the same command again any time to update. To install somewhere else, set `OPENMEMO_HOME=/path/to/folder` before `sh`.

## Features

- **Local hybrid search (`hybrid-rrf`)**: fuses vault FTS5, session BM25, an Arabic/substring trigram index and knowledge-graph aliases. No API calls, no cost.
- **Studio (`memo ui`)**: local web app at `http://127.0.0.1:8766` with the knowledge graph, live memory stream, duplicate cleaner and Markdown dossier reader.
- **Automatic session memory**: hooks record every agent session as Markdown in your vault, with secrets redacted before anything is written.
- **MCP server**: the same memory for any MCP client, locally over stdio or remotely with `docker compose up -d`.

---

## Choose Your Deployment Mode

| Feature | Local (`install.sh`) | 1-Click Hostinger VPS (`docker compose`) |
| --- | --- | --- |
| **Best For** | Individual machine & offline coding | Always-on remote MCP across desktop, laptop & web agents |
| **Setup Command** | `curl -fsSL …/install.sh \| sh` | `docker compose up -d` on [Hostinger KVM VPS](https://www.hostinger.com/ua?REFERRALCODE=aboalrejalai) |
| **Transport** | Local `stdio` (`scripts/mcp_server.py`) | HTTPS / Streaming MCP (`mcp-auth-proxy` on `:8080`) |
| **Auth** | Local OS user permissions | Password-only auth (auto-generated on first boot or via `.env`) |
| **Visual Studio** | `memo ui` (`127.0.0.1:8766`) | Local tunnel or Git-synced desktop clone |

---

## Manual install

If you prefer not to pipe a script into `sh`:

```bash
git clone https://github.com/aboalrejalai/openmemo.git ~/openmemo
cd ~/openmemo
./plugin/bin/memo init
./plugin/bin/memo ui
```

Run the test suites:

```bash
python3 scripts/test_mcp.py
python3 scripts/vault_lint.py
python3 -m unittest discover -s tests/capture -p "test_*.py"
```

Full walkthrough: [docs/QUICKSTART.md](docs/QUICKSTART.md).

---

## 1-Click VPS Deploy (Hostinger)

OpenMemo includes a root [`docker-compose.yml`](docker-compose.yml) ready for Hostinger's Docker VPS stack:

```bash
git clone https://github.com/aboalrejalai/openmemo.git
cd openmemo
cp .env.example .env
docker compose up -d
```

If `PASSWORD` is left blank in `.env`, the container automatically generates a cryptographically secure 24-character password on first boot, persists it in `/srv/proxy-data/.initial_password`, and prints it in `docker compose logs`.

Full VPS guide: [docs/DEPLOY_HOSTINGER.md](docs/DEPLOY_HOSTINGER.md).

---

## Skills (`.agents/skills/`)

Canonical skills live in [`.agents/skills/`](.agents/skills/) and are automatically linked into `~/.agents/skills/` when you run `./plugin/bin/memo init`:

- **`memo-setup`**: Interactive agent onboarding skill (works hand-in-hand with `memo init`).
- **`memo`**: Day-to-day recall, timeline queries, and observation retrieval.
- **`memo-vault`**: Institutional dossier creation and taxonomy-validated updates.
- **`memo-session-archive`**: Raw session transcript archiver and cleaner.

---

## Docs

| Doc | Purpose |
| --- | --- |
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | Clone → `memo init` → `memo ui` |
| [docs/VAULT_SPEC.md](docs/VAULT_SPEC.md) | Note types, tags, naming |
| [docs/MCP_SERVER.md](docs/MCP_SERVER.md) | Tool and protocol reference |
| [docs/DEPLOY_HOSTINGER.md](docs/DEPLOY_HOSTINGER.md) | VPS + Docker remote MCP |
| [docs/MEMO_GOALS.md](docs/MEMO_GOALS.md) | Product goals |
| [deploy/memo-mcp/README.md](deploy/memo-mcp/README.md) | Container details |

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). Release notes are in [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
