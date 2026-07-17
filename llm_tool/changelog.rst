18.0.4.2.0 (2026-07-17)
~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **Poisoned transaction defense-in-depth:** Restructured
  ``execute_tool_call`` to move the ``self.write`` (status = "executing")
  INSIDE the savepoint. Previously, this write was BEFORE the savepoint —
  if it failed (access check SQL error), the savepoint couldn't be created
  (``cr.flush()`` in ``_FlushingSavepoint.__init__`` would fail on the
  poisoned transaction), and the error handler's ``self.write`` would
  also fail. Yields moved OUTSIDE the savepoint to minimize the window.
  Error-path ``self.write`` wrapped in try/except as a final safety net.

18.0.4.1.6 (2026-07-16)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][IMP] Menu restructure: menus consolidated under 'König Intelligence'
  (was 'LLM'). Items re-parented and re-sequenced for clarity.

18.0.4.1.5 (2026-07-14)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] ``get_unexecuted_tool_calls()`` on ``mail.message`` — double-execution
  guard for the release-and-resume HITL pattern (Phase 4a). Filters out tool
  calls that already have a completed/error tool message on the thread,
  preventing accidental re-execution when the loop re-enters with an
  assistant message whose tool calls have already been processed (e.g.,
  structured resume after approval injection).
* [ADD] ``post_tool_result()`` on ``mail.message`` — posts a synthetic tool
  message with a real result (``status="completed"`` or ``"error"``),
  bypassing the normal ``post_tool_call`` → ``execute_tool_call`` flow.
  Used by the orchestrator's approval handler (Phase 4b) to inject the real
  write result after a human approves a paused tool call. The injected
  message is linked to the assistant message's tool call via
  ``tool_call_id`` — the validator sees the tool_call+result pair and keeps
  both, so the LLM sees the complete correct sequence and can chain naturally.

18.0.4.1.4 (2026-07-02)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] ``execute()`` now coerces JSON-stringified ``list``/``dict`` parameters
  to native Python types before Pydantic validation. LLMs (especially
  DeepSeek) sometimes pass arrays/objects as JSON strings (e.g.
  ``domain='[["x","=","y"]]'`` instead of ``domain=[["x","=","y"]]``),
  which Pydantic's strict validation rejected — the tool call failed with a
  validation error and the LLM fabricated an answer instead.

18.0.4.1.3 (2026-06-11)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] The Input Schema ``ace`` field used ``mode: 'text'``, which Odoo 18's
  ``CodeEditor`` rejects (valid modes: javascript/xml/qweb/scss/python) — the tool
  form crashed with "Invalid props for component 'CodeEditor': 'mode' is not
  valid". Switched to ``javascript`` (JSON-compatible highlighting).

18.0.4.1.1 (2025-12-03)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Fixed concurrency issue in _register_function_tool by using savepoint

18.0.4.1.0 (2025-11-28)
~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] Refactored _inject_tool_consent() to use _extract_content_text() helper from base provider (DRY)
* [IMP] Simplified content format handling using shared helper method

18.0.4.0.0 (2025-10-28)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Decorator System - Introduced @llm_tool decorator for zero-boilerplate tool creation
* [ADD] Auto-registration - Tools decorated with @llm_tool are automatically registered via _register_hook()
* [ADD] Schema Generation - Automatic input schema generation from Python type hints using Pydantic
* [ADD] Manual Schema Support - Optional manual schema override for methods without type hints
* [ADD] Tool Metadata - Support for read_only_hint, idempotent_hint, destructive_hint, and open_world_hint annotations
* [ADD] SQL Constraints - Added unique constraints for tool names and function tool (model, method) combinations
* [ADD] Auto-update Control - Added auto_update field to control whether decorator changes overwrite manual edits
* [ADD] Comprehensive Tests - Added 16 unit tests covering decorator functionality, schema generation, and constraints
* [IMP] Tool Cleanup - Automatic deactivation of stale tools when decorated methods are removed from code
* [IMP] Schema Reset - Enhanced action_reset_input_schema() to force regeneration from method signatures

18.0.3.0.0 (2025-10-23)
~~~~~~~~~~~~~~~~~~~~~~~

* [MIGRATION] Migrated to Odoo 18.0
* [IMP] Updated views and manifest for compatibility

16.0.3.0.0 (2025-07-04)
~~~~~~~~~~~~~~~~~~~~~~~

* [BREAKING] Refactored tool message storage to use body_json field instead of body field
* [IMP] Tool data now stored directly in body_json field preventing HTML corruption
* [IMP] Enhanced migration to handle both old separate fields and intermediate JSON-in-body formats
* [IMP] Unified migration approach - converts directly from old format to body_json
* [IMP] Better error handling and logging in migration process
* [IMP] Tool messages now use get_tool_data() method for clean data access
* [MIGRATION] Enhanced migration script to handle multiple data formats in single pass
* [PERF] Eliminated JSON parsing overhead with direct field access
* [MAINT] Cleaner separation between structured data (body_json) and display content

16.0.2.0.0 (2025-07-03)
~~~~~~~~~~~~~~~~~~~~~~~

* [BREAKING] Refactored tool message storage to use JSON in message body instead of separate fields
* [REMOVE] Removed tool_calls, tool_call_id, tool_call_definition, tool_call_result fields from mail.message
* [IMP] Simplified tool message structure - all tool data now stored as JSON in message body
* [IMP] Added display_body computed field for human-readable tool message content
* [IMP] Updated provider message formatting to parse tool data from JSON body
* [IMP] Enhanced frontend to handle new JSON-based tool message structure
* [IMP] Updated message validation to work with new tool message format
* [MIGRATION] Added migration script to convert existing tool messages to new JSON format
* [PERF] Reduced database storage overhead by eliminating duplicate tool data fields
* [MAINT] Cleaner architecture with single source of truth for tool data

16.0.1.0.1 (2025-04-08)
~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] Improvements:
  * Added explicit type hints (`list[str]`, `list[list[Any]]`) to list fields in Pydantic models for `fields_inspector`, `record_unlinker`, and `record_updater` tools to improve schema validation and API compatibility.

16.0.1.0.0 (2025-03-06)
~~~~~~~~~~~~~~~~~~~~~~~

* [INIT] Initial release of the module with the following features:
  * LLM Tool Integration - Added ability to chat with LLM models using llm.tool implementations
  * Tool Implementations - Support for odoo_record_retriever and odoo_server_action tools
  * Tool Message Handling - Chat UI for tool messages with cog icon and display of tool arguments and results
