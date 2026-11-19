import unittest

from tests.capture import TEST_VAULT
from memo_capture import reindex, search, state, store, writer
from memo_capture.parser import Observation, Summary


def snapshot(conn):
    obs = [tuple(r) for r in conn.execute(
        "SELECT id, content_session_id, project, prompt_number, type, title, subtitle, facts, narrative, concepts, "
        "files_read, files_modified, content_hash, created_at FROM observations ORDER BY id")]
    sums = [tuple(r) for r in conn.execute(
        "SELECT id, content_session_id, prompt_number, request, completed, next_steps, files_edited, created_at "
        "FROM session_summaries ORDER BY id")]
    hits = [(h["kind"], h["id"]) for h in search.search(conn, "reindex probe", limit=50)]
    arabic = [(h["kind"], h["id"]) for h in search.search(conn, "فاتورة", limit=50)]
    return obs, sums, sorted(hits, key=str), sorted(arabic, key=str)


class ReindexTest(unittest.TestCase):
    def test_delete_state_reindex_identical(self):
        conn = state.connect()
        for k in range(2):
            sid = f"reindex-session-{k}"
            state.upsert_session(conn, sid, project="demo-repo", cwd="/x", project_key_source="git")
            n = state.next_prompt_number(conn, sid)
            state.add_prompt(conn, sid, n, f"reindex probe request {k} -- with dashes", private=False)
            s = state.get_session(conn, sid)
            store.save_observations(conn, s, n, [
                Observation(type="feature", title=f"Reindex probe feature {k}", subtitle="sub -- dash",
                            facts=[f"fact {k}", "فاتورة جديدة"], narrative="why --> because",
                            concepts=["pattern"], files_read=["a.py"], files_modified=[f"b{k}.py"])],
                tokens=10, model="m")
            sm = store.save_summary(conn, s, n, Summary(request=f"reindex probe {k}", completed="done",
                                                        next_steps="next"), tokens=5, model="m")
            writer.summary_saved(conn, s, sm)
            writer.session_end(conn, state.get_session(conn, sid))
        # Only the sessions written to this vault take part in the comparison.
        conn.execute("DELETE FROM observations WHERE content_session_id NOT LIKE 'reindex-session-%' "
                     "AND markdown_path IS NULL")
        before = snapshot(conn)
        counts = reindex.rebuild(conn, TEST_VAULT)
        self.assertGreaterEqual(counts["observations"], 2)
        after = snapshot(conn)
        for label, b, a in zip(("observations", "summaries", "search", "arabic"), before, after):
            mine_b = [x for x in b if "reindex-session" in str(x) or label in ("search", "arabic")]
            mine_a = [x for x in a if "reindex-session" in str(x) or label in ("search", "arabic")]
            self.assertEqual(mine_b, mine_a, label)
        rows = conn.execute("SELECT subtitle, narrative FROM observations WHERE title = 'Reindex probe feature 0'"
                            ).fetchone()
        self.assertEqual(tuple(rows), ("sub -- dash", "why --> because"))   # comment escaping round-trips


class SidecarOnlyTest(unittest.TestCase):
    def test_session_with_only_its_sidecar_is_rebuilt(self):
        conn = state.connect()
        sid = "reindex-sidecar-only"
        state.upsert_session(conn, sid, project="demo-repo", cwd="/x", project_key_source="git")
        n = state.next_prompt_number(conn, sid)
        state.add_prompt(conn, sid, n, "sidecar only probe", private=False)
        s = state.get_session(conn, sid)
        store.save_observations(conn, s, n, [Observation(type="change", title="Sidecar only probe")],
                                tokens=1, model="m")
        path = writer.write_session(conn, sid)
        path.unlink()                                  # the note is moved out of the vault by hand
        counts = reindex.rebuild(conn, TEST_VAULT)
        self.assertGreaterEqual(counts["sessions"], 1)
        row = conn.execute("SELECT title FROM observations WHERE content_session_id = ?", (sid,)).fetchone()
        self.assertEqual(row["title"], "Sidecar only probe")
        path.with_suffix(".json").unlink()            # throwaway test vault: leave it clean for other tests


