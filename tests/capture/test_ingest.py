import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.capture import ROOT, TEST_VAULT
from memo_capture import ingest, observer, processor, settings, state

FAKE = str(ROOT / "tests" / "capture" / "fixtures" / "fake_claude.py")
GH = "ghp_" + "A1b2C3d4" * 4 + "Zz9x"


class IngestTest(unittest.TestCase):
    def setUp(self):
        settings.load(refresh=True)
        self.conn = state.connect()

    def event(self, sid, tool, tool_input, response=None, **extra):
        payload = {"content_session_id": sid, "project": "demo-repo", "cwd": "/x", "tool_name": tool,
                   "tool_input": tool_input, "tool_response": response or {}, "host": "claude-code"}
        payload.update(extra)
        ingest.enqueue_event(self.conn, "observation", payload)

    def rows(self, sid):
        return self.conn.execute("SELECT * FROM pending_messages WHERE content_session_id = ?", (sid,)).fetchall()

    def test_skip_tools_and_bash_patterns(self):
        with mock.patch.dict(os.environ, {"MEMO_SKIP_BASH_PATTERNS": r"^git status"}):
            settings.load(refresh=True)
            self.event("ing-1", "TodoWrite", {"todos": []})
            self.event("ing-1", "Bash", {"command": "git status --short"})
            self.event("ing-1", "Bash", {"command": "npm test"})
        settings.load(refresh=True)
        rows = self.rows("ing-1")
        self.assertEqual([json.loads(r["payload"])["tool_name"] for r in rows], ["Bash"])

    def test_private_prompt_suppresses_its_events(self):
        state.upsert_session(self.conn, "ing-2", project="demo-repo")
        n = state.next_prompt_number(self.conn, "ing-2")
        state.add_prompt(self.conn, "ing-2", n, "", private=True)
        self.event("ing-2", "Read", {"file_path": "x"})
        ingest.enqueue_event(self.conn, "summarize", {"content_session_id": "ing-2", "prompt_number": n})
        self.assertEqual(self.rows("ing-2"), [])

    def test_secrets_never_reach_the_queue(self):
        self.event("ing-3", "Bash", {"command": f"export GITHUB_TOKEN={GH}"}, {"stdout": f"token {GH}"})
        stored = self.rows("ing-3")[0]["payload"]
        backup = self.conn.execute("SELECT tool_input, tool_response FROM tool_uses WHERE content_session_id='ing-3'").fetchone()
        for text in (stored, backup[0], backup[1]):
            self.assertNotIn(GH, text)
        self.assertIn("redacted type=", stored)


class ReplayTest(unittest.TestCase):
    """A recorded session goes all the way to a Markdown file and a lint-clean vault."""

    def test_replay_to_markdown(self):
        with mock.patch.dict(os.environ, {"MEMO_CLAUDE_CLI": FAKE}):
            settings.load(refresh=True)
            conn = state.connect()
            sid = "replay-session-0001"
            state.upsert_session(conn, sid, project="demo-repo", cwd="/x/demo-repo", project_key_source="git")
            n = state.next_prompt_number(conn, sid)
            state.add_prompt(conn, sid, n, "Wire the worker", private=False)
            for tool in ("Read", "Edit", "Bash"):
                ingest.enqueue_event(conn, "observation", {"content_session_id": sid, "project": "demo-repo",
                                                           "cwd": "/x/demo-repo", "tool_name": tool,
                                                           "tool_input": {"k": tool}, "tool_response": {}})
            ingest.enqueue_event(conn, "summarize", {"content_session_id": sid, "project": "demo-repo",
                                                     "last_assistant_message": "Worker wired.", "prompt_number": n})
            ingest.enqueue_event(conn, "session_end", {"content_session_id": sid, "project": "demo-repo"})
            self.assertEqual(processor.process_session(sid, conn), 5)
            observer.POOL.observers.clear()
        settings.load(refresh=True)
        session = state.get_session(conn, sid)
        text = (TEST_VAULT / session["markdown_path"]).read_text()
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("- obs-")]), 3)
        self.assertIn("status: completed", text)
        self.assertIn("- **Request:** Fake request", text)
        import memo_core
        self.assertEqual(memo_core.VAULT_DIR, TEST_VAULT)
        report = memo_core.vault_lint()
        self.assertEqual(report["errors"], [], str(report["errors"])[:800])


if __name__ == "__main__":
    unittest.main()
