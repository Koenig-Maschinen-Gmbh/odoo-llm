18.0.1.1.1 (2026-07-22)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] ``anthropic_format_message`` silently dropped
  ``llm_role="system"`` messages (``return None`` fallthrough) — the same
  silent-drop class fixed in ``llm_openai`` 18.0.1.6.2. Mid-conversation
  context-injection messages (e.g. capability-gap bridge results) never
  reached Claude models. New ``_anthropic_format_llm_system_message``
  serializes them as attributed user messages (Anthropic allows only a
  single top-level ``system`` parameter).
* [KOENIG][REF] Extracted user-message formatting into
  ``_anthropic_format_llm_user_message`` (method complexity below lint gate).
* [KOENIG][ADD] ``tests/test_system_message.py`` — parity regression tests
  (system message serialized, user/assistant paths unchanged, empty body
  dropped).

18.0.1.1.0 (2026-01-17)
~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] Multimodal file support for images, PDFs, and text files
* [IMP] Refactored to use base module's _prepare_multimodal_attachments() method
* [IMP] Removed duplicate attachment handling code

18.0.1.0.1 (2026-01-07)
~~~~~~~~~~~~~~~~~~~~~~~

* [REM] Removed provider data file - users now create providers manually
* [IMP] Provider data is now user-owned and survives module uninstall

18.0.1.0.0 (2025-10-23)
~~~~~~~~~~~~~~~~~~~~~~~

* [INIT] Initial release
