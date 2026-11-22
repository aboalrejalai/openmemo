#!/bin/sh
# OpenMemo one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/aboalrejalai/openmemo/main/install.sh | sh
#
# Clones (or updates) OpenMemo into $OPENMEMO_HOME (default ~/openmemo), puts the
# `memo` command in $OPENMEMO_BIN (default ~/.local/bin), then runs `memo init --yes`
# to wire the AI agents it finds on this machine. Safe to run again to update.
set -eu

REPO="${OPENMEMO_REPO:-https://github.com/aboalrejalai/openmemo.git}"
DIR="${OPENMEMO_HOME:-$HOME/openmemo}"
BIN="${OPENMEMO_BIN:-$HOME/.local/bin}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$1"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required (macOS: xcode-select --install)"
PY=""
for py in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$py" >/dev/null 2>&1; then PY="$py"; break; fi
done
[ -n "$PY" ] || die "python3 is required (3.9 or newer)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "python3 3.9 or newer is required (found $("$PY" --version 2>&1))"
"$PY" -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")' 2>/dev/null \
  || die "your python3 sqlite has no FTS5 support; install Python from python.org or Homebrew"

if [ -d "$DIR/.git" ]; then
  say "Updating OpenMemo in $DIR"
  git -C "$DIR" pull --ff-only --quiet || die "could not update $DIR (local changes?). Run: git -C \"$DIR\" status"
elif [ -e "$DIR" ]; then
  die "$DIR exists and is not an OpenMemo checkout. Set OPENMEMO_HOME to another folder."
else
  say "Downloading OpenMemo into $DIR"
  git clone --quiet "$REPO" "$DIR"
fi

mkdir -p "$BIN"
ln -sf "$DIR/plugin/bin/memo" "$BIN/memo"
chmod +x "$DIR/plugin/bin/memo"
say "Installed the memo command: $BIN/memo"

say "Wiring your AI agents (memo init)"
MEMO_VAULT="$DIR" "$DIR/plugin/bin/memo" init --yes </dev/null

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) printf '\nAdd memo to your PATH:\n  echo '\''export PATH="%s:$PATH"'\'' >> ~/.zshrc && source ~/.zshrc\n' "$BIN" ;;
esac

printf '\n'
say "OpenMemo $(MEMO_VAULT="$DIR" "$DIR/plugin/bin/memo" --version | sed 's/^openmemo //') is ready."
printf '  memo ui       open the Studio at http://127.0.0.1:8766\n'
printf '  memo doctor   check the installation\n'
