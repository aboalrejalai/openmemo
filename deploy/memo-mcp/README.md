# OpenMemo remote MCP

Self-hosted Memo MCP behind [mcp-auth-proxy](https://github.com/sigbit/mcp-auth-proxy) **password-only** auth, published through the VPS host's systemd `cloudflared` Tunnel.

Auth (owner-locked 2026-11-13): one shared `PASSWORD` (min 20 chars). No Google / GitHub / OIDC. Access tokens 24h; refresh tokens **365 days** (patched build of proxy v2.10.2).

## Layout

| Path | Role |
|------|------|
| `Dockerfile` | Multi-stage: Go build of patched proxy + `python:3.12-slim` runtime (uid 1000) |
| `entrypoint.sh` | Password guard, clone, `machine.json`, git identity, exec proxy → `mcp_server.py` |
| `compose.yml` | Hardened container; `127.0.0.1:8765:8080`; **no** cloudflared service |
| `.env.example` | Placeholders only |

## VPS facts

Fill during TASK-004 / first deploy (do not invent values):

- cloudflared: host systemd unit (not a container)
- ingress: `memo.example.com` → `http://localhost:8765`
- docker / compose: record versions here when checked
- memory / vault status / arch / host ports / secret scan: record when checked

## First start (on the VPS)

1. Snapshot the VPS in Hostinger if you want a rollback point.
2. Create the deploy directory and copy files from this Mac:

```bash
ssh root@YOUR_VPS_IP 'mkdir -p /docker/memo-mcp && chmod 700 /docker/memo-mcp'
scp deploy/memo-mcp/Dockerfile deploy/memo-mcp/entrypoint.sh \
    deploy/memo-mcp/compose.yml deploy/memo-mcp/.env.example \
    root@YOUR_VPS_IP:/docker/memo-mcp/
```

3. Install a **read-only** GitHub deploy key as `/docker/memo-mcp/deploy_key` (`chmod 600`), and allow it on `YOUR_GITHUB_USER/openmemo`.
4. Create `.env` once (do **not** commit it):

```bash
cd /docker/memo-mcp
cp .env.example .env
chmod 600 .env
# Edit .env: set PASSWORD to a fresh ≥20-char secret (password manager).
# EXTERNAL_URL=https://memo.example.com
# TRUSTED_PROXIES=172.30.80.0/24
```

5. Build and start:

```bash
cd /docker/memo-mcp
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

6. Cloudflare Tunnel (host `cloudflared`): add public hostname `memo.example.com` → `http://localhost:8765`. Do not change other hostnames.
7. Smoke from your Mac:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://memo.example.com/mcp
# expect 401 until signed in
curl -s https://memo.example.com/.well-known/oauth-authorization-server | head
```

## Password and tokens

- Set `PASSWORD` only in `/docker/memo-mcp/.env` on the server (`chmod 600`).
- Sign in once in the browser when a client connects; refresh lasts ~1 year (patched proxy). Access tokens renew silently every 24h.
- To revoke every client: `docker compose down`, wipe the `memo-proxy-data` volume (or empty `/srv/proxy-data`), then `up -d`. Change `PASSWORD` if the secret leaked.

## Connect Cursor

1. Cursor Settings → MCP → add remote server URL `https://memo.example.com/mcp`.
2. Complete the password sign-in once when prompted; authorize the client.
3. Tools should list the remote profile (reads + `memo_save_memory` only).

## Connect Gemini

1. gemini.google.com → Settings → Connected Apps → Custom apps (or equivalent MCP connector).
2. URL `https://memo.example.com/mcp`; register automatically (DCR).
3. Sign in with the Memo password once; refresh ~1 year.

Claude / ChatGPT use the same URL and password flow (custom connector / MCP).

## Code updates

After pushing Memo changes to `main`, either restart the container (entrypoint ff-merges when clean) or wait for the background sync thread (plan 003 / TASK-001). If the child exits 75 after incoming `scripts/` merges, `docker compose restart memo-mcp`.

## Sync conflicts

When plan 003 sync is live: use `docker compose exec memo-mcp …` and `memo sync resolve` on the VPS worktree. Until then, only the entrypoint's `git fetch` + `ff-only` runs at start.

## Upkeep

Monthly: check [mcp-auth-proxy releases](https://github.com/sigbit/mcp-auth-proxy/releases) and advisories; bump `PROXY_REF` / rebuild after reading the changelog. Re-pin the `python:3.12-slim` digest when rebuilding.

## Isolation notes

- Port published only on `127.0.0.1:8765` (never `0.0.0.0`).
- Own bridge `memo-net` (`172.30.80.0/24`); no Docker socket; read-only root; `cap_drop: ALL`; `no-new-privileges`; 512m memory.
- Do not delete or reconfigure other VPS services (Vaultwarden, n8n, existing Tunnel routes).
