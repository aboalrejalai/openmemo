import unittest

from tests.capture import ROOT  # noqa: F401  (sets MEMO_HOME)
from memo_capture import settings, state


class SettingsTest(unittest.TestCase):
    def test_owner_defaults(self):
        s = settings.load(refresh=True)
        self.assertEqual(s["observer_model"], "claude-opus-5-5")
        self.assertEqual(s["observer_effort"], "medium")
        self.assertTrue(s["redact_enabled"])

    def test_env_override_is_typed(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"MEMO_CONTEXT_OBSERVATIONS": "7", "MEMO_SEMANTIC_INJECT": "true"}):
            s = settings.load(refresh=True)
            self.assertEqual(s["context_observations"], 7)
            self.assertIs(s["semantic_inject"], True)
        settings.load(refresh=True)


    def test_machine_json_id_base_and_env_override(self):
        import json, os
        from pathlib import Path
        from unittest import mock
        home = Path(os.environ["MEMO_HOME"])
        (home / "machine.json").write_text(json.dumps({
            "name": "vps", "id_base": 1_000_000_000, "owns_unstamped": False,
        }), encoding="utf-8")
        settings.load(refresh=True)
        self.assertEqual(settings.get("id_base"), 1_000_000_000)
        self.assertEqual(settings.source("id_base"), "machine.json")
        with mock.patch.dict(os.environ, {"MEMO_ID_BASE": "2000000000"}):
            settings.load(refresh=True)
            self.assertEqual(settings.get("id_base"), 2_000_000_000)
            self.assertEqual(settings.source("id_base"), "env")
        settings.load(refresh=True)

    def test_machine_keys_ignored_in_settings_json(self):
        import json, os
        from pathlib import Path
        home = Path(os.environ["MEMO_HOME"])
        path = home / "settings.json"
        data = {"observer_model": "claude-opus-5-5", "id_base": 999, "machine_name": "nope",
                "name": "nope", "host": "x", "owns_unstamped": True, "accept_incoming_code": True}
        path.write_text(json.dumps(data), encoding="utf-8")
        # Ensure machine.json sets a known base
        (home / "machine.json").write_text(json.dumps({
            "name": "test", "id_base": 0, "owns_unstamped": True,
        }), encoding="utf-8")
        s = settings.load(refresh=True)
        self.assertEqual(s["id_base"], 0)
        self.assertEqual(s["machine_name"], "test")

    def test_ensure_file_has_no_machine_key(self):
        import json, os, tempfile
        from pathlib import Path
        from unittest import mock
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"MEMO_HOME": str(tmp)}):
            settings.load(refresh=True)
            settings.ensure_file()
            data = json.loads((tmp / "settings.json").read_text(encoding="utf-8"))
        for k in ("machine_name", "name", "id_base", "machine_host", "host",
                  "owns_unstamped", "accept_incoming_code"):
            self.assertNotIn(k, data)

    def test_tracked_settings_has_no_machine_keys(self):
        import json
        from pathlib import Path
        tracked = Path(__file__).resolve().parents[2] / ".memo" / "settings.json"
        if not tracked.exists():
            self.skipTest("no .memo/settings.json in this checkout (fresh clone)")
        data = json.loads(tracked.read_text(encoding="utf-8"))
        for k in ("machine_name", "name", "id_base", "machine_host", "host",
                  "owns_unstamped", "accept_incoming_code"):
            self.assertNotIn(k, data)

    def test_no_machine_json_effective_name(self):
        import os, socket
        from pathlib import Path
        from unittest import mock
        home = Path(os.environ["MEMO_HOME"])
        mj = home / "machine.json"
        bak = mj.read_text(encoding="utf-8") if mj.exists() else None
        if mj.exists():
            mj.unlink()
        try:
            settings.load(refresh=True)
            self.assertEqual(settings.effective_name(), f"unnamed-{socket.gethostname()}")
        finally:
            if bak is not None:
                mj.write_text(bak, encoding="utf-8")
            settings.load(refresh=True)

    def test_malformed_machine_json_treated_as_missing(self):
        import os, socket
        from pathlib import Path
        home = Path(os.environ["MEMO_HOME"])
        mj = home / "machine.json"
        bak = mj.read_text(encoding="utf-8")
        mj.write_text("{not-json", encoding="utf-8")
        try:
            settings.load(refresh=True)
            self.assertTrue(settings.machine_json_malformed())
            self.assertEqual(settings.get("id_base"), 0)
            self.assertEqual(settings.effective_name(), f"unnamed-{socket.gethostname()}")
        finally:
            mj.write_text(bak, encoding="utf-8")
            settings.load(refresh=True)