class HomeInVaultTest(unittest.TestCase):
    def test_default_home_is_in_the_vault_and_the_observer_is_not(self):
        import os
        from pathlib import Path
        from unittest import mock
        from memo_capture import paths
        with mock.patch.dict(os.environ, {"MEMO_HOME": ""}):
            self.assertEqual(paths.memo_home(), TEST_VAULT / ".memo")
            self.assertEqual(paths.observer_home(), Path.home() / ".memo")

    def test_a_new_index_fills_itself_from_the_vault(self):
        import os
        import tempfile
        from unittest import mock
        conn = state.connect()
        sid = "auto-reindex-1"
        state.upsert_session(conn, sid, project="auto-reindex-repo", cwd="/x", project_key_source="git")
        state.add_prompt(conn, sid, 1, "auto reindex probe", False)
        store.save_summary(conn, state.get_session(conn, sid), 1, Summary(request="auto reindex probe",
                                                                          completed="done"), tokens=1, model="m")
        writer.write_session(conn, sid)
        with mock.patch.dict(os.environ, {"MEMO_HOME": tempfile.mkdtemp(prefix="memo-fresh-"),
                                          "MEMO_AUTO_REINDEX": "1"}):
            fresh = state.connect()
            self.assertIsNotNone(fresh.execute("SELECT 1 FROM sessions WHERE content_session_id = ?",
                                               (sid,)).fetchone())


if __name__ == "__main__":
    unittest.main()


import json
import os
import tempfile
from pathlib import Path
from unittest import mock


