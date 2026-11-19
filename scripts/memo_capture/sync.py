"""Memo git sync: fetch / merge / push with a side worktree (plan 003 D1/D2).

Public API: git_sync, run, read_state, main. resolve_* and the D8 guard land
in TASK-005.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import reindex, settings, state
from .paths import log_line, vault_dir

# Re-export / use core helpers. memo_core binds VAULT_DIR at import from MEMO_VAULT.
import memo_core as core

GIT_TIMEOUT = core.GIT_TIMEOUT
MERGE_WORKTREE = ".cache/sync-merge"
SYNC_STATE = ".cache/sync.json"
MERGE_LOCK = ".cache/sync-merge.lock"

_run_lock = threading.Lock()

_DEFAULT_STATE: Dict[str, Any] = {
    "status": "ok",
    "reason": "",
    "last_ok": None,
    "last_attempt": None,
    "failing_since": None,
    "conflict_paths": [],
    "stuck_machines": [],
    "resolving": None,
    "held_sha": None,
    "accepted_code_sha": None,
    "scripts_merged": None,
    "initiator": None,
    "last_adopted": None,
    "adopt_retry": [],
    "adopt_errors": [],
}

REQ001_FORBIDDEN = (
    "reset --hard", "push --force", "checkout --", "rebase", "stash drop", "merge --abort",
)


def _vault() -> Path:
    return Path(os.environ.get("MEMO_VAULT") or vault_dir()).resolve()


def _state_path() -> Path:
    return _vault() / SYNC_STATE


def _worktree_path() -> Path:
    return _vault() / MERGE_WORKTREE


def _trace(cwd: Path, argv: Sequence[str]) -> None:
    path = os.environ.get("MEMO_GIT_TRACE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{cwd}\t{' '.join(argv)}\n")
    except OSError:
        pass


def _git(cwd: Path, *args: str, check: bool = False,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    git_env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    if env:
        git_env.update(env)
    argv = ["git", *args]
    _trace(cwd, argv)
    return subprocess.run(
        argv, cwd=str(cwd), capture_output=True, text=True, check=check,
        stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT, env=git_env,
    )


def read_state() -> Dict[str, Any]:
    path = _state_path()
    data = dict(_DEFAULT_STATE)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, ValueError):
            pass
    return data


def _write_state(update: Dict[str, Any]) -> Dict[str, Any]:
    """Merge update into sync.json under the vault flock."""
    with core.vault_lock(timeout=30):
        cur = read_state()
        cur.update(update)
        cur["last_attempt"] = int(time.time() * 1000)
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        core.atomic_write(path, json.dumps(cur, indent=2, ensure_ascii=False) + "\n")
        return cur


def _now_ms() -> int:
    return int(time.time() * 1000)


def _head(cwd: Path) -> str:
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _has_origin(cwd: Path) -> bool:
    r = _git(cwd, "remote")
    return "origin" in (r.stdout or "").split()


def _index_lock_age(cwd: Path) -> Optional[float]:
    lock = cwd / ".git" / "index.lock"
    if not lock.exists():
        return None
    return time.time() - lock.stat().st_mtime


def _merge_worktree_busy() -> bool:
    """Non-blocking flock on the merge worktree lock file."""
    path = _vault() / MERGE_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        return False
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
        return False
    except OSError:
        fh.close()
        return True


class _MergeLock:
    def __init__(self):
        self.fh = None

    def __enter__(self):
        path = _vault() / MERGE_LOCK
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:
            return self
        self.fh = open(path, "w")
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            raise RuntimeError("busy")
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self.fh.close()


def _ensure_worktree(cwd: Path, head: str) -> Path:
    wt = _worktree_path()
    if not wt.exists():
        _git(cwd, "worktree", "prune")
        r = _git(cwd, "worktree", "add", "--detach", str(wt), head)
        if r.returncode != 0:
            raise RuntimeError(f"worktree add failed: {r.stderr}")
        return wt
    # leftover merge?
    if (wt / ".git").exists() or (wt / ".git").is_file():
        if (wt / ".git").is_file() or (cwd / ".git" / "worktrees").exists():
            if (wt / ".git").exists() or True:
                merge_head = None
                # detect MERGE_HEAD in worktree
                r = _git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD")
                if r.returncode == 0:
                    _git(wt, "merge", "--abort")
    r = _git(wt, "checkout", "--detach", "--force", head)
    if r.returncode != 0:
        # recreate
        _git(cwd, "worktree", "remove", "--force", str(wt))
        _git(cwd, "worktree", "prune")
        r = _git(cwd, "worktree", "add", "--detach", str(wt), head)
        if r.returncode != 0:
            raise RuntimeError(f"worktree recreate failed: {r.stderr}")
    return wt


def _conflict_paths(cwd: Path) -> List[str]:
    r = _git(cwd, "diff", "--name-only", "--diff-filter=U")
    return [l for l in (r.stdout or "").splitlines() if l.strip()]


def _push_side_ref(cwd: Path, name: str) -> None:
    _git(cwd, "push", "origin", f"main:refs/heads/sync/{name}",
         env={"GIT_TERMINAL_PROMPT": "0"})


def _stuck_machines(cwd: Path) -> List[str]:
    r = _git(cwd, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/sync")
    stuck = []
    main = _git(cwd, "rev-parse", "origin/main").stdout.strip()
    for ref in (r.stdout or "").splitlines():
        short = ref.replace("origin/", "", 1) if ref.startswith("origin/") else ref
        name = short.split("/", 1)[-1] if "/" in short else short
        # skip own
        if name == settings.effective_name():
            continue
        tip = _git(cwd, "rev-parse", ref).stdout.strip()
        # ancestor of origin/main?
        anc = _git(cwd, "merge-base", "--is-ancestor", tip, main)
        if anc.returncode != 0:
            stuck.append(name)
    return stuck


def _classify_push(res: subprocess.CompletedProcess) -> str:
    """Return ok | retry | error from push --porcelain output."""
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode == 0:
        return "ok"
    for line in (res.stdout or "").splitlines():
        if line.startswith("!") or "\t!" in f"\t{line}":
            # porcelain: <flag>\t<from>:<to>\t<summary>
            flag = line.split("\t", 1)[0].strip() if "\t" in line else line[:1]
            if "!" in flag or line.lstrip().startswith("!"):
                low = line.lower()
                if "fetch first" in low or "non-fast-forward" in low:
                    return "retry"
                return "error"
    # also check lines like "!	refs/heads/main:refs/heads/main	[rejected] (fetch first)"
    for line in (res.stdout or "").splitlines():
        if "[rejected]" in line or line.startswith("!"):
            low = line.lower()
            if "fetch first" in low or "non-fast-forward" in low:
                return "retry"
            return "error"
    return "retry"  # network / auth


def _changed_paths(cwd: Path, a: str, b: str) -> List[str]:
    r = _git(cwd, "diff", "--name-only", "--no-renames", f"{a}..{b}")
    return [l for l in (r.stdout or "").splitlines() if l.strip()]


VPS_AUTHOR = "OpenMemo VPS <openmemo@example.com>"
VPS_EMAIL = "openmemo@example.com"

VPS_ALLOW_PREFIXES = (
    "sessions/", "projects/", "clients/", "stack/", "subscriptions/",
    "repo-library/", "life/", "hackathons/",
)
VPS_ALLOW_EXACT = {
    "timeline.md", "decisions.md", "graph/entities.json", "agent/learned-heuristics.md",
}
DENY_PREFIXES = (
    "scripts/", "plugin/", "tests/", "docs/", "_templates/",
    ".agents/", ".claude/", ".cursor/", ".codex/", ".gemini/", ".obsidian/plugins/",
)
DENY_EXACT = {
    ".gitattributes", ".gitignore", ".memo/settings.json", ".mcp.json",
    "CLAUDE.md", "GEMINI.md", "AGENTS.md", "README.md",
}


def _path_held_for_author(path: str, author_email: str) -> bool:
    """True if this path should hold sync for the given author (D8)."""
    base = Path(path).name
    if base.lower() in core.INSTRUCTION_FILE_NAMES:
        return True
    is_vps = author_email == VPS_EMAIL or author_email.endswith("openmemo@example.com")
    # Also match by full author line from git log %ae
    if is_vps or author_email == "openmemo@example.com":
        if path in VPS_ALLOW_EXACT:
            return False
        if any(path.startswith(p) for p in VPS_ALLOW_PREFIXES):
            return False
        return True  # VPS path not on allow-list
    # other authors: hold deny-list
    if path in DENY_EXACT:
        return True
    if any(path.startswith(p) for p in DENY_PREFIXES):
        return True
    return False


def incoming_guard(cwd: Path, head: str, remote: str) -> Optional[Dict[str, Any]]:
    """D8: return a skipped result dict if incoming paths are held, else None."""
    if settings.get("accept_incoming_code"):
        return None
    st = read_state()
    accepted = st.get("accepted_code_sha")
    if accepted and accepted == remote:
        return None

    held: List[str] = []
    # commit-by-commit
    log = _git(cwd, "log", "--format=%H %ae", "--name-only", "--no-renames", "-m", f"{head}..{remote}")
    cur_email = ""
    for line in (log.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2 and len(parts[0]) == 40 and all(c in "0123456789abcdef" for c in parts[0]):
            cur_email = parts[1].strip()
            continue
        path = line.strip()
        if path and _path_held_for_author(path, cur_email):
            held.append(path)

    # net diff check
    diff = _git(cwd, "diff", "--name-only", "--no-renames", f"{head}...{remote}")
    for path in (diff.stdout or "").splitlines():
        path = path.strip()
        if not path:
            continue
        # for net check, apply deny-list + basename for every author, and allow-list only if all commits are VPS?
        # Plan: "plus a net check of git diff ... against the deny-list and the basename rule for every author"
        base = Path(path).name
        if base.lower() in core.INSTRUCTION_FILE_NAMES:
            held.append(path)
            continue
        if path in DENY_EXACT or any(path.startswith(p) for p in DENY_PREFIXES):
            held.append(path)

    if not held:
        return None
    # unique preserve order
    seen = set()
    uniq = []
    for h in held:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return {
        "status": "skipped",
        "reason": "code-change " + " ".join(uniq[:20]),
        "changed_paths": [],
        "conflict_paths": [],
        "scripts_merged": None,
        "held_sha": remote,
        "held_paths": uniq,
    }


def resolve_start() -> Dict[str, Any]:
    """D6 step 1: leave merge conflicted in worktree; set resolving."""
    cwd = _vault()
    st = read_state()
    if st.get("resolving"):
        print("already resolving", st["resolving"])
        return st
    head = _head(cwd)
    r_sha = _git(cwd, "rev-parse", "origin/main").stdout.strip()
    try:
        with _MergeLock():
            wt = _ensure_worktree(cwd, head)
            merge = _git(wt, "merge", "--no-edit", r_sha)
            paths = _conflict_paths(wt) if merge.returncode != 0 else []
            # leave conflicted — do NOT abort
            resolving = {"base": head, "remote": r_sha, "started": _now_ms()}
            update = {
                "status": "conflict",
                "reason": "resolving",
                "resolving": resolving,
                "conflict_paths": paths,
            }
            _write_state(update)
            print("conflict paths:", ", ".join(paths) or "(none)")
            print("worktree:", _worktree_path())
            return {**st, **update}
    except RuntimeError as exc:
        return {"status": "skipped", "reason": str(exc)}


def resolve_finish() -> Dict[str, Any]:
    """D6 step 3: merge live HEAD into worktree resolution, ff live, clear resolving, push."""
    cwd = _vault()
    st = read_state()
    if not st.get("resolving"):
        print("nothing to finish")
        return st
    wt = _worktree_path()
    if not wt.exists():
        print("merge worktree missing")
        return {"status": "error", "reason": "worktree missing"}

    # ensure no unmerged paths remain (don't scan source that mentions marker text)
    unmerged = _conflict_paths(wt)
    if unmerged:
        print("unmerged paths remain:", ", ".join(unmerged))
        return {"status": "conflict", "reason": "markers remain"}
    for rel in (st.get("conflict_paths") or []):
        path = wt / rel
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.startswith("<<<<<<< ") or "\n<<<<<<< " in text:
            print(f"conflict markers remain in {rel}")
            return {"status": "conflict", "reason": "markers remain"}

    # Complete an in-progress merge or commit leftover fixes
    merge_in_progress = _git(wt, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    st_u = _git(wt, "status", "--porcelain")
    if merge_in_progress or st_u.stdout.strip():
        _git(wt, "add", "-A")
        # drop unmerged state by adding resolved files
        c = _git(wt, "commit", "-m", "memo: sync resolve")
        if c.returncode != 0 and merge_in_progress:
            print("could not commit resolution:", (c.stderr or c.stdout)[:300])
            return {"status": "conflict", "reason": "commit failed"}

    live = _head(cwd)
    m = _git(wt, "merge", "--no-edit", live)
    if m.returncode != 0:
        print("new conflict while finishing; fix worktree and retry")
        print((m.stderr or m.stdout)[:300])
        return {"status": "conflict", "reason": "finish conflict"}
    M = _head(wt)
    try:
        with core.vault_lock(timeout=30):
            ff = _git(cwd, "merge", "--ff-only", M)
            if ff.returncode != 0:
                reason = "dirty " + ((ff.stderr or ff.stdout or "")[:200])
                print(reason)
                return {"status": "skipped", "reason": reason}
    except core.MemoError as exc:
        return {"status": "skipped", "reason": "busy"}

    push = _git(cwd, "push", "--porcelain", "origin", "main", env={"GIT_TERMINAL_PROMPT": "0"})
    kind = _classify_push(push)
    update = {
        "status": "ok" if kind == "ok" else kind,
        "reason": "" if kind == "ok" else ((push.stderr or push.stdout or "")[:200]),
        "resolving": None,
        "conflict_paths": [],
        "failing_since": None if kind == "ok" else st.get("failing_since"),
        "last_ok": _now_ms() if kind == "ok" else st.get("last_ok"),
    }
    _write_state(update)
    return update



def git_sync(accept_sha: Optional[str] = None) -> Dict[str, Any]:
    """One git sync tick (D2), without adoption. TASK-005 adds D8 + resolve."""
    cwd = _vault()
    out: Dict[str, Any] = {
        "status": "ok", "reason": "", "changed_paths": [],
        "conflict_paths": [], "scripts_merged": None,
    }

    if not core.git_enabled():
        out["status"] = "skipped"
        out["reason"] = "no-git"
        return out

    # Point core helpers at this vault (tests set MEMO_VAULT before import usually;
    # also override for subprocess runs that re-import).
    core.ensure_git_config()

    if not _has_origin(cwd):
        out["status"] = "skipped"
        out["reason"] = "no-remote"
        return out

    if (cwd / ".git" / "MERGE_HEAD").exists():
        out["status"] = "skipped"
        out["reason"] = "merge-in-progress"
        return out

    age = _index_lock_age(cwd)
    if age is not None:
        if age < GIT_TIMEOUT:
            out["status"] = "skipped"
            out["reason"] = "busy"
            return out
        out["status"] = "error"
        out["reason"] = "stale index.lock"
        return out

    st = read_state()
    if st.get("resolving"):
        out["status"] = "conflict"
        out["reason"] = "resolving"
        out["conflict_paths"] = st.get("conflict_paths") or []
        return out

    if _merge_worktree_busy():
        out["status"] = "skipped"
        out["reason"] = "busy"
        return out

    # Fetch
    fetch = _git(cwd, "fetch", "origin", env={"GIT_TERMINAL_PROMPT": "0"})
    if fetch.returncode != 0:
        out["status"] = "retry"
        reason = (fetch.stderr or fetch.stdout or "fetch failed").strip().splitlines()
        out["reason"] = reason[0] if reason else "fetch failed"
        return out

    r_sha = _git(cwd, "rev-parse", "origin/main").stdout.strip()
    head = _head(cwd)

    # stuck machines
    try:
        out["stuck_machines"] = _stuck_machines(cwd)
    except Exception:
        out["stuck_machines"] = []

    if head == r_sha:
        out["status"] = "ok"
        return out

    # ahead / behind
    mb = _git(cwd, "merge-base", head, r_sha).stdout.strip()
    ahead_only = mb == r_sha and head != r_sha
    behind_or_diverged = mb != head  # head is not ancestor of itself equal...
    # behind: mb == head; diverged: mb != head and mb != r_sha; ahead: mb == r_sha

    if ahead_only:
        # push only
        push = _git(cwd, "push", "--porcelain", "origin", "main",
                    env={"GIT_TERMINAL_PROMPT": "0"})
        kind = _classify_push(push)
        out["status"] = kind if kind != "ok" else "ok"
        if kind != "ok":
            out["reason"] = ((push.stderr or push.stdout or "push failed").strip().splitlines() or [""])[0]
        return out

    # behind or diverged → merge in worktree
    # D8 guard
    if accept_sha:
        # --accept-code <sha> must equal current R
        if accept_sha != r_sha:
            print(f"not the current incoming commit {r_sha}", file=sys.stderr)
            return {
                "status": "skipped",
                "reason": f"not the current incoming commit {r_sha}",
                "changed_paths": [],
                "conflict_paths": [],
                "scripts_merged": None,
                "exit_code": 2,
            }
        # record acceptance then proceed
        st = read_state()
        st["accepted_code_sha"] = accept_sha
        st["held_sha"] = None
        _write_state({"accepted_code_sha": accept_sha, "held_sha": None})

    held = incoming_guard(cwd, head, r_sha)
    if held is not None:
        # persist held_sha
        out.update({k: v for k, v in held.items() if k != "held_paths"})
        _write_state({
            "status": held["status"],
            "reason": held["reason"],
            "held_sha": held.get("held_sha"),
        })
        return out

    try:
        with _MergeLock():
            B = head
            wt = _ensure_worktree(cwd, B)
            merge = _git(wt, "merge", "--no-edit", r_sha)
            if merge.returncode != 0:
                paths = _conflict_paths(wt)
                _git(wt, "merge", "--abort")
                out["status"] = "conflict"
                out["reason"] = ", ".join(paths) if paths else "merge conflict"
                out["conflict_paths"] = paths
                try:
                    _push_side_ref(cwd, settings.effective_name())
                except Exception as exc:
                    log_line("sync", f"side ref push failed: {exc}")
                return out

            M = _head(wt)

            # Live step under flock
            try:
                with core.vault_lock(timeout=30):
                    # snapshot dirty tracked files that M changes relative to B
                    changed = _changed_paths(cwd, B, M)
                    dirty = []
                    st_u = _git(cwd, "status", "--porcelain", "-uall")
                    dirty_map = {}
                    for line in (st_u.stdout or "").splitlines():
                        if len(line) < 4:
                            continue
                        path = line[3:]
                        if path.startswith(".obsidian/") or path == ".obsidian":
                            continue
                        dirty_map[path] = line[:2]
                    overlap = [p for p in changed if p in dirty_map and not dirty_map[p].startswith("??")]
                    # untracked in the way
                    untracked_block = [p for p in changed if dirty_map.get(p, "").strip().startswith("??")
                                       or dirty_map.get(p, "") == "??"]
                    # also: untracked file that would be overwritten
                    for p in changed:
                        code = dirty_map.get(p, "")
                        if code.startswith("?") or code == "??":
                            untracked_block.append(p)
                    if untracked_block:
                        # check after snapshot attempt path - if untracked blocks ff
                        pass

                    if overlap:
                        core.git_snapshot("memo_sync", overlap)

                    head_now = _head(cwd)
                    if head_now != B:
                        # merge HEAD into M inside worktree
                        _git(wt, "checkout", "--detach", "--force", M)
                        m2 = _git(wt, "merge", "--no-edit", head_now)
                        if m2.returncode != 0:
                            paths = _conflict_paths(wt)
                            _git(wt, "merge", "--abort")
                            out["status"] = "conflict"
                            out["reason"] = ", ".join(paths) if paths else "merge conflict"
                            out["conflict_paths"] = paths
                            try:
                                _push_side_ref(cwd, settings.effective_name())
                            except Exception:
                                pass
                            return out
                        M = _head(wt)

                    ff = _git(cwd, "merge", "--ff-only", M)
                    if ff.returncode != 0:
                        # dirty paths in the way
                        msg = (ff.stderr or ff.stdout or "ff failed").strip()
                        # find untracked/local paths
                        reason_paths = []
                        for line in msg.splitlines():
                            if "would be overwritten" in line.lower() or "untracked" in line.lower():
                                reason_paths.append(line.strip())
                        out["status"] = "skipped"
                        out["reason"] = "dirty " + (
                            " ".join(reason_paths) if reason_paths else msg.splitlines()[0] if msg else "paths"
                        )
                        return out

                    # scripts_merged?
                    scripts_changed = any(p == "scripts" or p.startswith("scripts/")
                                          for p in _changed_paths(cwd, B, _head(cwd)))
                    if scripts_changed:
                        sm = _git(cwd, "rev-parse", "HEAD:scripts")
                        if sm.returncode == 0:
                            out["scripts_merged"] = sm.stdout.strip()
                    out["changed_paths"] = _changed_paths(cwd, B, _head(cwd))
            except core.MemoError as exc:
                if "lock" in str(exc).lower():
                    out["status"] = "skipped"
                    out["reason"] = "busy"
                    return out
                raise
    except RuntimeError as exc:
        if str(exc) == "busy":
            out["status"] = "skipped"
            out["reason"] = "busy"
            return out
        out["status"] = "error"
        out["reason"] = str(exc)
        return out
    except Exception as exc:
        out["status"] = "error"
        out["reason"] = str(exc)
        return out

    # Push
    push = _git(cwd, "push", "--porcelain", "origin", "main",
                env={"GIT_TERMINAL_PROMPT": "0"})
    kind = _classify_push(push)
    if kind != "ok":
        out["status"] = kind
        out["reason"] = ((push.stderr or push.stdout or "push failed").strip().splitlines() or [""])[0]
        return out
    out["status"] = "ok"
    return out


def _adopt_after(pre_head: str) -> Dict[str, Any]:
    """Adopt sidecars changed since last_adopted + adopt_retry (D1)."""
    cwd = _vault()
    st = read_state()
    last = st.get("last_adopted")
    if not last:
        last = pre_head
    head = _head(cwd)
    paths: List[str] = []
    if last != head:
        r = _git(cwd, "diff", "--name-only", f"{last}..{head}")
        for line in (r.stdout or "").splitlines():
            if line.startswith("sessions/") and line.endswith(".json"):
                paths.append(line)
    retry = list(st.get("adopt_retry") or [])
    selected = []
    seen = set()
    for p in paths + retry:
        if p not in seen:
            seen.add(p)
            selected.append(p)

    adopt_retry = []
    adopt_errors = []
    # move rebuild_collisions from meta
    try:
        conn = state.connect()
        row = conn.execute("SELECT value FROM meta WHERE key='rebuild_collisions'").fetchone()
        if row and row["value"]:
            try:
                adopt_errors.extend(json.loads(row["value"]))
            except ValueError:
                pass
            conn.execute("DELETE FROM meta WHERE key='rebuild_collisions'")
        for rel in selected:
            path = cwd / rel
            if not path.exists():
                continue  # dropped
            result = reindex.adopt_sidecar(conn, path.stem, path, rel.replace(".json", ".md"))
            if result.error or result.collisions:
                adopt_retry.append(rel)
                if result.error:
                    adopt_errors.append({"kind": "sidecar", "path": rel, "error": result.error})
                adopt_errors.extend(result.collisions)
        try:
            conn.raw.close()
        except Exception:
            pass
    except Exception as exc:
        log_line("sync", f"adopt failed: {exc}")
        adopt_errors.append({"kind": "adopt", "error": str(exc)})

    return {
        "last_adopted": head,
        "adopt_retry": adopt_retry,
        "adopt_errors": adopt_errors,
    }


def run(accept_sha: Optional[str] = None, initiator: str = "worker") -> Dict[str, Any]:
    if not _run_lock.acquire(blocking=False):
        result = {"status": "skipped", "reason": "busy", "initiator": initiator}
        _write_state({**result, "initiator": initiator})
        return result

    try:
        cwd = _vault()
        pre_head = _head(cwd) if (cwd / ".git").exists() else ""
        git = git_sync(accept_sha=accept_sha)
        status = git["status"]
        reason = git.get("reason") or ""

        # failing_since rules
        st = read_state()
        failing_since = st.get("failing_since")
        conflict_paths = st.get("conflict_paths") or []

        if status == "ok":
            failing_since = None
            conflict_paths = []
        elif status == "retry":
            failing_since = failing_since or _now_ms()
        elif status == "skipped" and reason in ("no-remote", "no-git", "busy"):
            pass  # leave failing_since
        elif status == "skipped":
            failing_since = failing_since or _now_ms()
        elif status == "conflict":
            failing_since = failing_since or _now_ms()
            conflict_paths = git.get("conflict_paths") or conflict_paths
        elif status == "error":
            failing_since = failing_since or _now_ms()

        adopt = _adopt_after(pre_head)

        update = {
            "status": status,
            "reason": reason,
            "initiator": initiator,
            "failing_since": failing_since,
            "conflict_paths": conflict_paths,
            "stuck_machines": git.get("stuck_machines") or [],
            "held_sha": git.get("held_sha") if "held_sha" in git else st.get("held_sha"),
            "accepted_code_sha": st.get("accepted_code_sha"),
            "scripts_merged": git.get("scripts_merged") or st.get("scripts_merged"),
            "last_adopted": adopt["last_adopted"],
            "adopt_retry": adopt["adopt_retry"],
            "adopt_errors": adopt["adopt_errors"],
        }
        if status == "ok":
            update["last_ok"] = _now_ms()
        _write_state(update)
        update["changed_paths"] = git.get("changed_paths") or []
        return update
    finally:
        _run_lock.release()


# Background sync for remote MCP (plan 004 TASK-001).
SYNC_SECONDS_PER_MINUTE = 60
_bg_wake: Optional[threading.Event] = None
_bg_thread: Optional[threading.Thread] = None


def start_background(wake_event: Optional[threading.Event] = None,
                     on_scripts_changed=None) -> threading.Event:
    """Daemon thread: run() at start, every sync_interval_minutes, and on wake.

    interval 0 (or missing) means the thread never starts ticks (waits forever on wake only).
    Never writes stdout. on_scripts_changed is called when scripts_merged changes.
    """
    global _bg_wake, _bg_thread
    if _bg_thread is not None and _bg_thread.is_alive():
        return _bg_wake  # type: ignore[return-value]
    wake = wake_event if wake_event is not None else threading.Event()
    _bg_wake = wake

    def loop() -> None:
        scripts_at_start = None
        try:
            scripts_at_start = read_state().get("scripts_merged")
        except Exception:
            scripts_at_start = None
        first = True
        while True:
            try:
                settings.load(refresh=True)
                interval = int(settings.get("sync_interval_minutes") or 0)
                if interval <= 0:
                    wake.wait(60)
                    wake.clear()
                    continue
                if not first:
                    wake.wait(interval * SYNC_SECONDS_PER_MINUTE)
                    wake.clear()
                first = False
                try:
                    run(initiator="mcp")
                except Exception as exc:
                    log_line("sync", f"mcp background tick failed: {exc}")
                try:
                    sm = read_state().get("scripts_merged")
                    if sm and scripts_at_start is not None and sm != scripts_at_start:
                        log_line("sync", f"scripts_merged changed ({scripts_at_start} -> {sm})")
                        if on_scripts_changed is not None:
                            on_scripts_changed()
                        else:
                            os._exit(75)
                    if scripts_at_start is None and sm:
                        scripts_at_start = sm
                except Exception as exc:
                    log_line("sync", f"scripts_merged check failed: {exc}")
            except Exception as exc:
                log_line("sync", f"mcp background loop error: {exc}")
                wake.wait(60)
                wake.clear()

    t = threading.Thread(target=loop, name="memo-mcp-sync", daemon=True)
    _bg_thread = t
    t.start()
    return wake



def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    accept = None
    if args and args[0] == "now":
        args = args[1:]
        if args[:1] == ["--accept-code"] and len(args) >= 2:
            accept = args[1]
            args = args[2:]
    elif args and args[0] == "resolve":
        if "--finish" in args:
            result = resolve_finish()
        else:
            result = resolve_start()
        print(f"{result.get('status')}: {result.get('reason') or ''}".rstrip(": "))
        return 0 if result.get("status") in ("ok", "conflict") and result.get("resolving") else (
            0 if result.get("status") == "ok" else 1)
    elif args and args[0] == "status":
        st = read_state()
        print(f"{st.get('status')}: {st.get('reason') or ''}".rstrip(": "))
        return 0
    result = run(accept_sha=accept, initiator="cli")
    if result.get("exit_code") == 2 or (
        result.get("status") == "skipped"
        and str(result.get("reason") or "").startswith("not the current incoming")
    ):
        print(f"{result.get('status')}: {result.get('reason') or ''}".rstrip(": "))
        return 2
    print(f"{result.get('status')}: {result.get('reason') or ''}".rstrip(": "))
    status = result.get("status")
    reason = result.get("reason") or ""
    if status == "ok":
        return 0
    if status == "skipped" and reason in ("no-remote", "no-git", "busy"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
