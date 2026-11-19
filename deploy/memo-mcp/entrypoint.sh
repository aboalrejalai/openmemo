#!/bin/sh
# OpenMemo VPS entrypoint: password-only mcp-auth-proxy wrapping mcp_server.py.
# Supports zero-config 1-click Hostinger deploy by auto-generating a persistent
# 24-character password if PASSWORD is not provided in environment variables.
set -eu

fail() {
  echo "openmemo-mcp entrypoint: $*" >&2
  exit 1
}

INITIAL_PW_FILE="/srv/proxy-data/.initial_password"

if [ -z "${PASSWORD:-}" ]; then
  mkdir -p /srv/proxy-data
  if [ -f "$INITIAL_PW_FILE" ]; then
    PASSWORD=$(cat "$INITIAL_PW_FILE")
  else
    PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18)[:24])')
    umask 077
    printf '%s' "$PASSWORD" > "$INITIAL_PW_FILE"
  fi
  export PASSWORD
  echo "======================================================================" >&2
  echo " OpenMemo 1-Click Deploy: Auto-Generated Initial Password" >&2
  echo " PASSWORD: $PASSWORD" >&2
  echo " (Persisted at $INITIAL_PW_FILE)" >&2
  echo "======================================================================" >&2
fi

pw_len=$(printf '%s' "$PASSWORD" | wc -c | tr -d ' ')
[ "$pw_len" -ge 20 ] || fail "PASSWORD must be at least 20 characters (got ${pw_len})"

forbid_set() {
  eval "val=\${$1-}"
  [ -z "$val" ] || fail "$1 must be unset for password-only auth (got a value)"
}
for v in \
  GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET GOOGLE_ALLOWED_USERS GOOGLE_ALLOWED_WORKSPACES \
  GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET GITHUB_ALLOWED_USERS GITHUB_ALLOWED_ORGS \
  GITHUB_URL GITHUB_API_URL \
  OIDC_CONFIGURATION_URL OIDC_CLIENT_ID OIDC_CLIENT_SECRET \
  OIDC_ALLOWED_USERS OIDC_ALLOWED_USERS_GLOB OIDC_ALLOWED_ATTRIBUTES OIDC_ALLOWED_ATTRIBUTES_GLOB \
  OIDC_SCOPES OIDC_USER_ID_FIELD OIDC_PROVIDER_NAME
do
  forbid_set "$v"
done

export EXTERNAL_URL="${EXTERNAL_URL:-https://localhost:8080}"
case "$EXTERNAL_URL" in
  https://*) ;;
  *) fail "EXTERNAL_URL must start with https:// (got: $EXTERNAL_URL)" ;;
esac

export MEMO_REMOTE="${MEMO_REMOTE:-1}"
export MEMO_VAULT="${MEMO_VAULT:-/srv/memo}"
export HOME="${HOME:-/tmp}"
export TZ="${TZ:-UTC}"
export MEMO_REPO_URL="${MEMO_REPO_URL:-https://github.com/aboalrejalai/openmemo.git}"

if [ -f /run/secrets/deploy_key ]; then
  export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -i /run/secrets/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes}"
elif [ "$MEMO_REPO_URL" = "git@github.com:YOUR_GITHUB_USER/openmemo.git" ]; then
  export MEMO_REPO_URL="https://github.com/aboalrejalai/openmemo.git"
fi

# ---- clone vault if volume is empty ----
if [ ! -d "$MEMO_VAULT/.git" ]; then
  echo "openmemo-mcp entrypoint: cloning $MEMO_REPO_URL into $MEMO_VAULT" >&2
  git clone "$MEMO_REPO_URL" "$MEMO_VAULT"
fi

mkdir -p "$MEMO_VAULT/.memo"

# Per-machine identity (untracked; .memo/* is gitignored except settings.json).
python3 -c 'import json,os; from pathlib import Path; p=Path(os.environ["MEMO_VAULT"])/".memo"/"machine.json"; w={"name":"vps","id_base":1000000000,"host":"memo-vps","owns_unstamped":False,"accept_incoming_code":True}; p.write_text(json.dumps(w, indent=2)+"\n", encoding="utf-8"); print("openmemo-mcp entrypoint: wrote", p)'

git -C "$MEMO_VAULT" config user.name "OpenMemo VPS"
git -C "$MEMO_VAULT" config user.email "openmemo@example.com"

if [ -f "$MEMO_VAULT/scripts/memo_core.py" ]; then
  (
    cd "$MEMO_VAULT"
    python3 -c 'import sys; from pathlib import Path; sys.path.insert(0, str(Path("scripts").resolve()));
import memo_core
fn = getattr(memo_core, "ensure_git_config", None)
if fn is not None:
    fn()' || true
  )
fi

if git -C "$MEMO_VAULT" status --porcelain | grep -q .; then
  echo "openmemo-mcp entrypoint: vault dirty; skipping fetch/ff-only" >&2
else
  if git -C "$MEMO_VAULT" fetch origin 2>/dev/null; then
    git -C "$MEMO_VAULT" merge --ff-only origin/main \
      || echo "openmemo-mcp entrypoint: ff-only merge failed; starting on current tree" >&2
  else
    echo "openmemo-mcp entrypoint: git fetch skipped; starting on current tree" >&2
  fi
fi

cd "$MEMO_VAULT"

PROXY_ARGS="--listen :8080 --no-auto-tls --http-streaming-only --data-path /srv/proxy-data --external-url ${EXTERNAL_URL}"
if [ -n "${TRUSTED_PROXIES:-}" ]; then
  PROXY_ARGS="$PROXY_ARGS --trusted-proxies ${TRUSTED_PROXIES}"
fi

exec mcp-auth-proxy $PROXY_ARGS -- \
  env MEMO_REMOTE=1 MEMO_VAULT="$MEMO_VAULT" \
  python3 scripts/mcp_server.py
