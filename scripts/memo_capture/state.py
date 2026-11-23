"""<vault>/.memo/state.db: the queue and a fast, rebuildable index of session memory.

The vault's Markdown under sessions/ is the source of truth; everything here
except pending_messages can be rebuilt from it (`memo reindex`).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .paths import memo_home

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    content_session_id TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL DEFAULT 'claude-code',
    project TEXT NOT NULL,
    project_key_source TEXT,
    cwd TEXT,
    observer_session_id TEXT,
    observer_prompt INTEGER NOT NULL DEFAULT 0,   -- last prompt the observer was told about
    observed_model TEXT,
    prompt_counter INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    markdown_path TEXT,
    transcript_path TEXT,
    machine TEXT,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_project ON sessions(project, started_at);

CREATE TABLE IF NOT EXISTS user_prompts (
    id INTEGER PRIMARY KEY,
    content_session_id TEXT NOT NULL,
    prompt_number INTEGER NOT NULL,
    prompt_text TEXT NOT NULL DEFAULT '',
    private INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE (content_session_id, prompt_number)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    content_session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    prompt_number INTEGER,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT,
    facts TEXT NOT NULL DEFAULT '[]',
    narrative TEXT,
    concepts TEXT NOT NULL DEFAULT '[]',
    files_read TEXT NOT NULL DEFAULT '[]',
    files_modified TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    discovery_tokens INTEGER NOT NULL DEFAULT 0,
    generated_by_model TEXT,
    agent_id TEXT,
    agent_type TEXT,
    markdown_path TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (project, content_hash)
);
CREATE INDEX IF NOT EXISTS observations_project ON observations(project, created_at);
CREATE INDEX IF NOT EXISTS observations_session ON observations(content_session_id, prompt_number);

CREATE TABLE IF NOT EXISTS observation_files (
    observation_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,              -- read | modified
    PRIMARY KEY (observation_id, path, kind)
);
CREATE INDEX IF NOT EXISTS observation_files_path ON observation_files(path);

CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY,
    content_session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    prompt_number INTEGER,
    request TEXT, investigated TEXT, learned TEXT, completed TEXT,
    next_steps TEXT, notes TEXT,
    files_read TEXT NOT NULL DEFAULT '[]',
    files_edited TEXT NOT NULL DEFAULT '[]',
    discovery_tokens INTEGER NOT NULL DEFAULT 0,
    generated_by_model TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (content_session_id, prompt_number)
);
CREATE INDEX IF NOT EXISTS summaries_project ON session_summaries(project, created_at);

CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY,
    content_session_id TEXT NOT NULL,
    message_type TEXT NOT NULL,      -- observation | summarize | session_end
    payload TEXT NOT NULL,
    prompt_number INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | processing | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    not_before INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_status ON pending_messages(status, content_session_id, id);

CREATE TABLE IF NOT EXISTS tool_uses (
    id INTEGER PRIMARY KEY,
    content_session_id TEXT NOT NULL,
    tool_use_id TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_response TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS file_context_seen (
    content_session_id TEXT NOT NULL,
    path TEXT NOT NULL,
    last_observation_id INTEGER NOT NULL,
    PRIMARY KEY (content_session_id, path)
);

CREATE TABLE IF NOT EXISTS spool_consumed (
    key TEXT PRIMARY KEY,
    consumed_at INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
    title, subtitle, narrative, facts, concepts,
    content='observations', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    request, investigated, learned, completed, next_steps, notes,
    content='session_summaries', content_rowid='id'
);
"""

