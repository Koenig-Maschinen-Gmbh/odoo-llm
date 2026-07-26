"""KOENIG fork: embedding-search failure stash (B1 honest degradation).

``_generate_embeddings_for_collections`` must record each failed embedding
model in a per-thread stash (and log a warning) instead of silently
swallowing the error — consumers pop the stash to distinguish "no matches"
from "retrieval degraded".
"""

from unittest import mock

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEmbeddingSearchStash(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Chunk = cls.env["llm.knowledge.chunk"]
        cls.provider = cls.env["llm.provider"].create(
            {
                "name": "Stash Test Provider",
                "service": "openai",
                "api_base": "https://test.example.com/v1",
                "api_key": "sk-test",
            }
        )
        cls.model_a = cls.env["llm.model"].create(
            {
                "name": "embed-a",
                "provider_id": cls.provider.id,
                "model_use": "embedding",
            }
        )
        cls.model_b = cls.env["llm.model"].create(
            {
                "name": "embed-b",
                "provider_id": cls.provider.id,
                "model_use": "embedding",
            }
        )
        cls.store = cls.env["llm.store"].create(
            {"name": "Stash Test Store", "service": "pgvector"}
        )
        cls.coll_a = cls.env["llm.knowledge.collection"].create(
            {
                "name": "Coll A",
                "embedding_model_id": cls.model_a.id,
                "store_id": cls.store.id,
            }
        )
        cls.coll_b = cls.env["llm.knowledge.collection"].create(
            {
                "name": "Coll B",
                "embedding_model_id": cls.model_b.id,
                "store_id": cls.store.id,
            }
        )

    def test_full_failure_stashes_and_pops(self):
        with mock.patch.object(
            type(self.model_a), "embedding", side_effect=Exception("401 Unauthorized")
        ):
            vector_map, remaining = self.Chunk._generate_embeddings_for_collections(
                self.coll_a, "maintenance"
            )
        self.assertEqual(vector_map, {})
        self.assertFalse(remaining, "failed model's collection must be filtered out")

        stashed = self.Chunk._pop_embedding_search_errors()
        self.assertTrue(stashed["full_failure"])
        self.assertEqual(stashed["total"], 1)
        self.assertEqual(stashed["failed"][0]["model_id"], self.model_a.id)
        self.assertEqual(stashed["failed"][0]["error"], "401 Unauthorized")
        # Pop clears the stash — a second pop sees nothing.
        self.assertIsNone(self.Chunk._pop_embedding_search_errors())

    def test_partial_failure_not_full(self):
        def _fake_embedding(model, text):
            if model.id == self.model_b.id:
                raise Exception("timeout")
            return [[0.1, 0.2, 0.3]]

        with mock.patch.object(type(self.model_a), "embedding", new=_fake_embedding):
            vector_map, remaining = self.Chunk._generate_embeddings_for_collections(
                self.coll_a | self.coll_b, "maintenance"
            )
        self.assertIn(self.model_a.id, vector_map)
        self.assertEqual(remaining, self.coll_a)

        stashed = self.Chunk._pop_embedding_search_errors()
        self.assertFalse(stashed["full_failure"])
        self.assertEqual(stashed["total"], 2)
        self.assertEqual(len(stashed["failed"]), 1)
        self.assertEqual(stashed["failed"][0]["model_id"], self.model_b.id)

    def test_success_clears_stale_stash(self):
        """A successful search resets the stash — a caller must never see
        leftovers from a previous failed search on the same thread."""
        with mock.patch.object(
            type(self.model_a), "embedding", side_effect=Exception("401")
        ):
            self.Chunk._generate_embeddings_for_collections(self.coll_a, "x")
        self.assertIsNotNone(self.Chunk._pop_embedding_search_errors())

        with mock.patch.object(
            type(self.model_a), "embedding", return_value=[[0.1, 0.2]]
        ):
            vector_map, remaining = self.Chunk._generate_embeddings_for_collections(
                self.coll_a, "x"
            )
        self.assertIn(self.model_a.id, vector_map)
        self.assertEqual(remaining, self.coll_a)
        self.assertIsNone(self.Chunk._pop_embedding_search_errors())
