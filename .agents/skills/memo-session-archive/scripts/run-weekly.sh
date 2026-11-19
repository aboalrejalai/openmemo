#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="${MEMO_VAULT:-${OPENMEMO_HOME:-$(cd "$DIR/../../../.." && pwd)}}"
cd "$ROOT"

echo "== Claude =="
python3 "$DIR/extract_claude_transcripts.py" --include-agents || true

echo "== Cursor =="
python3 "$DIR/extract_cursor_transcripts.py" || true

echo "== Antigravity =="
python3 "$DIR/extract_antigravity_transcripts.py" || true

echo "== Codex =="
python3 "$DIR/extract_codex_transcripts.py" || true

echo "== Gemini =="
python3 "$DIR/extract_gemini_transcripts.py" || true

echo "== Cleanup =="
for d in sessions/_raw-claude sessions/_raw-cursor sessions/_raw-antigravity sessions/_raw-codex sessions/_raw-gemini; do
  [[ -d $d ]] || continue
  python3 "$DIR/cleanup_raw_markdown.py" --root "$d" || true
done

echo "Done. Review git status; commit when ready."
git status --short sessions/_raw-claude sessions/_raw-cursor sessions/_raw-antigravity sessions/_raw-codex sessions/_raw-gemini 2>/dev/null | head -40 || true
