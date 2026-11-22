#!/usr/bin/env python3
"""
Memo Core — shared logic for the Memo sovereign memory vault.

Single source of truth for indexing, search, timeline parsing/insertion,
knowledge-graph access, path safety and mutation safety. Both the MCP server
(scripts/mcp_server.py) and the CLI (scripts/memory_engine.py) import from here
so that every fix lands exactly once.

100% offline, zero external dependencies (Python 3.9+ stdlib & SQLite FTS5).

Environment:
  MEMO_VAULT          Vault root (default: ${OPENMEMO_HOME:-$HOME/openmemo})
  MEMO_LOG_LEVEL      DEBUG|INFO|WARNING|ERROR (default: INFO)
  MEMO_GIT_SNAPSHOT   0 to disable automatic git snapshots (default: enabled)


IMPORTANT: nothing in this module may write to stdout. stdout is reserved for
the MCP JSON-RPC transport; all diagnostics go to stderr via `log`.
"""

from __future__ import annotations

import os
import re
import sys
import json
import errno
import shutil
import logging
import sqlite3
import difflib
import datetime
import posixpath
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

def env(name: str, default: str = "") -> str:
    """Read MEMO_<name>."""
    return os.environ.get(f"MEMO_{name}") or default


# Prefer the repository that contains this file (scripts/ lives inside the vault).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_OPENMEMO_HOME = Path(os.environ["OPENMEMO_HOME"]).expanduser() if os.environ.get("OPENMEMO_HOME") else None
_DEFAULT_VAULT = (
    _REPO_ROOT
    if (_REPO_ROOT / "timeline.md").exists() and (_REPO_ROOT / "scripts" / "memo_core.py").exists()
    else (_OPENMEMO_HOME if _OPENMEMO_HOME else Path.home() / "openmemo")
)

VAULT_DIR: Path = Path(env("VAULT") or _DEFAULT_VAULT).expanduser().resolve()
CACHE_DIR: Path = VAULT_DIR / ".cache"
DB_PATH: Path = CACHE_DIR / "memory.db"
LOCK_PATH: Path = CACHE_DIR / "memo.lock"
GRAPH_PATH: Path = VAULT_DIR / "graph" / "entities.json"
TIMELINE_PATH: Path = VAULT_DIR / "timeline.md"
SESSION_PATH: Path = VAULT_DIR / "sessions" / "current.md"

SKIP_DIRS = {".git", ".obsidian", ".cache", ".memo", ".gemini", ".claude", ".agents",
             "venv", ".venv", "node_modules", "__pycache__", ".trash",
             "_templates", "_attachments", "plugin", "tests", "deploy",
             "_raw-claude", "_raw-cursor", "_raw-antigravity"}

# Historical / one-shot meta: readable via dossier, excluded from FTS retrieval.
SKIP_INDEX_FILES = {
    "docs/_open-questions.md",
    "docs/_prompt-fill-gaps.md",
}

LIVING_HISTORY_HEADING = "## Living History"

log = logging.getLogger("memo")
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[memo] %(levelname)s %(message)s"))
    log.addHandler(_handler)
log.setLevel(env("LOG_LEVEL", "INFO").upper())
log.propagate = False


class MemoError(Exception):
    """Base class for all Memo errors that should reach the caller as text."""


class MemoPathError(MemoError):
    """Raised when a requested path escapes the vault."""


# ----------------------------------------------------------------------
# Path safety
# ----------------------------------------------------------------------

def safe_vault_path(rel: str, *, must_exist: bool = False) -> Path:
    """Resolve `rel` inside the vault, refusing anything that escapes it.

    Closes the path-traversal hole: `resolve()` collapses `..` and follows
    symlinks, then we assert containment before any I/O happens.
    """
    if not rel or not rel.strip():
        raise MemoPathError("Empty path is not a valid vault location.")

    cleaned = rel.strip().lstrip("/")
    candidate = (VAULT_DIR / cleaned).resolve()

    try:
        inside = candidate.is_relative_to(VAULT_DIR)
    except AttributeError:  # Python 3.8 fallback
        inside = str(candidate).startswith(str(VAULT_DIR) + os.sep) or candidate == VAULT_DIR

    if not inside:
        raise MemoPathError(
            f"Refused: '{rel}' resolves outside the Memo vault ({VAULT_DIR}). "
            "Only paths inside the vault may be accessed."
        )

    if must_exist and not candidate.exists():
        raise MemoPathError(f"File '{cleaned}' does not exist in the Memo vault.")

    return candidate


def rel_to_vault(p: Path) -> str:
    return p.resolve().relative_to(VAULT_DIR).as_posix()


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def on_disk_case(rel: str) -> str:
    """Spell a vault-relative path the way it is stored on disk.

    This Mac's filesystem is case-insensitive, so 'Clients/Acme-Corp.md' opens
    clients/acme-corp.md, but git pathspecs are case-sensitive and would miss it.
    Each existing segment takes its exact on-disk entry; a segment that does
    not exist yet keeps the spelling given. On a case-sensitive filesystem a
    mis-cased segment does not exist, so nothing is remapped there.
    """
    parts = [p for p in rel.replace(os.sep, "/").split("/") if p]
    current = VAULT_DIR
    out: List[str] = []
    for i, part in enumerate(parts):
        try:
            entries = os.listdir(current)
        except OSError:
            return "/".join(out + parts[i:])
        if part not in entries:
            if not os.path.lexists(current / part):
                return "/".join(out + parts[i:])
            key = _name_key(part)
            part = next((e for e in entries if _name_key(e) == key), part)
        out.append(part)
        current = current / part
    return "/".join(out)


def _vault_rel(p: Union[str, Path]) -> str:
    """Vault-relative POSIX spelling of `p` (absolute or relative), on-disk case."""
    path = Path(p)
    if path.is_absolute():
        try:
            path = path.relative_to(VAULT_DIR)
        except ValueError:
            path = path.resolve().relative_to(VAULT_DIR)
    return on_disk_case(path.as_posix())


# Names an agent loads as instructions. On a case-insensitive filesystem
# stack/claude.md is also stack/CLAUDE.md, so a note may never use them.
INSTRUCTION_FILE_NAMES = {"claude.md", "gemini.md", "agents.md"}


def normalize_note_path(path: str) -> str:
    """Validate a path for a NEW note and return its vault-relative spelling.

    Refuses an empty or absolute path, any '..' segment (checked on the raw
    path and again after posixpath.normpath), any dot segment ('.', or a hidden
    name such as '.obsidian'), and an agent instruction-file name in any case.
    Adds '.md' when missing and returns the on-disk case of existing folders.
    """
    raw = (path or "").strip()
    if not raw:
        raise MemoPathError("Empty path is not a valid vault location.")
    if posixpath.isabs(raw):
        raise MemoPathError(
            f"Refused: '{raw}' is an absolute path. Give a vault-relative path "
            "such as 'clients/globex.md'."
        )
    norm = posixpath.normpath(raw)
    segments = [s for s in raw.split("/") if s] + norm.split("/")
    if ".." in segments:
        raise MemoPathError(
            f"Refused: '{raw}' contains a '..' segment. Give a plain vault-relative "
            "path such as 'clients/globex.md'."
        )
    if any(s.startswith(".") for s in segments):
        raise MemoPathError(
            f"Refused: '{raw}' contains a dot segment ('.' or a hidden name such as "
            "'.obsidian'). Notes live in plain, visible folders."
        )
    if not norm.endswith(".md"):
        norm += ".md"
    base = posixpath.basename(norm).casefold()
    if base in INSTRUCTION_FILE_NAMES or base[:-3] in INSTRUCTION_FILE_NAMES:
        raise MemoPathError(
            f"Refused: '{norm}' uses an agent instruction-file name (CLAUDE.md, "
            "GEMINI.md, AGENTS.md). On a case-insensitive filesystem it would load "
            "as agent instructions. Use a vendor-prefixed name such as "
            "'stack/anthropic-claude.md' or 'stack/google-gemini.md'."
        )
    return on_disk_case(norm)


