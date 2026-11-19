import json
import re
import subprocess
import unittest

from tests.capture import TEST_VAULT
from memo_capture import state, store, writer
from memo_capture.parser import Observation, Summary


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.conn = state.connect()

    def make(self, sid, project="demo-repo"):
        state.upsert_session(self.conn, sid, project=project, cwd=f"/x/{project}", project_key_source="git")
        n = state.next_prompt_number(self.conn, sid)
        state.add_prompt(self.conn, sid, n, "Add the hooks\nand test them", private=False)
        s = state.get_session(self.conn, sid)
        store.save_observations(self.conn, s, n, [
            Observation(type="feature", title=f"Added hooks {sid[-2:]}", subtitle="All 8 events.", facts=["8 events", "exit 0"],
                        narrative="The plugin -- now records.", concepts=["how-it-works"],
                        files_read=["a.py"], files_modified=["plugin/hooks/hooks.json"])], tokens=120, model="m")
        return s, n

    def test_session_file_layout_and_frontmatter(self):
        s, n = self.make("writer-session-0001")
        path = writer.write_session(self.conn, "writer-session-0001")
        rel = path.relative_to(TEST_VAULT).as_posix()
        self.assertRegex(rel, r"^sessions/demo-repo/\d{4}-\d{2}-\d{2}/writer-session-0001\.md$")
        self.assertEqual(writer.file_stem("b1436e7e-480d-4c8b-86b2-653e40a5e14e"), "b1436e7e")
        text = path.read_text()
        self.assertIn("type: session", text)
        self.assertIn("  - type/session\n  - status/active", text)
        self.assertIn('project_note: "[[projects/demo-repo]]"', text)
        self.assertIn("> Add the hooks\n> and test them", text)
        self.assertIn("· ◆ feature · Added hooks 01", text)
        self.assertIn("`plugin/hooks/hooks.json`", text)
        self.assertNotIn("<!-- memo:", text)      # the note stays clean; data lives in the sidecar
        data = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual((data["observations"][0]["title"], data["observations"][0]["facts"]),
                         ("Added hooks 01", ["8 events", "exit 0"]))

    def test_summary_commits_one_turn(self):
        s, n = self.make("writer-session-0002")
        writer.observations_saved(self.conn, s, [1])
        before = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=TEST_VAULT,
                                capture_output=True, text=True).stdout.strip()
        sid_ = store.save_summary(self.conn, s, n, Summary(request="Add hooks", completed="Hooks added",
                                                           next_steps="Worker"), tokens=50, model="m")
        writer.summary_saved(self.conn, s, sid_)
        after = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=TEST_VAULT,
                               capture_output=True, text=True).stdout.strip()
        # The turn's note, then the checkpoint: the day's timeline line and the project's latest state.
        self.assertEqual(int(after), int(before) + 3)
        log = subprocess.run(["git", "log", "-3", "--format=%s", "--name-only"], cwd=TEST_VAULT,
                             capture_output=True, text=True).stdout
        self.assertIn("memo: session demo-repo writer-s — request 1: Add hooks", log)
        self.assertIn("sessions/demo-repo/", log)
        self.assertIn(".json", log)                 # the sidecar is committed with the note
        text = (TEST_VAULT / state.get_session(self.conn, "writer-session-0002")["markdown_path"]).read_text()
        self.assertIn("- **Completed:** Hooks added", text)
        self.assertIn("- **Files edited:** `plugin/hooks/hooks.json`", text)

    def test_private_prompt_and_empty_session(self):
        state.upsert_session(self.conn, "writer-session-0003", project="demo-repo")
        n = state.next_prompt_number(self.conn, "writer-session-0003")
        state.add_prompt(self.conn, "writer-session-0003", n, "secret", private=True)
        self.assertIsNone(writer.write_session(self.conn, "writer-session-0003"))

    def test_sync_stamps_survive_a_rewrite(self):
        self.make("writer-session-0005")
        path = writer.write_session(self.conn, "writer-session-0005")
        text = path.read_text().replace("memo_session: true\n", "memo_session: true\nsecond-brain-id:\n"
                                        "  - aaaa-1111\n  - bbbb-2222\nsecond-brain-synced: October 28, 2026\n", 1)
        path.write_text(text)
        s = state.get_session(self.conn, "writer-session-0005")
        store.save_observations(self.conn, s, 1, [Observation(type="change", title="Another change 05")],
                                tokens=1, model="m")
        new = writer.write_session(self.conn, "writer-session-0005").read_text()
        self.assertIn("Another change 05", new)
        self.assertIn("second-brain-id:\n  - aaaa-1111\n  - bbbb-2222\nsecond-brain-synced: October 28, 2026\n---", new)

    def test_new_note_is_written_as_stub_then_filled(self):
        import os
        from unittest import mock
        from memo_capture import writer as w
        self.make("writer-session-0006")
        core = w._core()
        writes = []
        real = core.atomic_write
        with mock.patch.dict(os.environ, {"MEMO_CREATE_FILL_DELAY": "0.05"}), \
                mock.patch.object(core, "atomic_write",
                                  side_effect=lambda p, txt: (p.suffix == ".md" and writes.append(txt), real(p, txt))):
            path = writer.write_session(self.conn, "writer-session-0006")
        self.assertEqual(len(writes), 2)
        self.assertIn("Memo is writing this session", writes[0])
        self.assertTrue(writes[0].startswith("---\ntitle:"))
        self.assertEqual(path.read_text(), writes[1])

    def test_index_losses_come_back_from_the_sidecar(self):
        """2026-11-02: the stuck write rolled back an observation the sidecar already held."""
        s, n = self.make("writer-session-0007")
        path = writer.write_session(self.conn, "writer-session-0007")
        lost = self.conn.execute("SELECT id FROM observations WHERE content_session_id = 'writer-session-0007'"
                                 ).fetchone()["id"]
        self.conn.execute("DELETE FROM observations WHERE id = ?", (lost,))     # rolled back in the index
        store.save_observations(self.conn, s, n, [Observation(type="change", title="Newer work 07")],
                                tokens=1, model="m")                            # may reuse the same id
        path = writer.write_session(self.conn, "writer-session-0007")
        titles = [o["title"] for o in json.loads(path.with_suffix(".json").read_text())["observations"]]
        self.assertIn("Added hooks 07", titles)
        self.assertIn("Newer work 07", titles)
        self.assertIn("Added hooks 07", path.read_text())

    def test_guard_keeps_a_richer_sidecar_it_cannot_merge(self):
        from unittest import mock
        self.make("writer-session-0010")
        path = writer.write_session(self.conn, "writer-session-0010")
        before = path.with_suffix(".json").read_text()
        self.conn.execute("DELETE FROM observations WHERE content_session_id = 'writer-session-0010'")
        from memo_capture.reindex import AdoptResult
        with mock.patch("memo_capture.reindex.adopt_sidecar", return_value=AdoptResult(added=0)):
            writer.write_session(self.conn, "writer-session-0010")
        self.assertEqual(path.with_suffix(".json").read_text(), before)

    def test_long_session_stays_a_small_note(self):
        sid = "writer-session-0008"
        state.upsert_session(self.conn, sid, project="demo-repo", cwd="/x", project_key_source="git")
        n = state.next_prompt_number(self.conn, sid)
        state.add_prompt(self.conn, sid, n, "big", private=False)
        s = state.get_session(self.conn, sid)
        store.save_observations(self.conn, s, n, [
            Observation(type="discovery", title=f"Finding {i}", facts=["f" * 2000] * 5, narrative="n" * 5000,
                        files_modified=[f"src/file{i}.ts"]) for i in range(300)], tokens=1, model="m")
        path = writer.write_session(self.conn, sid)
        text = path.read_text()
        self.assertLess(len(text), 100_000)                          # 300 × 15 KB of detail stays out
        self.assertLess(max(len(l) for l in text.splitlines()), 400)
        self.assertIn("- obs-", text)
        data = json.loads(path.with_suffix(".json").read_text())
        self.assertEqual(len(data["observations"]), 300)
        self.assertEqual(len(data["observations"][0]["narrative"]), 5000)   # full detail kept

    def test_observations_rewrite_the_note_at_most_once_a_minute(self):
        from unittest import mock
        s, n = self.make("writer-session-0009")
        with mock.patch.object(writer, "write_session") as ws:
            writer.observations_saved(self.conn, s, [1])
            writer.observations_saved(self.conn, s, [2])
        self.assertEqual(ws.call_count, 1)

    def test_rewrite_is_stable(self):
        self.make("writer-session-0004")
        first = writer.write_session(self.conn, "writer-session-0004").read_text()
        second = writer.write_session(self.conn, "writer-session-0004").read_text()
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()


