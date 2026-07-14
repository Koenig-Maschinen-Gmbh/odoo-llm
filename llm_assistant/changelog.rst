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
