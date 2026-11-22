import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.capture import ROOT

CLI = ROOT / "scripts" / "memo_capture" / "cli.py"


class WorkerProcessTest(unittest.TestCase):
    """Runs the real worker in its own MEMO_HOME on a free port."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="memo-worker-test-")
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        fake = str(ROOT / "tests" / "capture" / "fixtures" / "fake_claude.py")
        self.env = dict(os.environ, MEMO_HOME=self.home, MEMO_WORKER_PORT=str(self.port), MEMO_CLAUDE_CLI=fake)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                p.wait(5)

    def start(self):
        self.stderr_path = Path(self.home) / "worker-stderr.log"
        p = subprocess.Popen([sys.executable, str(CLI), "worker"], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=open(self.stderr_path, "wb"))
        self.procs.append(p)
        self.wait_health()
        return p

    def wait_health(self, timeout=10):
        import urllib.request
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=0.5) as r:
                    return json.loads(r.read())
            except Exception:
                time.sleep(0.1)
        detail = []
        if getattr(self, "stderr_path", None) and self.stderr_path.exists():
            detail.append("stderr:\n" + self.stderr_path.read_text(errors="replace")[-3000:])
        for log in sorted(Path(self.home).glob("logs/*")):
            detail.append(f"{log.name}:\n" + log.read_text(errors="replace")[-2000:])
        exited = [p.returncode for p in self.procs if p.poll() is not None]
        self.fail(f"worker did not answer /health (exited: {exited})\n" + "\n".join(detail))

    def hook(self, event, payload):
        res = subprocess.run([sys.executable, str(CLI), "hook", "claude-code", event], input=json.dumps(payload),
                             capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(res.returncode, 0)
        return json.loads(res.stdout)

    def queue_rows(self):
        """Queued or already processed tool events (the fake observer may finish them first)."""
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.home, "state.db"))
        try:
            return conn.execute("SELECT 'observation' FROM tool_uses ORDER BY id").fetchall()
        finally:
            conn.close()

    def wait_rows(self, n, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = self.queue_rows() if os.path.exists(os.path.join(self.home, "state.db")) else []
            if len(rows) >= n:
                return rows
            time.sleep(0.2)
        return rows

    def test_hooks_reach_the_queue_through_the_worker(self):
        self.start()
        base = {"session_id": "sess-w1", "cwd": str(ROOT)}
        self.hook("session-init", dict(base, prompt="rename the vault"))
        self.hook("observation", dict(base, tool_name="Bash", tool_use_id="toolu_w1",
                                      tool_input={"command": "ls"}, tool_response={"stdout": "a b"}))
        self.assertEqual([r[0] for r in self.wait_rows(1)], ["observation"])

    def test_events_survive_a_dead_worker(self):
        p = self.start()
        p.terminate(); p.wait(5)
        self.hook("observation", {"session_id": "sess-w2", "cwd": str(ROOT), "tool_name": "Read",
                                  "tool_use_id": "toolu_w2", "tool_input": {"file_path": "x"}})
        # The hook's nudge failed, so it started a fresh worker that drains the spool.
        self.wait_health()
        self.assertEqual(len(self.wait_rows(1)), 1)
        subprocess.run([sys.executable, str(CLI), "stop"], env=self.env, capture_output=True, timeout=20)

    def test_second_worker_does_not_start(self):
        self.start()
        second = subprocess.run([sys.executable, str(CLI), "worker"], env=self.env,
                                capture_output=True, timeout=20)
        self.assertEqual(second.returncode, 1)
        self.assertTrue(self.wait_health()["ok"])


class WatchdogTest(unittest.TestCase):
    def test_worker_exits_after_five_minutes_of_failed_writes(self):
        from unittest import mock
        from memo_capture import health, worker
        w = worker.Worker.__new__(worker.Worker)
        w.failing_since, w.last_error = None, ""
        with mock.patch.object(worker.Worker, "shutdown"), mock.patch.object(worker.os, "_exit") as exit_:
            w.note(False, "OperationalError: database is locked")
            exit_.assert_not_called()
            self.assertIsNotNone(health.read()["failing_since"])
            w.failing_since -= worker.STUCK_EXIT_SEC + 1
            w.note(False, "OperationalError: database is locked")
            exit_.assert_called_once_with(75)
            w.note(True)
        self.assertIsNone(health.read()["failing_since"])


if __name__ == "__main__":
    unittest.main()


import threading
import time
from unittest import mock


class SyncLoopTest(unittest.TestCase):
    def test_sync_loop_ticks_and_survives_errors(self):
        from memo_capture import worker as wmod
        from memo_capture.worker import Worker
        calls = {"n": 0, "exit": None}
        real_run = None

        def fake_run(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            if calls["n"] == 2:
                time.sleep(0.05)  # blocking tick
            return {"status": "ok", "reason": ""}

        def fake_exit(code):
            calls["exit"] = code
            raise SystemExit(code)

        w = Worker.__new__(Worker)
        w.stopping = threading.Event()
        with mock.patch.object(wmod, "SYNC_SECONDS_PER_MINUTE", 0.01), \
             mock.patch.dict("os.environ", {"MEMO_SYNC_INTERVAL_MINUTES": "3"}), \
             mock.patch("memo_capture.settings.load", side_effect=lambda refresh=False: {
                 "sync_interval_minutes": 3 if calls["n"] < 5 else 3
             }), \
             mock.patch("memo_capture.settings.get", side_effect=lambda k: 3 if k == "sync_interval_minutes" else None), \
             mock.patch("memo_capture.sync.run", side_effect=fake_run), \
             mock.patch("memo_capture.sync.read_state", return_value={"scripts_merged": None}), \
             mock.patch("os._exit", side_effect=fake_exit):
            # run loop briefly
            t = threading.Thread(target=w.sync_loop, daemon=True)
            t.start()
            deadline = time.time() + 2
            while calls["n"] < 2 and time.time() < deadline:
                time.sleep(0.02)
            w.stopping.set()
            t.join(1)
        self.assertGreaterEqual(calls["n"], 2)

    def test_interval_zero_then_starts(self):
        from memo_capture import worker as wmod
        from memo_capture.worker import Worker
        state = {"interval": 0, "n": 0}

        def get(k):
            if k == "sync_interval_minutes":
                return state["interval"]
            return None

        def fake_run(**kwargs):
            state["n"] += 1
            return {"status": "ok"}

        w = Worker.__new__(Worker)
        w.stopping = threading.Event()
        with mock.patch.object(wmod, "SYNC_SECONDS_PER_MINUTE", 0.01), \
             mock.patch.object(wmod, "SYNC_POLL_SECONDS", 0.01), \
             mock.patch("memo_capture.settings.load"), \
             mock.patch("memo_capture.settings.get", side_effect=get), \
             mock.patch("memo_capture.sync.run", side_effect=fake_run), \
             mock.patch("memo_capture.sync.read_state", return_value={"scripts_merged": None}):
            t = threading.Thread(target=w.sync_loop, daemon=True)
            t.start()
            time.sleep(0.05)
            self.assertEqual(state["n"], 0)
            state["interval"] = 3
            deadline = time.time() + 2
            while state["n"] < 1 and time.time() < deadline:
                time.sleep(0.02)
            w.stopping.set()
            t.join(1)
        self.assertGreaterEqual(state["n"], 1)

    def test_scripts_merged_exits_75(self):
        from memo_capture import worker as wmod
        from memo_capture.worker import Worker
        calls = {"n": 0, "exit": None}
        states = [{"scripts_merged": "aaa"}, {"scripts_merged": "bbb"}]

        def fake_run(**kwargs):
            calls["n"] += 1
            return {"status": "ok"}

        def read_state():
            # first call at start, then after tick
            if calls["n"] == 0:
                return states[0]
            return states[1]

        def fake_exit(code):
            calls["exit"] = code
            raise SystemExit(code)

        w = Worker.__new__(Worker)
        w.stopping = threading.Event()
        with mock.patch.object(wmod, "SYNC_SECONDS_PER_MINUTE", 0.01), \
             mock.patch("memo_capture.settings.load"), \
             mock.patch("memo_capture.settings.get", return_value=3), \
             mock.patch("memo_capture.sync.run", side_effect=fake_run), \
             mock.patch("memo_capture.sync.read_state", side_effect=read_state), \
             mock.patch.object(w, "shutdown"), \
             mock.patch("os._exit", side_effect=fake_exit):
            try:
                w.sync_loop()
            except SystemExit:
                pass
        self.assertEqual(calls["exit"], 75)

    def test_local_scripts_commit_no_exit(self):
        from memo_capture import worker as wmod
        from memo_capture.worker import Worker
        calls = {"n": 0, "exit": None}

        def fake_run(**kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise SystemExit(0)  # stop loop for test
            return {"status": "ok"}

        def fake_exit(code):
            calls["exit"] = code
            raise SystemExit(code)

        w = Worker.__new__(Worker)
        w.stopping = threading.Event()
        with mock.patch.object(wmod, "SYNC_SECONDS_PER_MINUTE", 0.01), \
             mock.patch("memo_capture.settings.load"), \
             mock.patch("memo_capture.settings.get", return_value=3), \
             mock.patch("memo_capture.sync.run", side_effect=fake_run), \
             mock.patch("memo_capture.sync.read_state", return_value={"scripts_merged": "same"}), \
             mock.patch("os._exit", side_effect=fake_exit):
            try:
                w.sync_loop()
            except SystemExit:
                pass
        self.assertIsNone(calls["exit"])
