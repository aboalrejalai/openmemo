import json
import tempfile
import unittest
from pathlib import Path

from tests.capture import ROOT
from memo_capture import backfill, ingest, spool, state

BASE = 1_900_000_000_000          # a fixed time in ms


def stamp(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class BackfillTest(unittest.TestCase):
    """The 2026-11-02 gap: tool events reached the spool, prompts and summaries did not."""

    def setUp(self):
        self.conn = state.connect()
        for p in spool.spool_dir().glob("*.json"):
            p.unlink()

    def test_lost_prompts_and_summaries_come_back_in_order(self):
        sid = "bf000001-0000-4000-8000-000000000001"
        state.upsert_session(self.conn, sid, project="memo", cwd=str(ROOT))
        n = state.next_prompt_number(self.conn, sid)
        state.add_prompt(self.conn, sid, n, "before the gap", False, created_at=BASE - 60_000)
        lines = [
            {"type": "user", "timestamp": stamp(BASE + 1_000), "cwd": str(ROOT),
             "message": {"role": "user", "content": "build the labs page"}},
            {"type": "assistant", "timestamp": stamp(BASE + 5_000),
             "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "Labs built."}]}},
            {"type": "user", "timestamp": stamp(BASE + 6_000), "isMeta": True,
             "message": {"role": "user", "content": "skill text, not a prompt"}},
            {"type": "user", "timestamp": stamp(BASE + 10_000), "cwd": str(ROOT),
             "message": {"role": "user", "content": "continue"}},
            {"type": "assistant", "timestamp": stamp(BASE + 15_000),
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Release 0.0.82 done."}]}},
        ]
        path = Path(tempfile.mkdtemp()) / f"{sid}.jsonl"
        path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
        # Tool events the hooks did spool during the gap.
        for i, at in enumerate((BASE + 3_000, BASE + 12_000)):
            spool.write_event("observation", {"content_session_id": sid, "project": "memo", "cwd": str(ROOT),
                                              "tool_name": "Bash", "tool_use_id": f"bf-{i}",
                                              "tool_input": {"command": f"step {i}"}, "tool_response": {}},
                              at_ns=at * 1_000_000)
        counts = backfill.backfill_transcript(self.conn, path, BASE, BASE + 3_600_000)
        self.assertEqual(counts, {"prompts": 2, "summaries": 2})
        files = len(list(spool.spool_dir().glob("*.json")))
        again = backfill.backfill_transcript(self.conn, path, BASE, BASE + 3_600_000)   # a second run adds nothing
        self.assertEqual((again["prompts"], len(list(spool.spool_dir().glob("*.json")))), (0, files))
        spool.drain(self.conn, lambda k, p: ingest.enqueue_event(self.conn, k, p))
        prompts = self.conn.execute("SELECT prompt_number, prompt_text, created_at FROM user_prompts "
                                    "WHERE content_session_id = ? ORDER BY prompt_number", (sid,)).fetchall()
        self.assertEqual([(r[0], r[1]) for r in prompts],
                         [(1, "before the gap"), (2, "build the labs page"), (3, "continue")])
        self.assertEqual(prompts[1][2], BASE + 1_000)              # the original time, not the backfill's
        queued = self.conn.execute("SELECT message_type, prompt_number FROM pending_messages "
                                   "WHERE content_session_id = ? ORDER BY id", (sid,)).fetchall()
        self.assertEqual([tuple(r) for r in queued],
                         [("observation", 2), ("summarize", 2), ("observation", 3), ("summarize", 3)])
        summary = json.loads(self.conn.execute("SELECT payload FROM pending_messages WHERE content_session_id = ? "
                                               "AND message_type = 'summarize' ORDER BY id DESC", (sid,)
                                               ).fetchone()[0])
        self.assertEqual(summary["last_assistant_message"], "Release 0.0.82 done.")


if __name__ == "__main__":
    unittest.main()