def iter_markdown_files() -> List[Path]:
    """Every indexable markdown file in the vault, skipping machine dirs."""
    found: List[Path] = []
    for root, dirs, files in os.walk(VAULT_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".md"):
                found.append(Path(root) / f)
    return found


# ----------------------------------------------------------------------
# Mutation safety: locking, atomic writes, git snapshots
# ----------------------------------------------------------------------

@contextmanager
def vault_lock(timeout: float = 10.0):
    """Advisory lock so two concurrent agents cannot interleave writes."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import time
    import oslock
    fh = open(LOCK_PATH, "w")
    deadline = time.time() + timeout
    try:
        while not oslock.try_lock(fh):
            if time.time() > deadline:
                raise MemoError("Another Memo process is holding the vault lock.")
            time.sleep(0.05)
        yield
    finally:
        try:
            oslock.unlock(fh)
        finally:
            fh.close()


def atomic_write(path: Path, text: str) -> None:
    """Write via temp file + os.replace so a crash can never truncate a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".memo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


GIT_TIMEOUT = 120  # seconds; a hung hook must not hang a memory write forever


def _git(*args: str, check: bool = False, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    # stdin=DEVNULL: a hook must never read the MCP JSON-RPC stream.
    # GIT_LITERAL_PATHSPECS: a '*' or '[' in a note name matches only that file.
    git_env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    if env:
        git_env.update(env)
    return subprocess.run(
        ["git", *args], cwd=str(VAULT_DIR),
        capture_output=True, text=True, check=check,
        stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT,
        env=git_env,
    )


def git_enabled() -> bool:
    if env("GIT_SNAPSHOT", "1") == "0":
        return False
    if not (VAULT_DIR / ".git").exists():
        return False
    return shutil.which("git") is not None


def ensure_git_config() -> None:
    """Register Memo merge drivers in the local git config (idempotent)."""
    if not (VAULT_DIR / ".git").exists():
        return
    driver = "python3 scripts/memo_merge.py {mode} %O %A %B"
    for mode, name in (("timeline", "memo-timeline"), ("note", "memo-note")):
        key = f"merge.{name}.driver"
        want = driver.format(mode=mode)
        cur = _git("config", "--local", "--get", key)
        if cur.returncode == 0 and (cur.stdout or "").strip() == want:
            continue
        _git("config", "--local", key, want)


def _git_output(res: subprocess.CompletedProcess) -> str:
    return " ".join(((res.stderr or "") + " " + (res.stdout or "")).split()) or "(no output)"


def _git_commit_paths(subject: str, paths: Sequence[Union[str, Path]]) -> Optional[str]:
    """Commit only `paths`, and only those git reports changed.

    Never `git add -A`: an unrelated dirty or staged file in the tree stays out
    of the commit. Hooks run (no --no-verify). Never fatal: on any failure the
    write stands uncommitted and a warning names the step, exit code and paths.
    """
    if not git_enabled():
        return None
    rels: List[str] = []
    try:
        rels = list(dict.fromkeys(_vault_rel(p) for p in paths))
        dirty = []
        for rel in rels:
            st = _git("status", "--porcelain", "--untracked-files=all", "--", rel)
            if st.returncode != 0:
                log.warning("git status failed (exit %d) for [%s]: %s",
                            st.returncode, rel, _git_output(st))
                return None
            if st.stdout.strip():
                dirty.append(rel)
            elif (VAULT_DIR / rel).exists() and _git(
                    "ls-files", "--error-unmatch", "--", rel).returncode != 0:
                log.warning("git sees no change in [%s] although it exists and is "
                            "untracked; is it ignored? Not committed.", rel)
        if not dirty:
            return None
        listed = ", ".join(dirty)
        res = _git("add", "--", *dirty)
        if res.returncode != 0:
            log.warning("git add failed (exit %d) for [%s]: %s. The write stands "
                        "uncommitted; a stale .git/index.lock may be the cause.",
                        res.returncode, listed, _git_output(res))
            return None
        res = _git("commit", "-m", subject, "--", *dirty)
        if res.returncode != 0:
            log.warning("git commit failed (exit %d) for [%s]: %s. The write stands "
                        "uncommitted; a pre-commit or commit-msg hook may have "
                        "rejected it, and these paths are left staged.",
                        res.returncode, listed, _git_output(res))
            return None
        log.info("git commit: %s [%s]", subject, listed)
        return subject
    except subprocess.TimeoutExpired as exc:
        step = exc.cmd[1] if isinstance(exc.cmd, list) and len(exc.cmd) > 1 else "?"
        log.warning("git %s timed out after %ss for [%s]; a hook may be hanging. "
                    "The write stands uncommitted.", step, GIT_TIMEOUT, ", ".join(rels))
        return None
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("git commit skipped for [%s]: %s",
                    ", ".join(rels) or ", ".join(map(str, paths)), exc)
        return None


def git_snapshot(reason: str, paths: Sequence[Union[str, Path]]) -> Optional[str]:
    """Before a mutation, commit the files it will touch if they hold
    uncommitted edits, so the pre-mutation state is recoverable.

    Path-scoped: only `paths` are staged. Returns the commit subject if a
    snapshot was taken, else None. Never fatal — a failure here must not block
    a memory write. Call it only through `write_mutation`, after every refusal
    and no-op check: a snapshot taken by a call that then refuses would commit
    the user's hand edit for nothing.
    """
    return _git_commit_paths(f"memo: pre-mutation snapshot ({reason})", paths)


def write_mutation(path: Path, text: str, snapshot: Optional[str] = None) -> None:
    """The one place a write tool reaches disk.

    Every refusal and no-op returns before this, so a refused, duplicate or
    unchanged call never makes a git commit. With `snapshot` (the tool name),
    a dirty target is snapshotted here, immediately before the write.
    """
    if snapshot:
        git_snapshot(snapshot, [path])
    atomic_write(path, text)


def git_commit_change(subject: str, paths: Sequence[Union[str, Path]]) -> Optional[str]:
    """Commit the mutation itself, for exactly the paths it wrote."""
    return _git_commit_paths(" ".join(subject.split()), paths)


# ----------------------------------------------------------------------
# SQLite FTS5 index
# ----------------------------------------------------------------------

def check_fts5() -> None:
    """Fail loudly on stderr if this Python's SQLite lacks FTS5."""
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        probe.close()
    except sqlite3.OperationalError as exc:
        raise MemoError(
            "This Python build's SQLite has no FTS5 support, which Memo requires. "
            f"(sqlite {sqlite3.sqlite_version}): {exc}"
        )


def has_trigram() -> bool:
    """Trigram tokenizer needs SQLite >= 3.34; used for Arabic substring recall."""
    try:
        probe = sqlite3.connect(":memory:")
        probe.execute("CREATE VIRTUAL TABLE t USING fts5(a, tokenize='trigram')")
        probe.close()
        return True
    except sqlite3.OperationalError:
        return False


_TRIGRAM = None


def get_db() -> sqlite3.Connection:
    """Open the index, creating schema on first use."""
    global _TRIGRAM
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            path, title, content,
            tokenize='porter unicode61'
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS docs_meta (
            path TEXT PRIMARY KEY,
            mtime REAL,
            title TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS docs_props (
            path TEXT PRIMARY KEY,
            type TEXT,
            status TEXT,
            entity_id TEXT,
            tags TEXT
        );
    """)
    if _TRIGRAM is None:
        _TRIGRAM = has_trigram()
    if _TRIGRAM:
        # Substring-capable mirror. porter/unicode61 only matches whole Arabic
        # words, so `مكتبة` would never match `المكتبة` without this.
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS docs_trigram USING fts5(
                path, title, content,
                tokenize='trigram'
            );
        """)
    return conn


def parse_frontmatter_props(content: str) -> Dict[str, Any]:
    """Extract type/status/entity_id/tags from YAML frontmatter if present."""
    if not content.startswith("---\n"):
        return {}
    end = content.find("\n---\n", 4)
    if end == -1:
        return {}
    block = content[4:end]
    props: Dict[str, Any] = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if ":" not in raw or raw.lstrip().startswith("#") or raw.lstrip().startswith("- "):
            continue
        key, val = raw.split(":", 1)
        key, val = key.strip(), val.strip()
        if key not in ("type", "status", "entity_id", "tags", "title", "index"):
            continue
        if key == "tags":
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                props["tags"] = [] if not inner else [
                    p.strip().strip("'\"") for p in re.split(r",\s*", inner) if p.strip()
                ]
            elif not val:
                items: List[str] = []
                while i < len(lines):
                    nxt = lines[i]
                    m = re.match(r"^\s+-\s+(.*)$", nxt)
                    if m:
                        items.append(m.group(1).strip().strip("'\""))
                        i += 1
                        continue
                    if not nxt.strip():
                        i += 1
                        continue
                    break
                props["tags"] = items
            else:
                props["tags"] = []
        elif key == "index":
            low = val.lower()
            if low in ("false", "no", "0", "off"):
                props["index"] = False
            elif low in ("true", "yes", "1", "on"):
                props["index"] = True
        elif len(val) >= 2 and val.startswith('"') and val.endswith('"'):
            # create_note writes titles as JSON strings (valid YAML double-quoted
            # scalars), so '"' and '\' arrive escaped; decode them.
            try:
                decoded = json.loads(val)
            except ValueError:
                decoded = None
            props[key] = decoded if isinstance(decoded, str) else val[1:-1]
        elif len(val) >= 2 and val.startswith("'") and val.endswith("'"):
            props[key] = val[1:-1]
        else:
            props[key] = val
    return props


def should_index(rel_path: str, props: Optional[Dict[str, Any]] = None) -> bool:
    """Whether a markdown file belongs in FTS / docs_props."""
    if rel_path in SKIP_INDEX_FILES:
        return False
    if props and props.get("index") is False:
        return False
    return True


def sync_index(conn: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
    """Incrementally reindex changed files and purge rows for deleted ones."""
    own = conn is None
    conn = conn or get_db()
    added = updated = purged = 0

    on_disk: Dict[str, Path] = {}
    for p in iter_markdown_files():
        try:
            on_disk[rel_to_vault(p)] = p
        except ValueError:
            continue

    try:
        with conn:
            for rel_path, p in on_disk.items():
                if rel_path in SKIP_INDEX_FILES:
                    row = conn.execute(
                        "SELECT mtime FROM docs_meta WHERE path = ?", (rel_path,)
                    ).fetchone()
                    if row is not None:
                        _delete_indexed(conn, rel_path)
                        purged += 1
                        log.info("excluded from index: %s", rel_path)
                    continue

                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue

                row = conn.execute(
                    "SELECT mtime FROM docs_meta WHERE path = ?", (rel_path,)
                ).fetchone()
                if row is not None and row[0] >= mtime:
                    continue

                content = p.read_text(encoding="utf-8", errors="replace")
                props = parse_frontmatter_props(content)
                if not should_index(rel_path, props):
                    if row is not None:
                        _delete_indexed(conn, rel_path)
                        purged += 1
                        log.info("excluded from index: %s", rel_path)
                    continue

                m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                title = props.get("title") or (m.group(1).strip() if m else p.stem)

                _delete_indexed(conn, rel_path)
                conn.execute(
                    "INSERT INTO docs_fts(path, title, content) VALUES (?, ?, ?)",
                    (rel_path, title, content),
                )
                if _TRIGRAM:
                    conn.execute(
                        "INSERT INTO docs_trigram(path, title, content) VALUES (?, ?, ?)",
                        (rel_path, title, content),
                    )
                conn.execute(
                    "INSERT INTO docs_meta(path, mtime, title) VALUES (?, ?, ?)",
                    (rel_path, mtime, title),
                )
                tags = props.get("tags") or []
                conn.execute(
                    "INSERT INTO docs_props(path, type, status, entity_id, tags) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        rel_path,
                        props.get("type"),
                        props.get("status"),
                        props.get("entity_id"),
                        json.dumps(tags, ensure_ascii=False) if tags else "[]",
                    ),
                )
                if row is None:
                    added += 1
                else:
                    updated += 1

            # Purge: rows whose file no longer exists on disk.
            known = {r[0] for r in conn.execute("SELECT path FROM docs_meta")}
            for stale in known - set(on_disk):
                _delete_indexed(conn, stale)
                purged += 1
                log.info("purged deleted file from index: %s", stale)
    finally:
        if own:
            conn.close()

    return {"added": added, "updated": updated, "purged": purged, "total": len(on_disk)}


def _delete_indexed(conn: sqlite3.Connection, rel_path: str) -> None:
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM docs_meta WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM docs_props WHERE path = ?", (rel_path,))
    if _TRIGRAM:
        conn.execute("DELETE FROM docs_trigram WHERE path = ?", (rel_path,))


# ----------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------

_TERM_RE = re.compile(r"\w+", re.UNICODE)


def _fts_literal(term: str) -> str:
    """Quote a term as an FTS5 string literal (neutralises -, :, *, (, NEAR...)."""
    return '"' + term.replace('"', '""') + '"'


def build_fts_queries(raw: str) -> List[Tuple[str, str, str]]:
    """Ordered (strategy, table, fts_query) ladder, tried until one matches.

    The old implementation wrapped the whole query in quotes, making every
    search a literal phrase search — so a natural two-word question returned
    nothing unless those words happened to be adjacent.
    """
    q = raw.strip()
    if not q:
        return []

    # Strategy 0: user asked for an exact phrase explicitly. Honour it alone.
    if len(q) >= 2 and q.startswith('"') and q.endswith('"'):
        inner = q[1:-1]
        return [("phrase", "docs_fts", _fts_literal(inner))]

    terms = _TERM_RE.findall(q)
    if not terms:
        return [("phrase", "docs_fts", _fts_literal(q))]

    quoted = [_fts_literal(t) for t in terms]

    ladder: List[Tuple[str, str, str]] = []
    if len(terms) == 1:
        ladder.append(("exact", "docs_fts", quoted[0]))
        ladder.append(("prefix", "docs_fts", quoted[0] + "*"))
    else:
        ladder.append(("phrase", "docs_fts", _fts_literal(" ".join(terms))))
        ladder.append(("all-terms", "docs_fts", " AND ".join(quoted)))
        ladder.append(("any-term", "docs_fts", " OR ".join(quoted)))
        ladder.append(("prefix", "docs_fts", " OR ".join(t + "*" for t in quoted)))

    if _TRIGRAM:
        # Trigram needs >= 3 chars per token to be useful.
        tri = [t for t in terms if len(t) >= 3]
        if tri:
            ladder.append(("substring", "docs_trigram",
                           " OR ".join(_fts_literal(t) for t in tri)))

    # Graph-alias expansion: expand matching entity aliases/bilingual names
    try:
        if GRAPH_PATH.exists():
            gdata = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
            q_low = q.lower()
            t_lows = {t.lower() for t in terms if len(t) >= 2}
            extra_terms: List[str] = []
            for ent in gdata.get("entities") or []:
                cands = [str(ent.get(k) or "").strip() for k in ("id", "name", "name_ar")]
                cands += [str(a).strip() for a in (ent.get("aliases") or []) if a]
                cands = [c for c in cands if c]
                if any(c.lower() in t_lows or (len(c) >= 3 and c.lower() in q_low) for c in cands):
                    for c in cands:
                        for st in _TERM_RE.findall(c):
                            if st.lower() not in t_lows and st not in extra_terms:
                                extra_terms.append(st)
            if extra_terms:
                ladder.append(("graph-alias", "docs_fts",
                               " OR ".join(_fts_literal(t) for t in (terms + extra_terms)[:16])))
    except Exception:
        pass
    return ladder


SEARCH_SQL = """
    SELECT path, title, snippet({table}, 2, '>>>', '<<<', '...', 15) AS snip
    FROM {table}
    WHERE {table} MATCH ?
    ORDER BY rank
    LIMIT ?
"""


def search(
    query: str,
    limit: int = 5,
    *,
    type: Optional[str] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
) -> Tuple[Optional[str], List[Dict[str, str]]]:
    """Run the strategy ladder; return (winning_strategy, results).

    Optional filters (`type`, `status`, `tag`) constrain results via docs_props
    so agents can ask structured questions like "active Salla clients".
    """
    conn = get_db()
    try:
        sync_index(conn)

        allowed: Optional[set] = None
        if type or status or tag:
            sql = "SELECT path, type, status, tags FROM docs_props WHERE 1=1"
            params: List[Any] = []
            if type:
                sql += " AND type = ?"
                params.append(type)
            if status:
                sql += " AND status = ?"
                params.append(status)
            rows = conn.execute(sql, params).fetchall()
            allowed = set()
            for path, _t, _s, tags_json in rows:
                if tag:
                    try:
                        tags = json.loads(tags_json or "[]")
                    except json.JSONDecodeError:
                        tags = []
                    if tag not in tags:
                        continue
                allowed.add(path)
            if not allowed:
                return None, []

        for strategy, table, fts_query in build_fts_queries(query):
            if table == "docs_trigram" and not _TRIGRAM:
                continue
            try:
                rows = conn.execute(
                    SEARCH_SQL.format(table=table),
                    (fts_query, max(limit * 5, limit)),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                log.debug("strategy %s rejected by fts5 (%s)", strategy, exc)
                continue
            if rows:
                results = []
                for r in rows:
                    if allowed is not None and r[0] not in allowed:
                        continue
                    results.append({
                        "path": r[0],
                        "title": r[1],
                        "snippet": r[2].replace("\n", " ").strip(),
                        "wikilink": r[0][:-3] if r[0].endswith(".md") else r[0],
                    })
                    if len(results) >= limit:
                        break
                if results:
                    log.debug("search '%s' matched via strategy=%s", query, strategy)
                    return strategy, results
        return None, []
    finally:
        conn.close()


def query_props(
    *,
    type: Optional[str] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List vault notes by frontmatter properties without a text query.

    Covers agent questions like "all active Salla clients" via
    type=client + status=active + tag=tech/salla.
    """
    limit = max(1, min(int(limit), 200))
    conn = get_db()
    try:
        sync_index(conn)
        sql = (
            "SELECT p.path, COALESCE(m.title, p.path) AS title, "
            "p.type, p.status, p.entity_id, p.tags "
            "FROM docs_props p "
            "LEFT JOIN docs_meta m ON m.path = p.path "
            "WHERE 1=1"
        )
        params: List[Any] = []
        if type:
            sql += " AND p.type = ?"
            params.append(type)
        if status:
            sql += " AND p.status = ?"
            params.append(status)
        sql += " ORDER BY p.path"
        rows = conn.execute(sql, params).fetchall()
        results: List[Dict[str, Any]] = []
        for path, title, note_type, note_status, entity_id, tags_json in rows:
            try:
                tags = json.loads(tags_json or "[]")
            except json.JSONDecodeError:
                tags = []
            if tag and tag not in tags:
                continue
            results.append({
                "path": path,
                "title": title,
                "type": note_type,
                "status": note_status,
                "entity_id": entity_id,
                "tags": tags,
                "wikilink": path[:-3] if path.endswith(".md") else path,
            })
            if len(results) >= limit:
                break
        return results
    finally:
        conn.close()


def vault_lint() -> Dict[str, Any]:
    """Run the vault integrity linter and return its JSON report."""
    import importlib.util
    lint_path = Path(__file__).resolve().parent / "vault_lint.py"
    spec = importlib.util.spec_from_file_location("memo_vault_lint", lint_path)
    if spec is None or spec.loader is None:
        raise MemoError("Could not load scripts/vault_lint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The linter defaults to its own parent folder; lint the vault in use
    # (MEMO_VAULT), or a throwaway test vault is never actually checked.
    mod.VAULT, mod.GRAPH = VAULT_DIR, GRAPH_PATH
    return mod.lint()


# ----------------------------------------------------------------------
# Timeline: block-aware reader
# ----------------------------------------------------------------------

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
MONTH_INDEX = {name.lower(): i for i, name in enumerate(MONTH_NAMES) if name}

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
BLOCK_HEADER_RE = re.compile(r"^-\s+\*\*(\d{4}-\d{2}-\d{2})[^*]*\*\*\s*$")
CHILD_RE = re.compile(r"^\s+-\s+(?:\*\*(?P<cat>[^*]+?):\*\*)?\s*(?P<body>.*\S)\s*$")
TABLE_ROW_RE = re.compile(r"^\|\s*\*{0,2}(\d{4}-\d{2}-\d{2})\*{0,2}\s*\|(?P<rest>.*)\|\s*$")
MONTH_HEADING_RE = re.compile(r"^###\s+([A-Za-z]+)\s+(\d{4})\s*$")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _strip_md(text: str) -> str:
    return text.replace("**", "").strip(" *→·|")


def parse_timeline() -> List[Dict[str, Any]]:
    """Parse timeline.md into structured events.

    The vault's documented format (see `## Log Ingestion Template`) is a
    two-line block:

        - **YYYY-MM-DD**
          - **Category:** Summary → [[link]]

    A line-by-line filter therefore captured only the empty date line and
    dropped every summary. This reader tracks the enclosing block instead, and
    also understands the `Active & Upcoming` markdown table.
    """
    if not TIMELINE_PATH.exists():
        return []

    events: List[Dict[str, Any]] = []
    current_date: Optional[str] = None
    in_fence = False

    for raw_line in TIMELINE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()

        # Never parse inside fenced code: the ingestion template lives there.
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        header = BLOCK_HEADER_RE.match(raw_line)
        if header:
            current_date = header.group(1)
            continue

        table = TABLE_ROW_RE.match(raw_line)
        if table:
            cells = [_strip_md(c) for c in table.group("rest").split("|")]
            cells = [c for c in cells if c]
            link = WIKILINK_RE.findall(raw_line)
            events.append({
                "date": table.group(1),
                "category": cells[0] if cells else "",
                "summary": " — ".join(cells[1:]) if len(cells) > 1 else "",
                "link": link[0] if link else "",
                "section": "upcoming",
            })
            current_date = None
            continue

        if current_date and raw_line.startswith((" ", "\t")):
            child = CHILD_RE.match(raw_line)
            if child:
                body = child.group("body").strip()
                link = WIKILINK_RE.findall(body)
                summary = WIKILINK_RE.sub("", body).strip(" →·")
                events.append({
                    "date": current_date,
                    "category": _strip_md(child.group("cat") or ""),
                    "summary": summary,
                    "link": link[0] if link else "",
                    "section": "history",
                })
            continue

        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("---"):
            current_date = None

    events.sort(key=lambda e: e["date"], reverse=True)
    return events


def timeline_contains(summary: str, date: Optional[str] = None) -> bool:
    """Dedup helper: has this exact summary already been recorded?"""
    needle = " ".join(summary.lower().split())
    if not needle:
        return False
    for ev in parse_timeline():
        if date and ev["date"] != date:
            continue
        if " ".join(ev["summary"].lower().split()) == needle:
            return True
    return False


# ----------------------------------------------------------------------
# Near-duplicate detection
# ----------------------------------------------------------------------

# A rephrased fact is still a duplicate. The exact-match guard only catches
# byte-identical repeats, which is the rarest case in practice.
NEAR_DUP_JACCARD = 0.6
NEAR_DUP_RATIO = 0.8

_DEDUP_NOISE = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "for", "on", "in",
    "at", "and", "or", "with", "now", "new", "from", "that", "this", "it", "as",
    "by", "be", "has", "have", "had", "per", "via",
}


def _dedup_tokens(text: str) -> set:
    """Normalise a fact into a comparable token set (Arabic-friendly)."""
    cleaned = WIKILINK_RE.sub(" ", text or "").lower()
    cleaned = cleaned.replace("**", " ").replace("_", " ")
    cleaned = re.sub(r"[\u0640\u064b-\u0652]", "", cleaned)  # Arabic tatweel/diacritics
    tokens = {t for t in _TERM_RE.findall(cleaned) if t not in _DEDUP_NOISE}
    return {t for t in tokens if len(t) > 1}


def _similarity(a: str, b: str) -> float:
    """Blend token-set overlap with sequence similarity; return the stronger signal."""
    ta, tb = _dedup_tokens(a), _dedup_tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    ratio = difflib.SequenceMatcher(
        None, " ".join(sorted(ta)), " ".join(sorted(tb))
    ).ratio()
    return max(jaccard, ratio if ratio >= NEAR_DUP_RATIO else 0.0)


def near_duplicates(summary: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Timeline events that say substantially the same thing as `summary`.

    Catches the rephrased duplicate ("Acme Corp base 1,500 USD" vs "عقد شركة
    تجريبية صار 1500") that an exact-string guard lets straight through.
    """
    if not summary.strip():
        return []
    scored: List[Dict[str, Any]] = []
    for ev in parse_timeline():
        score = _similarity(summary, ev["summary"])
        if score >= NEAR_DUP_JACCARD:
            scored.append({
                "date": ev["date"],
                "category": ev["category"],
                "summary": ev["summary"],
                "similarity": round(score, 2),
            })
    scored.sort(key=lambda e: e["similarity"], reverse=True)
    return scored[:limit]


# ----------------------------------------------------------------------
# Timeline: section-aware writer
# ----------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # ASCII digits only


def parse_iso_date(date_str: str) -> datetime.date:
    """Strict YYYY-MM-DD. fromisoformat alone also takes '20260905' and
    '2026-W36-6' on newer Pythons, and \\d would take Arabic-Indic digits."""
    if not isinstance(date_str, str) or not _ISO_DATE_RE.fullmatch(date_str):
        raise MemoError(f"Invalid date '{date_str}'. Expected YYYY-MM-DD.")
    try:
        return datetime.date.fromisoformat(date_str)
    except ValueError:
        raise MemoError(f"Invalid date '{date_str}'. Expected YYYY-MM-DD.")


def _one_line(text: Optional[str]) -> str:
    """Collapse CR/LF and whitespace runs, so one call can never write a second,
    forged timeline or session line."""
    return " ".join((text or "").split())


def _child_line(category: str, summary: str, link: Optional[str]) -> str:
    tail = f" → [[{link.strip()}]]" if link and link.strip() else ""
    return f"  - **{category.strip()}:** {summary.strip()}{tail}"


def _month_key(year: int, month: int) -> Tuple[int, int]:
    return (year, month)


def insert_timeline_event(date_str: str, category: str, summary: str,
                          link: Optional[str] = None, *,
                          snapshot: Optional[str] = None) -> Dict[str, Any]:
    """Insert an event into the correct month block of `## Living History`.

    The previous implementation used `open(path, "a")`, which appended past the
    `## Log Ingestion Template` fence at the end of the file — outside the
    Living History section entirely. `snapshot` is passed to write_mutation.
    """
    if not TIMELINE_PATH.exists():
        raise MemoError("timeline.md not found in vault.")
    date = parse_iso_date(date_str)

    lines = TIMELINE_PATH.read_text(encoding="utf-8").splitlines()

    living_start = next((i for i, l in enumerate(lines)
                         if l.startswith(LIVING_HISTORY_HEADING)), None)
    if living_start is None:
        raise MemoError(
            f"timeline.md has no '{LIVING_HISTORY_HEADING}' section to write into."
        )

    living_end = next((i for i in range(living_start + 1, len(lines))
                       if lines[i].startswith("## ")), len(lines))
    while living_end - 1 > living_start and lines[living_end - 1].strip() in ("", "---"):
        living_end -= 1

    target_label = f"{MONTH_NAMES[date.month]} {date.year}"
    new_child = _child_line(category, summary, link)
    new_block = [f"- **{date_str}**", new_child]

    headings: List[Tuple[int, Optional[Tuple[int, int]]]] = []
    for i in range(living_start + 1, living_end):
        if lines[i].startswith("### "):
            m = MONTH_HEADING_RE.match(lines[i])
            key = None
            if m and m.group(1).lower() in MONTH_INDEX:
                key = _month_key(int(m.group(2)), MONTH_INDEX[m.group(1).lower()])
            headings.append((i, key))

    sec_start = next(
        (i for i, _ in headings if lines[i][4:].strip() == target_label), None
    )

    if sec_start is None:
        target_key = _month_key(date.year, date.month)
        insert_at = living_end
        for idx, key in headings:
            if key is not None and key < target_key:
                insert_at = idx
                break
        payload = [f"### {target_label}", *new_block, ""]
        lines[insert_at:insert_at] = payload
        placement = f"new section '### {target_label}'"
    else:
        sec_end = next((i for i, _ in headings if i > sec_start), living_end)

        block_at = None
        for i in range(sec_start + 1, sec_end):
            m = BLOCK_HEADER_RE.match(lines[i])
            if m and m.group(1) == date_str:
                block_at = i
                break

        if block_at is not None:
            insert_at = block_at + 1
            while insert_at < sec_end and lines[insert_at].startswith((" ", "\t")):
                insert_at += 1
            lines[insert_at:insert_at] = [new_child]
            placement = f"existing block '- **{date_str}**'"
        else:
            insert_at = sec_end
            for i in range(sec_start + 1, sec_end):
                m = BLOCK_HEADER_RE.match(lines[i])
                if m and m.group(1) < date_str:
                    insert_at = i
                    break
            while insert_at > sec_start + 1 and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            lines[insert_at:insert_at] = new_block
            placement = f"new block under '### {target_label}'"

    write_mutation(TIMELINE_PATH, "\n".join(lines) + "\n", snapshot)
    return {
        "file": "timeline.md",
        "date": date_str,
        "category": category.strip(),
        "summary": summary.strip(),
        "link": (link or "").strip(),
        "placement": placement,
        "entry": new_child.strip(),
    }


# ----------------------------------------------------------------------
# Session scratchpad
# ----------------------------------------------------------------------

def append_session_note(category: str, summary: str, date_str: Optional[str] = None,
                        link: Optional[str] = None, *,
                        snapshot: Optional[str] = None) -> Dict[str, Any]:
    """Append a candidate fact to the active session scratchpad.

    A duplicate returns before write_mutation, so it never snapshots."""
    date_str = date_str or datetime.date.today().isoformat()
    tail = f" → [[{link.strip()}]]" if link and link.strip() else ""
    entry = f"- [{date_str}] **{category.strip()}:** {summary.strip()}{tail}"

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = SESSION_PATH.read_text(encoding="utf-8") if SESSION_PATH.exists() else ""
    if entry.strip() in existing:
        return {"file": "sessions/current.md", "entry": entry.strip(), "duplicate": True}

    if existing and not existing.endswith("\n"):
        existing += "\n"
    write_mutation(SESSION_PATH, existing + entry + "\n", snapshot)
    return {"file": "sessions/current.md", "entry": entry.strip(), "duplicate": False}


def append_fact(target: str, category: str, summary: str, *,
                date_str: Optional[str] = None, link: Optional[str] = None,
                force: bool = False, reason: str = "memo_append_fact") -> Dict[str, Any]:
    """The one write gateway for timeline and session facts (MCP and CLI).

    Collapses CR/LF so one call writes one event, validates a strict
    YYYY-MM-DD date, and, under the vault lock, runs the exact-duplicate and
    near-duplicate gates (timeline; `force` skips the near one), then a
    path-scoped snapshot immediately before the write (write_mutation), a
    path-scoped commit and the reindex. A refused, duplicate or held-back call
    makes no git commit.

    Returns a payload with `target`, `duplicate`, `near_duplicate`; a held-back
    write also carries `candidates`, a written one `file`, `entry`, `placement`.
    """
    target = (target or "").strip()
    if target not in ("timeline", "session"):
        raise MemoError(f"Invalid target '{target}'. Must be 'timeline' or 'session'.")
    category, summary = _one_line(category), _one_line(summary)
    link = _one_line(link) or None
    if not category or not summary:
        raise MemoError("Both 'category' and 'summary' are required.")
    date_str = (date_str or "").strip() or datetime.date.today().isoformat()
    parse_iso_date(date_str)

    with vault_lock():
        if target == "timeline":
            if timeline_contains(summary):
                return {"target": target, "duplicate": True, "near_duplicate": False,
                        "summary": summary}
            near = [] if force else near_duplicates(summary)
            if near:
                return {"target": target, "duplicate": False, "near_duplicate": True,
                        "summary": summary, "candidates": near}
            result = insert_timeline_event(date_str, category, summary, link,
                                           snapshot=reason)
            git_commit_change(f"memo: timeline {date_str} — {category}: {summary[:60]}",
                              [TIMELINE_PATH])
        else:
            result = append_session_note(category, summary, date_str, link,
                                         snapshot=reason)
            if not result["duplicate"]:
                git_commit_change(f"memo: session note — {category}: {summary[:60]}",
                                  [SESSION_PATH])
        sync_index()

    result.setdefault("duplicate", False)
    result.setdefault("near_duplicate", False)
    result["target"] = target
    return result


# ----------------------------------------------------------------------
# Dossiers & vault listing
# ----------------------------------------------------------------------

def resolve_dossier(path: str) -> str:
    """Vault-relative path of an existing markdown file, in its on-disk case."""
    cleaned = path.strip().lstrip("/")
    if not cleaned.endswith(".md"):
        cleaned += ".md"
    target = safe_vault_path(cleaned, must_exist=True)
    return on_disk_case(rel_to_vault(target))


def read_dossier(path: str) -> Tuple[str, str]:
    """Return (relative_path, content) for a vault markdown file."""
    rel = resolve_dossier(path)
    return rel, (VAULT_DIR / rel).read_text(encoding="utf-8", errors="replace")


_UPDATED_LINE_RE = re.compile(r"^updated:[ \t]*(?P<val>.*?)[ \t]*$", re.MULTILINE)


def touch_updated(content: str) -> str:
    """Set an existing top-level `updated:` frontmatter value to today.

    Keeps the quote style and any trailing `# comment`, never adds the key,
    and leaves every other byte alone. An empty frontmatter ('---\\n---\\n') has
    no key to bump; the body below it (where a later '---' rule may sit) is
    never searched.
    """
    if not content.startswith("---\n") or content.startswith("---\n---\n"):
        return content
    end = content.find("\n---\n", 3)
    if end == -1:
        return content
    block = content[4:end]
    m = _UPDATED_LINE_RE.search(block)
    if m is None:
        return content
    val = m.group("val")
    quote, tail = "", ""
    if val[:1] in ("'", '"') and val.find(val[0], 1) != -1:
        quote = val[0]
        tail = val[val.find(quote, 1) + 1:]
    elif val.startswith("#"):
        tail = " " + val
    else:
        c = re.search(r"\s#", val)
        tail = val[c.start():] if c else ""
    today = datetime.date.today().isoformat()
    line = f"updated: {quote}{today}{quote}{tail}"
    block = block[:m.start()] + line + block[m.end():]
    return content[:4] + block + content[end:]


def list_files(subdir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Enumerate vault markdown files so agents stop guessing paths."""
    root = safe_vault_path(subdir, must_exist=True) if subdir else VAULT_DIR
    if root.is_file():
        raise MemoPathError(f"'{subdir}' is a file, not a directory.")

    out: List[Dict[str, Any]] = []
    for p in sorted(iter_markdown_files()):
        try:
            rp = rel_to_vault(p)
        except ValueError:
            continue
        if root != VAULT_DIR and not str(p.resolve()).startswith(str(root) + os.sep):
            continue
        content = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        out.append({
            "path": rp,
            "title": m.group(1).strip() if m else p.stem,
            "bytes": p.stat().st_size,
        })
    return out


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def list_headings(content: str) -> List[Tuple[int, int, str]]:
    """Every markdown heading as (line_index, level, text), ignoring code fences."""
    out: List[Tuple[int, int, str]] = []
    in_fence = False
    for i, line in enumerate(content.splitlines()):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def append_section(path: str, heading: str, text: str,
                   *, create_missing: bool = False,
                   snapshot: Optional[str] = None) -> Dict[str, Any]:
    """Append `text` at the end of the section owned by `heading`.

    `update_fact` can only replace a uniquely-matching snippet, so adding a new
    line to a client or project note meant reverse-engineering a fragile anchor.
    This writes structurally instead: find the heading, walk to the next heading
    of the same or higher level, and insert before it. A duplicate or an
    unknown heading returns or raises before write_mutation (`snapshot`).
    """
    if not heading.strip():
        raise MemoError("'heading' cannot be empty.")
    if not text.strip():
        raise MemoError("'text' cannot be empty.")

    rel, content = read_dossier(path)
    wanted = heading.strip().lstrip("#").strip().lower()
    lines = content.splitlines()
    headings = list_headings(content)

    match = next((h for h in headings if h[2].lower() == wanted), None)
    if match is None:
        match = next((h for h in headings if wanted in h[2].lower()), None)

    body = text.rstrip("\n")

    # Idempotency: an identical block is never written twice.
    if body.strip() in content:
        return {"file": rel, "heading": heading.strip(), "entry": body,
                "duplicate": True, "placement": "already present"}

    if match is None:
        if not create_missing:
            available = ", ".join(h[2] for h in headings) or "(none)"
            raise MemoError(
                f"Heading '{heading}' not found in {rel}. Available headings: "
                f"{available}. Pass create_missing=true to add the section."
            )
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ["", f"## {heading.strip()}", "", body]
        placement = f"new section '## {heading.strip()}'"
    else:
        start, level, _ = match
        end = next((i for i, lv, _ in headings if i > start and lv <= level), len(lines))
        while end - 1 > start and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = [body]
        placement = f"end of section '{match[2]}'"

    target = safe_vault_path(rel, must_exist=True)
    write_mutation(target, touch_updated("\n".join(lines) + "\n"), snapshot)
    return {"file": rel, "heading": heading.strip(), "entry": body,
            "duplicate": False, "placement": placement}


# ----------------------------------------------------------------------
# Note creation
# ----------------------------------------------------------------------

TEMPLATES_DIR: Path = VAULT_DIR / "_templates"
FOLDER_TYPES = {
    "clients": "client", "projects": "project", "stack": "stack",
    "expertise": "expertise", "hackathons": "hackathon", "profiles": "profile",
    "agent": "agent", "sessions": "session", "docs": "core",
    "subscriptions": "subscription", "bookmarks": "bookmark",
}


def _lint_taxonomy() -> Tuple[set, set, set]:
    """(allowed_types, allowed_status, allowed_tags) from the linter itself.

    Read from vault_lint rather than duplicated here, so a note created through
    MCP can never be born failing the gate that validates it.
    """
    import importlib.util
    lint_path = Path(__file__).resolve().parent / "vault_lint.py"
    spec = importlib.util.spec_from_file_location("memo_vault_lint", lint_path)
    if spec is None or spec.loader is None:
        raise MemoError("Could not load scripts/vault_lint.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ALLOWED_TYPES, mod.ALLOWED_STATUS, mod.ALLOWED_TAGS


# The Second Brain plugin stamps these into every note, templates included.
# A new note must never inherit a template's stamp, or the plugin would treat
# it as the template. Matches the inline form and the block-list form.
_SECOND_BRAIN_LINE_RE = re.compile(
    r"^second-brain-(?:id|synced):.*(?:\n[ \t]+-.*)*\n?", re.MULTILINE)


def _template_body(note_type: str, title: str, today: str) -> Optional[str]:
    tpl = TEMPLATES_DIR / f"{note_type}.md"
    if not tpl.exists():
        return None
    raw = tpl.read_text(encoding="utf-8")  # universal newlines: CRLF arrives as LF
    if raw.startswith("---\n---\n"):
        raw = raw[8:]
    elif raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            raw = raw[end + 5:]
    raw = _SECOND_BRAIN_LINE_RE.sub("", raw)
    return raw.replace("{{title}}", title).replace("{{date}}", today).strip("\n")


def _yaml_quote(value: str) -> str:
    """A YAML double-quoted scalar. JSON string syntax is valid YAML, so a
    title or alias holding '"', ':', '#', ',' or '[' cannot break the block."""
    return json.dumps(value, ensure_ascii=False)


def create_note(path: str, title: str, note_type: str, *,
                status: str = "active",
                tags: Optional[List[str]] = None,
                body: Optional[str] = None,
                entity_id: Optional[str] = None,
                aliases: Optional[List[str]] = None) -> Dict[str, Any]:
    """Create a new vault note with lint-clean frontmatter, from its template.

    Refuses an absolute path, a '..' or dot segment, or an agent
    instruction-file name (normalize_note_path), then refuses to overwrite, to
    create at the root (six hubs only), or to produce frontmatter the linter
    would reject. Takes no snapshot: the target never exists, so a snapshot
    could only commit some other hand edit.
    """
    title = _one_line(str(title or ""))
    if not title:
        raise MemoError("'title' cannot be empty.")

    cleaned = normalize_note_path(path)
    if "/" not in cleaned:
        raise MemoError(
            f"Refused: '{cleaned}' would create a new file at the vault root. The root "
            "holds six hubs only (README, HOME, timeline, decisions, stack, subscriptions). "
            "Put this in a domain folder such as clients/, projects/, stack/, "
            "subscriptions/, or docs/."
        )

    top = cleaned.split("/", 1)[0]
    skipped = next((d for d in cleaned.split("/")[:-1] if d in SKIP_DIRS), None)
    if skipped:
        raise MemoError(f"Refused: '{skipped}/' is not an indexed vault folder.")

    target = safe_vault_path(cleaned)
    if target.exists():
        raise MemoError(
            f"'{cleaned}' already exists. Use memo_append_section to add to it, "
            "or memo_update_fact to correct it."
        )

    allowed_types, allowed_status, allowed_tags = _lint_taxonomy()
    note_type = note_type.strip()
    status = (status or "active").strip()
    if note_type not in allowed_types:
        raise MemoError(f"Unknown type '{note_type}'. Allowed: {', '.join(sorted(allowed_types))}.")
    if status not in allowed_status:
        raise MemoError(f"Unknown status '{status}'. Allowed: {', '.join(sorted(allowed_status))}.")

    expected_type = FOLDER_TYPES.get(top)
    if expected_type and note_type != expected_type:
        raise MemoError(
            f"Folder '{top}/' requires type '{expected_type}', got '{note_type}'."
        )

    final_tags = list(dict.fromkeys(
        [f"type/{note_type}", f"status/{status}", *(tags or [])]
    ))
    unknown = [t for t in final_tags if t not in allowed_tags]
    if unknown:
        raise MemoError(
            f"Tags outside the taxonomy: {', '.join(unknown)}. "
            "Use an existing tag or extend ALLOWED_TAGS in scripts/vault_lint.py first."
        )

    if entity_id:
        try:
            known = {e["id"] for e in graph_load().get("entities", [])}
        except MemoError:
            known = set()
        if entity_id not in known:
            raise MemoError(
                f"entity_id '{entity_id}' is not in the graph. Register it with "
                "memo_add_entity first, or omit it and add it after."
            )

    today = datetime.date.today().isoformat()
    if isinstance(aliases, str):
        aliases = [aliases]
    alias_list = [a for a in (_one_line(str(x)) for x in (aliases or [])) if a]
    fm = [
        "---",
        f"title: {_yaml_quote(title)}",
        f"aliases: [{', '.join(_yaml_quote(a) for a in alias_list)}]",
        f"type: {note_type}",
        f"status: {status}",
        f"tags: [{', '.join(final_tags)}]",
    ]
    if entity_id:
        fm.append(f"entity_id: {entity_id}")
    fm += [f"created: {today}", f"updated: {today}", "---"]

    content_body = (body or "").strip("\n") or _template_body(note_type, title.strip(), today)
    if not content_body:
        content_body = f"# {title.strip()}\n\n-"
    if not content_body.lstrip().startswith("#"):
        content_body = f"# {title.strip()}\n\n{content_body}"

    write_mutation(target, "\n".join(fm) + "\n" + content_body.rstrip("\n") + "\n")
    return {
        "file": cleaned,
        "title": title.strip(),
        "type": note_type,
        "status": status,
        "tags": final_tags,
        "entity_id": entity_id or "",
        "templated": body is None and (TEMPLATES_DIR / f"{note_type}.md").exists(),
    }


def update_fact(path: str, old_text: str, new_text: str, *,
                snapshot: Optional[str] = None) -> Dict[str, Any]:
    """Replace an exact, uniquely-occurring snippet inside a vault file.

    Deliberately conservative: a non-unique match is refused rather than
    guessed at, so a mutation can never silently hit the wrong record. A
    replacement identical to `old_text` is a no-op: nothing is written and
    `changed` is False, so the caller makes no commit.
    """
    if not old_text:
        raise MemoError("'old_text' cannot be empty.")
    rel, content = read_dossier(path)
    occurrences = content.count(old_text)
    if occurrences == 0:
        raise MemoError(
            f"'old_text' was not found verbatim in {rel}. "
            "Read the file with memo_get_dossier and copy the exact text."
        )
    if occurrences > 1:
        raise MemoError(
            f"'old_text' occurs {occurrences} times in {rel}; refusing an ambiguous "
            "edit. Include more surrounding context to make the match unique."
        )

    result = {
        "file": rel,
        "replaced": old_text.strip()[:200],
        "with": new_text.strip()[:200],
        "changed": new_text != old_text,
    }
    if not result["changed"]:
        return result
    target = safe_vault_path(rel, must_exist=True)
    write_mutation(target, touch_updated(content.replace(old_text, new_text, 1)), snapshot)
    return result


# ----------------------------------------------------------------------
# Knowledge graph
# ----------------------------------------------------------------------

# schema.md documents a subset; the live graph carries more. Accept the union
# so validation never rejects a type the vault already uses.
DOCUMENTED_ENTITY_TYPES = {"Person", "Organization", "Client", "Project", "Technology",
                           "Hackathon", "Discipline", "Profile"}
DOCUMENTED_RELATIONS = {"FOUNDER_OF", "EMPLOYED_AT", "PARTNER_WITH", "REFERRED_BY",
                        "SERVICES_CLIENT", "BUILT_WITH", "INTEGRATES_WITH",
                        "WON_CHAMPIONSHIP", "SUBMITTED_TO", "CANDIDATE_FOR",
                        "PRACTICES", "HAS_PROFILE", "USES", "SAVED"}


def graph_load() -> Dict[str, Any]:
    if not GRAPH_PATH.exists():
        raise MemoError("Knowledge graph file (graph/entities.json) not found.")
    return json.loads(GRAPH_PATH.read_text(encoding="utf-8"))


def graph_known_types(graph: Dict[str, Any]) -> Tuple[set, set]:
    types = DOCUMENTED_ENTITY_TYPES | {e.get("type") for e in graph.get("entities", [])}
    rels = DOCUMENTED_RELATIONS | {r.get("relation") for r in graph.get("relationships", [])}
    return {t for t in types if t}, {r for r in rels if r}


def graph_find(name: str) -> List[Dict[str, Any]]:
    graph = graph_load()
    target = name.lower().strip()
    matched = []
    for e in graph.get("entities", []):
        haystack = " ".join([
            e.get("id", ""), e.get("name", ""), e.get("name_ar", ""),
            *e.get("aliases", []),
        ]).lower()
        if target in haystack:
            ent = dict(e)
            ent["outgoing"] = [r for r in graph.get("relationships", [])
                               if r.get("source") == e.get("id")]
            ent["incoming"] = [r for r in graph.get("relationships", [])
                               if r.get("target") == e.get("id")]
            matched.append(ent)
    return matched


def graph_add_entity(entity: Optional[Dict[str, Any]] = None,
                     relationships: Optional[List[Dict[str, str]]] = None,
                     *, snapshot: Optional[str] = None
                     ) -> Dict[str, Any]:
    """Add an entity and/or relationships, validated against the live schema.

    Every validation error and the 'nothing new' case return before
    write_mutation (`snapshot`), so they never commit."""
    graph = graph_load()
    known_types, known_rels = graph_known_types(graph)
    ids = {e.get("id") for e in graph.get("entities", [])}
    added_entity = None

    if entity:
        for field in ("id", "type", "name"):
            if not entity.get(field):
                raise MemoError(f"Entity is missing required field '{field}'.")
        if entity["id"] in ids:
            raise MemoError(
                f"Entity id '{entity['id']}' already exists. Use memo_update_fact "
                "to change an existing record."
            )
        if entity["type"] not in known_types:
            raise MemoError(
                f"Unknown entity type '{entity['type']}'. Known types: "
                + ", ".join(sorted(known_types))
            )
        graph.setdefault("entities", []).append(entity)
        ids.add(entity["id"])
        added_entity = entity["id"]

    added_rels = []
    for rel in relationships or []:
        for field in ("source", "relation", "target"):
            if not rel.get(field):
                raise MemoError(f"Relationship is missing required field '{field}'.")
        if rel["relation"] not in known_rels:
            raise MemoError(
                f"Unknown relation '{rel['relation']}'. Known relations: "
                + ", ".join(sorted(known_rels))
            )
        for endpoint in ("source", "target"):
            if rel[endpoint] not in ids:
                raise MemoError(
                    f"Relationship {endpoint} '{rel[endpoint]}' is not a known entity id."
                )
        if rel in graph.get("relationships", []):
            continue
        graph.setdefault("relationships", []).append(rel)
        added_rels.append(rel)

    if added_entity is None and not added_rels:
        return {"entity": None, "relationships": [], "note": "Nothing new to add."}

    graph["generated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    write_mutation(GRAPH_PATH, json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
                   snapshot)
    return {
        "entity": added_entity,
        "relationships": added_rels,
        "entity_count": len(graph.get("entities", [])),
        "relationship_count": len(graph.get("relationships", [])),
    }


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------

def status() -> Dict[str, Any]:
    stats = sync_index()
    try:
        graph = graph_load()
        entity_count = len(graph.get("entities", []))
        rel_count = len(graph.get("relationships", []))
    except MemoError:
        entity_count = rel_count = 0

    return {
        "vault": str(VAULT_DIR),
        "markdown_files": stats["total"],
        "index": {
            "added": stats["added"],
            "updated": stats["updated"],
            "purged": stats["purged"],
            "db_kb": round(DB_PATH.stat().st_size / 1024, 1) if DB_PATH.exists() else 0,
            "trigram": bool(_TRIGRAM),
        },
        "graph": {"entities": entity_count, "relationships": rel_count},
        "timeline_events": len(parse_timeline()),
        "git_snapshots": git_enabled(),
        "sqlite_version": sqlite3.sqlite_version,
    }
