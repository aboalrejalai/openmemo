"""Id windows and machine-named manual sessions (plan 003 TASK-002)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.capture import ROOT  # noqa: F401
from memo_capture import manual, settings, state, store
from memo_capture.parser import Observation


def _write_machine(home: Path, **kwargs):
    (home / "machine.json").write_text(json.dumps(kwargs), encoding="utf-8")
    settings.load(refresh=True)


class StoreIdsTest(unittest.TestCase):
    def setUp(self):
        self._prev_home = os.environ["MEMO_HOME"]
        self.home = Path(tempfile.mkdtemp(prefix="memo-ids-"))
        os.environ["MEMO_HOME"] = str(self.home)
        _write_machine(self.home, name="vps", id_base=1_000_000_000, owns_unstamped=False)
        self.conn = state.connect()

    def tearDown(self):
        try:
            self.conn.raw.close()
        except Exception:
            pass
        os.environ["MEMO_HOME"] = self._prev_home
        settings.load(refresh=True)

    def test_first_id_in_window(self):
        state.upsert_session(self.conn, "s-first", project="demo")
        session = state.get_session(self.conn, "s-first")
        obs = Observation(type="discovery", title="t", narrative="n", facts=["f"])
        ids = store.save_observations(self.conn, session, 1, [obs], tokens=0, model="t")
        self.assertEqual(ids, [1_000_000_001])

    def test_foreign_id_does_not_move_window(self):
        state.upsert_session(self.conn, "s-foreign", project="demo")
        self.conn.execute(
            """INSERT INTO observations (id, content_session_id, project, prompt_number, type, title,
                   facts, content_hash, created_at)
               VALUES (5, 's-foreign', 'demo', 1, 'discovery', 'foreign', '[]', 'hash-foreign-5', 1)""")
        session = state.get_session(self.conn, "s-foreign")
        obs = Observation(type="discovery", title="own", narrative="n", facts=["f"])
        ids = store.save_observations(self.conn, session, 1, [obs], tokens=0, model="t")
        self.assertEqual(ids, [1_000_000_001])
        obs2 = Observation(type="discovery", title="own2", narrative="n2", facts=["f2"])
        ids2 = store.save_observations(self.conn, session, 1, [obs2], tokens=0, model="t")
        self.assertEqual(ids2, [1_000_000_002])

    def test_malformed_machine_json_raises(self):
        (self.home / "machine.json").write_text("{bad", encoding="utf-8")
        settings.load(refresh=True)
        with self.assertRaises(RuntimeError):
            store.next_id(self.conn, "observations")

    def test_old_schema_gains_machine_column(self):
        tmp = Path(tempfile.mkdtemp(prefix="memo-old-schema-"))
        db = tmp / "state.db"
        raw = sqlite3.connect(str(db))
        raw.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '1');
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY,
                content_session_id TEXT NOT NULL UNIQUE,
                host TEXT NOT NULL DEFAULT 'claude-code',
                project TEXT NOT NULL,
                project_key_source TEXT,
                cwd TEXT,
                observer_session_id TEXT,
                observer_prompt INTEGER NOT NULL DEFAULT 0,
                observed_model TEXT,
                prompt_counter INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                markdown_path TEXT,
                transcript_path TEXT,
                started_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
        """)
        raw.close()
        with mock.patch.dict(os.environ, {"MEMO_HOME": str(tmp), "MEMO_AUTO_REINDEX": "0"}):
            # db_path reads memo_home(); also need machine.json there
            (tmp / "machine.json").write_text(json.dumps({
                "name": "vps", "id_base": 1_000_000_000, "owns_unstamped": False,
            }), encoding="utf-8")
            settings.load(refresh=True)
            conn = state.connect()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            self.assertIn("machine", cols)
            conn.raw.close()
        settings.load(refresh=True)


class ManualIdTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.environ["MEMO_HOME"])
        self._bak = (self.home / "machine.json").read_text(encoding="utf-8")

    def tearDown(self):
        (self.home / "machine.json").write_text(self._bak, encoding="utf-8")
        settings.load(refresh=True)

    def test_vps_suffix(self):
        _write_machine(self.home, name="vps", id_base=1_000_000_000, owns_unstamped=False)
        conn = state.connect()
        with mock.patch.object(writer_mod(), "write_session", return_value=None):
            r = manual.save("t", "body", project="demo-vps", conn=conn)
        self.assertRegex(r["session"], r"^manual-demo-vps-\d{8}-vps$")

    def test_vps_source_suffix(self):
        _write_machine(self.home, name="vps", id_base=1_000_000_000, owns_unstamped=False)
        conn = state.connect()
        with mock.patch.object(writer_mod(), "write_session", return_value=None):
            r = manual.save("t", "body", project="demo-src", conn=conn, source="claude")
        self.assertRegex(r["session"], r"^manual-demo-src-\d{8}-vps-claude$")

    def test_owns_unstamped_keeps_plain_id(self):
        _write_machine(self.home, name="mac", id_base=0, owns_unstamped=True)
        conn = state.connect()
        with mock.patch.object(writer_mod(), "write_session", return_value=None):
            r = manual.save("t", "body", project="demo-mac", conn=conn, source="claude")
        self.assertRegex(r["session"], r"^manual-demo-mac-\d{8}$")

    def test_long_project_keeps_machine_suffix(self):
        _write_machine(self.home, name="vps", id_base=1_000_000_000, owns_unstamped=False)
        conn = state.connect()
        project = "p" * 70
        with mock.patch.object(writer_mod(), "write_session", return_value=None):
            r = manual.save("t", "body", project=project, conn=conn)
        from memo_capture.writer import file_stem
        stem = file_stem(r["session"])
        self.assertTrue(stem.endswith("-vps"), stem)
        self.assertLessEqual(len(stem), 80)

    def test_bad_source_raises(self):
        _write_machine(self.home, name="vps", id_base=1_000_000_000, owns_unstamped=False)
        with self.assertRaises(ValueError):
            manual.save("t", "body", project="demo", source="Bad Value")


def writer_mod():
    from memo_capture import writer
    return writer


if __name__ == "__main__":
    unittest.main()
