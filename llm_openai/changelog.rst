18.0.1.5.0 (2026-07-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] ``openai_simple_completion()`` — simple text completion
  using raw OpenAI client (no mail.message formatting). Bypasses the
  mail.message pipeline entirely for lightweight one-shot calls like
  title generation.

18.0.1.4.10 (2026-07-07)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] Sanitize harmony-format channel tokens leaked into tool-call
  names by gpt-oss models (e.g. ``odoo_model_inspector<|channel|>commentary``).
  The mangled name failed the downstream tool lookup ("Tool '...' not found
  in thread") and the conversation silently stalled without a user-visible
  error. Applied in both the non-streaming and streaming tool-call assembly
  paths (``_sanitize_tool_name``).

18.0.1.4.9 (2026-07-03)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] ``_openai_process_non_streaming_response`` now surfaces the
  ``reasoning`` field from the API response. Both IONOS and Scaleway return
  reasoning content in a JSON field called ``reasoning`` (NOT
  ``reasoning_content`` as the Scaleway docs claim — documentation error
  verified via raw curl 2026-07-03). The ``openai`` Python library parses it
  as a dynamic attribute (``getattr(message, "reasoning", None)``). The
  provider now exposes it as ``result["reasoning_content"]`` for downstream
  consumers. Previously, reasoning tokens were generated and billed but
  dropped before reaching Odoo.
* [KOENIG][ADD] ``_openai_process_streaming_response`` now yields
  ``{"reasoning": delta.reasoning}`` chunks. Both providers send reasoning
  as ``delta.reasoning`` (separate from ``delta.content``) in streaming mode.
* [KOENIG][FIX] Non-streaming response validity check now accepts
  reasoning-only responses (``"reasoning_content" in result``) — some models
  (e.g. gpt-oss-120b without explicit ``reasoning_effort``) return
  ``content=None`` with reasoning in the ``reasoning`` field.

18.0.1.4.8 (2026-06-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] ``openai_embedding`` stashes the API's real token ``usage`` via
  ``_stash_embedding_usage`` so the koenig cost gate can account embeddings
  exactly (= the count IONOS/OpenAI bills) instead of estimating chars/4.

18.0.1.4.7 (2026-06-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] ``openai_embedding`` replaces empty/whitespace-only inputs with a
  single space. IONOS rejects an empty string in the ``input`` array with HTTP 400
  "input cannot be empty" (OpenAI/OpenRouter tolerated it), failing the WHOLE batch
  and blocking embedding of any document with a blank chunk (empty wiki pages,
  whitespace-only chatter). The substitution keeps results aligned 1:1 with inputs.

18.0.1.4.6 (2026-06-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] ``openai_embedding`` now forces ``encoding_format="float"``. The
  OpenAI Python SDK defaults to requesting ``base64``-encoded embeddings; the IONOS
  AI Model Hub gateway cannot serve that and returns HTTP 500 "cannot unmarshal
  string into Go struct field Embedding.data.embedding of type []float32" (verified
  live 2026-06-26 for ``BAAI/bge-m3`` and ``Qwen/Qwen3-VL-Embedding-8B``). Asking
  for plain floats is correct for OpenAI too. Required for the IONOS embedding
  cutover.

18.0.1.4.5 (2026-06-11)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] ``reasoning_effort`` field on ``llm.model``, wired into
  ``openai_chat`` via OpenRouter's unified ``reasoning`` parameter. Set ``none`` to
  disable a reasoning model's internal thinking for ~2x lower latency on RAG /
  tool-using chat (measured: deepseek-v4-pro 4.8s -> 2.5s for a definition answer)
  where extended reasoning does not reliably improve quality. Empty = provider
  default; harmless for non-reasoning models.

18.0.1.4.4 (2026-06-11)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] ``openai_chat`` honours an ``append_messages`` kwarg — pre-formatted
  message dicts placed AFTER the conversation history (mirrors ``prepend_messages``,
  which goes before). Used by ``llm_assistant`` to append a transient "answer now,
  don't call more tools" nudge when the agentic loop hits its cap, without
  persisting it to the thread.

18.0.1.4.3 (2026-06-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] ``openai_rerank`` — rerank documents against a query via an
  OpenAI-compatible ``/rerank`` endpoint (Cohere/Jina/IONOS-style), POSTed
  through the SDK's HTTP client. Returns ``[{index, relevance_score}]``. Used
  by ``koenig.ai.reranker`` for the production cross-encoder backend.

18.0.1.4.2 (2026-06-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] Non-streaming chat responses now include a normalised ``usage``
  block (``prompt_tokens``/``completion_tokens``/``total_tokens``) via the new
  ``_openai_extract_usage`` helper, so callers can persist token/cost telemetry
  (koenig_ai_core). Guarded — providers that omit usage simply yield no block.

18.0.1.4.1 (2026-06-05)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][IMP] openai_get_client now sets an explicit request timeout
  (``llm_openai.timeout``, default 60s) and ``max_retries``
  (``llm_openai.max_retries``, default 3) so a hung embedding/chat request
  fails fast and retries instead of stalling bulk jobs.

18.0.1.4.0 (2026-01-17)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Multimodal file support for images, PDFs, and text files
* [IMP] Refactored to use base module's _prepare_multimodal_attachments() method
* [IMP] Removed duplicate attachment handling code

18.0.1.3.0 (2026-01-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [REM] Removed provider and model data files - users now create providers manually
* [IMP] Provider/model data is now user-owned and survives module uninstall

18.0.1.2.0 (2025-11-28)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Added openai_normalize_prepend_messages() for dispatch pattern compliance
* [IMP] Changed openai_chat to use generic format_messages() and format_tools() dispatch methods
* [IMP] Improved consistency with base provider dispatch pattern

18.0.1.1.4 (2025-11-17)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Updated batch job processing to use provider's _determine_model_use() method instead of wizard

18.0.1.1.3 (2025-10-23)
~~~~~~~~~~~~~~~~~~~~~~~

* [MIGRATION] Migrated to Odoo 18.0

16.0.1.1.3 (2025-05-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Fine tuning support

16.0.1.1.2 (2025-04-08)
~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] Added workaround for Gemini API compatibility (generates placeholder `tool_call_id` if missing)
* [IMP] Modified message formatting to conditionally include `content` key for Gemini compatibility
* [FIX] Fixed errors when using Gemini API due to missing `tool_call_id`

16.0.1.1.1 (2025-04-03)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Added default model for OpenAI, will work when user adds API key

16.0.1.1.0 (2025-03-06)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Tool support for OpenAI models - Implemented function calling capabilities
* [IMP] Enhanced message handling for tool execution
* [IMP] Added support for processing tool results in chat context

16.0.1.0.0 (2025-01-02)
~~~~~~~~~~~~~~~~~~~~~~~

* [INIT] Initial release of the module
