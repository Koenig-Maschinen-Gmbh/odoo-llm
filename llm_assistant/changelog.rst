18.0.1.23.0 (2026-07-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **429 (rate_limit) gets a configurable backoff base**
  (Item 5-residual).  The general ``backoff_base`` (default 1.0s) is used for
  all transient errors, but 429 "INSUFFICIENT QUOTA" errors need more time
  for the per-minute token quota to reset.  New ICP
  ``llm_assistant.backoff_base_rate_limit`` (default = ``backoff_base`` —
  backward compatible) gives 429s a potentially longer exponential backoff
  (e.g., 2.0s base → 2s, 4s, 8s instead of 1s, 2s, 4s).  Observed on
  intranettest: Scaleway glm-5.2 retried 3× within the same second on
  per-minute quota exhaustion — all hit the same rate limit.  Operators can
  set ``backoff_base_rate_limit`` to 2.0+ to give the quota time to reset
  before retrying.

18.0.1.22.0 (2026-07-24)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **Duration-cap kills now hop to the fallback model immediately**
  (R1 — no more identical same-model retries).  A PERF-09 total-duration-cap
  kill is near-deterministic for a given (context, model, effort) triple:
  re-issuing the identical request reproduces the same kill while burning the
  full cap per attempt (observed on intranettest 2026-07-24: three hard runs
  each burned 3×180s on identical retries; the RES-01 fallback then completed
  the identical turn in ~100s).  ``_handle_streaming_response`` now flags the
  sink (``duration_cap_kill``) and ``_generate_assistant_response`` skips the
  remaining same-model retries on a cap kill when a fallback model is
  configured, hopping straight to it.  With NO fallback configured the legacy
  same-model retry is kept (a genuinely stalled provider stream can recover).
  Regression guard: the fork's ``test_retry_loop_integration`` (no fallback)
  still asserts 3 attempts.
* [FIX] **The latest user message is pinned in the** ``get_llm_messages``
  **window** (R2).  In long tool-call runs the 25-message window can scroll
  the user's question out of context; the model then silently drifts from the
  original intent (observed: "list ALL Swiss customers" narrowed to "Top 10 by
  volume" once the question had scrolled out).  The pin costs at most one
  extra message of context; folded (summarized) user messages are not
  re-pinned (the D8 watermark already excludes them).

18.0.1.21.0 (2026-07-24)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [CHANGE] **Synthesis nudge strengthened to forbid reasoning preamble.**
  ``_prepare_chat_kwargs`` (``final_answer=True`` path): the transient nudge
  now explicitly instructs the model to provide ONLY the final answer with no
  preamble, meta-commentary, or transitional phrases ("Now I have all the
  data", "Let me compile the results").  Phase-2 baseline found the
  ``sap_breakdown`` answer began with "Now I have all the data needed. Let me
  compile the results." before the actual table — the judge penalises it and
  it wastes output tokens.  Verified on intranettest: probe answer starts
  directly with "Here is the breakdown of SAP sale orders for 2024…" (no
  preamble).

18.0.1.20.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **Provider error classifier — single source of truth
  (PROV / hardening).** ``llm.thread._classify_llm_error(exc)`` normalizes any
  provider/LLM exception into a stable taxonomy (``rate_limit`` / ``server`` /
  ``gateway`` / ``bad_request`` / ``auth`` / ``forbidden`` / ``not_found`` /
  ``not_implemented`` / ``timeout`` / ``connection`` / ``empty_response`` /
  ``unknown``), modeled on the SAP ``classify_sap_error`` idiom. Named
  ``LLM_ERR_*`` constants + a ``_LLM_TRANSIENT_CATEGORIES`` set.
* [CHANGE] ``_is_transient_llm_error`` now DELEGATES to the classifier so the
  retry decision, telemetry ``error_category``, and the user message never
  drift. **Gateway-shaped 400s (Scaleway ``category: GATEWAY, upstreamStatus:
  400``) are now FATAL** (were already fatal as plain 400s, but now explicitly
  categorized) — retrying inflates the context window and the model fabricates.
  A gateway 5xx stays transient. **HTTP 501 is now FATAL** (was transient under
  the old blanket ``status >= 500`` rule) — Not Implemented won't succeed on
  retry. All other retry behavior is byte-for-byte unchanged (verified by the
  full ``test_llm_retry`` classification + retry-loop suite).
* [ADD] ``_llm_error_status(exc)`` — status extraction (``status_code`` attr,
  text fallback) and ``_describe_llm_error(exc)`` — clean, translatable
  user-facing message per category; UNKNOWN falls back to ``str(exc)`` so real
  code bugs are not hidden.
* [ADD] ``_finalize_llm_trace`` records ``error_category`` on every failed
  attempt (consumed by ``koenig.ai.llm.trace.error_category`` for per-category
  provider reliability reporting).
* [TEST] ``test_llm_retry.py`` +2 classes (``TestClassifyLLMError``,
  ``TestDescribeLLMError``): gateway-400 fatal, gateway-502 transient, 501
  fatal, category mapping, clean-message + str(exc)-fallback. Full retry suite
  stays green.

18.0.1.19.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **CAP-03 deterministic prompt-fragment assembler.**
  ``_build_system_messages()`` replaces the ``get_prepend_messages()``
  override chain with ONE assembler: collects ordered fragments from
  ``_system_prompt_fragments()`` (super() chain) + per-tool
  ``_system_prompt_fragment(thread)`` (effective tools only, CAP-02 fix) +
  consent fragment, sorts by ``sequence`` (deterministic order, not MRO),
  applies a token budget (droppable fragments dropped in reverse sequence).
  ``get_prepend_messages()`` kept as a backward-compat shim.
* [ADD] ``SystemPromptFragment`` namedtuple (sequence, role, content,
  droppable, source) — the fragment protocol.
* [ADD] ``llm.tool._system_prompt_fragment(thread)`` — per-tool guidance
  hook (base: returns ``None``).
* [ADD] Token budget via ICP ``llm_assistant.prompt_token_budget``
  (default 8000 tokens).
* [TEST] ``tests/test_prompt_assembler.py`` — 13 tests: order, budget,
  per-tool-effective (CAP-02 fix), consent, backward compat, dedup.

18.0.1.18.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **CAP-02 bundle subscription + resolver.** ``llm.assistant.bundle_ids``
  (subscribe to ``llm.tool.bundle`` capability packs) + ``_resolved_tools()`` =
  ``(tool_ids | bundle_ids.tool_ids).filtered(active)`` — the assistant's live
  tool set. A tool added to a subscribed bundle reaches every bound thread at
  once, retiring the per-assistant ``assistant_attach.xml`` + self-heal hooks.
* [CHANGE] The CAP-01 ``_effective_tools()`` override is replaced by a
  ``_candidate_tools()`` override (``assistant._resolved_tools()`` ∪ ``extra`` −
  ``disabled``); the base ``_effective_tools()`` then applies the per-tool
  availability gates. Same effective result for existing threads; the resolver is
  now the single gating point (adds capability/consent gates on top).
* [ADD] ``assistant_ids`` inverse on ``llm.tool.bundle`` (subscribers display).
* [TEST] ``tests/test_tool_resolver.py`` — bundle union, live add, inactive
  bundle/tool exclusion, capability gate (hidden on chat / shown on multimodal),
  deviation-with-bundles, no-assistant capability gating.

18.0.1.17.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **CAP-01 live tool resolution (kills 97% snapshot staleness).**
  ``llm.thread._effective_tools()`` now derives the tool set LIVE from the
  bound assistant: ``(assistant.tool_ids | tool_ids_extra) - tool_ids_disabled``.
  A tool added to an assistant reaches all its existing threads instantly — no
  migration, no "re-select your assistant". Two new per-thread override M2M
  fields (``tool_ids_extra`` / ``tool_ids_disabled``, normally empty) express
  deliberate per-thread deviation. ``_prepare_chat_kwargs`` offers the model the
  effective set, not the raw ``tool_ids`` snapshot (which becomes vestigial when
  an assistant is bound, retained for no-assistant threads + display).
* [TECH] The override M2M fields use EXPLICIT distinct relation tables
  (``llm_thread_tool_disabled_rel`` / ``llm_thread_tool_extra_rel``) with auto
  columns — a second/third auto M2M to ``llm.tool`` on ``llm.thread`` would
  collide on the shared auto table name. ``llm.thread.mock`` redefines both as
  non-stored to avoid materializing the tables. Verified by clean install +
  registry load.
* [TEST] ``tests/test_effective_tools.py`` — live derivation, disabled/extra,
  no-assistant fallback, ``_prepare_chat_kwargs`` wiring, tool validation.
* [DOC] Fork ADR
  ``docs_dev/ADR_2026-07-23_THREAD_TOOL_PROMPT_RESOLUTION.md``.

18.0.1.16.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P2-a per-call reasoning_effort kwarg on
  ``_generate_assistant_response`` — threaded into the provider's D2
  precedence (per-call kwarg > model field > provider default). Used by
  the EFF-02 explicit synthesis stage (koenig_ai_orchestrator 18.0.0.38.0).

18.0.1.15.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] RES-01 runtime fallback chain: after the transient-error
  retry loop is exhausted (incl. PERF-09 stream-cap kills), the turn is
  retried ONCE with the next chain model from the new overridable
  ``_get_fallback_model`` hook (empty in the base fork = chain of 1,
  unchanged behavior; koenig overrides consult the master fallback chain
  ICP slots). One hop per turn, no loops; non-transient errors
  (400/401/403) never hop. The fallback attempt's traces carry the
  fallback provider/model automatically, and the fallback model's own
  effort profile applies via the provider's per-model
  ``reasoning_effort`` precedence.

18.0.1.14.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] PERF-09 total-duration streaming cap: at each chunk
  boundary, when the stream's total elapsed time exceeds the cap (default
  180 s via ICP ``llm_assistant.max_stream_duration_s``; ``<= 0``
  disables), the stream is closed and a ``TransientLLMError`` is raised —
  the existing retry loop retries with backoff, so pathological
  reasoning explosions (200–1200 reasoning chunks over 48–325 s) are
  killed and re-attempted on a warm provider instance instead of
  blocking a worker for minutes. New per-call
  ``max_stream_duration_s`` kwarg on ``_generate_assistant_response`` /
  ``_handle_streaming_response`` (threaded through, wins over the ICP);
  new overridable ``_get_max_stream_duration_s`` resolver hook.
  Partial-content handling: an already-posted partial assistant message
  is kept (never deleted), the kill is annotated on the trace
  (``error_class`` / ``error_message`` via ``_finalize_llm_trace``), and
  a successful retry posts a fresh assistant message.
  ``GenerationCancelled`` keeps precedence and is never swallowed by the
  cap path.

