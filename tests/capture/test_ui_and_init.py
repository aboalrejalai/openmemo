"""Tests for Memo Sovereign Studio (memo_ui.py) JSON endpoints and memo init command."""

from __future__ import annotations

import unittest

import tests.capture as tc
import memo_ui
from memo_capture import cli, parser, state, store


class TestMemoUIAndInit(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = state.connect()
        sess = state.upsert_session(self.conn, "ui-sess-1", project="demo-repo")
        obs = parser.Observation(
            type="feature",
            title="Built interactive Canvas Knowledge Graph in memo_ui",
            subtitle="Supports physics, radial, and activity clustering",
            facts=["Zero npm dependencies", "60fps Canvas rendering"],
            narrative="Added full graph and hybrid RRF search studio.",
            concepts=["how-it-works"],
            files_read=["graph/entities.json"],
            files_modified=["scripts/memo_ui.py"],
        )
        store.save_observations(self.conn, sess, 1, [obs], tokens=120, model="test")

    def tearDown(self) -> None:
        self.conn.close()

    def test_ui_status_and_graph_and_search_payloads(self) -> None:
        st = memo_ui.build_status_payload()
        self.assertEqual(st["search_mode"], "hybrid-rrf")
        self.assertGreaterEqual(st["capture"]["total_observations"], 1)

        graph = memo_ui.build_graph_payload()
        self.assertGreaterEqual(graph["node_count"], 1)
        self.assertTrue(any(n["label"] == "Demo Repo" or "demo-repo" in n["id"] for n in graph["nodes"]))

        feed = memo_ui.build_feed_payload(limit=20, project="demo-repo")
        self.assertGreaterEqual(feed["count"], 1)

        srch = memo_ui.build_search_payload("Canvas Knowledge Graph")
        self.assertEqual(srch["search_mode"], "hybrid-rrf")
        self.assertGreaterEqual(len(srch["ranked_fusion"]), 1)

        dossier = memo_ui.build_dossier_payload("projects/demo-repo.md")
        self.assertEqual(dossier["path"], "projects/demo-repo.md")
        self.assertIn("Demo Repo", dossier["content"])

    def test_cli_help_lists_init_and_ui(self) -> None:
        self.assertEqual(cli.main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