class SessionLintTest(unittest.TestCase):
    def test_lint_checks_session_fields_and_sidecar(self):
        import memo_core
        bad = TEST_VAULT / "sessions" / "lint-probe" / "2026-10-28" / "bad.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("---\ntitle: x\ntype: session\nstatus: active\ntags:\n  - type/session\n  - status/active\n"
                       "memo_session: true\n---\n# x\n")
        try:
            report = memo_core.vault_lint()
            codes = {(e["file"], e["code"]) for e in report["errors"] + report["warnings"]}
            self.assertIn(("sessions/lint-probe/2026-10-28/bad.md", "session_field"), codes)
            self.assertIn(("sessions/lint-probe/2026-10-28/bad.md", "session_sidecar"), codes)
        finally:
            bad.unlink()


import json
import os
import socket
import tempfile
from pathlib import Path
from unittest import mock


class ForeignWriteTest(unittest.TestCase):
    def test_foreign_session_left_byte_identical(self):
        home = Path(os.environ["MEMO_HOME"])
        bak = (home / "machine.json").read_text(encoding="utf-8")
        (home / "machine.json").write_text(json.dumps({
            "name": "mac", "id_base": 0, "owns_unstamped": True,
        }), encoding="utf-8")
        from memo_capture import settings, state, writer, reindex, commands
        settings.load(refresh=True)
        conn = state.connect()
        # Insert a foreign session row pointing at existing files
        sid = "foreign-write-session"
        vault = Path(os.environ["MEMO_VAULT"])
        rel = f"sessions/demo-repo/2026-11-13/{sid}.md"
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "---\ntitle: foreign\n---\n# foreign\n"
        path.write_text(body, encoding="utf-8")
        sidecar = path.with_suffix(".json")
        sidecar.write_text(json.dumps({
            "memo_session_data": 1,
            "session": {"session_id": sid, "project": "demo-repo", "host": "vps",
                        "started_at": 1, "updated_at": 1, "status": "ended", "machine": "vps"},
            "prompts": [], "observations": [{"id": 42, "prompt": 1, "type": "discovery",
                "title": "f", "facts": [], "narrative": "", "concepts": [], "files_read": [],
                "files_modified": [], "hash": "fh", "count": 1, "tokens": 0, "at": 1}],
            "summaries": [],
        }), encoding="utf-8")
        conn.execute("""INSERT OR REPLACE INTO sessions
            (content_session_id, host, project, status, markdown_path, machine, started_at, updated_at, prompt_counter)
            VALUES (?, 'vps', 'demo-repo', 'ended', ?, 'vps', 1, 1, 1)""", (sid, rel))
        before_md = path.read_bytes()
        before_js = sidecar.read_bytes()
        self.assertIsNone(writer.write_session(conn, sid))
        self.assertEqual(path.read_bytes(), before_md)
        self.assertEqual(sidecar.read_bytes(), before_js)
        # rerender skips it
        from memo_capture import commands as cmds
        # count via write_session returning None
        self.assertFalse(reindex.is_own("vps"))
        path.unlink()
        sidecar.unlink()
        (home / "machine.json").write_text(bak, encoding="utf-8")
        settings.load(refresh=True)

    def test_unnamed_machine_stamps_and_rewrites(self):
        home = Path(tempfile.mkdtemp(prefix="memo-unnamed-"))
        # no machine.json
        with mock.patch.dict(os.environ, {"MEMO_HOME": str(home), "MEMO_AUTO_REINDEX": "0"}):
            from memo_capture import settings, state, store, writer
            from memo_capture.parser import Observation
            settings.load(refresh=True)
            expected = f"unnamed-{socket.gethostname()}"
            self.assertEqual(settings.effective_name(), expected)
            conn = state.connect()
            sid = "unnamed-session-1"
            state.upsert_session(conn, sid, project="demo-repo")
            row = state.get_session(conn, sid)
            self.assertEqual(row["machine"], expected)
            n = state.next_prompt_number(conn, sid)
            state.add_prompt(conn, sid, n, "hi", private=False)
            s = state.get_session(conn, sid)
            store.save_observations(conn, s, n, [Observation(type="discovery", title="u", narrative="n", facts=["a"])],
                                    tokens=0, model="t")
            path = writer.write_session(conn, sid)
            self.assertIsNotNone(path)
            data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(data["session"]["machine"], expected)
            # recovery: delete obs from index, rewrite brings it back
            conn.execute("DELETE FROM observations WHERE content_session_id = ?", (sid,))
            path2 = writer.write_session(conn, sid)
            self.assertIsNotNone(path2)
            row = conn.execute("SELECT id FROM observations WHERE content_session_id = ?", (sid,)).fetchone()
            self.assertIsNotNone(row)
            conn.raw.close()
