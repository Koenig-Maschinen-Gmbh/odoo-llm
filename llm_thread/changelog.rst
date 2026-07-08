18.0.1.11.0 (2026-07-08)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P-CHAT link-navigation: clicking an Odoo record link
  (``/odoo/<model>/<id>``) in an AI answer now opens the record via the
  action service (``doAction``) — pushing the form onto the SPA action
  stack so the breadcrumb navigates **back to the AI thread**. Previously
  the router's global SPA handler loaded the record but REPLACED the
  action stack (no breadcrumb back). ``LLMChatClientAction`` gained
  ``useSetupAction({ getLocalState })`` (the canonical OCB pattern, used
  by every controller) to save the active thread id on leave + restore it
  on breadcrumb-back. ``LLMChatContainer`` gained a delegated click
  handler on the thread area that intercepts ``/odoo/<model>/<id>``
  links → ``doAction`` (plain click) or ``window.open`` (Ctrl/Cmd/middle).
  External links + ``/web/content/`` PDF preview links → browser default.
  Pattern: ``koenig_wiki/static/src/wiki_editor.js`` (the proven wiki
  implementation).

18.0.1.10.0 (2026-07-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P-CHAT M6 (step 1, pulled forward): AI chat threads never
  notify followers. ``message_post`` now injects ``mail_create_nosubscribe``
  (prevents auto-subscribing the poster) and ``_notify_thread`` is a no-op
  on ``llm.thread`` (no ``mail.notification`` / ``mail.mail`` / bus push).
  The asking user reads answers live via the chat UI (SSE + the M3 bus
  reload), never via inbox/email — followers are not meaningful for an AI
  chat. Generic (any AI chat product wants this). Other ``mail.thread``
  models are unaffected (the override is ``llm.thread``-only). Tests:
  assistant + user messages create zero notifications/mails; a
  ``res.partner`` message still notifies its followers (suppression is
  llm.thread-specific).

18.0.1.9.0 (2026-07-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] Self-review (round 2): ``is_error`` is now declared as a
  Mail Store field (``Record.attr()`` in the MessageModel patch setup) so
  the value serialized by ``_extras_to_store`` is actually populated on
  the frontend record. Without the declaration the store field-detection
  scan did not see it — closing the loop on the M2 ``is_error`` fix (new
  Hoot suite ``llm_message_is_error.test.js`` proves the store populates
  it + the field is registered).
* [KOENIG][ADD] P-CHAT M4 support: ``[[model:id label]]`` record markers
  survive ``_process_llm_body`` (markdown2) — verified incl. inside pipe
  tables (space separator avoids the ``|`` conflict). New
  ``test_record_marker_survives_markdown`` /
  ``test_record_marker_survives_in_table`` tests.

18.0.1.8.0 (2026-07-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P-CHAT M3: live run-feedback API on the llm store —
  ``threadRunState`` map + ``setThreadRunState`` / ``getThreadRunState`` /
  ``isThreadRunning`` / ``clearThreadRunState`` / ``reloadThreadMessages``.
  A generic, channel-agnostic API that koenig bus subscribers call to drive
  the sidebar working-indicator and answer arrival without a manual reload.
  ``isStreamingThread`` now also treats a background run (running state) as
  streaming, so the composer disables send while a run is active.
* [KOENIG][ADD] Sidebar working-indicator: spinner + elapsed ``mm:ss``
  while a thread runs, ✓ / ⚠ / ⊘ flash on done / failed / cancelled. A
  gated 1s tick drives the counter and flash — idle chats don't re-render.

18.0.1.7.0 (2026-07-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P-CHAT M2: foreground/background message hierarchy. Tool
  messages and intermediate assistant messages (with ``tool_calls``) now
  collapse into one muted "Work steps (N)" accordion per turn
  (ChatGPT/Claude thinking-drawer pattern); the first step of a turn
  renders the toggle, expanding reveals each step's args/result via the
  existing tool-message component. The final answer (assistant without
  ``tool_calls``) renders fully expanded. Failed-run error messages
  (``is_error``) stay prominent outside the drawer.
* [KOENIG][ADD] Pure message-classification + turn-grouping helpers
  (``utils/llm_message_classify.js``) — tool / intermediate-assistant /
  final-answer / error / step / turn-id / first-step / step-count —
  unit-tested in Hoot (11 tests).
* [KOENIG][ADD] Per-turn steps-drawer collapse state on the llm store
  (``stepDrawerOpen`` map + ``toggleStepDrawer`` / ``isStepDrawerOpen``).
* [KOENIG][ADD] Hoot test runner (``tests/test_js.py``) + the
  classification Hoot suite
  (``static/tests/llm_message_classify.test.js``).
* [KOENIG][FIX] Self-review: ``_extras_to_store`` now serializes
  ``is_error`` so the frontend can classify failed-run error messages
  (was missing — ``isLLMError`` was always false on the client, so error
  prominence silently did not work). New
  ``tests/test_mail_message_store.py``.
* [KOENIG][FIX] Self-review: the Message ``className`` getter (where the
  hierarchy classes live) was dead code — the base ``attClass`` only
  forwards ``props.className``. Patched ``attClass`` to merge
  ``this.className`` so ``o-llm-step`` / ``o-llm-step-collapsed`` /
  ``o-llm-message-error`` / ``o-llm-final-answer`` actually reach the DOM.
* [KOENIG][FIX] Self-review: ``llm.store`` in the Message patch is now
  wrapped in ``useState`` (matches composer_patch / llm_thread_header) —
  without it the drawer-toggle and run-state reads did not re-render.

18.0.1.6.0 (2026-07-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P-CHAT M1: assistant answers now render at wiki-page
  markdown fidelity. ``_process_llm_body`` extras aligned with the wiki
  content converter (``koenig_wiki_outline_importer`` ``ContentConverter``)
  — added ``task_list`` (``- [ ]`` / ``- [x]`` checkboxes),
  ``code-friendly`` (identifiers with underscores no longer turn into
  italic/bold), and ``break-on-newline`` (single newlines render as
  ``<br>``, the ChatGPT/Claude chat convention). ``header-ids``
  deliberately skipped (heading anchors are noise in a chat bubble).
* [KOENIG][ADD] New ``.o-llm-md-body`` SCSS scope on assistant message
  bodies: bordered/striped tables, dark fenced code blocks, inline code,
  blockquotes, compact headings, task-list checkboxes, subtle links —
  ported from the wiki client action style reference, scoped under
  ``o-llm-message-assistant`` so tool/user/mail messages are untouched.
* [KOENIG][ADD] Copy button on assistant answers — copies the markdown
  source when ``body_json.markdown`` is present, else the rendered plain
  text (clipboard API; no-op on non-secure contexts).
* [KOENIG][ADD] Python test suite for ``_process_llm_body`` (tables, task
  lists, emoji survival + shortcode conversion, fenced code, code-friendly,
  break-on-newline, strike, Markup passthrough, empty body).

18.0.1.5.4 (2026-07-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] Assistant answers now render markdown pipe tables as real
  (Bootstrap-styled) HTML tables — the ``tables`` extra was missing from the
  ``markdown2`` call, so tables displayed as raw ``| a | b |`` text.
* [KOENIG][FIX] Emoji no longer appear as ``:factory:``-style text markers:
  ``_process_llm_body`` used ``emoji.demojize`` (emoji → shortcode text);
  now ``emoji.emojize`` (shortcode → emoji, real emoji untouched).

18.0.1.5.3 (2026-06-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Also strip the chatter message-action set from the **mobile** overflow
  menu in AI threads. 18.0.1.5.2 emptied the desktop toolbar via the Message
  component, but ``MessageActionMenuMobile`` builds its own action set with
  ``useMessageActions()``, so on mobile the chatter actions (reaction, star,
  edit, …) still appeared for ``llm.thread`` messages. The mobile menu's action
  set is now empty for ``llm.thread`` messages too. Regular mail/discuss
  messages are unaffected.

18.0.1.5.2 (2026-06-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Stop leaking chatter-only UI into AI conversation threads. Because the
  AI chat reuses mail's ``Message`` and ``Composer`` components, every standard
  chatter affordance appeared on AI messages/compose: the message hover toolbar
  (Add a Reaction, Mark as Todo/star, Reply, Edit, Delete, Copy Link, Translate
  and the overflow "Expand" menu) and the composer's Emoji picker, GIF picker
  and "Full composer" button — none of which make sense when talking to an AI.
  The message action toolbar is now empty for ``llm.thread`` messages, and the
  emoji/GIF/full-composer buttons are hidden for ``llm.thread`` composers.
  "Attach files" (multimodal input) and Send/Stop are kept. Regular
  mail/discuss messages and composers are completely unaffected (the guards are
  per-component on the ``llm.thread`` model).

18.0.1.5.1 (2026-06-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] The chatter AI-chat extension no longer ``position="replace"``-s the
  whole ``o-mail-Chatter-content`` subtree. That replace silently dropped every
  sibling other addons inject into the chatter content — in particular
  ``cloud_base``'s SharePoint folder tree (``.cb-attachment-box``), which
  disappeared from every record's chatter once this module was installed
  alongside cloud_base/koenig_onedrive. The extension is now additive: it flags
  the content container while AI chat is active and appends the AI chat host;
  new ``chatter_ai.scss`` hides the normal content while chatting, so only the
  AI chat shows without destroying any sibling content. AI-chat behaviour is
  unchanged for the user.

18.0.1.5.0 (2026-06-11)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] The standalone chat client now only offers chat/multimodal models as a
  thread's chat model (was: any active model). Selecting an embedding model as the
  chat model produced a provider 400 ("is an embedding model and cannot be used
  with the chat/completions endpoint"). ``getFirstAvailableModel`` now prefers the
  model flagged ``default``.

18.0.1.4.5 (2026-01-17)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Unsupported file detection with is_error message exclusion
* [ADD] New _post_error_message() for displaying API errors in thread
* [ADD] New _check_unsupported_attachments() for file compatibility validation
* [ADD] is_error parameter to message_post() for error message tracking
* [IMP] Error messages excluded from LLM context via is_error=False filter
* [IMP] Skip markdown processing for pre-formatted Markup content

18.0.1.4.4 (2026-01-16)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Multimodal file attachment support in chat interface
* [FIX] Restored nginx buffering comment for SSE streaming documentation

18.0.1.4.3 (2025-12-02)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Replaced unreliable bus notification with client action pattern for AI chat opening
* [IMP] Added pendingOpenInChatter state to llm.store service for cross-navigation state
* [IMP] Added checkPendingAIChatOpen() method to chatter patch
* [REMOVE] Removed redundant bus subscription code from chatter patch

18.0.1.4.2 (2025-11-26)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Removed prompt_id reference from _thread_to_store() - field is defined in llm_assistant
* [IMP] Module can now be installed standalone without llm_assistant dependency

18.0.1.4.1 (2025-11-21)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Fixed broken create() method using self.model_id in @api.model context
* [IMP] Unified thread naming with backend-generated names using record display_name
* [IMP] Added unique ID suffix to standalone thread names (e.g., "New Chat #123")
* [IMP] Proper @api.model_create_multi decorator for batch creation support
* [REMOVE] Removed hardcoded name generation from chatter patch and client action

18.0.1.4.0 (2025-10-23)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Related Record component - Link chat threads to any Odoo record
* [IMP] Service layer architecture for record linking/unlinking
* [FIX] Field naming collision in store serialization (model vs res_model)
* [MIGRATION] Replace name_get() with searchRead() for Odoo 18.0 compatibility

18.0.1.3.0 (2025-01-04)
~~~~~~~~~~~~~~~~~~~~~~~

* [BREAKING] Refactored to use stored llm_role field for maximum efficiency
* [PERF] Added computed stored llm_role field for instant role lookups
* [PERF] Optimized message queries using direct field filtering instead of batch methods
* [PERF] Improved frontend performance with direct field comparison instead of computed properties
* [PERF] Enhanced database performance with proper indexing on llm_role field
* [IMP] Simplified message_post API with llm_role parameter instead of subtype_xmlid
* [IMP] Updated JavaScript models to use direct field access for role checking
* [IMP] Streamlined message template rendering with direct field conditionals
* [IMP] Simplified message action visibility logic with direct role comparison
* [MIGRATION] Added migration script to compute llm_role for existing messages
* [REMOVE] Removed complex role checking computed properties (replaced with direct field access)
* [OPT] Leveraged database indexing for improved query performance on llm_role field

16.0.1.2.0 (2025-01-04)
~~~~~~~~~~~~~~~~~~~~~~~

* [BREAKING] Refactored to use LLM base module message subtypes instead of separate llm_mail_message_subtypes module
* [MIGRATION] Added migration script to convert existing message subtypes to new format
* [REMOVE] Removed dependency on llm_mail_message_subtypes module
* [IMP] Simplified subtype handling by using direct XML IDs from llm base module
* [OPT] Optimized XML ID resolution using _xmlid_to_res_id instead of env.ref

16.0.1.1.1 (2025-04-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Update method names to be consistent

16.0.1.1.0 (2025-03-06)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Tool integration in chat interface - Support for displaying tool executions and results
* [IMP] Enhanced UI for tool messages with cog icon and argument display
* [IMP] Updated chat components to handle tool-related message types

16.0.1.0.0 (2025-01-02)
~~~~~~~~~~~~~~~~~~~~~~~

* [INIT] Initial release of the module
