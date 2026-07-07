import logging

from pgvector import Vector
from pgvector.psycopg2 import register_vector

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ANN dimensionality for embeddings beyond pgvector's 4000-dim halfvec index
# cap (e.g. Qwen3-Embedding-8B = 4096): index only the LEADING dims via a
# subvector expression index and re-rank candidates on the full-precision
# vector. Valid for MRL (Matryoshka) trained models, which pack the dominant
# semantics into the leading dimensions by design (Qwen3 trains 512/1024/
# 2048/4096 checkpoints). 2048 matches a trained checkpoint and stays well
# inside the halfvec cap. The STORED vector remains full-precision and
# full-dimension — changing this constant only requires an index rebuild
# (force=True), never re-embedding.
SUBVECTOR_INDEX_DIMS = 2048

# ANN oversampling for the subvector two-stage search: fetch N× the requested
# rows from the truncated-dims index, then re-rank exactly on the full vector.
SUBVECTOR_OVERSAMPLE = 4


class LLMStorePgVector(models.Model):
    _inherit = "llm.store"
    _description = "PgVector Store Implementation"

    @api.model
    def _get_available_services(self):
        services = super()._get_available_services()
        return services + [("pgvector", "PGVector")]

    # PgVector specific configuration options
    pgvector_index_method = fields.Selection(
        [
            ("ivfflat", "IVFFlat (faster search)"),
            ("hnsw", "HNSW (balanced)"),
        ],
        string="Index Method",
        default="ivfflat",
        help="The index method to use for vector search",
    )

    # -------------------------------------------------------------------------
    # Store Interface Implementation
    # -------------------------------------------------------------------------

    # Add the specific sanitization method for pgvector compatibility
    def pgvector_sanitize_collection_name(self, name):
        """Sanitize a collection name for pgvector (uses default)."""
        # Although pgvector doesn't use collection names directly,
        # no-op for now
        return name

    def pgvector_collection_exists(self, collection_id):
        """Check if a collection exists - for pgvector, collections always 'exist'"""
        self.ensure_one()

        # For pgvector, we always return True as we're using the existing Odoo tables
        # and not creating separate collections
        return True

    def pgvector_create_collection(
        self, collection_id, dimension=None, metadata=None, **kwargs
    ):
        """Create a collection - for pgvector, this is a no-op"""
        self.ensure_one()

        # For pgvector, creating a collection is essentially a no-op
        # since we're using the existing Odoo tables
        collection = self.env["llm.knowledge.collection"].browse(collection_id)
        if not collection.exists():
            _logger.warning(f"Collection {collection_id} does not exist")
            return False

        # But we might want to create the index for the embedding model
        if collection.embedding_model_id:
            self._create_vector_index(collection.embedding_model_id.id)

        return True

    def pgvector_delete_collection(self, collection_id):
        """Delete a collection - for pgvector, we just drop indexes"""
        self.ensure_one()

        # For pgvector, deleting a collection just means dropping its indexes
        collection = self.env["llm.knowledge.collection"].browse(collection_id)
        if not collection.exists():
            return True

        # Get embedding model ID
        embedding_model_id = (
            collection.embedding_model_id.id if collection.embedding_model_id else False
        )
        if not embedding_model_id:
            return True  # Nothing to delete if no embedding model

        # Drop any vector indexes for this embedding model
        self._drop_vector_index(embedding_model_id)

        # Find chunks that belong to this collection
        chunks = self.env["llm.knowledge.chunk"].search(
            [("collection_ids", "in", [collection_id])]
        )

        # Identify chunks that don't belong to other collections with the same embedding model
        chunks_to_delete = []
        for chunk in chunks:
            other_collections = chunk.collection_ids - collection
            if not any(
                c.embedding_model_id.id == embedding_model_id for c in other_collections
            ):
                chunks_to_delete.append(chunk.id)

        # Delete embeddings in a batch
        if chunks_to_delete:
            self.env["llm.knowledge.chunk.embedding"].search(
                [
                    ("chunk_id", "in", chunks_to_delete),
                    ("embedding_model_id", "=", embedding_model_id),
                ]
            ).unlink()

        return True

    def pgvector_insert_vectors(
        self, collection_id, vectors, metadata=None, ids=None, **kwargs
    ):
        """Insert vectors into collection using batch operations"""
        self.ensure_one()

        # Check parameters
        if not ids or len(ids) != len(vectors):
            raise UserError(_("Must provide chunk IDs matching the vectors"))

        # Get the collection
        collection = self.env["llm.knowledge.collection"].browse(collection_id)
        if not collection.exists() or not collection.embedding_model_id:
            return False

        # Get the embedding model
        embedding_model_id = collection.embedding_model_id.id

        # First, delete any existing embeddings for these chunks with this embedding model
        # This handles the update case by replacing existing embeddings
        if ids:
            self.env["llm.knowledge.chunk.embedding"].search(
                [
                    ("chunk_id", "in", ids),
                    ("embedding_model_id", "=", embedding_model_id),
                ]
            ).unlink()

        # Prepare values for batch creation
        vals_list = []
        for chunk_id, vector in zip(ids, vectors):  # noqa: B905
            vals_list.append(
                {
                    "chunk_id": chunk_id,
                    "embedding_model_id": embedding_model_id,
                    "embedding": vector,
                }
            )

        # Batch create all embeddings in a single operation
        if vals_list:
            self.env["llm.knowledge.chunk.embedding"].create(vals_list)

        # Make sure the index exists
        self._create_vector_index(embedding_model_id)

    def pgvector_delete_vectors(self, collection_id, ids, **kwargs):
        """Delete vectors (embeddings) for specified chunk IDs"""
        self.ensure_one()

        if ids is None:
            return False

        # Get the collection to determine the embedding model
        collection = self.env["llm.knowledge.collection"].browse(collection_id)
        if not collection.exists() or not collection.embedding_model_id:
            return False

        embedding_model_id = collection.embedding_model_id.id
        chunks = self.env["llm.knowledge.chunk"].browse(ids)

        # Get all chunks that don't belong to any other collection with the same embedding model
        chunks_to_delete = []
        for chunk in chunks:
            # Find if this chunk belongs to other collections with the same embedding model
            other_collections = chunk.collection_ids - collection
            if not any(
                c.embedding_model_id.id == embedding_model_id for c in other_collections
            ):
                chunks_to_delete.append(chunk.id)

        # Delete embeddings in a batch if there are any to delete
        if chunks_to_delete:
            self.env["llm.knowledge.chunk.embedding"].search(
                [
                    ("chunk_id", "in", chunks_to_delete),
                    ("embedding_model_id", "=", embedding_model_id),
                ]
            ).unlink()

        return True

    def pgvector_search_vectors(
        self,
        collection_id,
        query_vector,
        limit=10,
        filter=None,
        offset=0,
        query_operator="<=>",
        min_similarity=0.5,
    ):
        """
        Search for similar vectors in the collection

        Returns:
            list of dicts with 'id', 'score', and 'metadata'
        """
        self.ensure_one()

        collection = self.env["llm.knowledge.collection"].browse(collection_id)
        if not collection.exists() or not collection.embedding_model_id:
            return []

        embedding_model_id = collection.embedding_model_id.id

        # Format the query vector using pgvector's Vector class
        register_vector(self.env.cr._cnx)
        vector_str = Vector._to_db(query_vector)

        # Match the index's type: for >2000-dim embeddings the ANN index is built
        # on the `halfvec` cast (see _create_vector_index), so the query must cast
        # both sides to halfvec for the index to be used. The distance is still
        # correct (half precision); the stored column remains full-precision.
        # Beyond the 4000-dim halfvec index cap, use the two-stage
        # subvector-ANN + exact-rerank search instead.
        dims = len(query_vector) if query_vector is not None else 0
        if dims > 4000:
            return self._pgvector_search_subvector_rerank(
                collection_id,
                embedding_model_id,
                vector_str,
                dims,
                limit=limit,
                offset=offset,
                query_operator=query_operator,
                min_similarity=min_similarity,
            )
        if dims > 2000:
            vec_type = f"halfvec({dims})"
            emb_expr = f"e.embedding::halfvec({dims})"
        else:
            vec_type = "vector"
            emb_expr = "e.embedding"

        # Build the query with proper index hints
        index_name = self._get_index_name(
            "llm_knowledge_chunk_embedding", embedding_model_id
        )
        index_hint = f"/*+ IndexScan(llm_knowledge_chunk_embedding {index_name}) */"

        # Execute the query to find similar vectors
        # Join to llm_knowledge_chunk_embedding instead of directly using chunks
        query = f"""
            WITH query_vector AS (
                SELECT '{vector_str}'::{vec_type} AS vec
            )
            SELECT {index_hint} e.chunk_id, 1 - ({emb_expr} {query_operator} query_vector.vec) as score
            FROM llm_knowledge_chunk_embedding e
            JOIN llm_knowledge_chunk c ON e.chunk_id = c.id
            JOIN llm_knowledge_resource_collection_rel rel ON c.resource_id = rel.resource_id
            CROSS JOIN query_vector
            WHERE rel.collection_id = %s
            AND e.embedding_model_id = %s
            AND e.embedding IS NOT NULL
            AND (1 - ({emb_expr} {query_operator} query_vector.vec)) >= %s
            ORDER BY score DESC
            LIMIT %s
            OFFSET %s
        """

        self.env.cr.execute(
            query, (collection_id, embedding_model_id, min_similarity, limit, offset)
        )
        results = self.env.cr.fetchall()

        # Format results with chunk IDs as the main identifiers
        formatted_results = []
        chunk_ids = []

        for chunk_id, score in results:
            chunk_ids.append(chunk_id)
            formatted_results.append(
                {
                    "id": chunk_id,
                    "score": score,
                    "metadata": {},  # We don't store additional metadata currently
                }
            )

        return formatted_results

    def _pgvector_search_subvector_rerank(
        self,
        collection_id,
        embedding_model_id,
        vector_str,
        dims,
        limit=10,
        offset=0,
        query_operator="<=>",
        min_similarity=0.5,
    ):
        """Two-stage search for embeddings beyond the 4000-dim halfvec cap.

        Stage 1 (ANN): order by distance on the leading SUBVECTOR_INDEX_DIMS
        dimensions (halfvec) — matches the expression index built by
        _create_vector_index — oversampling SUBVECTOR_OVERSAMPLE× the
        requested window.
        Stage 2 (exact): re-rank the candidates on the FULL-precision,
        full-dimension stored vector; min_similarity applies to the exact
        score, so result quality equals exact scan for any candidate set
        that contains the true top-k.

        Cosine distance is scale-invariant, so the truncated stage needs no
        re-normalization.
        """
        sub = SUBVECTOR_INDEX_DIMS
        ann_limit = max((limit + offset) * SUBVECTOR_OVERSAMPLE, 40)
        query = f"""
            WITH query_vector AS (
                SELECT '{vector_str}'::vector({dims}) AS vec,
                       subvector('{vector_str}'::vector({dims}), 1, {sub})::halfvec({sub}) AS sub_vec
            ),
            candidates AS (
                SELECT e.chunk_id, e.embedding
                FROM llm_knowledge_chunk_embedding e
                JOIN llm_knowledge_chunk c ON e.chunk_id = c.id
                JOIN llm_knowledge_resource_collection_rel rel ON c.resource_id = rel.resource_id
                CROSS JOIN query_vector
                WHERE rel.collection_id = %s
                AND e.embedding_model_id = %s
                AND e.embedding IS NOT NULL
                ORDER BY subvector(e.embedding, 1, {sub})::halfvec({sub})
                         {query_operator} query_vector.sub_vec
                LIMIT %s
            )
            SELECT cand.chunk_id,
                   1 - (cand.embedding {query_operator} query_vector.vec) AS score
            FROM candidates cand
            CROSS JOIN query_vector
            WHERE (1 - (cand.embedding {query_operator} query_vector.vec)) >= %s
            ORDER BY score DESC
            LIMIT %s
            OFFSET %s
        """
        self.env.cr.execute(
            query,
            (
                collection_id,
                embedding_model_id,
                ann_limit,
                min_similarity,
                limit,
                offset,
            ),
        )
        return [
            {"id": chunk_id, "score": score, "metadata": {}}
            for chunk_id, score in self.env.cr.fetchall()
        ]

    # -------------------------------------------------------------------------
    # Vector Index Management
    # -------------------------------------------------------------------------

    def _get_index_name(self, table_name, embedding_model_id):
        """Generate a consistent index name based on table and embedding model"""
        return f"{table_name}_emb_model_{embedding_model_id}_idx"

    def _create_vector_index(self, embedding_model_id, dimensions=None, force=False):
        """Create a vector index for the specified embedding model"""
        self.ensure_one()

        cr = self.env.cr
        table_name = "llm_knowledge_chunk_embedding"

        # Register vector with this cursor
        register_vector(cr._cnx)

        # Generate index name
        index_name = self._get_index_name(table_name, embedding_model_id)

        # Check if index already exists — BEFORE any dimension probing, so the
        # common path (index present, called from every insert batch) costs one
        # catalog lookup and no embedding API call.
        if force:
            # Drop existing index if force is True
            cr.execute(f"DROP INDEX IF EXISTS {index_name}")
        else:
            # Check if index exists
            cr.execute(
                """
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = %s
                """,
                (index_name,),
            )

            # If index already exists, return early
            if cr.fetchone():
                _logger.info(f"Index {index_name} already exists, skipping creation")
                return True

        # Determine dimensions if not provided: cheapest source is an already
        # stored embedding row; fall back to a live API probe only when the
        # table has no rows for this model yet.
        if not dimensions and embedding_model_id:
            cr.execute(
                f"""
                    SELECT vector_dims(embedding) FROM {table_name}
                    WHERE embedding_model_id = %s AND embedding IS NOT NULL
                    LIMIT 1
                """,
                (embedding_model_id,),
            )
            row = cr.fetchone()
            if row:
                dimensions = row[0]
        if not dimensions and embedding_model_id:
            embedding_model = self.env["llm.model"].browse(embedding_model_id)
            if embedding_model.exists():
                # Generate a sample embedding to determine dimensions
                sample_embedding = embedding_model.embedding("")[0]
                dimensions = len(sample_embedding) if sample_embedding else None

        # Choose the indexable expression. pgvector's ivfflat/hnsw indexes cap
        # the `vector` type at 2000 dimensions and `halfvec` at 4000:
        # - <= 2000 dims: index the vector column directly.
        # - 2001..4000 dims (e.g. text-embedding-3-large = 3072): index the
        #   half-precision `halfvec` cast (pgvector >= 0.7.0).
        # - > 4000 dims (e.g. Qwen3-Embedding-8B = 4096): index only the
        #   leading SUBVECTOR_INDEX_DIMS dimensions via a subvector expression
        #   (valid for MRL-trained models); queries re-rank exactly on the
        #   full vector (_pgvector_search_subvector_rerank).
        # In ALL cases the stored column stays full-precision, full-dimension
        # `vector` — index strategy changes never require re-embedding.
        if dimensions and dimensions > 4000:
            sub = SUBVECTOR_INDEX_DIMS
            index_expr = f"(subvector(embedding, 1, {sub})::halfvec({sub}))"
            ops = "halfvec_cosine_ops"
            descr = f"subvector({sub})/halfvec"
        elif dimensions and dimensions > 2000:
            index_expr = f"(embedding::halfvec({dimensions}))"
            ops = "halfvec_cosine_ops"
            descr = f"halfvec({dimensions})"
        else:
            cast_type = f"vector({dimensions})" if dimensions else "vector"
            index_expr = f"(embedding::{cast_type})"
            ops = "vector_cosine_ops"
            descr = cast_type

        index_method = self.pgvector_index_method or "ivfflat"
        # Prefer the configured method; fall back hnsw -> ivfflat.
        methods = ["hnsw", "ivfflat"] if index_method == "hnsw" else [index_method]
        for method in methods:
            try:
                # Isolate each attempt in a savepoint so a failure (unsupported
                # method, dimension cap, ...) rolls back ONLY this subtransaction
                # and never poisons the surrounding embedding transaction.
                with cr.savepoint():
                    cr.execute(
                        f"""
                        CREATE INDEX {index_name} ON {table_name}
                        USING {method}({index_expr} {ops})
                        WHERE embedding_model_id = %s AND embedding IS NOT NULL
                        """,
                        (embedding_model_id,),
                    )
                _logger.info(
                    f"Created {method} vector index {index_name} ({descr}) "
                    f"for embedding model {embedding_model_id}"
                )
                return True
            except Exception as e:
                _logger.warning(f"Could not create {method} index {index_name}: {e}")
        _logger.error(
            f"No vector index created for {index_name}; falling back to exact search."
        )
        return False

    def _drop_vector_index(self, embedding_model_id=None):
        """Drop vector index for the specified embedding model"""
        self.ensure_one()
        table_name = "llm_knowledge_chunk_embedding"

        if embedding_model_id:
            # Drop specific index for this model
            index_name = self._get_index_name(table_name, embedding_model_id)
            self.env.cr.execute(f"DROP INDEX IF EXISTS {index_name}")
            _logger.info(f"Dropped vector index {index_name}")
        else:
            # Try to find all indexes for this table
            query = """
                SELECT indexname FROM pg_indexes
                WHERE tablename = %s
            """
            self.env.cr.execute(query, (table_name,))
            indexes = self.env.cr.fetchall()

            # Drop each embedding model index
            for index in indexes:
                if "emb_model_" in index[0]:
                    self.env.cr.execute(f"DROP INDEX IF EXISTS {index[0]}")
                    _logger.info(f"Dropped vector index {index[0]}")

        return True
