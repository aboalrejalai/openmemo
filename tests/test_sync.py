"""Sync tick tests (plan 003 TASK-004 / TASK-005)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cwd, *args, env=None, check=True):
    e = {**os.environ, **(env or {})}
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check, env=e)


class _SyncRepo:
    """Bare remote + two clones with scripts/, machine.json, identities."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="memo-sync-"))
        self.remote = self.tmp / "remote.git"
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        _run(self.tmp, "git", "init", "--bare", str(self.remote))
        seed = self.tmp / "seed"
        self._build_seed(seed)
        _run(seed, "git", "remote", "add", "origin", str(self.remote))
        _run(seed, "git", "push", "-u", "origin", "main")
        _run(self.tmp, "git", "clone", str(self.remote), str(self.a))
        _run(self.tmp, "git", "clone", str(self.remote), str(self.b))
        self._configure(self.a, "mac", 0, True, "Mac Writer", "mac@test")
        self._configure(self.b, "vps", 1_000_000_000, False, "OpenMemo VPS", "openmemo@example.com")

    def _build_seed(self, path: Path):
        path.mkdir()
        _run(path, "git", "init", "-b", "main")
        _run(path, "git", "config", "user.name", "seed")
        _run(path, "git", "config", "user.email", "seed@test")
        shutil.copytree(ROOT / "scripts", path / "scripts")
        (path / ".gitattributes").write_text(
            "*.md merge=memo-note\n"
            "sessions/current.md merge=union\n"
            "timeline.md merge=memo-timeline\n", encoding="utf-8")
        (path / "timeline.md").write_text(
            "---\ntitle: Timeline\nupdated: 2026-11-01\n---\n\n"
            "## Living History\n\n### November 2026\n"
            "- **2026-11-10**\n  - **Note:** seed\n\n"
            "## Superseded Entries\n\n", encoding="utf-8")
        (path / "sessions").mkdir()
        (path / "sessions" / "current.md").write_text(
            "---\ntitle: current\nupdated: 2026-11-01\n---\n\nbase\n", encoding="utf-8")
        (path / "projects").mkdir()
        (path / "projects" / ".gitkeep").write_text("", encoding="utf-8")
        (path / ".memo").mkdir()
        (path / ".memo" / "settings.json").write_text(
            json.dumps({"sync_interval_minutes": 0}), encoding="utf-8")
        (path / ".gitignore").write_text(".memo/*\n!.memo/settings.json\n.cache/\n.obsidian/\n", encoding="utf-8")
        _run(path, "git", "add", "-A")
        _run(path, "git", "commit", "-m", "seed")

    def _configure(self, path: Path, name: str, id_base: int, owns: bool, uname: str, email: str):
        _run(path, "git", "config", "user.name", uname)
        _run(path, "git", "config", "user.email", email)
        (path / ".memo").mkdir(exist_ok=True)
        (path / ".memo" / "machine.json").write_text(json.dumps({
            "name": name, "id_base": id_base, "owns_unstamped": owns,
            "accept_incoming_code": name != "mac",
        }), encoding="utf-8")
        if not (path / ".memo" / "settings.json").exists():
            (path / ".memo" / "settings.json").write_text(
                json.dumps({"sync_interval_minutes": 0}), encoding="utf-8")
        env = {**os.environ, "MEMO_VAULT": str(path)}
        _run(path, sys.executable, "-c",
             "import sys; sys.path.insert(0,'scripts'); import memo_core; memo_core.ensure_git_config()",
             env=env)

    def sync(self, clone: Path, *extra, env_extra=None):
        env = {
            **os.environ,
            "MEMO_VAULT": str(clone),
            "MEMO_HOME": str(clone / ".memo"),
            "PYTHONPATH": str(clone / "scripts"),
            "MEMO_GIT_TRACE": str(self.tmp / "git-trace.log"),
            "MEMO_SYNC_INTERVAL_MINUTES": "0",
            "MEMO_AUTO_REINDEX": "0",
        }
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-m", "memo_capture.sync", "now", *extra],
            cwd=clone, capture_output=True, text=True, env=env,
        )

    def commit(self, clone: Path, msg: str):
        _run(clone, "git", "add", "-A")
        _run(clone, "git", "commit", "-m", msg, check=False)


