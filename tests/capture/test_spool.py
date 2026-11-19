import unittest

from tests.capture import ROOT  # noqa: F401
from memo_capture import spool, state


class SpoolTest(unittest.TestCase):
    def setUp(self):
        self.conn = state.connect()
        for p in spool.spool_dir().glob("*.json"):
            p.unlink()
        self.seen = []

    def ingest(self, kind, payload):
        self.seen.append((kind, payload))

    def test_same_event_twice_is_one_file_and_one_ingest(self):
        ev = {"content_session_id": "s1", "tool_use_id": "toolu_1", "tool_name": "Bash"}
        spool.write_event("observation", ev)
        spool.write_event("observation", ev)
        self.assertEqual(len(list(spool.spool_dir().glob("*.json"))), 1)
        spool.drain(self.conn, self.ingest)
        spool.write_event("observation", ev)          # re-delivered after it was consumed
        stats = spool.drain(self.conn, self.ingest)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(stats["duplicate"], 1)

    def test_corrupt_file_moves_aside_and_drain_continues(self):
        (spool.spool_dir() / "observation-bad.json").write_text("{nope")
        spool.write_event("observation", {"content_session_id": "s2", "tool_use_id": "toolu_2"})
        stats = spool.drain(self.conn, self.ingest)
        self.assertEqual((stats["corrupt"], stats["ingested"]), (1, 1))
        self.assertTrue((spool.spool_dir() / "corrupt" / "observation-bad.json").exists())

    def test_failed_ingest_stays_for_retry(self):
        spool.write_event("summarize", {"content_session_id": "s3", "prompt_number": 1})
        stats = spool.drain(self.conn, lambda k, p: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(spool.drain(self.conn, self.ingest)["ingested"], 1)

    def test_oldest_first(self):
        import json, os
        for i, tid in enumerate(["a", "b"]):
            p = spool.write_event("observation", {"content_session_id": "s4", "tool_use_id": tid})
            body = json.loads(p.read_text()); body["enqueued_at_ms"] = 1_900_000_000_000 + 1 - i
            body.pop("enqueued_at_ns", None)          # an older spool file has milliseconds only
            p.write_text(json.dumps(body))
        spool.drain(self.conn, self.ingest)
        self.assertEqual([p["tool_use_id"] for _, p in self.seen], ["b", "a"])   # time order, not write order

    def test_old_event_is_never_expired(self):
        import json
        p = spool.write_event("observation", {"content_session_id": "s5", "tool_use_id": "old"})
        body = json.loads(p.read_text()); body["enqueued_at_ms"] = 1_000_000_000_000   # 2001
        p.write_text(json.dumps(body))
        stats = spool.drain(self.conn, self.ingest, 7)
        self.assertEqual((stats["expired"], stats["ingested"]), (0, 1))
        self.assertFalse((spool.spool_dir() / "expired").exists())


if __name__ == "__main__":
    unittest.main()