18.0.1.13.3 (2026-07-22)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] TEL-04 degenerate-stream harness (test-only):
  ``TestStreamTraceMatrix`` — consumer-boundary matrix driving the REAL
  ``_handle_streaming_response`` / ``_generate_assistant_response`` with
  fake normalized streams; hook-level trace assertions per degenerate
  shape + a hook-never-raises telemetry-discipline test.

18.0.1.13.2 (2026-07-22)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][IMP] Tool/assistant wiring clarity: new ``assistant_ids``
  inverse M2M on ``llm.tool`` (reuses the auto-generated
  ``llm_assistant_llm_tool_rel`` table — editing either side updates the
  same rows) + a read-visible "Usage" page on the tool form listing the
  assistants that whitelist the tool. The assistant form's Tools page
  gains a "Shared pool" note. Without the inverse view, each assistant's
  Tools page read as if tools belonged to that assistant exclusively —
  they are a shared capability pool across assistants and expert profiles.

18.0.1.13.1 (2026-07-21)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][IMP] EFF-01 P1-5: the LLM trace fingerprint now records the
  EFFECTIVE reasoning effort — ``chat_kwargs.get("reasoning_effort")``
  (per-call kwarg) takes precedence over ``model_su.reasoning_effort``
  (model field), matching the D2 precedence used by the provider. In
  Phase 1 (no per-call chat override yet), this records the model field;
  in Phase 2 (synthesis stage), it will record the per-call override.

