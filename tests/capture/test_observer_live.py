"""Live observer check against the real `claude` CLI (uses the owner's quota).

    MEMO_LIVE=1 python3 -m unittest tests.capture.test_observer_live
"""
import json
import os
import unittest

from tests.capture import ROOT  # noqa: F401
from memo_capture import observer, processor, settings, spool, state


@unittest.skipUnless(os.environ.get("MEMO_LIVE") == "1", "set MEMO_LIVE=1 to call the real observer")
class LiveObserverTest(unittest.TestCase):
    def test_one_event_becomes_an_observation(self):
        settings.load(refresh=True)
        conn = state.connect()
        sid = "live-observer-test"
        state.upsert_session(conn, sid, project="memo-live-test")
        n = state.next_prompt_number(conn, sid)
        state.add_prompt(conn, sid, n, "Rename the vault folder from oldname to memo", private=False)
        state.enqueue(conn, sid, "observation", {
            "tool_name": "Bash",
            "tool_input": json.dumps({"command": "mv ${OPENMEMO_HOME:-$HOME/openmemo} ${OPENMEMO_HOME:-$HOME/openmemo} && "
                                                 "ln -s memo ${OPENMEMO_HOME:-$HOME/openmemo}"}),
            "tool_response": json.dumps({"stdout": "", "exit_code": 0}),
            "cwd": "/Users/demo/Desktop/Project"}, n)
        spool_before = sorted(p.name for p in spool.spool_dir().glob("*.json"))
        self.assertEqual(processor.process_session(sid, conn), 1)
        rows = conn.execute("SELECT type, title, generated_by_model FROM observations "
                            "WHERE content_session_id = ?", (sid,)).fetchall()
        self.assertGreaterEqual(len(rows), 1, "the observer wrote no observation")
        self.assertEqual(rows[0]["generated_by_model"], "claude-opus-5-5")
        print("\nobserver wrote:", [(r["type"], r["title"]) for r in rows])
        self.assertEqual(sorted(p.name for p in spool.spool_dir().glob("*.json")), spool_before)
        observer.POOL.drop(sid)


if __name__ == "__main__":
    unittest.main()
