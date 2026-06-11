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
