import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.capture import ROOT  # noqa: F401
from memo_capture import hooks, state
from memo_capture.adapters import HookResult, codex, cursor


class CodexAdapterTest(unittest.TestCase):
    def test_normalize_and_session_start_output(self):
        inp = codex.normalize({"hook_event_name": "Stop", "session_id": "c1", "cwd": "/p",
                               "last_assistant_message": "Done.", "turn_id": "t"}, "summarize")
        self.assertEqual((inp.host, inp.session_id, inp.last_assistant_message), ("codex", "c1", "Done."))
        out = codex.format_output(HookResult(additional_context="ctx"), "context",
                                  codex.normalize({"hook_event_name": "SessionStart"}, "context"))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "ctx"}})

    def test_post_compact_uses_system_message(self):
        inp = codex.normalize({"hook_event_name": "PostCompact", "session_id": "c2"}, "context")
        self.assertEqual(codex.format_output(HookResult(additional_context="ctx"), "context", inp),
                         {"systemMessage": "ctx"})

    def test_hooks_file_covers_codex_events(self):
        data = json.loads((ROOT / "plugin" / "hooks" / "codex-hooks.json").read_text())
        self.assertEqual(sorted(data["hooks"]), ["PostCompact", "PostToolUse", "PreToolUse", "SessionStart",
                                                 "Stop", "UserPromptSubmit"])
        self.assertIn("hook codex observation", data["hooks"]["PostToolUse"][0]["hooks"][0]["command"])


class CursorAdapterTest(unittest.TestCase):
    def test_shell_edit_and_mcp_payloads(self):
        shell = cursor.normalize({"conversation_id": "k1", "workspace_roots": ["/w"], "command": "ls",
                                  "output": "a"}, "observation")
        self.assertEqual((shell.tool_name, shell.tool_input, shell.tool_response, shell.cwd),
                         ("Bash", {"command": "ls"}, {"output": "a"}, "/w"))
        edit = cursor.normalize({"conversation_id": "k1", "file_path": "/w/a.py", "edits": [{"old": "a"}]},
                                "observation")
        self.assertEqual(edit.tool_name, "Edit")
        mcp = cursor.normalize({"conversation_id": "k1", "tool_name": "mcp_x", "tool_input": {"q": 1},
                                "result_json": {"ok": True}}, "observation")
        self.assertEqual((mcp.tool_name, mcp.tool_response), ("mcp_x", {"ok": True}))

    def test_context_goes_to_an_excluded_rule_file(self):
        repo = Path(tempfile.mkdtemp(prefix="memo-cursor-"))
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        inp = cursor.normalize({"conversation_id": "k2", "workspace_roots": [str(repo)]}, "context")
        out = cursor.format_output(HookResult(additional_context="<memo-context>x</memo-context>"), "context", inp)
        self.assertEqual(out, {"continue": True})
        rule = repo / ".cursor" / "rules" / "memo-context.mdc"
        self.assertIn("alwaysApply: true", rule.read_text())
        self.assertIn(".cursor/rules/memo-context.mdc", (repo / ".git" / "info" / "exclude").read_text())
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "")

    def test_memo_own_tools_are_not_recorded(self):
        from unittest import mock
        from memo_capture import handlers, spool
        before = set(spool.spool_dir().glob("*.json"))
        with mock.patch("memo_capture.worker_client.nudge"):
            for name in ("memo_search", "mcp__memo__memo_get_observations"):
                handlers.observation(cursor.normalize({"conversation_id": "own-1", "tool_name": name,
                                                       "workspace_roots": [str(ROOT)]}, "observation"))
        self.assertEqual(set(spool.spool_dir().glob("*.json")), before)

    def test_errors_still_continue(self):
        self.assertEqual(hooks.run("cursor", "observation", "not json"), {"continue": True})


class CursorTranscriptTest(unittest.TestCase):
    LINES = [
        {"role": "user", "message": {"content": [{"type": "text", "text":
            "<timestamp>Wednesday</timestamp>\n<user_query>\nAdd multiply to calc.py\n</user_query>"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "Adding it."},
                                                      {"type": "tool_use", "name": "Shell", "input": {}}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "Added multiply."}]}},
        {"type": "turn_ended", "status": "success"},
    ]

    def path(self):
        p = tempfile.mktemp(suffix=".jsonl")
        Path(p).write_text("\n".join(json.dumps(l) for l in self.LINES) + "\n")
        return p

    def test_cursor_lines_are_read(self):
        from memo_capture import transcript
        p = self.path()
        self.assertEqual(transcript.last_user_prompt(p), "Add multiply to calc.py")
        self.assertEqual(transcript.last_assistant_turn(p)[0], "Adding it.\n\nAdded multiply.")

    def test_headless_cursor_registers_the_prompt_once(self):
        from memo_capture import handlers
        from unittest import mock
        p = self.path()
        payload = {"conversation_id": "cur-h1", "workspace_roots": [str(ROOT)], "command": "ls", "output": "x",
                   "transcript_path": p, "tool_use_id": "t1"}
        with mock.patch("memo_capture.worker_client.nudge"):
            handlers.observation(cursor.normalize(payload, "observation"))
            handlers.observation(cursor.normalize(dict(payload, tool_use_id="t2"), "observation"))
        from memo_capture import ingest, spool
        c = state.connect()
        spool.drain(c, lambda k, pl: ingest.enqueue_event(c, k, pl))
        conn = state.connect()
        rows = conn.execute("SELECT prompt_number, prompt_text FROM user_prompts WHERE content_session_id = 'cur-h1'"
                            ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "Add multiply to calc.py")])


class IdleWrapUpTest(unittest.TestCase):
    def test_quiet_codex_session_gets_a_session_end(self):
        from memo_capture.worker import Worker
        w = Worker()
        state.upsert_session(w.conn, "codex-idle-1", project="demo-repo", host="codex")
        w.conn.execute("UPDATE sessions SET updated_at = 0 WHERE content_session_id = 'codex-idle-1'")
        w.conn.commit()
        w.wrap_up_idle()
        kinds = [r[0] for r in w.conn.execute("SELECT message_type FROM pending_messages "
                                              "WHERE content_session_id = 'codex-idle-1'")]
        self.assertEqual(kinds, ["session_end"])   # no transcript: nothing to summarise
        w.wrap_up_idle()   # not twice
        self.assertEqual(w.conn.execute("SELECT COUNT(*) FROM pending_messages WHERE "
                                        "content_session_id = 'codex-idle-1'").fetchone()[0], 1)
        w.pool.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
