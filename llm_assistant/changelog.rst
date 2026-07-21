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
