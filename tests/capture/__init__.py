"""Tests for scripts/memo_capture. Run from the vault root:

    python3 -m unittest discover -s tests/capture -t .
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

# Every test run gets a throwaway ~/.memo and a throwaway git vault, so real
# state and the real vault are never touched.
os.environ["MEMO_HOME"] = tempfile.mkdtemp(prefix="memo-home-test-")
import json as _json
(_p := Path(os.environ["MEMO_HOME"])).mkdir(parents=True, exist_ok=True)
(_p / "machine.json").write_text(_json.dumps({
    "name": "test", "id_base": 0, "owns_unstamped": True,
}) + "\n", encoding="utf-8")
os.environ["MEMO_SYNC_INTERVAL_MINUTES"] = "0"
os.environ["MEMO_AUTO_REINDEX"] = "0"   # tests opt in where they check it
TEST_VAULT = Path(tempfile.mkdtemp(prefix="memo-vault-test-")).resolve()
(TEST_VAULT / "projects").mkdir()
(TEST_VAULT / ".gitignore").write_text(".cache/\n")
(TEST_VAULT / "graph").mkdir()
(TEST_VAULT / "graph" / "entities.json").write_text('{"version": 1, "entities": [], "relationships": []}\n')
(TEST_VAULT / "timeline.md").write_text((ROOT / "tests" / "capture" / "fixtures" / "timeline.md").read_text())
(TEST_VAULT / "projects" / "demo-repo.md").write_text(
    "---\ntitle: Demo Repo\ntype: project\nstatus: active\ntags:\n  - type/project\n  - status/active\n---\n"
    "# Demo Repo\n\nA test project.\n")
import subprocess as _sp
_sp.run(["git", "init", "-q"], cwd=TEST_VAULT, check=True)
_sp.run(["git", "-c", "user.email=t@memo.local", "-c", "user.name=t", "add", "-A"], cwd=TEST_VAULT, check=True)
_sp.run(["git", "-c", "user.email=t@memo.local", "-c", "user.name=t", "commit", "-qm", "seed"], cwd=TEST_VAULT,
        check=True)
_sp.run(["git", "config", "user.email", "t@memo.local"], cwd=TEST_VAULT, check=True)
_sp.run(["git", "config", "user.name", "memo-test"], cwd=TEST_VAULT, check=True)
os.environ["MEMO_VAULT"] = str(TEST_VAULT)
os.environ.pop("MEMO_OBSERVER", None)
os.environ["MEMO_CREATE_FILL_DELAY"] = "0"   # no Obsidian in tests
os.environ.pop("MEMO_DISABLED", None)
