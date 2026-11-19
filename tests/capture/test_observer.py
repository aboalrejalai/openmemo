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
        self.env = mock.patch.dict(os.environ, {"MEMO_CLAUDE_CLI": FAKE, "FAKE_LOG": self.log})
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

    def test_session_is_processed_in_one_conversation(self):
        self.seed("obs-1")
        self.assertEqual(processor.process_session("obs-1", self.conn), 3)
        titles = [r["title"] for r in self.conn.execute(
            "SELECT title FROM observations WHERE content_session_id = 'obs-1' ORDER BY id")]
        self.assertEqual(titles, ["Ran Bash #2", "Ran Read #3"])   # message 1 was the init prompt
        summary = self.conn.execute("SELECT * FROM session_summaries WHERE content_session_id = 'obs-1'").fetchone()
        self.assertEqual(summary["request"], "Fake request")
        self.assertEqual(json.loads(summary["files_read"]), ["src/a.py"])
        calls = self.calls()
        self.assertEqual(len(calls), 1)                           # one process for the whole session
        self.assertEqual(calls[0]["observer"], "1")              # recursion guard set
        session = state.get_session(self.conn, "obs-1")
        self.assertTrue(session["observer_session_id"])
        left = self.conn.execute("SELECT COUNT(*) FROM pending_messages WHERE content_session_id = 'obs-1'")
        self.assertEqual(left.fetchone()[0], 0)

    def test_restart_resumes_the_conversation(self):
        self.seed("obs-2")
        processor.process_session("obs-2", self.conn)
        first_id = state.get_session(self.conn, "obs-2")["observer_session_id"]
        observer.POOL.drop("obs-2")                                # worker restarted
        state.enqueue(self.conn, "obs-2", "observation",
                      {"tool_name": "Grep", "tool_input": "{}", "tool_response": "{}", "cwd": "/p"}, 1)
        processor.process_session("obs-2", self.conn)
        last = self.calls()[-1]["argv"]
        self.assertEqual(last[last.index("--resume") + 1], first_id)

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
