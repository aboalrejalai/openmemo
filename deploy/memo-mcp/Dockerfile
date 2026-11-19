# Memo remote MCP: patched mcp-auth-proxy + vault runtime.
# Proxy built from source tag v2.10.2; RefreshTokenLifespan patched to 365d.
# python:3.12-slim (linux/amd64 digest pinned below; re-check when rebuilding).

ARG GOLANG_IMAGE=golang:1.26-bookworm
ARG PYTHON_IMAGE=python:3.12-slim@sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177
ARG PROXY_REF=v2.10.2

# ---------- stage: build mcp-auth-proxy with 365d refresh ----------
FROM ${GOLANG_IMAGE} AS proxy-build
ARG PROXY_REF
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${PROXY_REF}" https://github.com/sigbit/mcp-auth-proxy.git .
# Branded login + authorize pages (OpenMemo branding).
COPY branding/login.html pkg/auth/templates/login.html
COPY branding/authorize.html /tmp/authorize.html
COPY branding/patch_authorize.py /tmp/patch_authorize.py
RUN python3 /tmp/patch_authorize.py
# Owner requirement: refresh ~1 year; access stays 24h (upstream default).
RUN sed -i 's|RefreshTokenLifespan:[[:space:]]*30 \* 24 \* time\.Hour|RefreshTokenLifespan:           365 * 24 * time.Hour|' pkg/idp/idp.go \
    && grep -n 'RefreshTokenLifespan' pkg/idp/idp.go
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/mcp-auth-proxy .

# ---------- stage: runtime ----------
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TZ=Asia/Riyadh

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client tzdata ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# GitHub SSH host keys (build-time HTTPS fetch of api.github.com/meta).
RUN python3 -c "import json,urllib.request; from pathlib import Path; \
meta=json.load(urllib.request.urlopen('https://api.github.com/meta', timeout=60)); \
keys=meta.get('ssh_keys') or []; \
assert keys, 'api.github.com/meta returned no ssh_keys'; \
Path('/etc/ssh/ssh_known_hosts').write_text(chr(10).join('github.com '+k for k in keys)+chr(10)); \
print('wrote %d github.com host keys' % len(keys))"

COPY --from=proxy-build /out/mcp-auth-proxy /usr/local/bin/mcp-auth-proxy
RUN chmod 755 /usr/local/bin/mcp-auth-proxy

RUN groupadd --gid 1000 memo \
    && useradd --uid 1000 --gid 1000 --home-dir /tmp --shell /usr/sbin/nologin memo \
    && mkdir -p /srv/memo /srv/proxy-data \
    && chown -R 1000:1000 /srv/memo /srv/proxy-data

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

USER 1000:1000
WORKDIR /srv/memo
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
