"""The Memo worker: one long-running local process per machine.

- HTTP on 127.0.0.1 (port 37800 + uid % 100): /health, /api/spool/nudge.
- Drain thread: spool files → queue rows, on boot, on every nudge, and every
  30 seconds as a sweep.
- Process loop: hands each session's queued events to the observer
  (processor.process_session), at most `observer_max_concurrent` at a time.

Hooks never depend on it being up: they spool to disk and start it.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Set

import sqlite3

from . import __version__, health, ingest, settings, spool, state
from .paths import log_line, memo_home

SWEEP_SECONDS = 30
STUCK_EXIT_SEC = 300
SYNC_SECONDS_PER_MINUTE = 60
SYNC_POLL_SECONDS = 60      # writes failing this long: exit, and the next hook starts a fresh worker


def pid_file():
    return memo_home() / "worker.pid"


class Worker:
    def __init__(self) -> None:
        self.conn = state.connect()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.busy: Set[str] = set()
        self.busy_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max(1, int(settings.get("observer_max_concurrent"))))
        self.last_activity = time.time()
        self.server: Optional[ThreadingHTTPServer] = None
        self.failing_since: Optional[float] = None
        self.last_error = ""

    # --------------------------------------------------------------- health
    def note(self, ok: bool, error: str = "") -> None:
        """Track whether the index takes writes; a worker stuck for STUCK_EXIT_SEC exits."""
        now = time.time()
        if ok:
            self.failing_since, self.last_error = None, ""
        else:
            self.failing_since = self.failing_since or now
            self.last_error = error[:300]
        health.write({"pid": os.getpid(), "at": int(now * 1000),
                      "failing_since": int(self.failing_since * 1000) if self.failing_since else None,
                      "last_error": self.last_error})
        if self.failing_since and now - self.failing_since > STUCK_EXIT_SEC:
            log_line("worker", f"index not writable for {int(now - self.failing_since)}s ({self.last_error}); "
                               "exiting so a fresh worker takes over")
            self.shutdown()
            os._exit(75)

    # ------------------------------------------------------------- draining
    def drain_once(self) -> None:
        stats = spool.drain(self.conn, lambda kind, payload: ingest.enqueue_event(self.conn, kind, payload))
        if stats["ingested"] or stats["corrupt"] or stats["failed"]:
            log_line("worker", f"drain {stats}")
            self.last_activity = time.time()
        if stats["ingested"]:
            self.note(True)
        elif stats["failed"] and "OperationalError" in stats.get("error", ""):
            self.note(False, stats["error"])
        elif not stats["failed"]:
            self.note(True)

    def drain_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.drain_once()
                self.dispatch()
                from .observer import POOL
                POOL.close_idle()
                self.wrap_up_idle()
            except sqlite3.OperationalError as exc:
                log_line("worker", "drain failed:\n" + traceback.format_exc())
                self.note(False, f"OperationalError: {exc}")
            except Exception:
                log_line("worker", "drain failed:\n" + traceback.format_exc())
            self.wake.wait(SWEEP_SECONDS)
            self.wake.clear()
            idle = int(settings.get("worker_idle_exit_sec") or 0)
            if idle and time.time() - self.last_activity > idle and not self.busy:
                log_line("worker", "idle exit")
                self.shutdown()

    def wrap_up_idle(self) -> None:
        """Hosts without a reliable SessionEnd hook: end a session after it goes quiet."""
        cutoff = state.now_ms() - int(settings.get("observer_idle_close_sec")) * 1000
        rows = self.conn.execute("""SELECT content_session_id, prompt_counter, transcript_path FROM sessions
                                    WHERE status = 'active' AND host IN ('codex', 'cursor', 'antigravity', 'opencode', 'gemini', 'zadloop')
                                    AND updated_at < ?""", (cutoff,)).fetchall()
        for row in rows:
            sid = row["content_session_id"]
            busy = self.conn.execute("SELECT 1 FROM pending_messages WHERE content_session_id = ? LIMIT 1",
                                     (sid,)).fetchone()
            if busy:
                continue
            number = int(row["prompt_counter"] or 1)
            has_summary = self.conn.execute("SELECT 1 FROM session_summaries WHERE content_session_id = ? "
                                            "AND prompt_number = ?", (sid, number)).fetchone()
            if not has_summary and row["transcript_path"]:
                # Headless runs (cursor-agent -p) never fire their stop hook.
                from . import privacy, transcript
                text, model = transcript.last_assistant_turn(row["transcript_path"])
                if text:
                    state.enqueue(self.conn, sid, "summarize",
                                  {"last_assistant_message": privacy.clean_text(text, 32_000),
                                   "observed_model": model}, number)
            with state.transaction(self.conn):
                state.enqueue(self.conn, sid, "session_end", {"reason": "idle"}, number)
                self.conn.execute("UPDATE sessions SET status = 'ending' WHERE content_session_id = ?", (sid,))
            self.wake.set()

    # ----------------------------------------------------------- processing
    def dispatch(self) -> None:
        try:
            from . import processor
        except ImportError:
            return  # the observer is not installed yet
        for sid in state.sessions_with_pending(self.conn):
            with self.busy_lock:
                if sid in self.busy:
                    continue
                self.busy.add(sid)
            self.pool.submit(self._run_session, processor, sid)

    def _run_session(self, processor, sid: str) -> None:
        try:
            if processor.process_session(sid):
                self.note(True)
            self.last_activity = time.time()
        except sqlite3.OperationalError as exc:
            log_line("worker", f"session {sid} failed:\n" + traceback.format_exc())
            self.note(False, f"OperationalError: {exc}")
        except Exception:
            log_line("worker", f"session {sid} failed:\n" + traceback.format_exc())
        finally:
            with self.busy_lock:
                self.busy.discard(sid)
            self.wake.set()   # more may have arrived while it ran


    def sync_loop(self) -> None:
        """Background git sync ticks (plan 003 TASK-006)."""
        from . import sync as sync_mod
        scripts_at_start = None
        try:
            st0 = sync_mod.read_state()
            scripts_at_start = st0.get("scripts_merged")
        except Exception:
            scripts_at_start = None
        first = True
        while not self.stopping.is_set():
            try:
                settings.load(refresh=True)
                interval = int(settings.get("sync_interval_minutes") or 0)
                if interval <= 0:
                    self.stopping.wait(SYNC_POLL_SECONDS)
                    first = True
                    continue
                if first:
                    first = False
                else:
                    # wait interval between ticks
                    self.stopping.wait(interval * SYNC_SECONDS_PER_MINUTE)
                    if self.stopping.is_set():
                        break
                    settings.load(refresh=True)
                    interval = int(settings.get("sync_interval_minutes") or 0)
                    if interval <= 0:
                        first = True
                        continue
                try:
                    sync_mod.run(initiator="worker")
                except Exception:
                    log_line("worker", "sync tick failed:\n" + traceback.format_exc())
                try:
                    st = sync_mod.read_state()
                    sm = st.get("scripts_merged")
                    if sm and scripts_at_start is not None and sm != scripts_at_start:
                        log_line("worker", f"scripts_merged changed ({scripts_at_start} -> {sm}); restarting")
                        self.shutdown()
                        os._exit(75)
                    if scripts_at_start is None and sm:
                        scripts_at_start = sm
                except Exception:
                    pass
            except Exception:
                log_line("worker", "sync_loop error:\n" + traceback.format_exc())
                self.stopping.wait(SYNC_POLL_SECONDS)

    # ----------------------------------------------------------------- http
    def serve(self) -> None:
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # keep stdout/stderr clean
                pass

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/health"):
                    self._send(200, {"ok": True, "pid": os.getpid(), "version": __version__,
                                     "queue": state.queue_counts(worker.conn),
                                     "busy_sessions": sorted(worker.busy)})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(min(length, 1_000_000))
                if self.path.startswith("/api/spool/nudge"):
                    worker.wake.set()
                    self._send(200, {"ok": True})
                elif self.path.startswith("/api/shutdown"):
                    self._send(200, {"ok": True})
                    threading.Thread(target=worker.shutdown, daemon=True).start()
                else:
                    self._send(404, {"error": "not found"})

        import oslock
        self.server = oslock.local_http_server((settings.get("worker_host"), settings.worker_port()), Handler)
        self.server.daemon_threads = True
        self.server.serve_forever(poll_interval=0.5)

    def shutdown(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.wake.set()
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        from .observer import POOL
        POOL.kill_all()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def run(self) -> int:
        reset = state.reset_stale_processing(self.conn)
        if reset:
            log_line("worker", f"requeued {reset} item(s) left processing by a previous worker")
        state.prune_spool_consumed(self.conn)
        try:
            self.server_thread = threading.Thread(target=self.serve, daemon=True)
            self.server_thread.start()
            time.sleep(0.2)
            if not self.server_thread.is_alive():
                raise OSError("could not bind the worker port")
        except OSError as exc:
            log_line("worker", f"not starting: {exc}")
            return 1
        pid_file().write_text(json.dumps({"pid": os.getpid(), "port": settings.worker_port()}))
        signal.signal(signal.SIGTERM, lambda *_: self.shutdown())
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())
        log_line("worker", f"started pid={os.getpid()} port={settings.worker_port()} version={__version__}")
        self.sync_thread = threading.Thread(target=self.sync_loop, name="memo-sync", daemon=True)
        self.sync_thread.start()
        try:
            self.drain_loop()
        finally:
            try:
                if json.loads(pid_file().read_text()).get("pid") == os.getpid():
                    pid_file().unlink()
            except (OSError, ValueError):
                pass
            log_line("worker", "stopped")
        return 0


def main() -> int:
    import faulthandler
    if hasattr(signal, "SIGUSR1"):
        # `kill -USR1 <pid>` prints every thread's stack to stderr: how a stuck worker is diagnosed.
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    settings.ensure_file()
    return Worker().run()
