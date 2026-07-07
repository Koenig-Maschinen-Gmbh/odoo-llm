18.0.1.2.0 (2026-07-07)
------------------------

* [KOENIG][ADD] Embeddings beyond pgvector's 4000-dim ``halfvec`` index cap
  (e.g. ``Qwen3-Embedding-8B`` = 4096) are now indexable: the ANN index is a
  ``subvector(embedding, 1, 2048)::halfvec(2048)`` expression index (valid
  for MRL/Matryoshka-trained models, which pack the dominant semantics into
  the leading dimensions), and ``pgvector_search_vectors`` runs a two-stage
  query — ANN on the subvector with 4x oversampling, then exact re-rank on
  the full-precision stored vector (``_pgvector_search_subvector_rerank``).
  ``min_similarity`` applies to the exact score. The stored column remains
  full-precision, full-dimension: index strategy changes (including this
  one) never require re-embedding. Previously 4096-dim models fell back to
  exact scan with an ERROR logged on every insert batch.
* [KOENIG][IMP] ``_create_vector_index`` checks index existence BEFORE
  probing dimensions and reads dimensions from an existing embedding row
  (``vector_dims``) instead of a live embedding API call when possible —
  the per-insert-batch call no longer hits the provider.
* [KOENIG][ADD] Tests: subvector index DDL, exact-ranked two-stage search,
  min-similarity on exact score (``tests/test_subvector_index.py``).

18.0.1.1.0 (2026-06-10)
------------------------

* [FIX] High-dimensional embeddings (> 2000 dims) can now be indexed. pgvector's
  ``ivfflat``/``hnsw`` indexes cap the ``vector`` type at 2000 dims, which made
  index creation fail (and abort the embedding transaction) for models such as
  ``text-embedding-3-large`` (3072) or ``Qwen3-Embedding`` (4096). The ANN index
  is now built on the half-precision ``halfvec`` cast for > 2000-dim models
  (pgvector >= 0.7.0, up to 4000 dims); the stored column stays full-precision
  ``vector``. ``pgvector_search_vectors`` casts the query to ``halfvec`` to match,
  so the index is used.
* [FIX] Index creation is wrapped in a SAVEPOINT and falls back ``hnsw`` ->
  ``ivfflat``: a failed/unsupported index can no longer poison the surrounding
  embedding transaction — it logs and falls back to exact search.
