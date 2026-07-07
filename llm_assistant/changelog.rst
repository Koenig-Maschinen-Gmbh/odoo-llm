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