class StateTest(unittest.TestCase):
    def setUp(self):
        self.conn = state.connect()

    def test_session_prompt_numbers_increase(self):
        state.upsert_session(self.conn, "s-num", project="demo")
        self.assertEqual(state.next_prompt_number(self.conn, "s-num"), 1)
        self.assertEqual(state.next_prompt_number(self.conn, "s-num"), 2)
        state.upsert_session(self.conn, "s-num", project="demo")  # re-upsert keeps the counter
        self.assertEqual(state.current_prompt_number(self.conn, "s-num"), 2)

    def test_private_prompt_stored_empty(self):
        state.upsert_session(self.conn, "s-priv", project="demo")
        state.add_prompt(self.conn, "s-priv", 1, "secret plan", private=True)
        self.assertTrue(state.prompt_is_private(self.conn, "s-priv", 1))
        self.assertEqual(state.get_prompt(self.conn, "s-priv", 1)["prompt_text"], "")

    def test_queue_claim_finish_retry(self):
        a = state.enqueue(self.conn, "s-q", "observation", {"x": 1}, 1)
        b = state.enqueue(self.conn, "s-q", "observation", {"x": 2}, 1)
        self.assertIn("s-q", state.sessions_with_pending(self.conn))
        rows = state.claim_batch(self.conn, "s-q")
        self.assertEqual([r["id"] for r in rows], [a, b])
        self.assertEqual(state.claim_batch(self.conn, "s-q"), [])
        state.finish(self.conn, [a])
        state.retry_later(self.conn, [b], "quota")
        row = self.conn.execute("SELECT status, attempts FROM pending_messages WHERE id = ?", (b,)).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertNotIn("s-q", state.sessions_with_pending(self.conn))  # backoff not elapsed

    def test_stale_processing_is_requeued(self):
        state.enqueue(self.conn, "s-stale", "observation", {}, 1)
        state.claim_batch(self.conn, "s-stale")
        self.assertGreaterEqual(state.reset_stale_processing(self.conn), 1)
        self.assertIn("s-stale", state.sessions_with_pending(self.conn))


class NoStuckLockTest(unittest.TestCase):
    """2026-11-02: one write left open inside the worker locked every writer for 3 days."""

    def test_error_mid_transaction_leaves_nothing_open(self):
        a, b = state.connect(), state.connect()
        state.upsert_session(a, "s-lock", project="demo")
        with self.assertRaises(RuntimeError):
            with state.transaction(a):
                a.execute("UPDATE sessions SET prompt_counter = 99 WHERE content_session_id = 's-lock'")
                raise RuntimeError("observer crashed")
        self.assertFalse(a.in_transaction)
        self.assertEqual(state.get_session(a, "s-lock")["prompt_counter"], 0)      # rolled back
        b.execute("PRAGMA busy_timeout=200")
        state.upsert_session(b, "s-lock-2", project="demo")                         # not locked out
        self.assertIsNotNone(state.get_session(a, "s-lock-2"))

    def test_failed_single_write_leaves_nothing_open(self):
        a = state.connect()
        with self.assertRaises(Exception):
            a.execute("INSERT INTO sessions (content_session_id) VALUES ('missing-not-null-columns')")
        self.assertFalse(a.in_transaction)

    def test_store_error_rolls_back_whole_batch(self):
        from unittest import mock
        from memo_capture import store
        from memo_capture.parser import Observation
        a, b = state.connect(), state.connect()
        state.upsert_session(a, "s-batch", project="demo")
        s = state.get_session(a, "s-batch")
        with mock.patch.object(store, "_index", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                store.save_observations(a, s, 1, [Observation(type="change", title="never half-saved")],
                                        tokens=1, model="m")
        self.assertFalse(a.in_transaction)
        self.assertIsNone(a.execute("SELECT 1 FROM observations WHERE title = 'never half-saved'").fetchone())
        b.execute("PRAGMA busy_timeout=200")
        state.upsert_session(b, "s-batch-2", project="demo")

    def test_reader_does_not_need_the_write_lock(self):
        a, b = state.connect(), state.connect()
        a.execute("BEGIN IMMEDIATE")                   # another process is writing
        try:
            b.execute("PRAGMA busy_timeout=200")
            c = state.connect()                        # a hook opening the index to read
            c.execute("SELECT COUNT(*) FROM sessions").fetchone()
        finally:
            a.execute("ROLLBACK")

    def test_threads_sharing_the_worker_connection_finish(self):
        """The worker's drain, HTTP and observer threads all use SQLite (2026-11-05 hang)."""
        import threading
        shared = state.connect()
        errors = []

        def work(k):
            try:
                own = state.connect()
                for i in range(150):
                    with state.transaction(shared):
                        state.enqueue(shared, f"thr-{k}", "observation", {"i": i}, 1)
                    state.queue_counts(own)
                    for _ in shared.execute("SELECT id FROM pending_messages WHERE content_session_id = ?",
                                            (f"thr-{k}",)):
                        pass
                own.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(60)
        self.assertFalse(any(th.is_alive() for th in threads), "a thread hung inside SQLite")
        self.assertEqual(errors, [])
        n = shared.execute("SELECT COUNT(*) FROM pending_messages WHERE content_session_id LIKE 'thr-%'").fetchone()[0]
        self.assertEqual(n, 600)

    def test_queue_rows_are_never_given_up(self):
        a = state.connect()
        rid = state.enqueue(a, "s-retry", "observation", {}, 1)
        for _ in range(12):
            state.retry_later(a, [rid], "boom")
        row = a.execute("SELECT status, attempts FROM pending_messages WHERE id = ?", (rid,)).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 12))


if __name__ == "__main__":
    unittest.main()
