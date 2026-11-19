import unittest

from tests.capture import ROOT  # noqa: F401
from memo_capture import context, settings, state, store
from memo_capture.adapters import NormalizedInput
from memo_capture.parser import Observation, Summary


class ContextTest(unittest.TestCase):
    def setUp(self):
        settings.load(refresh=True)
        self.conn = state.connect()

    def fill(self, project, n_obs, long=False):
        sid = f"ctx-{project}"
        state.upsert_session(self.conn, sid, project=project)
        s = state.get_session(self.conn, sid)
        for i in range(n_obs):
            store.save_observations(self.conn, s, 1, [Observation(
                type="feature" if i % 2 else "bugfix", title=f"{project} item {i} " + ("x" * 260 if long else ""),
                facts=[f"fact {i}"], narrative="n" * (400 if long else 10))], tokens=500, model="m")
        store.save_summary(self.conn, s, 1, Summary(request=f"Build {project}", completed="Phase 1 done",
                                                    next_steps="Phase 2"), tokens=300, model="m")

    def test_empty_project_prints_nothing(self):
        self.assertEqual(context.build(self.conn, ["ctx-nothing-here"], "x")[0], "")

    def test_layout(self):
        self.fill("ctx-small", 3)
        text, n_obs, n_sessions = context.build(self.conn, ["ctx-small"], "ctx-small")
        self.assertTrue(text.startswith("<memo-context>\n# [ctx-small] recent context"))
        self.assertIn("Legend: 🎯session ●bugfix ◆feature", text)
        self.assertRegex(text, r"\n\d+ \d\d:\d\d ◆ ctx-small item 1")
        self.assertIn("🎯 Build ctx-small", text)
        self.assertIn("**Next Steps**: Phase 2", text)
        self.assertIn("memo_get_observations", text)
        self.assertEqual((n_obs, n_sessions), (3, 1))

    def test_budget_is_never_exceeded(self):
        self.fill("ctx-big", 50, long=True)
        text, n_obs, _ = context.build(self.conn, ["ctx-big"], "ctx-big")
        self.assertLessEqual(len(text), 10_000)
        self.assertLess(n_obs, 50)
        self.assertTrue(text.endswith("</memo-context>"))
        mid, _, _ = context.build(self.conn, ["ctx-big"], "ctx-big", budget=4_000)
        self.assertIn("Latest summary", mid)            # kept while observations are trimmed
        tiny, _, _ = context.build(self.conn, ["ctx-big"], "ctx-big", budget=600)
        self.assertLessEqual(len(tiny), 600)

    def test_projects_are_separated_and_hook_output(self):
        self.fill("ctx-a", 2)
        self.fill("ctx-b", 2)
        text, _, _ = context.build(self.conn, ["ctx-a"], "ctx-a")
        self.assertNotIn("ctx-b item", text)
        res = context.session_start(NormalizedInput(host="claude-code", event="context", session_id="s",
                                                    cwd=str(ROOT)))
        # ROOT resolves to the "memo" project, which has no memory in the test state.
        self.assertIsNone(res)


class WarningAndRepoTest(unittest.TestCase):
    def setUp(self):
        settings.load(refresh=True)

    def test_stuck_spool_shows_a_warning_even_without_memory(self):
        import json, os, time
        from memo_capture import health, spool
        path = spool.write_event("observation", {"content_session_id": "ctx-stuck", "tool_use_id": "stuck-1"})
        old = time.time() - 3 * 3600
        os.utime(path, (old, old))
        try:
            res = context.session_start(NormalizedInput(host="claude-code", event="context", session_id="s",
                                                        cwd=str(ROOT)))
            self.assertIn("Memo is not recording", res.system_message)
            self.assertIn("Nothing is lost", res.additional_context)
        finally:
            path.unlink()
        self.assertIsNone(health.warning())

    def test_index_that_cannot_open_is_reported(self):
        from unittest import mock
        with mock.patch.object(state, "connect", side_effect=RuntimeError("database is locked")):
            res = context.session_start(NormalizedInput(host="claude-code", event="context", session_id="s",
                                                        cwd=str(ROOT)))
        self.assertIn("cannot be opened (RuntimeError: database is locked)", res.system_message)

    def test_repo_state_lists_branch_and_commits(self):
        import subprocess, tempfile
        repo = tempfile.mkdtemp(prefix="ctx-repo-")
        run = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
        run("init", "-q", "-b", "feat/x")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "TASK-004 shell")
        text = context.repo_state(repo)
        self.assertIn("Branch: feat/x · 0 uncommitted file(s)", text)
        self.assertIn("TASK-004 shell", text)
        self.assertEqual(context.repo_state(tempfile.mkdtemp()), "")

    def test_context_is_sent_on_resume(self):
        import json
        for name in ("hooks.json", "codex-hooks.json"):
            hooks = json.loads((ROOT / "plugin" / "hooks" / name).read_text())["hooks"]["SessionStart"]
            text = lambda h: " ".join([h["command"], *h.get("args", [])])
            context_groups = [g for g in hooks if any(text(h).endswith(" context") or "context'" in text(h)
                                                      for h in g["hooks"])]
            self.assertTrue(context_groups, name)
            for g in context_groups:
                self.assertIn("resume", g["matcher"].split("|"), name)



class SyncContextTest(unittest.TestCase):
    def test_session_start_includes_sync_warning(self):
        import json, os
        from pathlib import Path
        from memo_capture import context
        from memo_capture.adapters import NormalizedInput
        cache = Path(os.environ["MEMO_VAULT"]) / ".cache"
        cache.mkdir(exist_ok=True)
        (cache / "sync.json").write_text(json.dumps({
            "status": "conflict", "reason": "x", "conflict_paths": ["projects/taaib.md"],
            "stuck_machines": [], "adopt_errors": [],
        }), encoding="utf-8")
        res = context.session_start(NormalizedInput(
            host="claude-code", event="context", session_id="s",
            cwd="/tmp/no-such-project-xyz-memo"))
        self.assertIsNotNone(res)
        self.assertIn("Memo sync:", res.system_message or "")


if __name__ == "__main__":
    unittest.main()
