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
