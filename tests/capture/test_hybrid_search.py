"""Tests for Zero-Cost Hybrid Search (RRF + Graph Alias Expansion) and observation hygiene."""

from __future__ import annotations

import json
import unittest

import tests.capture as tc
from memo_capture import parser, search, state, store


class TestHybridSearchAndHygiene(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = state.connect()
        self.conn.execute("DELETE FROM observations")
        self.conn.execute("DELETE FROM observation_files")
        self.conn.execute("DELETE FROM session_summaries")
        self.conn.execute("INSERT INTO observations_fts(observations_fts) VALUES ('rebuild')")
        self.conn.execute("INSERT INTO summaries_fts(summaries_fts) VALUES ('rebuild')")
        if state.has_trigram(self.conn):
            self.conn.execute("DELETE FROM memory_trigram")

        # Seed a bilingual entity with aliases into TEST_VAULT/graph/entities.json
        self._orig_graph = (tc.TEST_VAULT / "graph" / "entities.json").read_text(encoding="utf-8")
        graph_data = {
            "version": "2.2.0",
            "entities": [
                {
                    "id": "demo-repo",
                    "type": "Project",
                    "name": "Acme Corp",
                    "name_ar": "شركة تجريبية",
                    "aliases": ["Acme Alias", "Acme Studio"],
                    "file": "projects/demo-repo.md",
                }
            ],
            "relationships": [],
        }
        (tc.TEST_VAULT / "graph" / "entities.json").write_text(
            json.dumps(graph_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        search._GRAPH_CACHE["mtime"] = 0.0

    def tearDown(self) -> None:
        (tc.TEST_VAULT / "graph" / "entities.json").write_text(self._orig_graph, encoding="utf-8")
        search._GRAPH_CACHE["mtime"] = 0.0
        self.conn.close()

    def test_graph_alias_and_bilingual_expansion_in_rrf_search(self) -> None:
        sess = state.upsert_session(self.conn, "hyb-sess-1", project="demo-repo")
        obs = parser.Observation(
            type="decision",
            title="Migrated Acme Corp agency OS database",
            subtitle="Updated billing and client tables",
            facts=["Switched to Postgres pooler"],
            narrative="Completed database migration for Acme Corp.",
            concepts=["what-changed"],
            files_read=[],
            files_modified=["db/schema.sql"],
        )
        ids = store.save_observations(self.conn, sess, 1, [obs], tokens=100, model="test")
        self.assertEqual(len(ids), 1)

        # Querying by alias 'Acme Alias' or Arabic 'شركة تجريبية' should find the 'Acme Corp' observation!
        hits_alias = search.search(self.conn, "Acme Alias")
        self.assertTrue(any(h["id"] == ids[0] for h in hits_alias))

        hits_arabic = search.search(self.conn, "شركة تجريبية")
        self.assertTrue(any(h["id"] == ids[0] for h in hits_arabic))

    def test_delete_observation_and_deduplicate(self) -> None:
        sess = state.upsert_session(self.conn, "hyb-sess-2", project="demo-repo")
        obs1 = parser.Observation(
            type="bugfix",
            title="Fixed race condition in worker loop",
            subtitle=" Lock order ",
            facts=["Fact A"],
            narrative="N1",
            concepts=[],
            files_read=[],
            files_modified=[],
        )
        obs2 = parser.Observation(
            type="bugfix",
            title="Fixed race condition in worker loop",
            subtitle="Different subtitle",
            facts=["Fact B so content_hash differs"],
            narrative="N2",
            concepts=[],
            files_read=[],
            files_modified=[],
        )
        ids = store.save_observations(self.conn, sess, 1, [obs1, obs2], tokens=80, model="test")
        self.assertEqual(len(ids), 2)

        dups = search.find_duplicate_observations(self.conn)
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["keep_id"], ids[0])
        self.assertEqual(dups[0]["drop_ids"], [ids[1]])

        res = search.deduplicate_observations(self.conn)
        self.assertEqual(res["groups_merged"], 1)
        self.assertEqual(res["observations_removed"], 1)

        # Delete the remaining observation
        deleted = search.delete_observation(self.conn, ids[0])
        self.assertTrue(deleted)
        self.assertFalse(search.delete_observation(self.conn, ids[0]))
        self.assertEqual(len(search.search(self.conn, "race condition")), 0)


if __name__ == "__main__":
    unittest.main()
