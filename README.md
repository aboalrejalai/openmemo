# OpenMemo v2.1

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

## What's New in v2.1

- **Zero-Cost Unified Hybrid Search (`hybrid-rrf`)**: Fuses 4 local signals in sub-20ms with $0 API cost — Vault Dossier FTS5, Session Observation BM25, Arabic/Substring Trigram index, and Knowledge Graph bilingual alias expansion.
- **Built-in Interactive Web Studio (`memo ui`)**: Single-file local web server (`http://127.0.0.1:8766`) with a 60fps HTML5 Canvas force-directed/radial Knowledge Graph, live memory stream, one-click duplicate observation cleaner, and slide-over Markdown dossier reader.
- **One-Command Onboarding (`memo init`)**: Auto-detects and wires installed AI agents (`Claude Code`, `Cursor`, `Google Antigravity`, `Gemini CLI`, `OpenAI Codex CLI`, `Windsurf`, `OpenCode`), symlinks skills, and runs `memo doctor`.
- **1-Click Hostinger Docker Compose**: Root `docker-compose.yml` with auto-generated 24-character initial password fallback in `entrypoint.sh` for instant VPS deployment.

---

## Choose Your Deployment Mode

| Feature | Local (`memo init` / `uvx`) | 1-Click Hostinger VPS (`docker compose`) |
| --- | --- | --- |
| **Best For** | Individual machine & offline coding | Always-on remote MCP across desktop, laptop & web agents |
| **Setup Command** | `./plugin/bin/memo init` | `docker compose up -d` on [Hostinger KVM VPS](https://www.hostinger.com/ua?REFERRALCODE=aboalrejalai) |
| **Transport** | Local `stdio` (`scripts/mcp_server.py`) | HTTPS / Streaming MCP (`mcp-auth-proxy` on `:8080`) |
| **Auth** | Local OS user permissions | Password-only auth (auto-generated on first boot or via `.env`) |
| **Visual Studio** | `./plugin/bin/memo ui` (`127.0.0.1:8766`) | Local tunnel or Git-synced desktop clone |

---

## Quickstart (Local Zero-Friction Setup)

```bash
git clone https://github.com/aboalrejalai/openmemo.git ~/openmemo
cd ~/openmemo

# 1. Auto-detect & wire your installed AI agents + skills + diagnostics
./plugin/bin/memo init

# 2. Launch the interactive Knowledge Graph & Hybrid Search Web Studio
./plugin/bin/memo ui
```

Run verification suites anytime:

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

## License

MIT — see [LICENSE](LICENSE).