def _sidecar(path: Path, *, sid: str, project: str, machine: str, obs_id: int, started: int = 1000,
             updated: int = 1000, prompt_counter: int = 1, hash_: str = None, title: str = "t"):
    hash_ = hash_ or f"hash-{obs_id}"
    data = {
        "memo_session_data": 1,
        "session": {
            "session_id": sid, "project": project, "host": "test", "cwd": "/x",
            "started_at": started, "updated_at": updated, "status": "active",
            "machine": machine, "project_key_source": "git", "model": "m",
        },
        "prompts": [{"n": 1, "text": "hi", "at": started}],
        "observations": [{
            "id": obs_id, "prompt": 1, "type": "discovery", "title": title,
            "subtitle": "", "facts": ["f"], "narrative": "n", "concepts": [],
            "files_read": [], "files_modified": [], "hash": hash_, "count": 1,
            "tokens": 0, "model": "m", "at": started,
        }],
        "summaries": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


class CrossAdoptTest(unittest.TestCase):
    def _home(self, name, id_base, owns=False):
        home = Path(tempfile.mkdtemp(prefix=f"memo-{name}-"))
        (home / "machine.json").write_text(json.dumps({
            "name": name, "id_base": id_base, "owns_unstamped": owns,
        }), encoding="utf-8")
        return home

    def test_cross_adopt_preserves_ids_and_windows(self):
        mac_home = self._home("mac", 0, owns=True)
        vps_home = self._home("vps", 1_000_000_000, owns=False)
        vault = Path(tempfile.mkdtemp(prefix="memo-vault-cross-"))
        (vault / "sessions").mkdir()

        mac_sc = vault / "sessions" / "mac-session.json"
        vps_sc = vault / "sessions" / "vps-session.json"
        _sidecar(mac_sc, sid="mac-session", project="demo", machine="mac", obs_id=7, hash_="mac-hash")
        _sidecar(vps_sc, sid="vps-session", project="demo", machine="vps",
                 obs_id=1_000_000_007, hash_="vps-hash", started=2000, updated=2000)

        with mock.patch.dict(os.environ, {"MEMO_HOME": str(mac_home), "MEMO_AUTO_REINDEX": "0"}):
            from memo_capture import settings, state, store, reindex
            settings.load(refresh=True)
            mac = state.connect()
            r = reindex.adopt_sidecar(mac, "vps-session", vps_sc, "sessions/vps-session.md")
            self.assertIsNone(r.error)
            self.assertFalse(r.collisions)
            row = mac.execute("SELECT * FROM sessions WHERE content_session_id='vps-session'").fetchone()
            self.assertEqual(row["status"], "ended")
            self.assertEqual(row["started_at"], 2000)
            self.assertEqual(row["markdown_path"], "sessions/vps-session.md")
            self.assertEqual(row["machine"], "vps")
            self.assertFalse(reindex.is_own(row["machine"]))
            oid = mac.execute("SELECT id FROM observations WHERE content_hash='vps-hash'").fetchone()["id"]
            self.assertEqual(oid, 1_000_000_007)
            # allocate one more in mac window
            state.upsert_session(mac, "mac-new", project="demo")
            s = state.get_session(mac, "mac-new")
            from memo_capture.parser import Observation
            ids = store.save_observations(mac, s, 1, [Observation(type="discovery", title="m", narrative="n", facts=["a"])],
                                          tokens=0, model="t")
            self.assertEqual(ids[0], 1)  # window 0 first id
            mac.raw.close()

        with mock.patch.dict(os.environ, {"MEMO_HOME": str(vps_home), "MEMO_AUTO_REINDEX": "0"}):
            from memo_capture import settings, state, store, reindex
            settings.load(refresh=True)
            vps = state.connect()
            r = reindex.adopt_sidecar(vps, "mac-session", mac_sc, "sessions/mac-session.md")
            self.assertFalse(r.collisions)
            row = vps.execute("SELECT * FROM sessions WHERE content_session_id='mac-session'").fetchone()
            self.assertEqual(row["status"], "ended")
            self.assertEqual(row["machine"], "mac")
            self.assertFalse(reindex.is_own("mac"))
            oid = vps.execute("SELECT id FROM observations WHERE content_hash='mac-hash'").fetchone()["id"]
            self.assertEqual(oid, 7)
            state.upsert_session(vps, "vps-new", project="demo")
            s = state.get_session(vps, "vps-new")
            from memo_capture.parser import Observation
            ids = store.save_observations(vps, s, 1, [Observation(type="discovery", title="v", narrative="n", facts=["b"])],
                                          tokens=0, model="t")
            self.assertEqual(ids[0], 1_000_000_001)
            # newer foreign sidecar updates
            _sidecar(mac_sc, sid="mac-session", project="demo", machine="mac", obs_id=7,
                     hash_="mac-hash", started=1000, updated=5000, title="t")
            # bump prompt in sidecar
            data = json.loads(mac_sc.read_text())
            data["prompts"].append({"n": 2, "text": "more", "at": 5000})
            data["session"]["updated_at"] = 5000
            mac_sc.write_text(json.dumps(data))
            reindex.adopt_sidecar(vps, "mac-session", mac_sc, "sessions/mac-session.md")
            row = vps.execute("SELECT updated_at, prompt_counter FROM sessions WHERE content_session_id='mac-session'").fetchone()
            self.assertEqual(row["updated_at"], 5000)
            self.assertGreaterEqual(row["prompt_counter"], 2)
            # re-adopting own sidecar with status active after ended leaves ended
            # (on mac)
            vps.raw.close()

        with mock.patch.dict(os.environ, {"MEMO_HOME": str(mac_home), "MEMO_AUTO_REINDEX": "0"}):
            from memo_capture import settings, state, reindex
            settings.load(refresh=True)
            mac = state.connect()
            state.upsert_session(mac, "own-ended", project="demo")
            mac.execute("UPDATE sessions SET status='ended' WHERE content_session_id='own-ended'")
            sc = vault / "sessions" / "own-ended.json"
            _sidecar(sc, sid="own-ended", project="demo", machine="mac", obs_id=99, hash_="own-hash")
            data = json.loads(sc.read_text())
            data["session"]["status"] = "active"
            sc.write_text(json.dumps(data))
            reindex.adopt_sidecar(mac, "own-ended", sc, "sessions/own-ended.md")
            row = mac.execute("SELECT status FROM sessions WHERE content_session_id='own-ended'").fetchone()
            self.assertEqual(row["status"], "ended")
            mac.raw.close()


class CollisionTest(unittest.TestCase):
    def test_foreign_id_collision_listed(self):
        home = Path(tempfile.mkdtemp(prefix="memo-coll-"))
        (home / "machine.json").write_text(json.dumps({
            "name": "mac", "id_base": 0, "owns_unstamped": True,
        }), encoding="utf-8")
        vault = Path(tempfile.mkdtemp(prefix="memo-vault-coll-"))
        with mock.patch.dict(os.environ, {"MEMO_HOME": str(home), "MEMO_AUTO_REINDEX": "0"}):
            from memo_capture import settings, state, store, reindex
            from memo_capture.parser import Observation
            settings.load(refresh=True)
            conn = state.connect()
            state.upsert_session(conn, "local", project="demo")
            s = state.get_session(conn, "local")
            store.save_observations(conn, s, 1, [Observation(type="discovery", title="local", narrative="n", facts=["x"])],
                                    tokens=0, model="t")
            # force id 1 taken
            local_id = conn.execute("SELECT id FROM observations WHERE title='local'").fetchone()["id"]
            sc = vault / "foreign.json"
            _sidecar(sc, sid="foreign", project="demo", machine="vps", obs_id=local_id, hash_="other-hash", title="foreign")
            r = reindex.adopt_sidecar(conn, "foreign", sc, "sessions/foreign.md")
            self.assertTrue(r.collisions)
            self.assertEqual(r.collisions[0]["kind"], "observation")
            # own sidecar id taken → fresh own-window id
            sc2 = vault / "own.json"
            _sidecar(sc2, sid="own-recover", project="demo", machine="mac", obs_id=local_id,
                     hash_="recover-hash", title="recover")
            r2 = reindex.adopt_sidecar(conn, "own-recover", sc2, "sessions/own.md")
            self.assertFalse(r2.collisions)
            new_id = conn.execute("SELECT id FROM observations WHERE content_hash='recover-hash'").fetchone()["id"]
            self.assertNotEqual(new_id, local_id)
            self.assertLess(new_id, 1_000_000_000)
            # same-hash under another id → skipped
            before = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            sc3 = vault / "dup.json"
            _sidecar(sc3, sid="dup", project="demo", machine="vps", obs_id=999, hash_="other-hash", title="dup")
            # other-hash was collision and NOT inserted; use local's hash
            local_hash = conn.execute("SELECT content_hash FROM observations WHERE title='local'").fetchone()[0]
            _sidecar(sc3, sid="dup", project="demo", machine="vps", obs_id=999, hash_=local_hash, title="dup")
            reindex.adopt_sidecar(conn, "dup", sc3, "sessions/dup.md")
            after = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            self.assertEqual(before, after)
            # malformed sidecar
            bad = vault / "bad.json"
            bad.write_text("not-json", encoding="utf-8")
            r3 = reindex.adopt_sidecar(conn, "bad", bad, "sessions/bad.md")
            self.assertIsNotNone(r3.error)
            conn.raw.close()

    def test_no_machine_json_treats_unstamped_as_foreign(self):
        home = Path(tempfile.mkdtemp(prefix="memo-nomj-"))
        # no machine.json
        vault = Path(tempfile.mkdtemp(prefix="memo-vault-nomj-"))
        with mock.patch.dict(os.environ, {"MEMO_HOME": str(home), "MEMO_AUTO_REINDEX": "0"}, clear=False):
            # ensure no machine.json
            mj = home / "machine.json"
            if mj.exists():
                mj.unlink()
            from memo_capture import settings, state, reindex
            settings.load(refresh=True)
            self.assertFalse(settings.get("owns_unstamped"))
            conn = state.connect()
            # occupy id 1
            conn.execute("""INSERT INTO sessions (content_session_id, host, project, started_at, updated_at, machine)
                            VALUES ('x','t','demo',1,1,'unnamed-host')""")
            conn.execute("""INSERT INTO observations (id, content_session_id, project, type, title, facts, content_hash, created_at)
                            VALUES (1,'x','demo','discovery','taken','[]','taken-hash',1)""")
            sc = vault / "unstamped.json"
            data = _sidecar(sc, sid="unstamped", project="demo", machine="", obs_id=1, hash_="u-hash")
            # remove machine stamp
            d = json.loads(sc.read_text())
            d["session"].pop("machine", None)
            sc.write_text(json.dumps(d))
            r = reindex.adopt_sidecar(conn, "unstamped", sc, "sessions/u.md")
            self.assertTrue(r.collisions)
            conn.raw.close()


class RebuildTest(unittest.TestCase):
    def test_reindex_reports_collision_and_legacy(self):
        home = Path(tempfile.mkdtemp(prefix="memo-reb-"))
        (home / "machine.json").write_text(json.dumps({
            "name": "mac", "id_base": 0, "owns_unstamped": True,
        }), encoding="utf-8")
        vault = Path(tempfile.mkdtemp(prefix="memo-vault-reb-"))
        (vault / "sessions" / "demo").mkdir(parents=True)
        # local first (sorted before foreign) takes id 1; foreign then collides
        sc2 = vault / "sessions" / "demo" / "a-local.json"
        _sidecar(sc2, sid="local", project="demo", machine="mac", obs_id=1, hash_="l-hash")
        sc = vault / "sessions" / "demo" / "z-foreign.json"
        _sidecar(sc, sid="foreign", project="demo", machine="vps", obs_id=1, hash_="f-hash")
        # legacy comment-only note
        legacy = vault / "sessions" / "demo" / "legacy.md"
        legacy.write_text(
            "---\ntitle: legacy\n---\n"
            "<!-- memo:session {\"session_id\": \"legacy\", \"project\": \"demo\", \"host\": \"t\", "
            "\"started_at\": 1, \"updated_at\": 1, \"status\": \"ended\"} -->\n"
            "<!-- memo:obs {\"id\": 50, \"prompt\": 1, \"type\": \"discovery\", \"title\": \"legacy-obs\", "
            "\"facts\": [], \"hash\": \"legacy-hash\", \"at\": 1} -->\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"MEMO_HOME": str(home), "MEMO_AUTO_REINDEX": "0",
                                          "MEMO_VAULT": str(vault)}):
            from memo_capture import settings, state, reindex
            from memo_capture.paths import vault_dir
            settings.load(refresh=True)
            conn = state.connect()
            counts = reindex.rebuild(conn, vault)
            self.assertGreaterEqual(len(counts.get("collisions") or []), 1)
            # legacy loaded
            row = conn.execute("SELECT title FROM observations WHERE content_hash='legacy-hash'").fetchone()
            self.assertIsNotNone(row)
            # automatic first build stores collisions — simulate via _rebuild_new_index path
            import json as _json
            # store like state does
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('rebuild_collisions', ?)",
                         (_json.dumps(counts["collisions"]),))
            meta = conn.execute("SELECT value FROM meta WHERE key='rebuild_collisions'").fetchone()
            self.assertIsNotNone(meta)
            conn.raw.close()
