18.0.1.4.1 (2026-07-16)
------------------------

* [KOENIG][IMP] Menu restructure: menus consolidated under 'König Intelligence'
  (was 'LLM'). Items re-parented and re-sequenced for clarity.

18.0.1.4.0 (2026-06-10)
------------------------

* [ADD] Opt-in Anthropic-style Contextual Retrieval in the structured chunker
  (default OFF). When ``llm_knowledge.contextual_retrieval`` is enabled and
  ``llm_knowledge.contextual_model_id`` names a chat model, each chunk is prefixed
  with a short LLM-generated context blurb (situating it in the document) before
  embedding, replacing the deterministic breadcrumb. Best-effort per chunk (a
  provider failure leaves the chunk unchanged). One LLM call per chunk at index
  time — validate cost/quality before enabling in production.

18.0.1.3.0 (2026-06-10)
------------------------

* [IMP] Structured chunker now prepends a ``Context: Title > Section`` breadcrumb
  (built from the Markdown heading hierarchy) to each chunk — a cheap,
  deterministic contextual-retrieval signal that situates the chunk in its
  document, with no per-chunk LLM call. (Full LLM-generated Contextual Retrieval
  remains a future opt-in.)

18.0.1.2.0 (2026-06-10)
------------------------

* [ADD] Structured (token-aware, Markdown-aware) chunker option on
  ``llm.resource`` (``chunker = "structured"``). Packs structural blocks
  (headings / paragraphs / lists / fenced code / tables) greedily up to
  ``target_chunk_size`` tokens with ``target_chunk_overlap`` token overlap,
  preferring to break at headings and never splitting a block unless it alone
  exceeds the target. Token count is estimated without tiktoken (word/punct runs
  floored by chars/4). Replaces the naive 200-token char-based sentence splitter
  for structure-rich content (Koenig wiki AI search uses it at 700/100). Added
  via ``_chunk_structured`` + a ``_get_available_chunkers`` entry; the default
  chunker is unchanged.
