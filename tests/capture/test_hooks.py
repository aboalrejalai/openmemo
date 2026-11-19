import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.capture import ROOT
from memo_capture import hooks, handlers
from memo_capture.adapters import HookResult, claude_code

LAUNCHER = ROOT / "plugin" / "bin" / "memo"


class DispatcherTest(unittest.TestCase):
    def test_garbage_stdin_returns_empty_object(self):
        self.assertEqual(hooks.run("claude-code", "observation", "not json"), {})

    def test_unknown_event_and_host_return_empty_object(self):
        self.assertEqual(hooks.run("claude-code", "nope", "{}"), {})
        self.assertEqual(hooks.run("nope", "observation", "{}"), {})

    def test_handler_exception_is_swallowed(self):
        with mock.patch.object(handlers, "observation", side_effect=RuntimeError("boom")):
            self.assertEqual(hooks.run("claude-code", "observation", "{}"), {})

    def test_observer_guard_skips_everything(self):
        called = mock.Mock(return_value=HookResult(additional_context="x"))
        with mock.patch.dict(os.environ, {"MEMO_OBSERVER": "1"}), \
                mock.patch.object(handlers, "context", called):
            self.assertEqual(hooks.run("claude-code", "context", "{}"), {})
        called.assert_not_called()

    def test_session_start_envelope(self):
        with mock.patch.object(handlers, "context",
                               return_value=HookResult(additional_context="ctx", system_message="hi")):
            out = hooks.run("claude-code", "context", json.dumps({"session_id": "s1", "cwd": "/tmp"}))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                      "additionalContext": "ctx"},
                               "systemMessage": "hi"})


class AdapterTest(unittest.TestCase):
    def test_failure_payload_becomes_error_response(self):
        inp = claude_code.normalize({"hook_event_name": "PostToolUseFailure", "error": "x",
                                     "is_interrupt": True, "tool_name": "Bash"}, "observation")
        self.assertEqual(inp.tool_response, {"error": "x", "is_interrupt": True})

    def test_project_dir_env_wins_over_cwd(self):
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/p"}):
            self.assertEqual(claude_code.normalize({"cwd": "/p/sub"}, "observation").cwd, "/p")

    def test_deny_only_on_pre_tool_use(self):
        r = HookResult(permission_decision="deny", permission_reason="big")
        self.assertEqual(claude_code.format_output(r, "observation"), {})
        self.assertEqual(claude_code.format_output(r, "file-context")["hookSpecificOutput"]["permissionDecision"], "deny")


class LauncherTest(unittest.TestCase):
    def test_launcher_always_exits_zero_with_json(self):
        env = dict(os.environ, MEMO_VAULT=str(ROOT))
        res = subprocess.run([str(LAUNCHER), "hook", "claude-code", "observation"],
                             input="{}", capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(json.loads(res.stdout), {})

    def test_launcher_with_missing_vault_still_answers(self):
        env = dict(os.environ, MEMO_VAULT="/nonexistent/memo")
        res = subprocess.run([str(LAUNCHER), "hook", "claude-code", "context"],
                             input="{}", capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "{}"))


if __name__ == "__main__":
    unittest.main()