class SyncTickTest(unittest.TestCase):
    def setUp(self):
        self.repo = _SyncRepo()

    def tearDown(self):
        shutil.rmtree(self.repo.tmp, ignore_errors=True)

    def test_ahead_only_pushes(self):
        a = self.repo.a
        (a / "projects").mkdir(exist_ok=True)
        (a / "projects" / "x.md").write_text("# x\n", encoding="utf-8")
        self.repo.commit(a, "ahead")
        # dirty tracked untouched: leave another dirty file
        (a / "timeline.md").write_text(
            (a / "timeline.md").read_text() + "\n", encoding="utf-8")  # dirty
        dirty_before = (a / "timeline.md").read_text()
        r = self.repo.sync(a)
        self.assertIn(r.returncode, (0, 1), r.stdout + r.stderr)
        # push happened
        _run(a, "git", "fetch", "origin")
        local = _run(a, "git", "rev-parse", "HEAD").stdout.strip()
        remote = _run(a, "git", "rev-parse", "origin/main").stdout.strip()
        self.assertEqual(local, remote)
        self.assertEqual((a / "timeline.md").read_text(), dirty_before)

    def test_behind_fast_forward(self):
        a, b = self.repo.a, self.repo.b
        (b / "projects").mkdir(exist_ok=True)
        (b / "projects" / "y.md").write_text("# y\n", encoding="utf-8")
        self.repo.commit(b, "b-ahead")
        self.repo.sync(b)
        r = self.repo.sync(a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((a / "projects" / "y.md").exists())

    def test_diverged_timeline_merge(self):
        a, b = self.repo.a, self.repo.b
        # both append different timeline lines
        def append(clone, text):
            tl = (clone / "timeline.md").read_text()
            (clone / "timeline.md").write_text(
                tl.replace("  - **Note:** seed", f"  - **Note:** seed\n  - **Note:** {text}"),
                encoding="utf-8")
            self.repo.commit(clone, text)
        append(a, "from-a")
        append(b, "from-b")
        self.repo.sync(a)
        r = self.repo.sync(b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # pull a
        r2 = self.repo.sync(a)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        tl = (a / "timeline.md").read_text()
        self.assertIn("from-a", tl)
        self.assertIn("from-b", tl)

    def test_conflict_preserves_live_tree(self):
        a, b = self.repo.a, self.repo.b
        (a / "projects").mkdir(exist_ok=True)
        (a / "projects" / "taaib.md").write_text("# a\n", encoding="utf-8")
        (b / "projects").mkdir(exist_ok=True)
        (b / "projects" / "taaib.md").write_text("# b\n", encoding="utf-8")
        self.repo.commit(a, "a-taaib")
        self.repo.commit(b, "b-taaib")
        self.repo.sync(a)
        head_b = _run(b, "git", "rev-parse", "HEAD").stdout.strip()
        r = self.repo.sync(b)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(_run(b, "git", "rev-parse", "HEAD").stdout.strip(), head_b)
        self.assertFalse((b / ".git" / "MERGE_HEAD").exists())
        st = json.loads((b / ".cache" / "sync.json").read_text())
        self.assertEqual(st["status"], "conflict")
        # side ref
        _run(b, "git", "fetch", "origin")
        refs = _run(b, "git", "ls-remote", "--heads", "origin", "sync/vps").stdout
        self.assertIn("sync/vps", refs)
        # a sees stuck
        r2 = self.repo.sync(a)
        st_a = json.loads((a / ".cache" / "sync.json").read_text())
        self.assertIn("vps", st_a.get("stuck_machines") or [])

    def test_no_remote_skipped(self):
        a = self.repo.a
        _run(a, "git", "remote", "remove", "origin")
        r = self.repo.sync(a)
        self.assertEqual(r.returncode, 0)
        self.assertIn("no-remote", r.stdout)

    def test_no_git_skipped(self):
        r = self.repo.sync(self.repo.a, env_extra={"MEMO_GIT_SNAPSHOT": "0"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("no-git", r.stdout)

    def test_merge_in_progress_skipped(self):
        a = self.repo.a
        # fabricate MERGE_HEAD
        (a / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n", encoding="utf-8")
        r = self.repo.sync(a)
        # D1: skipped merge-in-progress exits 1 (only no-remote/no-git/busy are 0)
        self.assertEqual(r.returncode, 1)
        self.assertIn("merge-in-progress", r.stdout)
        (a / ".git" / "MERGE_HEAD").unlink()

    def test_failing_since_and_clear(self):
        a = self.repo.a
        # unreachable remote
        _run(a, "git", "remote", "set-url", "origin", str(self.repo.tmp / "missing.git"))
        r = self.repo.sync(a)
        st = json.loads((a / ".cache" / "sync.json").read_text())
        self.assertEqual(st["status"], "retry")
        self.assertIsNotNone(st["failing_since"])
        fs = st["failing_since"]
        r2 = self.repo.sync(a)
        st2 = json.loads((a / ".cache" / "sync.json").read_text())
        self.assertEqual(st2["failing_since"], fs)
        # restore and succeed
        _run(a, "git", "remote", "set-url", "origin", str(self.repo.remote))
        r3 = self.repo.sync(a)
        st3 = json.loads((a / ".cache" / "sync.json").read_text())
        self.assertEqual(st3["status"], "ok")
        self.assertIsNone(st3["failing_since"])

    def test_obsidian_untouched(self):
        a = self.repo.a
        ob = a / ".obsidian" / "x.json"
        ob.parent.mkdir(exist_ok=True)
        ob.write_text("{}", encoding="utf-8")
        (a / "projects").mkdir(exist_ok=True)
        (a / "projects" / "z.md").write_text("# z\n", encoding="utf-8")
        self.repo.commit(a, "z")
        self.repo.sync(a)
        self.assertTrue(ob.exists())
        # not in last commit
        names = _run(a, "git", "show", "--name-only", "--pretty=", "HEAD").stdout
        self.assertNotIn(".obsidian", names)

    def test_overlap_harness(self):
        env = {
            **os.environ,
            "MEMO_VAULT": str(self.repo.a),
            "MEMO_HOME": str(self.repo.a / ".memo"),
            "PYTHONPATH": str(self.repo.a / "scripts"),
            "MEMO_AUTO_REINDEX": "0",
        }
        r = subprocess.run([sys.executable, str(ROOT / "tests" / "sync_harness.py"), "overlap"],
                           cwd=self.repo.a, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_lockfree_harness(self):
        env = {
            **os.environ,
            "MEMO_VAULT": str(self.repo.a),
            "MEMO_HOME": str(self.repo.a / ".memo"),
            "PYTHONPATH": str(self.repo.a / "scripts"),
            "MEMO_AUTO_REINDEX": "0",
        }
        r = subprocess.run([sys.executable, str(ROOT / "tests" / "sync_harness.py"), "lockfree"],
                           cwd=self.repo.a, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_req001_no_forbidden_in_live_tree(self):
        a = self.repo.a
        trace = self.repo.tmp / "git-trace.log"
        if trace.exists():
            trace.unlink()
        (a / "projects").mkdir(exist_ok=True)
        (a / "projects" / "t.md").write_text("# t\n", encoding="utf-8")
        self.repo.commit(a, "t")
        self.repo.sync(a)
        if not trace.exists():
            return
        forbidden = ("reset --hard", "push --force", "rebase", "stash drop")
        for line in trace.read_text().splitlines():
            cwd, _, cmd = line.partition("\t")
            if Path(cwd).resolve() != a.resolve():
                continue
            for f in forbidden:
                self.assertNotIn(f, cmd, line)
            # merge --abort only allowed in worktree
            if "merge --abort" in cmd:
                self.assertIn("sync-merge", cwd)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.repo = _SyncRepo()
        # Mac rejects incoming code
        mj = json.loads((self.repo.a / ".memo" / "machine.json").read_text())
        mj["accept_incoming_code"] = False
        (self.repo.a / ".memo" / "machine.json").write_text(json.dumps(mj))

    def tearDown(self):
        shutil.rmtree(self.repo.tmp, ignore_errors=True)

    def _push_from_b(self, rel: str, content: str, msg: str):
        b = self.repo.b
        path = b / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.repo.commit(b, msg)
        self.repo.sync(b)

    def test_vps_projects_merged(self):
        self._push_from_b("projects/x.md", "# x\n", "vps-proj")
        r = self.repo.sync(self.repo.a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.repo.a / "projects" / "x.md").exists())

    def test_vps_docs_held(self):
        self._push_from_b("docs/x.md", "# doc\n", "vps-docs")
        r = self.repo.sync(self.repo.a)
        st = json.loads((self.repo.a / ".cache" / "sync.json").read_text())
        self.assertEqual(st["status"], "skipped")
        self.assertIn("code-change", st["reason"])
        self.assertIsNotNone(st.get("held_sha"))

    def test_other_author_scripts_held(self):
        b = self.repo.b
        _run(b, "git", "config", "user.name", "Other")
        _run(b, "git", "config", "user.email", "other@test")
        (b / "scripts" / "x.py").write_text("print(1)\n", encoding="utf-8")
        self.repo.commit(b, "other-scripts")
        self.repo.sync(b)
        r = self.repo.sync(self.repo.a)
        st = json.loads((self.repo.a / ".cache" / "sync.json").read_text())
        self.assertIn("code-change", st["reason"])

    def test_readme_held(self):
        b = self.repo.b
        _run(b, "git", "config", "user.name", "Other")
        _run(b, "git", "config", "user.email", "other@test")
        (b / "README.md").write_text("# hi\n", encoding="utf-8")
        self.repo.commit(b, "readme")
        self.repo.sync(b)
        st = json.loads((self.repo.a / ".cache" / "sync.json").read_text()) if (self.repo.a / ".cache" / "sync.json").exists() else {}
        self.repo.sync(self.repo.a)
        st = json.loads((self.repo.a / ".cache" / "sync.json").read_text())
        self.assertIn("code-change", st["reason"])

    def test_instruction_basename_held(self):
        b = self.repo.b
        _run(b, "git", "config", "user.name", "Other")
        _run(b, "git", "config", "user.email", "other@test")
        (b / "subscriptions").mkdir(exist_ok=True)
        (b / "subscriptions" / "Claude.md").write_text("# c\n", encoding="utf-8")
        self.repo.commit(b, "claude-md")
        self.repo.sync(b)
        self.repo.sync(self.repo.a)
        st = json.loads((self.repo.a / ".cache" / "sync.json").read_text())
        self.assertIn("code-change", st["reason"])

    def test_accept_code_merges(self):
        self._push_from_b("docs/y.md", "# y\n", "vps-docs-y")
        self.repo.sync(self.repo.a)
        st = json.loads((self.repo.a / ".cache" / "sync.json").read_text())
        held = st["held_sha"]
        self.assertTrue(held)
        r = self.repo.sync(self.repo.a, "--accept-code", held)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.repo.a / "docs" / "y.md").exists())

    def test_accept_wrong_sha_exits_2(self):
        self._push_from_b("docs/z.md", "# z\n", "vps-docs-z")
        self.repo.sync(self.repo.a)
        r = self.repo.sync(self.repo.a, "--accept-code", "0" * 40)
        self.assertEqual(r.returncode, 2)

    def test_accept_incoming_true_merges(self):
        mj = json.loads((self.repo.a / ".memo" / "machine.json").read_text())
        mj["accept_incoming_code"] = True
        (self.repo.a / ".memo" / "machine.json").write_text(json.dumps(mj))
        self._push_from_b("docs/ok.md", "# ok\n", "docs-ok")
        r = self.repo.sync(self.repo.a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.repo.a / "docs" / "ok.md").exists())


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.repo = _SyncRepo()

    def tearDown(self):
        shutil.rmtree(self.repo.tmp, ignore_errors=True)

    def _make_conflict(self):
        a, b = self.repo.a, self.repo.b
        (a / "projects").mkdir(exist_ok=True)
        (b / "projects").mkdir(exist_ok=True)
        (a / "projects" / "taaib.md").write_text("# a\n", encoding="utf-8")
        (b / "projects" / "taaib.md").write_text("# b\n", encoding="utf-8")
        self.repo.commit(a, "a-taaib")
        self.repo.commit(b, "b-taaib")
        self.repo.sync(a)
        self.repo.sync(b)  # conflict on b

    def test_resolve_flow(self):
        self._make_conflict()
        b = self.repo.b
        env = {
            **os.environ,
            "MEMO_VAULT": str(b),
            "MEMO_HOME": str(b / ".memo"),
            "PYTHONPATH": str(b / "scripts"),
            "MEMO_AUTO_REINDEX": "0",
        }
        # resolve start
        r = subprocess.run([sys.executable, "-m", "memo_capture.sync", "resolve"],
                           cwd=b, env=env, capture_output=True, text=True)
        st = json.loads((b / ".cache" / "sync.json").read_text())
        self.assertIsNotNone(st.get("resolving"))
        wt = b / ".cache" / "sync-merge"
        self.assertTrue(wt.exists())
        # sync while resolving leaves worktree
        before = list(wt.rglob("*"))
        self.repo.sync(b)
        st2 = json.loads((b / ".cache" / "sync.json").read_text())
        self.assertEqual(st2["reason"], "resolving")
        # second resolve_start changes nothing
        r2 = subprocess.run([sys.executable, "-m", "memo_capture.sync", "resolve"],
                            cwd=b, env=env, capture_output=True, text=True)
        self.assertIn("already resolving", r2.stdout + r2.stderr)
        # fix conflict in worktree
        unmerged = _run(wt, "git", "diff", "--name-only", "--diff-filter=U", check=False).stdout.splitlines()
        for rel in unmerged:
            path = wt / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# resolved\n", encoding="utf-8")
            _run(wt, "git", "add", "--", rel)
        _run(wt, "git", "commit", "-m", "resolve", check=False)
        # live commit meanwhile
        (b / "projects" / "extra.md").write_text("# e\n", encoding="utf-8")
        self.repo.commit(b, "live-extra")
        # no markers in live tree
        for path in b.rglob("*.md"):
            if ".cache" in path.parts:
                continue
            self.assertNotIn("<<<<<<< ", path.read_text(encoding="utf-8", errors="replace"))
        r3 = subprocess.run([sys.executable, "-m", "memo_capture.sync", "resolve", "--finish"],
                            cwd=b, env=env, capture_output=True, text=True)
        st3 = json.loads((b / ".cache" / "sync.json").read_text())
        self.assertIsNone(st3.get("resolving"), msg=r3.stdout + r3.stderr + str(st3))
        for path in b.rglob("*.md"):
            if ".cache" in path.parts:
                continue
            self.assertNotIn("<<<<<<< ", path.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