18.0.1.13.0 (2026-07-20)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] TEL-01: ``_record_llm_call_trace`` hook (no-op + log in the
  base fork; koenig overrides persist it as ``koenig.ai.llm.trace``). OCB
  hook precedent: ``mail_thread._get_customer_information``.
* [KOENIG][ADD] TEL-01: ``_finalize_llm_trace`` helper — merges
  ``request_trace`` + ``sink``, sets status/error from the exception, calls
  the hook. Fired at the attempt boundary for all three outcomes (ok /
  transient / fatal). Capture is purely additive — retry/raise semantics
  are byte-for-byte unchanged (the committed 18.0.1.12.1 semantics are
  untouchable).
* [KOENIG][ADD] TEL-01: ``trace_sink`` parameter in both
  ``_handle_streaming_response`` and ``_handle_non_streaming_response``.
  When passed, the sink is filled incrementally with the stream histogram
  (content/reasoning/tool chunk counts + char lengths), finish_reason,
  usage (from the terminal metadata chunk), first-token latency, and
  message_created/id. The sink lives in the caller
  (``_generate_assistant_response``) so it survives both the return and
  the raise paths. Filling is purely additive — existing yield/raise
  semantics are unchanged. Never raises from sink writes.

18.0.1.12.1 (2026-07-20)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **Empty / reasoning-only LLM responses now retry and fail visibly
  instead of silent ``None`` or fake success.** ``_handle_streaming_response``
  raises ``TransientLLMError`` when the stream completes with no ``content``
  and no ``tool_calls`` (e.g. reasoning-only deltas) — it previously returned
  ``None``. ``_handle_non_streaming_response`` raises ``TransientLLMError``
  when the response dict has no ``content`` and no ``tool_calls`` — it
  previously fabricated an assistant message with body ``"No response from
  model"`` (false success). The retry loop in ``_generate_assistant_response``
  (18.0.1.10.0) now wraps the Phase 2 response-processing call too, so both
  empty-response cases re-call ``chat()`` with exponential backoff; after
  retries are exhausted the error propagates to ``_execute`` which marks the
  run "failed" (not "done") with a clear message.

