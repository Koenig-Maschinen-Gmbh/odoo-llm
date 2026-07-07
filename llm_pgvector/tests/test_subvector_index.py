"""Tests for high-dimensional (>4000) embedding indexing and search.

pgvector caps ANN indexes at 2000 dims (vector) / 4000 dims (halfvec).
For larger MRL-trained embeddings (e.g. Qwen3-Embedding-8B = 4096) the
store indexes the leading SUBVECTOR_INDEX_DIMS dimensions via an
expression index and re-ranks candidates exactly on the full vector.
"""

import math
import random

from odoo.tests import TransactionCase, tagged

from ..models.llm_store_pgvector import SUBVECTOR_INDEX_DIMS

DIMS = 4096


def _unit_vector(seed):
    rnd = random.Random(seed)
    vec = [rnd.uniform(-1.0, 1.0) for _ in range(DIMS)]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


@tagged("post_install", "-at_install")
class TestSubvectorIndex(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.store = cls.env["llm.store"].create(
            {"name": "Test PgVector Store", "service": "pgvector"}
        )
        cls.provider = cls.env["llm.provider"].create(
            {
                "name": "Subvector Test Provider",
                "service": "openai",
                "api_base": "https://test.example.com/v1",
                "api_key": "sk-test",
            }
        )
        cls.embedding_model = cls.env["llm.model"].create(
            {
                "name": "subvector-test-embedding",
                "provider_id": cls.provider.id,
                "model_use": "embedding",
            }
        )
        cls.collection = cls.env["llm.knowledge.collection"].create(
            {
                "name": "Subvector Test Collection",
                "embedding_model_id": cls.embedding_model.id,
                "store_id": cls.store.id,
            }
        )
        cls.resource = cls.env["llm.resource"].create(
            {
                "name": "Subvector Test Resource",
                "model_id": cls.env["ir.model"]._get_id("llm.knowledge.collection"),
                "res_id": cls.collection.id,
                "collection_ids": [(4, cls.collection.id)],
            }
        )
        cls.chunks = cls.env["llm.knowledge.chunk"].create(
            [
                {
                    "resource_id": cls.resource.id,
                    "content": f"chunk {i}",
                    "sequence": i,
                }
                for i in range(5)
            ]
        )

    def _insert_vectors(self, vectors):
        self.store.pgvector_insert_vectors(
            self.collection.id, vectors, ids=self.chunks.ids
        )

    def test_index_created_with_subvector_expression(self):
        """>4000-dim embeddings get a subvector expression index (the plain
        halfvec cast is above the 4000-dim cap and must NOT be attempted as
        the final result)."""
        self._insert_vectors([_unit_vector(i) for i in range(5)])
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            (
                self.store._get_index_name(
                    "llm_knowledge_chunk_embedding", self.embedding_model.id
                ),
            ),
        )
        row = self.env.cr.fetchone()
        self.assertTrue(row, "no ANN index was created for 4096-dim embeddings")
        indexdef = row[0]
        self.assertIn(f"subvector(embedding, 1, {SUBVECTOR_INDEX_DIMS})", indexdef)
        self.assertIn(f"halfvec({SUBVECTOR_INDEX_DIMS})", indexdef)

    def test_search_returns_exact_ranked_results(self):
        """The two-stage search returns the true nearest neighbour first,
        with the exact full-vector score."""
        base = _unit_vector(42)
        near = list(base)
        near[0] += 0.01  # tiny perturbation → nearest neighbour of base
        vectors = [near] + [_unit_vector(100 + i) for i in range(4)]
        self._insert_vectors(vectors)

        results = self.store.pgvector_search_vectors(
            self.collection.id, base, limit=3, min_similarity=0.0
        )
        self.assertTrue(results)
        self.assertEqual(
            results[0]["id"],
            self.chunks[0].id,
            "nearest neighbour must rank first after exact rerank",
        )
        self.assertGreater(results[0]["score"], 0.99)
        # scores strictly ordered
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_min_similarity_applies_to_exact_score(self):
        self._insert_vectors([_unit_vector(i) for i in range(5)])
        query = _unit_vector(9999)  # unrelated to all stored vectors
        results = self.store.pgvector_search_vectors(
            self.collection.id, query, limit=5, min_similarity=0.95
        )
        self.assertFalse(
            results, "random unit vectors must not pass a 0.95 similarity floor"
        )
