"""memo sync command and sync_warning (plan 003 TASK-007)."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.capture import ROOT  # noqa: F401
from memo_capture import commands, health, settings, status


class SyncStatusTest(unittest.TestCase):
    def setUp(self):
        self.cache = Path(os.environ["MEMO_VAULT"]) / ".cache"
        self.cache.mkdir(exist_ok=True)
        self.path = self.cache / "sync.json"
        self.path.write_text(json.dumps({
            "status": "conflict",
            "reason": "projects/taaib.md",
            "conflict_paths": ["projects/taaib.md"],
            "stuck_machines": [],
            "adopt_errors": [],
        }), encoding="utf-8")

    def test_sync_status_prints_conflict_first(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = commands.cmd_sync(["status"])
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("conflict: projects/taaib.md"), out)

    def test_memo_status_prints_sync_line(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status.main([])
        self.assertIn("Memo sync:", buf.getvalue())


class SyncWarningTest(unittest.TestCase):
    def setUp(self):
        self.cache = Path(os.environ["MEMO_VAULT"]) / ".cache"
        self.cache.mkdir(exist_ok=True)
        self.path = self.cache / "sync.json"
        self.home = Path(os.environ["MEMO_HOME"])
        self._bak = (self.home / "machine.json").read_text(encoding="utf-8")

    def tearDown(self):
        (self.home / "machine.json").write_text(self._bak, encoding="utf-8")
        settings.load(refresh=True)
        if self.path.exists():
            self.path.unlink()

    def _write(self, **kw):
        base = {"status": "ok", "reason": "", "conflict_paths": [], "stuck_machines": [],
                "adopt_errors": [], "held_sha": None, "resolving": None,
                "failing_since": None, "last_attempt": None}
        base.update(kw)
        self.path.write_text(json.dumps(base), encoding="utf-8")

    def test_conflict(self):
        self._write(status="conflict", conflict_paths=["projects/taaib.md"])
        line = health.sync_warning()
        self.assertIn("conflict", line)
        self.assertLessEqual(len(line), 300)

    def test_resolving(self):
        self._write(status="conflict", resolving={"base": "a", "remote": "b", "started": 1},
                    conflict_paths=["x.md"])
        self.assertIn("conflict", health.sync_warning())

    def test_stuck(self):
        self._write(stuck_machines=["vps"])
        self.assertIn("stuck", health.sync_warning())

    def test_code_change(self):
        self._write(status="skipped", reason="code-change docs/x.md", held_sha="abc123")
        line = health.sync_warning()
        self.assertIn("accept-code", line)
        self.assertIn("abc123", line)

    def test_adopt_errors(self):
        self._write(adopt_errors=[{"kind": "observation", "path": "sessions/x.json"}])
        self.assertIn("machine.json", health.sync_warning())

    def test_missing_machine_json(self):
        mj = self.home / "machine.json"
        mj.unlink()
        settings.load(refresh=True)
        self._write()
        line = health.sync_warning()
        self.assertIn("machine.json", line)
        mj.write_text(self._bak, encoding="utf-8")
        settings.load(refresh=True)

    def test_host_mismatch(self):
        (self.home / "machine.json").write_text(json.dumps({
            "name": "mac", "id_base": 0, "owns_unstamped": True, "host": "other-host",
        }), encoding="utf-8")
        settings.load(refresh=True)
        self._write()
        self.assertIn("host mismatch", health.sync_warning())

    def test_conflict_plus_adopt_more(self):
        self._write(status="conflict", conflict_paths=["a.md"],
                    adopt_errors=[{"kind": "observation", "path": "s.json"}])
        line = health.sync_warning()
        self.assertIn("conflict", line)
        self.assertIn("+1 more", line)

    def test_failing_short_none(self):
        now = 1_000_000
        self._write(status="retry", failing_since=now - 60_000, last_attempt=now)
        self.assertIsNone(health.sync_warning(now_ms=now))

    def test_failing_long_line(self):
        now = 1_000_000
        self._write(status="retry", failing_since=now - 31 * 60_000, last_attempt=now)
        self.assertIn("keeps failing", health.sync_warning(now_ms=now))

    def test_no_sync_json(self):
        if self.path.exists():
            self.path.unlink()
        # with valid machine.json, no warning
        (self.home / "machine.json").write_text(self._bak, encoding="utf-8")
        settings.load(refresh=True)
        self.assertIsNone(health.sync_warning())

    def test_machine_without_host(self):
        (self.home / "machine.json").write_text(json.dumps({
            "name": "mac", "id_base": 0, "owns_unstamped": True,
        }), encoding="utf-8")
        settings.load(refresh=True)
        self._write()
        self.assertIsNone(health.sync_warning())

    def test_long_conflict_truncated(self):
        paths = [f"projects/p{i}.md" for i in range(50)]
        self._write(status="conflict", conflict_paths=paths)
        line = health.sync_warning()
        self.assertLessEqual(len(line), 300)


class DoctorSyncTest(unittest.TestCase):
    def test_doctor_sync_section(self):
        from memo_capture import doctor
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor.main([])
        out = buf.getvalue()
        self.assertIn("Sync", out)
        self.assertIn("machine_name=", out)
        self.assertIn("id_base=", out)
