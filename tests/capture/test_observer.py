import json
import os
import tempfile
import unittest
from unittest import mock

from tests.capture import ROOT
from memo_capture import observer, processor, settings, state

FAKE = str(ROOT / "tests" / "capture" / "fixtures" / "fake_claude.py")


class ObserverUnitTest(unittest.TestCase):
    def setUp(self):
        self.log = tempfile.mktemp(prefix="fake-claude-")
        self.prompts = tempfile.mktemp(prefix="fake-prompts-")
        self.env = mock.patch.dict(os.environ, {"MEMO_CLAUDE_CLI": FAKE, "FAKE_LOG": self.log,
                                                "FAKE_PROMPTS": self.prompts})
        self.env.start()
        settings.load(refresh=True)
        self.conn = state.connect()
        observer.POOL.observers.clear()

    def tearDown(self):
        for sid in list(observer.POOL.observers):
            observer.POOL.drop(sid)
        self.env.stop()
        settings.load(refresh=True)

    def calls(self):
        with open(self.log) as fh:
            return [json.loads(l) for l in fh]

    def seed(self, sid):
        state.upsert_session(self.conn, sid, project=f"demo-{sid}")
        n = state.next_prompt_number(self.conn, sid)
        state.add_prompt(self.conn, sid, n, "add a feature", private=False)
        state.enqueue(self.conn, sid, "observation",
                      {"tool_name": "Bash", "tool_input": '{"command":"ls"}', "tool_response": "{}", "cwd": "/p"}, n)
        state.enqueue(self.conn, sid, "observation",
                      {"tool_name": "Read", "tool_input": '{"file_path":"a"}', "tool_response": "{}", "cwd": "/p"}, n)
        state.enqueue(self.conn, sid, "summarize", {"last_assistant_message": "Done."}, n)

    def test_command_is_locked_down_with_owner_model(self):
        cmd = observer.build_command()
        self.assertEqual(cmd[0], FAKE)
        for flag in ("--safe-mode", "--strict-mcp-config", "--verbose"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")

    def sent(self):
        with open(self.prompts) as fh:
            return [json.loads(l) for l in fh]

    def test_session_is_processed_in_fresh_small_calls(self):
        self.seed("obs-1")
        self.assertEqual(processor.process_session("obs-1", self.conn), 3)
        titles = [r["title"] for r in self.conn.execute(
            "SELECT title FROM observations WHERE content_session_id = 'obs-1' ORDER BY id")]
        self.assertEqual(titles, ["Ran Bash #1.1", "Ran Read #1.2"])   # both events in one call
        summary = self.conn.execute("SELECT * FROM session_summaries WHERE content_session_id = 'obs-1'").fetchone()
        self.assertEqual(summary["request"], "Fake request")
        self.assertEqual(json.loads(summary["files_read"]), ["src/a.py"])
        calls = self.calls()
        self.assertEqual(len(calls), 2)                           # one batch call + one summary call
        self.assertTrue(all(c["observer"] == "1" for c in calls))  # recursion guard set
        self.assertTrue(all("--resume" not in c["argv"] for c in calls))
        sent = self.sent()
        self.assertEqual(len(sent), 2)                            # no priming message
        self.assertIn("add a feature", sent[0])                   # the request rides with the batch
        self.assertIn("Ran Bash #1.1", sent[1])                   # the summary sees this request's work
        left = self.conn.execute("SELECT COUNT(*) FROM pending_messages WHERE content_session_id = 'obs-1'")
        self.assertEqual(left.fetchone()[0], 0)

    def test_later_calls_carry_only_new_events_and_prior_context(self):
        self.seed("obs-2")
        processor.process_session("obs-2", self.conn)
        state.enqueue(self.conn, "obs-2", "observation",
                      {"tool_name": "Grep", "tool_input": "{}", "tool_response": "{}", "cwd": "/p"}, 1)
        processor.process_session("obs-2", self.conn)
        self.assertTrue(all("--resume" not in c["argv"] for c in self.calls()))
        last = self.sent()[-1]
        self.assertEqual(last.count("<what_happened>"), 1)        # only the new event, not the history
        self.assertIn("<previous_summary>", last)
        self.assertIn("Ran Bash #1.1", last)                      # earlier titles so it does not repeat them

    def test_trivial_request_gets_no_summary_call(self):
        state.upsert_session(self.conn, "obs-5", project="demo5")
        n = state.next_prompt_number(self.conn, "obs-5")
        state.add_prompt(self.conn, "obs-5", n, "hi", private=False)
        state.enqueue(self.conn, "obs-5", "summarize", {"last_assistant_message": "Hello."}, n)
        self.assertEqual(processor.process_session("obs-5", self.conn), 1)
        self.assertFalse(os.path.exists(self.log))                # the observer never started

    def test_ready_waits_for_quiet_or_full_batch(self):
        sid = "obs-6"
        state.upsert_session(self.conn, sid, project="demo6")
        for _ in range(3):
            state.enqueue(self.conn, sid, "observation", {"tool_name": "Read"}, 1)
        self.assertNotIn(sid, state.sessions_with_pending(self.conn, 60_000, 15))   # still busy
        self.assertIn(sid, state.sessions_with_pending(self.conn, 60_000, 3))       # batch full
        self.assertIn(sid, state.sessions_with_pending(self.conn, 0, 15))           # quiet
        self.assertGreater(state.next_ready_in_ms(self.conn, 60_000), 50_000)
        state.enqueue(self.conn, sid, "session_end", {}, 1)
        self.assertIn(sid, state.sessions_with_pending(self.conn, 60_000, 15))      # ended

    def test_failure_keeps_items_queued(self):
        with mock.patch.dict(os.environ, {"FAKE_FAIL": "1"}):
            self.seed("obs-3")
            self.assertEqual(processor.process_session("obs-3", self.conn), 0)
        counts = self.conn.execute("SELECT status, COUNT(*) FROM pending_messages "
                                   "WHERE content_session_id = 'obs-3' GROUP BY status").fetchall()
        self.assertEqual([tuple(r) for r in counts], [("pending", 3)])

    def test_repeated_observation_is_deduped(self):
        from memo_capture import store
        from memo_capture.parser import Observation
        state.upsert_session(self.conn, "obs-4", project="demo4")
        s = state.get_session(self.conn, "obs-4")
        o = Observation(type="change", title="Same thing", facts=["a"])
        self.assertEqual(len(store.save_observations(self.conn, s, 1, [o], tokens=1, model="m")), 1)
        self.assertEqual(store.save_observations(self.conn, s, 1, [o], tokens=1, model="m"), [])
        row = self.conn.execute("SELECT occurrence_count FROM observations WHERE project='demo4'").fetchone()
        self.assertEqual(row[0], 2)


if __name__ == "__main__":
    unittest.main()