_TRIGRAM = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_trigram USING fts5(
    kind UNINDEXED, ref UNINDEXED, body, tokenize='trigram'
);
"""

_lock = threading.Lock()

# One thread inside SQLite at a time, per process. macOS's system SQLite spun
# forever in purgeableCacheShrink (inside sqlite3_reset) when the worker's
# threads used it at once (sampled 2026-11-05: every SQLite thread stuck
# there, the worker silent, its write lock held for good). The worker's
# threads wait on the model, not on SQLite, so serialising costs nothing.
_SQL = threading.RLock()


class Result:
    """A finished statement: rows are read and the statement reset while the
    lock is held, so no SQLite call ever runs outside it."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.description = cursor.description
        self.rows = cursor.fetchall() if cursor.description is not None else []
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        self._at = 0

    def fetchone(self):
        if self._at >= len(self.rows):
            return None
        self._at += 1
        return self.rows[self._at - 1]

    def fetchall(self):
        rest, self._at = self.rows[self._at:], len(self.rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """sqlite3.Connection with every call serialised through _SQL."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self.raw = raw

    def _run(self, method: str, *args) -> Result:
        with _SQL:
            cursor = getattr(self.raw, method)(*args)
            try:
                return Result(cursor)
            finally:
                cursor.close()

    def execute(self, sql: str, params=()) -> Result:
        return self._run("execute", sql, params)

    def executemany(self, sql: str, seq) -> Result:
        return self._run("executemany", sql, list(seq))

    def executescript(self, script: str) -> None:
        with _SQL:
            self.raw.executescript(script)

    def commit(self) -> None:
        with _SQL:
            self.raw.commit()

    def close(self) -> None:
        with _SQL:
            self.raw.close()

    @property
    def in_transaction(self) -> bool:
        return self.raw.in_transaction

    @property
    def row_factory(self):
        return self.raw.row_factory

    @row_factory.setter
    def row_factory(self, value) -> None:
        self.raw.row_factory = value


def now_ms() -> int:
    return int(time.time() * 1000)


def db_path():
    return memo_home() / "state.db"


def connect() -> Connection:
    """A connection in autocommit mode.

    Python's default mode opens a transaction before every write and keeps it
    open until commit(); an error in between left one open for good inside the
    long-lived worker (2026-11-02) and locked every other writer. In
    autocommit mode a single statement is its own transaction, and grouped
    writes use transaction(), which always commits or rolls back.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    with _SQL:
        raw = sqlite3.connect(str(path), timeout=10, check_same_thread=False, isolation_level=None)
    raw.row_factory = sqlite3.Row
    conn = Connection(raw)
    conn.execute("PRAGMA busy_timeout=10000")
    if not _schema_ready(conn):
        conn.execute("PRAGMA journal_mode=WAL")
        with _lock:
            conn.executescript(_SCHEMA)
            try:
                conn.executescript(_TRIGRAM)
            except sqlite3.OperationalError:
                pass  # SQLite without the trigram tokenizer: FTS5 search still works
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            if "transcript_path" not in cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN transcript_path TEXT")
            if "machine" not in cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN machine TEXT")
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
    conn.execute("PRAGMA foreign_keys=ON")
    if fresh and os.environ.get("MEMO_AUTO_REINDEX", "1") != "0":
        _rebuild_new_index(conn)
    return conn


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """True when the schema is already in place, so a hook that only reads
    never has to take the write lock."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    except sqlite3.Error:
        return False
    return row is not None and "transcript_path" in cols and "machine" in cols


@contextlib.contextmanager
def transaction(conn) -> Iterator:
    """Group writes: BEGIN IMMEDIATE, then COMMIT, or ROLLBACK on any error.

    Nested use joins the outer transaction. Whatever happens, the connection
    leaves this block with no transaction open. The process's SQLite lock is
    held for the whole block, so another thread never waits on this
    transaction's write lock while holding the SQLite lock itself.
    """
    with _SQL:
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")


def _rebuild_new_index(conn: sqlite3.Connection) -> None:
    """A new machine or a deleted index: refill it from the vault's session notes,
    under a file lock so two hooks starting at once rebuild it only once."""
    import oslock
    from . import reindex
    from .paths import log_line, vault_dir
    try:
        with open(memo_home() / "reindex.lock", "w") as lock:
            oslock.lock(lock)
            if conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is None:
                counts = reindex.rebuild(conn, vault_dir())
                collisions = counts.get("collisions") or []
                if collisions:
                    import json as _json
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES ('rebuild_collisions', ?)",
                        (_json.dumps(collisions),))
                log_line("state", f"new index rebuilt from the vault: {counts}")
    except Exception as exc:   # memory stays readable from the Markdown; never block a hook
        log_line("state", f"automatic reindex failed: {exc!r}")


def has_trigram(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_trigram'").fetchone() is not None


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def loads(value: Optional[str], default: Any = None) -> Any:
    if value in (None, ""):
        return [] if default is None else default
    try:
        return json.loads(value)
    except ValueError:
        return [] if default is None else default


# ---------------------------------------------------------------- sessions

def upsert_session(conn: sqlite3.Connection, content_session_id: str, *, project: str,
                   host: str = "claude-code", cwd: str = "", project_key_source: str = "") -> sqlite3.Row:
    from . import settings as _settings
    ts = now_ms()
    machine = _settings.effective_name()
    conn.execute(
        """INSERT INTO sessions (content_session_id, host, project, project_key_source, cwd,
                                 machine, started_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(content_session_id) DO UPDATE SET updated_at = excluded.updated_at""",
        (content_session_id, host, project, project_key_source, cwd, machine, ts, ts))
    return get_session(conn, content_session_id)


def get_session(conn: sqlite3.Connection, content_session_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM sessions WHERE content_session_id = ?",
                        (content_session_id,)).fetchone()


def update_session(conn: sqlite3.Connection, content_session_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_ms()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE sessions SET {cols} WHERE content_session_id = ?",
                 (*fields.values(), content_session_id))


def next_prompt_number(conn: sqlite3.Connection, content_session_id: str) -> int:
    conn.execute("UPDATE sessions SET prompt_counter = prompt_counter + 1, updated_at = ? "
                 "WHERE content_session_id = ?", (now_ms(), content_session_id))
    row = get_session(conn, content_session_id)
    return int(row["prompt_counter"]) if row else 1


def current_prompt_number(conn: sqlite3.Connection, content_session_id: str) -> int:
    row = get_session(conn, content_session_id)
    return int(row["prompt_counter"]) if row else 0


def add_prompt(conn: sqlite3.Connection, content_session_id: str, number: int,
               text: str, private: bool, created_at: Optional[int] = None) -> None:
    conn.execute("""INSERT OR REPLACE INTO user_prompts
                    (content_session_id, prompt_number, prompt_text, private, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                 (content_session_id, number, "" if private else text, 1 if private else 0,
                  created_at or now_ms()))


def prompt_is_private(conn: sqlite3.Connection, content_session_id: str, number: int) -> bool:
    row = conn.execute("SELECT private FROM user_prompts WHERE content_session_id = ? "
                       "AND prompt_number = ?", (content_session_id, number)).fetchone()
    return bool(row and row["private"])


def get_prompt(conn: sqlite3.Connection, content_session_id: str, number: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM user_prompts WHERE content_session_id = ? AND prompt_number = ?",
                        (content_session_id, number)).fetchone()


# ------------------------------------------------------------------- queue

def enqueue(conn: sqlite3.Connection, content_session_id: str, message_type: str,
            payload: Dict[str, Any], prompt_number: Optional[int]) -> int:
    ts = now_ms()
    cur = conn.execute(
        """INSERT INTO pending_messages (content_session_id, message_type, payload, prompt_number,
                                         created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)""",
        (content_session_id, message_type, json.dumps(payload, ensure_ascii=False), prompt_number, ts, ts))
    return int(cur.lastrowid)


_READY_SQL = """SELECT content_session_id,
                        MAX(created_at) AS newest,
                        SUM(message_type = 'observation') AS events,
                        SUM(message_type = 'session_end') AS ends
                 FROM pending_messages WHERE status = 'pending' AND not_before <= ?
                 GROUP BY content_session_id ORDER BY MIN(id)"""


def _is_ready(row, now: int, quiet_ms: int, batch: int) -> bool:
    """Send a session's events once it went quiet, filled a batch, or ended."""
    return (now - int(row["newest"]) >= quiet_ms or int(row["events"] or 0) >= batch
            or int(row["ends"] or 0) > 0)


def sessions_with_pending(conn: sqlite3.Connection, quiet_ms: int = 0, batch: int = 1) -> List[str]:
    """Sessions whose queued events are ready for the observer.

    With quiet_ms > 0 a session waits until no event arrived for quiet_ms, unless
    `batch` observation events are already queued or the session has ended.
    """
    now = now_ms()
    rows = conn.execute(_READY_SQL, (now,)).fetchall()
    return [r["content_session_id"] for r in rows if _is_ready(r, now, quiet_ms, max(1, batch))]


def session_ready(conn: sqlite3.Connection, content_session_id: str, quiet_ms: int, batch: int) -> bool:
    return content_session_id in sessions_with_pending(conn, quiet_ms, batch)


def next_ready_in_ms(conn: sqlite3.Connection, quiet_ms: int) -> Optional[int]:
    """How long until the next waiting session goes quiet (None: nothing waits)."""
    now = now_ms()
    rows = conn.execute(_READY_SQL, (now,)).fetchall()
    if not rows:
        return None
    return max(0, min(int(r["newest"]) + quiet_ms - now for r in rows))


def claim_batch(conn: sqlite3.Connection, content_session_id: str, limit: int = 50) -> List[sqlite3.Row]:
    with _lock, transaction(conn):
        rows = conn.execute("""SELECT * FROM pending_messages WHERE content_session_id = ?
                               AND status = 'pending' AND not_before <= ? ORDER BY id LIMIT ?""",
                            (content_session_id, now_ms(), limit)).fetchall()
        if rows:
            conn.executemany("UPDATE pending_messages SET status = 'processing', updated_at = ? WHERE id = ?",
                             [(now_ms(), r["id"]) for r in rows])
    return rows


def finish(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """A processed queue row is done: its result is already in observations or summaries."""
    with transaction(conn):
        conn.executemany("DELETE FROM pending_messages WHERE id = ?", [(i,) for i in ids])


def retry_later(conn: sqlite3.Connection, ids: Iterable[int], error: str, max_attempts: int = 0) -> None:
    """Put rows back with a growing delay (1 minute doubling, at most 1 hour).

    A row is never given up on: memory is never dropped because the observer
    or the database failed for a while. `max_attempts` is kept for old callers.
    """
    with transaction(conn):
        for i in ids:
            row = conn.execute("SELECT attempts FROM pending_messages WHERE id = ?", (i,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            delay = min(60_000 * (2 ** min(attempts - 1, 10)), 3_600_000)
            conn.execute("""UPDATE pending_messages SET status = 'pending', attempts = ?, last_error = ?,
                            not_before = ?, updated_at = ? WHERE id = ?""",
                         (attempts, error[:2000], now_ms() + delay, now_ms(), i))


def reset_stale_processing(conn: sqlite3.Connection) -> int:
    """Items left 'processing' by a crashed worker, and items an older version
    gave up on ('failed'), go back to the queue."""
    cur = conn.execute("UPDATE pending_messages SET status = 'pending', not_before = 0 "
                       "WHERE status IN ('processing', 'failed')")
    return cur.rowcount


def queue_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) FROM pending_messages GROUP BY status").fetchall()
    return {r[0]: r[1] for r in rows}


# --------------------------------------------------------- spool bookkeeping

def spool_seen(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM spool_consumed WHERE key = ?", (key,)).fetchone() is not None


def mark_spool_consumed(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("INSERT OR IGNORE INTO spool_consumed(key, consumed_at) VALUES (?, ?)", (key, now_ms()))


def prune_spool_consumed(conn: sqlite3.Connection, older_than_days: int = 14) -> None:
    conn.execute("DELETE FROM spool_consumed WHERE consumed_at < ?",
                 (now_ms() - older_than_days * 86_400_000,))
