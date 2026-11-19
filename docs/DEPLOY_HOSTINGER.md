---
title: Deploy OpenMemo on Hostinger
type: core
status: active
tags:
  - type/core
  - status/active
created: 2026-11-16
updated: 2026-11-16
---
# Deploy OpenMemo MCP on a Hostinger VPS

Recommended path: a Hostinger KVM VPS running Docker, with optional Cloudflare Tunnel in front.

## 1. Get a VPS

Use Hostinger (referral): [https://www.hostinger.com/ua?REFERRALCODE=aboalrejalai](https://www.hostinger.com/ua?REFERRALCODE=aboalrejalai)

Pick a KVM plan with enough RAM for Docker (2 GB+ recommended).

## 2. Copy deploy files

From your OpenMemo clone:

```bash
ssh root@YOUR_VPS_IP 'mkdir -p /docker/memo-mcp && chmod 700 /docker/memo-mcp'
scp deploy/memo-mcp/Dockerfile deploy/memo-mcp/entrypoint.sh \
    deploy/memo-mcp/compose.yml deploy/memo-mcp/.env.example \
    root@YOUR_VPS_IP:/docker/memo-mcp/
```

## 3. Configure secrets

```bash
cd /docker/memo-mcp
cp .env.example .env
chmod 600 .env
# Set PASSWORD (>= 20 chars), EXTERNAL_URL=https://memo.example.com, TRUSTED_PROXIES as needed
```

Install a read-only GitHub deploy key as `/docker/memo-mcp/deploy_key` (`chmod 600`) with access to your OpenMemo fork/clone.

## 4. Build and run

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f --tail=100
```

Compose publishes the proxy on `127.0.0.1:8765` by default. Point Cloudflare Tunnel (or another reverse proxy) at that port.

## 5. Connect an agent

Use your public MCP URL (for example `https://memo.example.com/mcp`) in Cursor, Claude Code, Gemini, or Codex. Complete the password sign-in once when prompted.

Full container notes: [deploy/memo-mcp/README.md](../deploy/memo-mcp/README.md).