* [FIX] **Error chunk before any content is now transient and retried.** When
  an ``{"error": ...}`` chunk arrives before any assistant message was posted
  (no partial state), ``_handle_streaming_response`` raises
  ``TransientLLMError`` so the retry loop re-attempts. An error chunk AFTER
  partial content was posted still returns the partial message (existing
  semantics preserved — the partial message cannot be un-posted).

* [ADD] **Regression coverage:** new ``TestEmptyResponseRetry`` suite covers
  reasoning-only-then-success, empty-then-success (streaming + non-streaming),
  retries-exhausted, error-chunk-before-content (retry + exhausted),
  error-chunk-after-partial (returns partial, no retry), normal
  content/tool-call paths unchanged, and ``GenerationCancelled``
  (``BaseException``) propagating through the empty-response retry
  ``except TransientLLMError`` block.

18.0.1.12.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **``_handle_non_streaming_response``: guard against non-dict provider
  response.** Some providers (observed with the Mistral provider used by the
  ``media_describe`` expert) return a raw string instead of a dict on edge
  cases (empty response, rate-limit fallback). The unguarded
  ``response.get("content", "")`` raised ``AttributeError: 'str' object has
  no attribute 'get'``, which propagated as an expert failure. The fix adds
  an ``isinstance(response, dict)`` guard mirroring the existing guard at
  line 823 in ``_llm_fold_into_summary``, treating a non-dict response as
  plain content. Fixes 3 of the 5 remaining historical ``media_describe``
  expert failures.

18.0.1.11.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **``final_answer`` nudge role: ``user`` → ``system``.** The nudge
  appended by ``_prepare_chat_kwargs`` when ``final_answer=True`` used
  ``role: "user"``. When the last message in history was a ``tool`` result,
  this created an invalid ``user`` after ``tool`` sequence, causing a 400
  "Unexpected role 'user' after role 'tool'" API error. Changed to
  ``role: "system"`` — system messages can appear anywhere in the OpenAI
  message sequence. This fixes the ``media_describe`` expert's 9 consecutive
  failures + circuit breaker block.

