"""Gemini CLI, Antigravity, Windsurf/Devin and OpenCode: adapters and installers."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.capture import ROOT
from memo_capture import commands, handlers, hooks, state
from memo_capture.adapters import HookResult, antigravity, gemini, opencode, windsurf


class GeminiAdapterTest(unittest.TestCase):
    def test_payloads(self):
        start = gemini.normalize({"session_id": "g1", "cwd": "/p", "hook_event_name": "SessionStart",
                                  "source": "startup"}, "context")
        self.assertEqual((start.host, start.session_id, start.cwd, start.source), ("gemini", "g1", "/p", "startup"))
        tool = gemini.normalize({"session_id": "g1", "tool_name": "run_shell_command",
                                 "tool_input": {"command": "ls"}, "tool_response": {"llmContent": "a"}},
                                "observation")
        self.assertEqual((tool.tool_name, tool.tool_input, tool.tool_response),
                         ("run_shell_command", {"command": "ls"}, {"llmContent": "a"}))
        stop = gemini.normalize({"session_id": "g1", "prompt": "hi", "prompt_response": "Done."}, "summarize")
        self.assertEqual(stop.last_assistant_message, "Done.")
        self.assertIsNone(stop.transcript_path)   # Gemini's transcript is not JSONL

    def test_context_output(self):
        self.assertEqual(gemini.format_output(HookResult(additional_context="ctx"), "context"),
                         {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "ctx"}})
        self.assertEqual(gemini.format_output(HookResult(additional_context="x"), "session-init")
                         ["hookSpecificOutput"]["hookEventName"], "BeforeAgent")
        self.assertEqual(gemini.format_output(HookResult(), "observation"), {})


class AntigravityAdapterTest(unittest.TestCase):
    def test_payloads(self):
        inp = antigravity.normalize({"conversationId": "a1", "workspacePaths": ["/w"], "stepIdx": 4,
                                     "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}},
                                     "error": "", "transcriptPath": "/t.jsonl"}, "observation")
        self.assertEqual((inp.host, inp.session_id, inp.cwd, inp.tool_name, inp.tool_input, inp.tool_use_id),
                         ("antigravity", "a1", "/w", "run_command", {"CommandLine": "ls"}, "a1-4"))
        self.assertEqual(inp.transcript_path, "/t.jsonl")
        failed = antigravity.normalize({"conversationId": "a1", "toolCall": {"name": "x"}, "error": "boom"},
                                       "observation")
        self.assertEqual(failed.tool_response, {"error": "boom"})

    def test_context_is_an_injected_step(self):
        self.assertEqual(antigravity.format_output(HookResult(additional_context="ctx"), "context"),
                         {"injectSteps": [{"systemMessage": {"systemMessage": "ctx"}}]})
        self.assertEqual(antigravity.format_output(HookResult(), "summarize"), {})

    def test_pre_invocation_registers_the_prompt_once(self):
        p = Path(tempfile.mktemp(suffix=".jsonl"))
        p.write_text(json.dumps({"role": "user", "content": "Fix the login bug"}) + "\n"
                     + json.dumps({"role": "assistant", "content": "Looking."}) + "\n")
        raw = {"conversationId": "ag-p1", "workspacePaths": [str(ROOT)], "transcriptPath": str(p),
               "invocationNum": 1}
        with mock.patch("memo_capture.worker_client.ensure_started"):
            handlers.session_init(antigravity.normalize(raw, "session-init"))
            handlers.session_init(antigravity.normalize(dict(raw, invocationNum=2), "session-init"))
        from memo_capture import ingest, spool
        c = state.connect()
        spool.drain(c, lambda k, pl: ingest.enqueue_event(c, k, pl))
        rows = state.connect().execute("SELECT prompt_number, prompt_text FROM user_prompts "
                                       "WHERE content_session_id = 'ag-p1'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "Fix the login bug")])


class WindsurfAdapterTest(unittest.TestCase):
    def test_claude_style_payload_under_its_own_host(self):
        with mock.patch.dict(os.environ, {"DEVIN_PROJECT_DIR": "/proj"}):
            inp = windsurf.normalize({"hook_event_name": "PostToolUse", "session_id": "w1", "tool_name": "exec",
                                      "tool_input": {"command": "ls"}, "prompt_id": "p"}, "observation")
        self.assertEqual((inp.host, inp.cwd, inp.tool_name), ("windsurf", "/proj", "exec"))
        self.assertEqual(windsurf.format_output(HookResult(additional_context="c"), "session-init"),
                         {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "c"}})


class OpenCodeAdapterTest(unittest.TestCase):
    def test_tool_names_and_context(self):
        inp = opencode.normalize({"session_id": "o1", "cwd": "/c", "tool_name": "read",
                                  "tool_input": {"filePath": "/c/a"}, "tool_use_id": "call-1"}, "observation")
        self.assertEqual((inp.host, inp.tool_name, inp.tool_use_id), ("opencode", "Read", "call-1"))
        self.assertEqual(opencode.format_output(HookResult(additional_context="ctx"), "context"), {"context": "ctx"})
        self.assertEqual(opencode.format_output(HookResult(), "observation"), {})

    def test_errors_return_empty(self):
        for host in ("gemini", "antigravity", "windsurf", "opencode"):
            self.assertEqual(hooks.run(host, "observation", "not json"), {})

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_plugin_file_parses(self):
        res = subprocess.run(["node", "--check", str(ROOT / "plugin" / "opencode" / "memo.js")],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="memo-home-"))
        patches = [mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}),
                   mock.patch("memo_capture.paths.vault_dir", return_value=ROOT)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_gemini_keeps_other_hooks_and_is_idempotent(self):
        path = self.home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"theme": "x", "hooks": {"AfterTool": [
            {"matcher": "write_file", "hooks": [{"type": "command", "command": "lint.sh"}]}]}}))
        commands.cmd_install_gemini([])
        commands.cmd_install_gemini([])
        data = json.loads(path.read_text())
        self.assertEqual(data["theme"], "x")
        self.assertEqual(len(data["hooks"]["AfterTool"]), 2)
        self.assertEqual(data["hooks"]["AfterTool"][0]["hooks"][0]["command"], "lint.sh")
        self.assertIn("hook gemini observation", data["hooks"]["AfterTool"][1]["hooks"][0]["command"])
        self.assertEqual(sorted(data["hooks"]), ["AfterAgent", "AfterTool", "BeforeAgent", "SessionEnd",
                                                 "SessionStart"])
        self.assertTrue(data["mcpServers"]["memo"]["args"][0].endswith("scripts/mcp_server.py"))

    def test_antigravity_named_entry_and_mcp(self):
        hooks_path = self.home / ".gemini" / "config" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"other": {"Stop": []}}))
        commands.cmd_install_antigravity([])
        data = json.loads(hooks_path.read_text())
        self.assertEqual(sorted(data), ["memo", "other"])
        self.assertEqual(sorted(data["memo"]), ["PostToolUse", "PreInvocation", "SessionStart", "Stop"])
        self.assertIn("hook antigravity context", data["memo"]["SessionStart"][0]["command"])
        for mcp in ("config/mcp_config.json", "antigravity/mcp_config.json"):
            self.assertIn("memo", json.loads((self.home / ".gemini" / mcp).read_text())["mcpServers"])

    def test_windsurf_and_opencode(self):
        commands.cmd_install_windsurf([])
        devin = json.loads((self.home / ".config" / "devin" / "config.json").read_text())
        self.assertIn("hook windsurf session-init", devin["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
        commands.cmd_install_opencode([])
        plugin = (self.home / ".config" / "opencode" / "plugins" / "memo.js").read_text()
        self.assertNotIn("__MEMO_LAUNCHER__", plugin)
        self.assertIn(str(ROOT / "plugin" / "bin" / "memo"), plugin)
        conf = json.loads((self.home / ".config" / "opencode" / "opencode.json").read_text())
        self.assertEqual(conf["mcp"]["memo"]["type"], "local")

    def test_unreadable_config_is_left_alone(self):
        path = self.home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("// comment\n{}")
        self.assertEqual(commands.cmd_install_gemini([]), 1)
        self.assertEqual(path.read_text(), "// comment\n{}")


if __name__ == "__main__":
    unittest.main()
