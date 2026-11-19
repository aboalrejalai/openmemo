"""Tests for scripts/memo_merge.py (plan 003 TASK-001)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import memo_merge  # noqa: E402
from memo_core import LIVING_HISTORY_HEADING  # noqa: E402


def _tl(body: str, *, updated: str = "2026-11-01", extra_fm: str = "") -> str:
    return (
        "---\n"
        "title: Timeline\n"
        f"updated: {updated}\n"
        f"{extra_fm}"
        "---\n"
        "# Timeline\n\n"
        "## Active\n\n"
        "| Date | Event |\n| --- | --- |\n| 2026-01-01 | x |\n\n"
        f"{LIVING_HISTORY_HEADING}\n"
        f"{body}"
        "## Superseded Entries\n\n"
        "- none\n"
    )


def _month(name: str, year: int, dates: str) -> str:
    return f"### {name} {year}\n{dates}\n"


def _date(d: str, *entries: str) -> str:
    lines = [f"- **{d}**"] + list(entries)
    return "\n".join(lines) + "\n"


def _session(cat: str, summary: str, link: str) -> str:
    return f"  - **{cat}:** {summary} → [[{link}]]"


def _fact(cat: str, summary: str) -> str:
    return f"  - **{cat}:** {summary}"


def _run_driver(mode: str, base: str, ours: str, theirs: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        bp, op, tp = t / "base", t / "ours", t / "theirs"
        bp.write_text(base, encoding="utf-8")
        op.write_text(ours, encoding="utf-8")
        tp.write_text(theirs, encoding="utf-8")
        code = memo_merge.main([mode, str(bp), str(op), str(tp)])
        return code, op.read_text(encoding="utf-8")


class TimelineModeTest(unittest.TestCase):
    def test_both_sides_add_under_one_date(self):
        base = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "base", "sessions/p/2026-11-10/aaaa"))))
        ours = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "base", "sessions/p/2026-11-10/aaaa"),
            _session("Session", "ours", "sessions/p/2026-11-10/bbbb"))))
        theirs = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "base", "sessions/p/2026-11-10/aaaa"),
            _session("Session", "theirs", "sessions/p/2026-11-10/cccc"))))
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertIn("sessions/p/2026-11-10/bbbb", out)
        self.assertIn("sessions/p/2026-11-10/cccc", out)
        self.assertIn("sessions/p/2026-11-10/aaaa", out)

    def test_rewrite_session_while_other_adds(self):
        base = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "old", "sessions/p/2026-11-10/aaaa"),
            _fact("Note", "keep"))))
        ours = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "new", "sessions/p/2026-11-10/aaaa"),
            _fact("Note", "keep"))))
        theirs = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "old", "sessions/p/2026-11-10/aaaa"),
            _fact("Note", "keep"),
            _session("Session", "added", "sessions/p/2026-11-10/bbbb"))))
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertIn("**Session:** new", out)
        self.assertNotIn("**Session:** old", out)
        self.assertIn("sessions/p/2026-11-10/bbbb", out)

    def test_correct_non_session_while_other_adds(self):
        base = _tl(_month("November", 2026, _date("2026-11-10",
            _fact("Decision", "old wording"))))
        ours = _tl(_month("November", 2026, _date("2026-11-10",
            _fact("Decision", "new wording"))))
        theirs = _tl(_month("November", 2026, _date("2026-11-10",
            _fact("Decision", "old wording"),
            _fact("Note", "extra"))))
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertIn("new wording", out)
        self.assertNotIn("old wording", out)
        self.assertIn("extra", out)

    def test_new_month_heading_inserted_in_date_order(self):
        base = _tl(_month("October", 2026, _date("2026-10-01", _fact("Note", "oct"))))
        ours = base
        theirs = _tl(
            _month("November", 2026, _date("2026-11-01", _fact("Note", "nov")))
            + _month("October", 2026, _date("2026-10-01", _fact("Note", "oct")))
        )
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertIn("### November 2026", out)
        self.assertLess(out.index("### November 2026"), out.index("### October 2026"))

    def test_updated_later_wins_quoted_and_unquoted(self):
        base = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))),
                   updated="2026-11-01")
        ours = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))),
                   updated='"2026-11-05"')
        theirs = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))),
                     updated="2026-11-03")
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertIn('updated: "2026-11-05"', out)

    def test_non_date_updated_conflicts(self):
        base = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))),
                   updated="2026-11-01")
        ours = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))),
                   updated="soon")
        theirs = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))),
                     updated="2026-11-03")
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ", out)

    def test_frontmatter_stamp_one_side_clean(self):
        base = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))))
        ours = base
        theirs = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))),
                     extra_fm="second-brain-synced: October 28, 2026\n")
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertIn("second-brain-synced:", out)

    def test_both_rewrite_same_session_differently_conflicts(self):
        base = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "old", "sessions/p/2026-11-10/aaaa"))))
        ours = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "ours", "sessions/p/2026-11-10/aaaa"))))
        theirs = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "theirs", "sessions/p/2026-11-10/aaaa"))))
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ", out)

    def test_remove_session_other_changed_conflicts(self):
        base = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "old", "sessions/p/2026-11-10/aaaa"))))
        ours = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "new", "sessions/p/2026-11-10/aaaa"))))
        theirs = _tl(_month("November", 2026, _date("2026-11-10",
            _fact("Note", "only"))))
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ", out)

    def test_duplicate_category_link_keeps_both(self):
        dup_a = _session("Session", "one", "sessions/p/2026-11-10/aaaa")
        dup_b = _session("Session", "two", "sessions/p/2026-11-10/aaaa")
        base = _tl(_month("November", 2026, _date("2026-11-10", dup_a, dup_b)))
        ours = _tl(_month("November", 2026, _date("2026-11-10", dup_a, dup_b,
            _fact("Note", "ours"))))
        theirs = _tl(_month("November", 2026, _date("2026-11-10", dup_a, dup_b,
            _fact("Note", "theirs"))))
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("sessions/p/2026-11-10/aaaa"), 2)
        self.assertIn("ours", out)
        self.assertIn("theirs", out)

    def test_fail_closed_when_keyed_merge_drops_entry(self):
        base = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "base", "sessions/p/2026-11-10/aaaa"))))
        ours = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "base", "sessions/p/2026-11-10/aaaa"),
            _session("Session", "ours", "sessions/p/2026-11-10/bbbb"))),
                   updated="2026-11-05")
        theirs = _tl(_month("November", 2026, _date("2026-11-10",
            _session("Session", "base", "sessions/p/2026-11-10/aaaa"),
            _session("Session", "theirs", "sessions/p/2026-11-10/cccc"))),
                     updated="2026-11-03")

        def drop_one(result_multi):
            if result_multi:
                result_multi = dict(result_multi)
                result_multi.pop(next(iter(result_multi)))
            return result_multi

        with mock.patch.object(memo_merge, "finalize_result_multi", side_effect=drop_one):
            code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ", out)


    def test_milestone_table_conflict_exits_1(self):
        base = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))))
        ours = base.replace("| 2026-01-01 | x |", "| 2026-01-01 | ours |")
        theirs = base.replace("| 2026-01-01 | x |", "| 2026-01-01 | theirs |")
        code, out = _run_driver("timeline", base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ", out)

    def test_hunk_straddling_living_history_exits_1(self):
        # Make a conflict that starts just before Living History and continues into it
        # by changing both the last mid-section line and the first LH line differently.
        base = _tl(_month("November", 2026, _date("2026-11-10", _fact("Note", "x"))))
        # Insert a unique marker line just before Living History
        marker = "PRE-LH-LINE\n\n"
        base2 = base.replace(f"{LIVING_HISTORY_HEADING}\n", marker + f"{LIVING_HISTORY_HEADING}\n")
        ours = base2.replace("PRE-LH-LINE", "PRE-LH-OURS").replace(
            _fact("Note", "x"), _fact("Note", "ours-entry"))
        theirs = base2.replace("PRE-LH-LINE", "PRE-LH-THEIRS").replace(
            _fact("Note", "x"), _fact("Note", "theirs-entry"))
        code, out = _run_driver("timeline", base2, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ", out)


class NoteModeTest(unittest.TestCase):
    def test_sb_stamp_and_updated_merge(self):
        base = textwrap.dedent("""\
            ---
            title: Note
            updated: 2026-11-01
            ---
            # Note

            body
            """)
        ours = textwrap.dedent("""\
            ---
            title: Note
            updated: 2026-11-05
            ---
            # Note

            body
            """)
        theirs = textwrap.dedent("""\
            ---
            title: Note
            updated: 2026-11-01
            second-brain-id:
              - aaa-111
              - bbb-222
            second-brain-synced: October 28, 2026 - 7:52 AM GMT+3
            ---
            # Note

            body
            """)
        code, out = _run_driver("note", base, ours, theirs)
        self.assertEqual(code, 0)
        self.assertIn("updated: 2026-11-05", out)
        self.assertIn("aaa-111", out)
        self.assertIn("second-brain-synced:", out)

    def test_body_conflict_exits_1(self):
        base = textwrap.dedent("""\
            ---
            title: Note
            updated: 2026-11-01
            ---
            # Note

            body
            """)
        ours = base.replace("body", "ours body")
        theirs = base.replace("body", "theirs body")
        code, out = _run_driver("note", base, ours, theirs)
        self.assertEqual(code, 1)
        self.assertIn("<<<<<<< ", out)


class GitattributesTest(unittest.TestCase):
    def _init_repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        repo.mkdir()
        # copy scripts needed for driver
        shutil.copytree(ROOT / "scripts", repo / "scripts")
        (repo / ".gitattributes").write_text(
            "*.md merge=memo-note\n"
            "sessions/current.md merge=union\n"
            "timeline.md merge=memo-timeline\n",
            encoding="utf-8",
        )
        (repo / "sessions").mkdir()
        env = {**os.environ, "MEMO_VAULT": str(repo), "GIT_CONFIG_NOSYSTEM": "1"}
        def g(*args):
            return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env)
        r = subprocess.run(["git", "init"], cwd=repo, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout)
        g("checkout", "-B", "main")
        g("config", "user.email", "t@memo.local")
        g("config", "user.name", "memo-test")
        # write seed files
        (repo / "timeline.md").write_text(_tl(_month("November", 2026, _date(
            "2026-11-10", _fact("Note", "seed")))), encoding="utf-8")
        (repo / "sessions" / "current.md").write_text(
            "---\ntitle: current\nupdated: 2026-11-01\n---\n\nline-base\n", encoding="utf-8")
        g("add", "-A")
        g("commit", "-m", "seed")
        # ensure_git_config via memo_core with MEMO_VAULT
        subprocess.run(
            [sys.executable, "-c",
             "import os,sys; sys.path.insert(0,'scripts'); "
             "import importlib; import memo_core; "
             # re-import under vault — memo_core reads VAULT_DIR at import
             "os.environ['MEMO_VAULT']=os.environ['MEMO_VAULT']; "
             ],
            cwd=repo, env=env, check=False)
        # Directly set drivers the way ensure_git_config does, after importing under env
        code = (
            "import os, sys\n"
            "sys.path.insert(0, 'scripts')\n"
            # Force re-read: memo_core binds VAULT_DIR at import from env
            "import memo_core\n"
            "memo_core.ensure_git_config()\n"
            "memo_core.ensure_git_config()\n"
            "import subprocess\n"
            "for name in ('memo-timeline','memo-note'):\n"
            "  r=subprocess.run(['git','config','--local','--get-all',f'merge.{name}.driver'],\n"
            "                   capture_output=True,text=True)\n"
            "  lines=[l for l in r.stdout.splitlines() if l.strip()]\n"
            "  assert len(lines)==1, (name, lines)\n"
            "print('drivers-ok')\n"
        )
        # Need a fresh interpreter with MEMO_VAULT set BEFORE import
        # memo_core reads env at module level — check how
        r = subprocess.run([sys.executable, "-c", code], cwd=repo, env=env,
                           capture_output=True, text=True)
        self.assertIn("drivers-ok", r.stdout, msg=r.stderr)
        return repo

    def test_timeline_and_current_merge_and_idempotent_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._init_repo(Path(tmp))
            env = {**os.environ, "MEMO_VAULT": str(repo), "GIT_CONFIG_NOSYSTEM": "1"}

            def g(*args, check=True):
                return subprocess.run(["git", *args], cwd=repo, check=check,
                                      capture_output=True, text=True, env=env)

            # Branch A: append timeline + current
            g("checkout", "-b", "a")
            tl = (repo / "timeline.md").read_text(encoding="utf-8")
            (repo / "timeline.md").write_text(tl.replace(
                _fact("Note", "seed"),
                _fact("Note", "seed") + "\n" + _fact("Note", "from-a"),
            ), encoding="utf-8")
            cur = (repo / "sessions" / "current.md").read_text(encoding="utf-8")
            (repo / "sessions" / "current.md").write_text(cur + "line-a\n", encoding="utf-8")
            g("add", "-A")
            g("commit", "-m", "a")

            # Branch B from seed
            g("checkout", "main")
            g("checkout", "-b", "b")
            tl = (repo / "timeline.md").read_text(encoding="utf-8")
            (repo / "timeline.md").write_text(tl.replace(
                _fact("Note", "seed"),
                _fact("Note", "seed") + "\n" + _fact("Note", "from-b"),
            ), encoding="utf-8")
            cur = (repo / "sessions" / "current.md").read_text(encoding="utf-8")
            (repo / "sessions" / "current.md").write_text(cur + "line-b\n", encoding="utf-8")
            g("add", "-A")
            g("commit", "-m", "b")

            # Merge a into b
            r = g("merge", "a", "--no-edit", check=False)
            self.assertEqual(r.returncode, 0, msg=r.stdout + r.stderr)
            tl = (repo / "timeline.md").read_text(encoding="utf-8")
            self.assertIn("from-a", tl)
            self.assertIn("from-b", tl)
            cur = (repo / "sessions" / "current.md").read_text(encoding="utf-8")
            self.assertIn("line-a", cur)
            self.assertIn("line-b", cur)


if __name__ == "__main__":
    unittest.main()