18.0.1.10.0 (2026-07-17)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **LLM API transient-error retry layer:** ``_generate_assistant_response``
  now retries ``model_id.chat()`` with exponential backoff (3 attempts, 1s/2s/4s
  base) for transient LLM API failures (5xx, timeout, connection, 429). This is
  a second layer on top of the OpenAI SDK's own retry (``max_retries=3`` in
  ``openai_get_client``). Non-transient errors (400, 401, 403) propagate
  immediately — the run is marked "failed" (not "done") with a clear error
  message. ``GenerationCancelled`` (``BaseException``) is never caught — it
  propagates cleanly to the cancel handler.

* [ADD] ``TransientLLMError`` exception class and ``_is_transient_llm_error``
  generic classifier (class-name heuristic + ``status_code`` attribute — no
  SDK-specific imports). Retry parameters configurable via
  ``ir.config_parameter``: ``llm_assistant.max_retries`` (default 3),
  ``llm_assistant.backoff_base`` (default 1.0s).

* [CHANGED] **Error propagation redesign:** The broad ``except Exception`` in
  ``_generate_assistant_response`` (which caught ALL errors, posted an error
  message, and returned it — making the run look "done") is replaced by the
  retry loop. After retries are exhausted, the exception propagates to
  ``_execute`` which marks the run "failed" and posts the error message.

18.0.1.9.0 (2026-07-17)
~~~~~~~~~~~~~~~~~~~~~~~~

* [CHANGED] **Removed ``koenig_no_auto_commit`` guards:** The 3
  ``cr.commit()`` calls in ``generate_messages`` are now unconditional.
  Sub-agents run on an independent cursor (``self.pool.cursor()``) in
  ``koenig_ai_expert._run_subagent``, so their commits go to the
  independent cursor, not the master's savepoint. The fragile
  ``koenig_no_auto_commit`` context flag is no longer needed and has been
  removed. See ``RESEARCH_FINDINGS_2026-07-17_ORCHESTRATOR_ARCHITECTURE.md``.

18.0.1.8.0 (2026-07-17)
~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **Poisoned transaction root cause:** Guarded 3 ``cr.commit()`` calls
  in ``generate_messages`` with ``koenig_no_auto_commit`` context flag. When
  the ``dispatch_expert`` tool runs a sub-agent, the sub-thread's
  ``generate_messages`` commits were destroying the master's tool-call
  savepoint → "savepoint does not exist" → ``InFailedSqlTransaction`` for
  all subsequent SQL → cascading error handler failure. The context flag
  is set by ``koenig_ai_expert._run_subagent``.
* [FIX] **Defense-in-depth:** Added outer savepoint in ``_execute_tool_call``
  so that if the inner savepoint fails to clear a poisoned transaction, the
  outer savepoint rollback clears it. The error handler's
  ``create_tool_error_message`` (which needs SQL) can now execute safely.

18.0.1.7.4 (2026-07-16)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][IMP] Menu restructure: menus consolidated under 'König Intelligence'
  (was 'LLM'). Items re-parented and re-sequenced for clarity.

