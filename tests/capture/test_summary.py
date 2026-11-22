import json
import os
import subprocess
import tempfile
import unittest

from tests.capture import ROOT, TEST_VAULT
from memo_capture import state, store, transcript, writer, wrapup
from memo_capture.parser import Observation, Summary


class TranscriptTest(unittest.TestCase):
    def test_whole_last_turn_is_read(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": "first question"}},
            {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "old"}]}},
            {"type": "user", "message": {"role": "user", "content": "rename it"}},
            {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5-5",
                                              "content": [{"type": "text", "text": "Starting."},
                                                          {"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}},
        ]
        path = tempfile.mktemp(suffix=".jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(l) for l in lines) + "\n{broken\n")
        text, model = transcript.last_assistant_turn(path)
        self.assertEqual(text, "Starting.\n\nDone.")
        self.assertEqual(model, "claude-opus-5-5")
        self.assertEqual(transcript.last_assistant_turn("/nope"), ("", None))


class WrapupTest(unittest.TestCase):
    def setUp(self):
        self.conn = state.connect()

    def marker(self, sid):
        """The wikilink that ties a timeline line to its session note."""
        return f"[[{state.get_session(self.conn, sid)['markdown_path'][:-3]}]]"

    def run_session(self, sid, requests):
        state.upsert_session(self.conn, sid, project="demo-repo", cwd="/x/demo-repo", project_key_source="git")
        for i, (asked, done) in enumerate(requests, 1):
            n = state.next_prompt_number(self.conn, sid)
            state.add_prompt(self.conn, sid, n, asked, private=False)
            s = state.get_session(self.conn, sid)
            store.save_observations(self.conn, s, n, [Observation(type="change", title=f"{sid} change {i}")],
                                    tokens=1, model="m")
            summary_id = store.save_summary(self.conn, s, n, Summary(request=asked, completed=done,
                                                                    next_steps=f"step after {i}"), tokens=1, model="m")
            writer.summary_saved(self.conn, s, summary_id)
        writer.session_end(self.conn, state.get_session(self.conn, sid))

    def test_two_requests_one_timeline_event_and_one_block(self):
        self.run_session("wrap-session-01", [("Rename OldName", "Phase 1 renamed"), ("Build hooks", "Hooks installed")])
        timeline = (TEST_VAULT / "timeline.md").read_text()
        self.assertEqual(len([l for l in timeline.splitlines() if self.marker("wrap-session-01") in l]), 1)
        self.assertIn("**Session:** demo-repo: Rename OldName — Hooks installed (2 request(s)", timeline)
        note = (TEST_VAULT / "projects" / "demo-repo.md").read_text()
        self.assertTrue(note.startswith("---\ntitle: Demo Repo"))
        self.assertEqual(note.count(wrapup.BLOCK_START), 1)
        self.assertIn("- **Last request:** Build hooks", note)
        self.assertIn("- **Next steps:** step after 2", note)
        # A later session replaces the block instead of adding a second one.
        self.run_session("wrap-session-02", [("Write docs", "Docs written")])
        note = (TEST_VAULT / "projects" / "demo-repo.md").read_text()
        self.assertEqual(note.count(wrapup.BLOCK_START), 1)
        self.assertEqual(note.count(wrapup.HEADING), 1)
        self.assertIn("- **Last request:** Write docs", note)
        status = subprocess.run(["git", "status", "--porcelain", "timeline.md", "projects", "sessions/demo-repo"],
                                cwd=TEST_VAULT, capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "", "every wrap-up write is committed")

    def test_open_session_is_current_after_each_request(self):
        """No session end: Claude Code sessions stay open for weeks."""
        sid = "wrap-session-open"
        state.upsert_session(self.conn, sid, project="demo-repo", cwd="/x/demo-repo", project_key_source="git")
        for i, asked in enumerate(["Plan the labs page", "Build the labs page"], 1):
            n = state.next_prompt_number(self.conn, sid)
            state.add_prompt(self.conn, sid, n, asked, private=False)
            s = state.get_session(self.conn, sid)
            sm = store.save_summary(self.conn, s, n, Summary(request=asked, completed=f"done {i}",
                                                            next_steps=f"open step {i}"), tokens=1, model="m")
            writer.summary_saved(self.conn, s, sm)
            note = (TEST_VAULT / "projects" / "demo-repo.md").read_text()
            self.assertIn(f"- **Last request:** {asked}", note)
            self.assertIn(f"- **Next steps:** open step {i}", note)
            timeline = (TEST_VAULT / "timeline.md").read_text()
            mine = [l for l in timeline.splitlines() if self.marker(sid) in l]
            self.assertEqual(len(mine), 1, "one line per session per day, rewritten in place")
            self.assertIn(f"({i} request(s), claude-code, session {sid[:8]})", mine[0])
        self.assertEqual(state.get_session(self.conn, sid)["status"], "active")

    def test_late_summary_goes_to_its_request_day_and_never_hides_newer_work(self):
        """Backfilled requests are summarised hours after they were asked."""
        import time
        day_ms = 86_400_000
        old_at = state.now_ms() - 3 * day_ms
        sid = "wrap-session-late"
        state.upsert_session(self.conn, sid, project="demo-repo", cwd="/x/demo-repo", project_key_source="git")
        n = state.next_prompt_number(self.conn, sid)
        state.add_prompt(self.conn, sid, n, "old request", False, created_at=old_at)
        s = state.get_session(self.conn, sid)
        # A newer request from another session is already shown.
        note_path = TEST_VAULT / "projects" / "demo-repo.md"
        text = note_path.read_text()
        newer = wrapup.replace_block(text, wrapup.BLOCK_START + f"\n<!-- memo:asked_at {state.now_ms()} -->\n"
                                     "- **Last request:** newest work\n" + wrapup.BLOCK_END)
        note_path.write_text(newer)
        sm = store.save_summary(self.conn, s, n, Summary(request="old request", completed="old done"),
                                tokens=1, model="m")
        writer.summary_saved(self.conn, s, sm)
        self.assertIn("newest work", note_path.read_text())
        day = time.strftime("%Y-%m-%d", time.localtime(old_at / 1000))
        lines = (TEST_VAULT / "timeline.md").read_text().splitlines()
        at = next(i for i, l in enumerate(lines) if self.marker(sid) in l)
        header = next(l for l in reversed(lines[:at]) if l.startswith("- **"))
        self.assertEqual(header, f"- **{day}**")

    def test_session_without_summary_adds_nothing(self):
        before = (TEST_VAULT / "timeline.md").read_text()
        state.upsert_session(self.conn, "wrap-session-03", project="demo-repo")
        writer.session_end(self.conn, state.get_session(self.conn, "wrap-session-03"))
        self.assertEqual((TEST_VAULT / "timeline.md").read_text(), before)


if __name__ == "__main__":
    unittest.main()
