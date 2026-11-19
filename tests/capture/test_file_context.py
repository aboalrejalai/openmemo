import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.capture import ROOT  # noqa: F401
from memo_capture import file_context, project, settings, state, store
from memo_capture.adapters import NormalizedInput
from memo_capture.parser import Observation


class FileContextTest(unittest.TestCase):
    def setUp(self):
        settings.load(refresh=True)
        project.resolve.cache_clear()
        self.conn = state.connect()
        self.repo = Path(tempfile.mkdtemp(prefix="memo-fc-")) / "fc-repo"
        (self.repo / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.big = self.repo / "src" / "engine.py"
        self.big.write_text("x = 1\n" * 7000)        # ~42 KB
        state.upsert_session(self.conn, "fc-old", project="fc-repo")
        s = state.get_session(self.conn, "fc-old")
        store.save_observations(self.conn, s, 1, [Observation(type="bugfix", title="Fixed engine loop",
                                                              files_modified=["src/engine.py"])], tokens=1, model="m")

    def inp(self, tool="Read", sid="fc-new", **tool_input):
        ti = {"file_path": str(self.big)}
        ti.update(tool_input)
        return NormalizedInput(host="claude-code", event="file-context", session_id=sid, cwd=str(self.repo),
                               tool_name=tool, tool_input=ti)

    def test_history_injected_once_per_session(self):
        first = file_context.pre_tool_use(self.inp())
        self.assertIn("Fixed engine loop", first.additional_context)
        self.assertIn("not the file's current contents", first.additional_context)
        self.assertIsNone(file_context.pre_tool_use(self.inp(tool="Edit")))
        self.assertIsNotNone(file_context.pre_tool_use(self.inp(sid="fc-other")))

    def test_small_unknown_and_subagent_files_get_nothing(self):
        small = self.repo / "src" / "tiny.py"
        small.write_text("y = 2\n")
        self.assertIsNone(file_context.pre_tool_use(self.inp(file_path=str(small))))
        self.assertIsNone(file_context.pre_tool_use(self.inp(file_path=str(self.repo / "missing.py"))))
        sub = self.inp(sid="fc-sub")
        sub.agent_id, sub.agent_type = "a1", "Explore"
        self.assertIsNone(file_context.pre_tool_use(sub))

    def test_gate_is_off_by_default_and_exact_when_on(self):
        self.assertIsNone(file_context.pre_tool_use(self.inp(sid="fc-gate-0")).permission_decision)
        with mock.patch.dict(os.environ, {"MEMO_FILE_READ_GATE_ENABLED": "true"}):
            settings.load(refresh=True)
            denied = file_context.pre_tool_use(self.inp(sid="fc-gate-1"))
            self.assertEqual(denied.permission_decision, "deny")
            self.assertIn("Grep", denied.permission_reason)
            ranged = file_context.pre_tool_use(self.inp(sid="fc-gate-2", offset=100, limit=50))
            self.assertIsNone(ranged.permission_decision)
            edit = file_context.pre_tool_use(self.inp(tool="Edit", sid="fc-gate-3"))
            self.assertIsNone(edit.permission_decision)
        settings.load(refresh=True)


if __name__ == "__main__":
    unittest.main()