18.0.1.7.3 (2026-07-14)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Release-and-resume HITL pause mechanism (Phase 4a — Iteration 2).
  Four fork-level additions (all additive, no behaviour change without the
  hook) that enable human-in-the-loop write-tool approval via the
  release-and-resume-with-structured-injection pattern (OpenAI ``RunState``,
  LangGraph ``interrupt()``, Pydantic AI deferred tools):

  1. ``GenerationPaused(BaseException)`` — parallel to
     ``GenerationCancelled``; raised at the per-tool-call boundary when the
     loop-control hook returns ``{"pause": True}`` for a write-tool.
     Inherits ``BaseException`` so ``except Exception:`` blocks in the tool
     execution pipeline don't swallow it.

  2. ``_check_loop_control_tool`` method — calls the ``loop_control_check``
     hook with the ``tool_call`` dict so the hook can decide whether the
     upcoming tool is a write-tool that needs approval. Returns the full
     result dict so the caller can check both ``cancel`` and ``pause``.
     Used at the per-tool-call boundary only; ``_check_loop_control``
     (top of while loop) and ``_check_loop_control_streaming`` (between
     chunks) are unchanged.

  3. ``post_tool_result`` on ``mail.message`` — posts a synthetic tool
     message with a real result (``status="completed"`` or ``"error"``),
     bypassing the normal ``post_tool_call`` → ``execute_tool_call`` flow.
     Used by the orchestrator's approval handler (Phase 4b) to inject the
     real write result after a human approves a paused tool call.

  4. ``get_unexecuted_tool_calls`` on ``mail.message`` — double-execution
     guard: filters out tool calls that already have a completed/error tool
     message on the thread. Prevents accidental re-execution on structured
     resume (the tool result was injected before re-entering the loop, so
     the assistant message's tool calls should NOT be re-executed).

* [ADD] ``generate_messages`` now uses ``get_unexecuted_tool_calls()`` instead
  of ``get_tool_calls()`` in the tool-execution branch, and adds a combined
  cancel+pause check via ``_check_loop_control_tool`` at the per-tool-call
  boundary. If all tool calls already have results (structured resume edge
  case), the loop breaks with an info log to avoid an infinite loop.

18.0.1.7.2 (2026-07-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Mode-aware between-chunks cancel check (Phase 3). The
  ``loop_control_check`` callable can now return a ``mode`` key
  (``"after_turn"`` or ``"immediate"``) alongside the ``cancel`` boolean.
  In ``after_turn`` mode (the default for user cancels), the between-chunks
  check in ``_handle_streaming_response`` does NOT raise — the LLM response
  is allowed to finish so the in-flight tool call's savepoint commits or
  rolls back cleanly. The cancel is caught at the next tool-call boundary
  (``_check_loop_control`` always raises on cancel regardless of mode). In
  ``immediate`` mode (or when no ``mode`` is returned), the between-chunks
  check raises at every chunk boundary (the Phase 2 behaviour, reserved for
  hard kills / zombie reclaim). New method ``_check_loop_control_streaming``
  encapsulates the mode-aware logic; ``_check_loop_control`` (used at
  non-streaming boundaries) is unchanged — it always raises on cancel.
  Backward compatible: callers that return ``{"cancel": bool}`` without a
  ``mode`` get the Phase 2 behaviour (immediate).

18.0.1.7.1 (2026-07-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] ``GenerationCancelled`` now inherits from ``BaseException`` (not
  ``Exception``) so it propagates through all ``except Exception as e:`` blocks
  in the tool execution pipeline. Previously, the between-chunks cancel check in
  ``_handle_streaming_response`` was silently swallowed by
  ``_generate_assistant_response``'s error handler (line 478), and the
  expert's cancel signal was swallowed by ``_execute_tool_call``'s error
  handler (line 810) and ``mail_message.execute_tool_call``'s error handler
  (line 159). With ``BaseException``, the cancel signal propagates through all
  three swallow points — matching the Python convention for control-flow
  signals (``KeyboardInterrupt``, ``SystemExit``, ``GeneratorExit``).

18.0.1.7.0 (2026-07-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Cooperative loop-control hook for ``generate_messages``. An optional
  ``loop_control_check`` callable (returning ``{"cancel": bool}``) is consulted
  at three sites — before each ``while`` iteration, before each tool-call
  execution, and between stream chunks in ``_handle_streaming_response`` — and
  raises the new ``GenerationCancelled`` exception as soon as it reports a
  cancel. ``_generate_assistant_response`` gained the same keyword argument so
  the hook threads through to the streaming path. Pure addition: with no hook
  (or a hook that never cancels) the loop is byte-for-byte the previous
  behaviour. This lets a caller bound cancellation to one tool/LLM-call
  granularity even when a single ``next(gen)`` step blocks inside a long tool
  call or stream — a between-``next(gen)`` poll alone cannot interrupt that.
* [FIX] ``llm.prompt`` now auto-detects arguments from the template on create
  (when no explicit ``arguments_json`` is supplied) and on template write, via
  ``_ensure_arguments_sync``. The ``create``/``write`` overrides were previously
  no-ops, so the arguments schema stayed empty and ``argument_count`` /
  ``input_schema_json`` computed from ``{}`` — the auto-detection the tests pin
  was never wired in. An explicit ``arguments_json`` is still preserved on
  create so ``undefined_arguments`` can flag template vars the author chose not
  to define.

18.0.1.6.3 (2026-07-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Manifest ``description`` simplified to a plain-text paragraph (removed
  the too-short RST title underline) so Odoo no longer emits a Docutils "Title
  underline too short" warning at module load.

18.0.1.6.2 (2026-07-06)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Rolling thread summarization (D8 / P-MEM): two new fields on ``llm.thread``
  — ``llm_summary`` (Text) and ``llm_summary_upto_message_id`` (M2o ``mail.message``).
  ``get_llm_messages()`` excludes folded messages (id <= watermark) so the summary
  block + raw messages never overlap. ``get_prepend_messages()`` appends the summary
  as a system block when set. New method ``_llm_fold_into_summary(model, keep_last=10)``
  performs an anchored iterative merge (only the newly-dropped span is summarized and
  merged into the existing anchor — never regenerated from scratch). Fail-soft: any
  error keeps the old anchor and watermark unchanged. The model is a parameter — the
  fork stays koenig-free; ``koenig_ai_memory`` picks the cheap model and enqueues the job.

18.0.1.6.1 (2026-06-11)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] The assistant, prompt, and prompt-test ``ace`` fields used ace modes that
  Odoo 18's ``CodeEditor`` rejects (``text`` / ``json`` / ``markdown`` / ``yaml``),
  crashing those forms with "Invalid props for component 'CodeEditor': 'mode' is
  not valid". Mapped to valid modes: ``javascript`` for JSON, ``qweb`` for the
  template / markdown / yaml text fields.

18.0.1.6.0 (2026-06-11)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Enforce ``tool_calls_max`` in the agentic loop. ``generate_messages`` now
  caps the number of tool-execution rounds (the assistant's ``tool_calls_max``,
  default 5); the field was previously defined but never enforced, so a model that
  kept requesting tools looped without bound (a hanging thread). Once the soft cap
  is reached the next assistant turn is **nudged** to answer now (a transient
  "you have enough information, don't call more tools" message) while tools stay
  available — dropping tools or forcing ``tool_choice="none"`` makes some models
  (e.g. DeepSeek-V4-Pro) emit their native tool-call syntax as plain text instead
  of answering. A hard cap (soft cap + 3) is the final backstop.
  ``_generate_assistant_response`` / ``_prepare_chat_kwargs`` gained a
  ``final_answer`` argument. Requires ``llm_openai`` >= 18.0.1.4.4 (``append_messages``).

18.0.1.5.4 (2025-12-02)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Removed invalid `unaccent` parameter from parent_path field (Odoo 18 compatibility)
* [FIX] Added missing access rules for llm.thread.mock transient model

18.0.1.5.3 (2025-12-02)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Replaced bus notification with client action for "Process with AI" button reliability
* [ADD] New client action llm_open_ai_chat_in_chatter for reliable AI chat opening
* [IMP] action_open_llm_assistant() now returns ir.actions.client instead of bus notification
* [IMP] Works reliably on cloud deployments with WebSocket/bus issues

18.0.1.5.2 (2025-11-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Moved prompt_id serialization to _thread_to_store() from llm_thread module
* [IMP] prompt_id handling now properly resides in the module that defines the field

18.0.1.5.1 (2025-11-21)
~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] Simplified action_open_llm_assistant by removing unused pre/post action hooks
* [IMP] Enhanced thread naming in mixin - backend now generates names from record display_name
* [FIX] Cleaned up unnecessary kwargs handling for better maintainability

18.0.1.5.0 (2025-10-23)
~~~~~~~~~~~~~~~~~~~~~~~

* [MIGRATION] Migrated to Odoo 18.0
* [IMP] Updated views and OWL components for compatibility

16.0.1.5.0 (2025-07-13)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Assistant code system with unique constraint
* [ADD] Model association via res_model field
* [ADD] Default assistant system with is_default flag
* [ADD] get_assistant_by_code() discovery method
* [MIGRATION] Auto-generate codes from category hierarchy

16.0.1.0.1 (2025-04-04)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Assistant Creator assistant data record
* [ADD] Data directory structure

16.0.1.0.0 (2025-03-01)
~~~~~~~~~~~~~~~~~~~~~~~

* [INIT] Initial release
