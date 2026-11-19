import os
import unittest
from unittest import mock

from tests.capture import ROOT
from memo_capture import handlers, project, session_init, settings, state, store
from memo_capture.adapters import NormalizedInput
from memo_capture.parser import Observation


def prompt(sid, text, cwd=str(ROOT)):
    return NormalizedInput(host="claude-code", event="session-init", session_id=sid, cwd=cwd, prompt=text)


class SessionInitTest(unittest.TestCase):
    def setUp(self):
        settings.load(refresh=True)
        project.resolve.cache_clear()
        self.conn = state.connect()
        self.proj = project.resolve(str(ROOT))

    def test_numbers_increase_and_private_is_empty(self):
        session_init.register_prompt(prompt("si-1", "first"), self.proj)
        session_init.register_prompt(prompt("si-1", "<private>card 4111</private>"), self.proj)
        session_init.register_prompt(prompt("si-1", ""), self.proj)
        rows = self.conn.execute("SELECT prompt_number, prompt_text, private FROM user_prompts "
                                 "WHERE content_session_id = 'si-1' ORDER BY prompt_number").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "first", 0), (2, "", 1), (3, "[media prompt]", 0)])
        self.assertEqual(state.get_session(self.conn, "si-1")["project"], self.proj.name)

    def test_task_notification_is_not_a_prompt(self):
        with mock.patch("memo_capture.worker_client.ensure_started"):
            handlers.session_init(prompt("si-2", "<task-notification>agent done</task-notification>"))
        self.assertIsNone(state.get_session(self.conn, "si-2"))

    def test_semantic_inject_off_by_default_on_when_set(self):
        state.upsert_session(self.conn, "si-old", project=self.proj.name)
        store.save_observations(self.conn, state.get_session(self.conn, "si-old"), 1,
                                [Observation(type="decision", title="Decided invoice numbering scheme",
                                             facts=["invoices use INV-YYYY-NNN"])], tokens=1, model="m")
        self.assertIsNone(session_init.register_prompt(prompt("si-3", "change the invoice numbering"), self.proj))
        with mock.patch.dict(os.environ, {"MEMO_SEMANTIC_INJECT": "true"}):
            settings.load(refresh=True)
            res = session_init.register_prompt(prompt("si-4", "change the invoice numbering"), self.proj)
        settings.load(refresh=True)
        self.assertIn("Decided invoice numbering scheme", res.additional_context)
        self.assertTrue(res.additional_context.startswith("<memo-context>"))


class SpooledPromptTest(unittest.TestCase):
    """Prompts and summaries travel through the spool, so a locked index only delays them."""

    def setUp(self):
        settings.load(refresh=True)
        project.resolve.cache_clear()
        self.conn = state.connect()

    def drain(self):
        from memo_capture import ingest, spool
        return spool.drain(self.conn, lambda k, p: ingest.enqueue_event(self.conn, k, p))

    def test_locked_index_loses_nothing(self):
        locker = state.connect()
        locker.execute("BEGIN IMMEDIATE")              # the 2026-11-02 situation
        try:
            with mock.patch("memo_capture.worker_client.nudge"), \
                    mock.patch("memo_capture.worker_client.ensure_started"):
                handlers.session_init(prompt("sp-1", "first request"))
                handlers.observation(NormalizedInput(host="claude-code", event="observation", session_id="sp-1",
                                                     cwd=str(ROOT), tool_name="Bash", tool_use_id="sp-1-t1",
                                                     tool_input={"command": "ls"}, tool_response={}))
                handlers.summarize(NormalizedInput(host="claude-code", event="summarize", session_id="sp-1",
                                                   cwd=str(ROOT), last_assistant_message="Listed."))
                handlers.session_init(prompt("sp-1", "first request"))   # the same words again
        finally:
            locker.execute("ROLLBACK")
        self.drain()
        rows = self.conn.execute("SELECT prompt_number, prompt_text FROM user_prompts "
                                 "WHERE content_session_id = 'sp-1' ORDER BY prompt_number").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, "first request"), (2, "first request")])
        queued = self.conn.execute("SELECT message_type, prompt_number FROM pending_messages "
                                   "WHERE content_session_id = 'sp-1' ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in queued], [("observation", 1), ("summarize", 1)])

    def test_stop_and_session_end_give_one_summary(self):
        with mock.patch("memo_capture.worker_client.nudge"), mock.patch("memo_capture.worker_client.ensure_started"):
            handlers.session_init(prompt("sp-2", "do it"))
            for _ in range(2):
                handlers.summarize(NormalizedInput(host="claude-code", event="summarize", session_id="sp-2",
                                                   cwd=str(ROOT), last_assistant_message="Done."))
        self.drain()
        n = self.conn.execute("SELECT COUNT(*) FROM pending_messages WHERE content_session_id = 'sp-2' "
                              "AND message_type = 'summarize'").fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
