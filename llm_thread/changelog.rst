18.0.1.33.0 (2026-07-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **FIX-4c: narrow staleness guard to placeholder pattern** —
  ``_isStaleBodyUpdate`` now only skips incoming bodies that match the
  "Thinking..." placeholder pattern, not ANY shorter body. The previous
  "any shorter body" guard also skipped the legitimate final message
  when ``_strip_preamble`` + linkify made it shorter than the streamed
  body — silently defeating Phase-3 F8 (preamble strip).
  Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §12 V7.

18.0.1.32.0 (2026-07-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **FIX-4d: reasoning chunk bus subscription (JS side)** —
  ``llm_store_service.js`` now subscribes to the ``llm.thread/reasoning_chunk``
  bus event and accumulates reasoning text on the message's ``body_json.reasoning``
  field. The UI can render a collapsible "Thinking" section above the answer
  (Kilo-Code pattern). Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §4.

18.0.1.31.0 (2026-07-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **FIX-2: context usage HUD (Kilo-Code-style)** — new
  ``get_context_stats(thread_id)`` server method returns the context window,
  last real prompt tokens (from ``koenig.ai.llm.trace.usage_input``), and an
  estimated breakdown (system/prompt, tools schema, summary, message window).
  The HUD component now displays a context ring/bar + percentage
  (``24.3k / 256k (9%)``) next to the existing tokens/cost, with a click
  popover showing the breakdown. Thresholds: green <70%, warning 70-90%,
  danger ≥90%. Gracefully degrades when ``koenig_ai_core`` is not installed
  (returns zeros → HUD hides the context segment).
  Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §2.

18.0.1.30.0 (2026-07-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **FIX-4g: killed-run transient cleanup** — all terminal run events
  (``run_failed``, ``run_cancelled``, ``run_killed``, ``run_timed_out``) in
  ``llm_store_service.js`` now call ``_removeTransientStatusMessage`` +
  ``stopStreaming`` so the "Analyzing your request…" line and stale SSE
  connections are cleaned up immediately. Before the fix, a killed run
  (dead-worker guard / service restart) left the transient status lingering
  in the sidebar + HUD showing "Killed" with no answer cleanup.
  Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §4.

18.0.1.29.0 (2026-07-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **FIX-4b: placeholder bus broadcast suppressed** — ``_notify_thread``
  now skips ``_bus_send_store`` + ``_bus_send`` when the message was posted
  with ``llm_streaming_placeholder=True`` context. Before the fix, the
  placeholder's bus payload was baked at post time (precommit) but flushed at
  the main transaction's final commit — AFTER all progressive chunk
  broadcasts — so the stale escaped placeholder overwrote the streamed answer
  at the end of a run. The orchestration bridge now handles ``message_create``
  itself (independent cursor, CURRENT body). The direct-SSE path
  (``message_post_from_stream``) also posts the placeholder with the context
  key. Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §4.

* [FIX] **FIX-4c: browser staleness guard** — ``_isStaleBodyUpdate`` helper
  in ``llm_store_service.js`` skips applying a body that is a strict
  downgrade (shorter than the current rendered body while a stream/run is
  active on that thread). Belt + suspenders alongside FIX-4b. Applied in
  both the ``handleStreamMessage`` (SSE) and ``llm.thread/new_message`` (bus)
  handlers. Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §4.

18.0.1.28.0 (2026-07-25)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **FIX-4a: assistant ``message_post`` bodies wrapped in ``Markup``**
  — the fork's ``message_post`` passed the ``str`` output of
  ``_process_llm_body`` to OCB's ``mail.thread.message_post``, which calls
  ``escape(str)`` on plain strings (mail_thread.py:2366). The escaped text
  then got wrapped in ``<p>`` by the Html-field sanitizer, producing the
  double-escaped ``<p>&lt;p&gt;Thinking...&lt;/p&gt;</p>`` the user saw
  replacing streamed answers at the end of a run. Fix: wrap the renderer
  output in ``Markup`` so OCB's ``escape`` is a no-op.
  Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §4.

* [ADD] **FIX-3: ``get_thread_store_data(ids)`` server method** — returns
  the store dict for given thread IDs so the client can insert a
  freshly-created thread deterministically. No dependence on the bus
  broadcast or a heavy ``init_messaging`` re-fetch (which under load takes
  50+ seconds). Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §3.

* [FIX] **FIX-1: stable sidebar ordering** — ``llmThreadList`` getter now
  uses a pinned-order map (``_threadOrderPin``) so background
  ``write_date`` bumps (running threads) don't reshuffle the sidebar
  mid-run. Only user actions (create/archive/delete/tag), new threads, and
  F5 change position.
  Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §1 RC-1a.

* [FIX] **FIX-1: no active-thread yank on create** — ``createNewThread``
  captures the active thread before the create RPC and skips auto-select if
  the user switched threads since clicking ``+``. Deterministic store insert
  + immediate ``selectThread`` (no ``init_messaging`` blocking). Error
  notification on failure (no more silent dead ``+`` button).
  Ref: ``TRACKER_2026-07-25_UI_RESEARCH.md`` §1 RC-1b + §3.

18.0.1.27.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [CHANGE] **CAP-02: resolver split — the ``_effective_tools()`` seam is now
  ``candidate ∩ per-turn availability gates``.** New ``_candidate_tools()``
  (base: the raw ``tool_ids`` column) yields the assistant/bundle/deviation set;
  ``_effective_tools()`` then filters it through each tool's
  ``llm.tool._ai_is_available_for(thread)`` gate (active / capability / consent)
  for the calling user. The single execution seam is unchanged for every reader;
  the environment gates now apply to no-assistant threads too. Backward
  compatible: with no ``requires_capability`` and no consent override, the gate
  is a pass-through (byte-for-byte CAP-01).

18.0.1.26.0 (2026-07-23)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **CAP-01 live tool resolution — ``llm.thread._effective_tools()``
  seam.** New method on the base thread returning ``self.tool_ids`` (unchanged
  behaviour when ``llm_assistant`` is absent). It is the single seam the whole
  execution path reads to decide which tools the model may call, so the tool
  set can be resolved LIVE (assistant-derived in ``llm_assistant``) instead of
  from a per-thread snapshot that rots. Odoo-aligned: capability resolved at
  the point of use, never copied per record. See the fork ADR
  ``llm_assistant/docs_dev/ADR_2026-07-23_THREAD_TOOL_PROMPT_RESOLUTION.md``.

18.0.1.25.1 (2026-07-21)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **Composer attachments lost on thread switch (stale
  attachmentUploader).** The chat container rendered ``<Thread>`` and
  ``<Composer>`` without ``t-key`` — unlike OCB ``discuss.xml:96-97`` which
  keys both on ``thread.localId``. Without the key, switching threads reused
  the Composer component via props updates, so its ``attachmentUploader``
  (created once in ``setup()``) kept pushing uploaded files into the STALE
  composer record of the thread active at mount time. Uploads succeeded
  server-side (parked as pending on ``mail.compose.message``) but never
  appeared in the composer and were silently dropped from the sent message —
  breaking the image-upload path for the media_describe expert. Both
  components now carry ``t-key="activeThread.localId"`` (OCB pattern), so a
  thread switch remounts them with the correct composer. Found during the
  2026-07-21 hardening run.

18.0.1.25.0 (2026-07-21)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] **UI-10: Steps drawer — debug-aware rendering.** The per-turn
  "Work steps (N)" drawer is now debug-aware. Regular users: step messages
  (tool calls, args, results, intermediate assistant narration) are removed
  from the timeline entirely — the slim progress status lines (UI-06) tell
  the story instead, and no drawer toggle renders. Debug users (``?debug=1``):
  every step renders fully expanded (the pre-drawer behavior); the toggle
  still folds a turn for focus. New ``isDebugMode`` getter on the message
  component (OCB ``list_renderer.js`` pattern: ``Boolean(odoo.debug)``);
  ``isStepDrawerOpen`` is now derived from debug mode (replaces the UI-06 S3
  running-turn default). New ``o-llm-step-hidden-user`` CSS class removes
  step messages from layout for regular users.

* [IMP] **UI-11: Hide expert sub-threads from the sidebar by default.**
  New ``is_expert_subthread`` field on ``llm.thread`` (set at creation time
  by the orchestrator's ``_create_and_drain_sub_thread`` — so sub-threads
  are hidden immediately, even while the expert is still running and before
  the ``active = False`` archiving lands). Shipped to the mail store via
  ``_thread_store_dict`` (unconditional, same store-merge rationale as
  ``active``) and declared as ``Record.attr`` on the JS Thread model. The
  sidebar filters them by default; a new "Show expert threads" toggle
  (``fa-user-secret`` icon, next to "Show archived") reveals them for
  debugging.

* [IMP] **UI-12: HUD run-health indicators.** The HUD now shows three
  subtle, real-time counters between the phase indicator and the run-summary
  chip — for ALL users (not debug-only): "● N experts running" (pulsing
  dot, live dispatched-minus-completed), "🔧 N tool calls" (cumulative for
  the current run), "⚠ N errors" (expert/tool failures; persists after a
  failed run until the next run resets it). Only non-zero values render
  (same pattern as the existing tokens/cost spans). New store counters
  ``expertsRunning`` / ``errorCount`` on ``threadRunState`` (reset on
  ``run_started``; the terminal events zero the live counter). New pure
  helper ``getRunHealth`` in ``llm_phase.js`` (Hoot-tested: 8 test cases).

* [TEST] 8 new Hoot cases for ``getRunHealth`` (``llm_phase.test.js`` —
  suite now 45 cases) + a Python store-payload test for
  ``is_expert_subthread`` (``test_llm_thread_search.py``).

18.0.1.24.0 (2026-07-21)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] **UI-08: Sidebar declutter — hover dates.** The persistent per-item
  relative-date label ("Just now", "3h ago", "2d ago") is removed from the
  sidebar. Date context is already given by the bucket headers (Today /
  Yesterday / This Week / Older). Thread timestamps now live in a rich
  tooltip showing the thread name, creation date, and last activity date —
  full info on hover, zero visual noise at rest. New ``threadTooltip``
  method in ``llm_sidebar.js``.

* [IMP] **UI-08: Sidebar declutter — persistent status icon.** Each sidebar
  thread item now has a dedicated status indicator span (``o-llm-thread-status``)
  with a CSS-only fade animation for the "done" check icon. The green check
  stays fully visible for ~8s, then fades to 30% opacity over 2s — enough
  time for the user to glance at the sidebar and see the run finished,
  without the sidebar filling with permanent green checks.

* [IMP] **UI-09 H1: HUD live phase indicator.** The HUD (stats bar under
  the composer) now shows the current orchestration phase in real time:
  "Idle" → "Analyzing…" → "Dispatching expert…" → "Gathering results…" →
  "Synthesizing answer…" → "Done". Driven by bus events already handled
  in ``llm_store_service.js`` — a new ``phase`` field on
  ``threadRunState`` maps each orchestration event to a canonical phase.
  The phase indicator shows a spinner for active phases, a green check for
  "done", a red exclamation for "failed". Includes an elapsed mm:ss counter
  for running phases. New pure helper ``llm_phase.js`` (Hoot-tested: 37
  test cases for event-to-phase mapping, label lookup, run-summary
  formatting, and 30s auto-dismiss logic).

* [IMP] **UI-09 H2: HUD run-summary chip.** After a run completes, a compact
  chip appears in the HUD: "✓ 3 tools · 2 experts · 4.2s". The chip is
  clickable — it opens the steps drawer of the last turn. Auto-dismisses
  after 30 seconds (time-based, not event-based — works even without user
  interaction). A new run automatically hides the old chip. The store
  tracks ``expertCount``, ``toolCount``, and computes ``durationSec`` from
  ``startedAt``/``finishedAt``. New ``getRunSummary`` store method with
  30s auto-dismiss + hide-on-new-run logic.

* [TEST] New Hoot test suite ``llm_phase.test.js`` — 37 test cases covering
  ``eventToPhase`` (12 cases), ``PHASE_LABELS`` (2 cases), ``getPhaseLabel``
  (3 cases), ``formatRunSummary`` (10 cases including singular/plural forms
  and exact combined output), ``getVisibleRunSummary`` (10 cases including
  30s boundary, auto-dismiss, missing shownAt safety, and custom ``now``
  parameter). All 172 Hoot tests pass (94 llm_thread + 78 others).

18.0.1.23.0 (2026-07-21)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] **UI-05: Instant client-side status line.** On
  ``orchestration_started``, an ephemeral transient message ("Analyzing your
  request…") is inserted into the thread timeline immediately — sub-second
  perceived feedback. It follows the OCB ``is_transient`` precedent
  (``discuss_core_common_service.js:53-69``) and is automatically removed
  when the real progress message arrives via the ``llm.thread/new_message``
  WebSocket bus subscriber, or on done/error/stop. Rapid double-submit guard
  skips insertion if a transient already exists. New pure helper
  ``buildTransientStatusMessage`` (Hoot-tested).

* [IMP] **UI-06 S1: Progress notification classification.** New pure helper
  ``isLLMProgressMessage(msg)`` classifies system-style notifications
  (``message_type='notification'`` + no ``llm_role``) as progress messages.
  The ``className`` getter adds ``o-llm-message-status`` to these messages,
  enabling slim status-line rendering. Error messages are excluded (they stay
  prominent). This also fixes the "Unbenannt" author header on progress
  notifications — status lines never render the author header.

* [IMP] **UI-06 S2: Slim in-between status lines.** SCSS on the existing
  empty CSS hooks (``o-llm-step``, ``o-llm-message-status``): hides the
  avatar sidebar and author/date header for step messages (tool/intermediate
  assistant) and progress notifications. The message content takes full width
  as a muted, small, one-line status row. User messages and final answers
  keep full chrome (avatar, author, timestamp). Progress notifications are
  constrained to one line with ellipsis truncation. The ``display: none``
  rules use ``!important`` to override Bootstrap's ``d-flex`` class
  (``display: flex !important``) which OCB adds to the sidebar/header
  elements in ``message.xml:23,35``.

* [IMP] **UI-06 S3: Drawer defaults — active turn open, completed collapsed.**
  The steps drawer now defaults to OPEN for the latest turn of a running
  thread, so the user sees the live status feed (tool calls, master
  narration, progress notifications) during an active AI run. Completed turns
  default to collapsed (folded "▸ N work steps"). Explicit user toggles always
  take precedence over the default.

* [IMP] **UI-07: König Intelligence avatar for AI messages.** The
  ``authorAvatarUrl`` getter is patched to return the branded König
  Intelligence app icon (``/llm/static/description/icon.png``) for all AI-side
  ``llm.thread`` messages (assistant, tool, progress). User messages keep the
  user's own avatar. OCB precedent: ``message.js:238-253`` ``authorAvatarUrl``
  getter.

* [TEST] Extended ``llm_message_classify.test.js`` with
  ``isLLMProgressMessage`` tests (7 new cases: classification, null safety,
  error exclusion, step exclusion). Extended ``llm_thread_messages.test.js``
  with ``buildTransientStatusMessage`` tests (8 new cases: shape, escaping,
  date default, progress classification, transient flag, step exclusion) and
  fractional-id removal tests.

18.0.1.22.2 (2026-07-21)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][IMP] EFF-01 P1-3: ``_maybe_generate_name`` now passes
  ``reasoning_effort='none'`` + ``max_tokens=50`` to ``simple_completion``
  (explicit call-site override per D2 — stays ``none`` even when the model
  record is configured ``low``). Measured: ``none`` cuts the title call
  from 8–55 s to ~0.4 s (RESEARCH_2026-07-20 §1.1 — 1849 tokens → 12).

18.0.1.22.1 (2026-07-20)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **OCB-canonical authorless bus notifications.** ``llm.thread``
  overrides ``_message_compute_author`` to delegate to ``super()`` with
  ``raise_on_email=False``, mirroring the OCB ``discuss.channel`` precedent
  (``discuss_channel.py:696-697``). Progress/system messages
  (``message_type='notification'``, ``subtype_xmlid='mail.mt_note'``) are
  posted with ``author_id=False`` and no ``email_from`` — they have no human
  sender. The base guard raised ``UserError`` on every authorless
  notification. Safe because ``_notify_thread`` is already a no-op for the
  email pipeline on ``llm.thread``; the bus broadcast (the real delivery
  path) does not use ``email_from``. Regression test:
  ``test_message_compute_author.py``.

* [FIX] **Explicit reactive bus-message linking.** The
  ``llm.thread/new_message`` WebSocket bus subscriber in
  ``llm_store_service.js`` now links each inserted ``mail.message`` into the
  target ``llm.thread``'s reactive ``messages`` collection (dedupe by id) via
  the new pure ``linkMessagesToThread`` helper. Previously it only did
  ``mailStore.insert(...)`` — the message landed in the store but the Thread
  component (which renders ``thread.messages``) never re-rendered, so
  background-posted answers only appeared after a manual reload. Mirrors the
  OCB ``discuss.channel/new_message`` handler
  (``discuss_core_common_service.js:147-160``).

* [FIX] **Optimistic HTML escaping + ghost cleanup.** The optimistic user
  message body is now built by ``buildOptimisticMessageBody``, which
  HTML-escapes the raw user text (OCB ``escape``) so pasted markup
  (``<script>``, ``&``) renders as text, not parsed/executed. The shared
  ``_removeOptimisticMessage`` helper now runs on the SSE ``message_create``
  path AND on the EventSource ``onerror`` / creation-failure paths, so a
  ghost of the user's text never lingers after a dead stream. Helpers are
  pure (Hoot-tested in ``llm_thread_messages.test.js``).

* [FIX] **Setup-time reactive service usage.** ``thread_patch.js`` now
  resolves the ``llm.store`` service during ``setup()`` and wraps it in
  ``useState`` (``this.llmStore = useState(useService("llm.store"))``)
  instead of calling ``useService`` lazily inside a getter. Calling a hook
  outside ``setup()`` breaks the OWL hook contract and silently drops
  reactivity / cleanup. The store is a singleton, so resolving it for
  non-LLM threads is cheap; ``isLLMThread`` guards keep LLM-specific
  behavior gated.

* [FIX] **Per-thread teardown.** ``LLMChatClientAction.cleanup()`` now calls
  ``stopStreaming(threadId)`` for the active thread only, instead of
  ``this.llmStore.destroy()``. The store is a singleton shared across every
  LLM thread and across background orchestration runs on other threads;
  ``destroy()`` closed every active EventSource and cleared the whole
  ``streamingThreads`` set, killing in-flight runs the user could not see.

18.0.1.22.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Thread header: provider switch now selects the provider's *default*
  model (the store loads the field ``default``; the header read the
  non-existent ``is_default`` and silently fell back to ``models[0]``), and
  the "Default" badge in the model dropdown now renders (same one-word fix
  in the XML). RESEARCH_2026-07-19_FULL_SYSTEM_REVIEW §6.5. No Hoot component
  test infra exists for this component yet — component tests are a P1 backlog
  item (noted in the research doc).

18.0.1.21.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **SSE ``done`` event no longer prematurely sets orchestration threads
  to "done".** The ``orchestration_started`` SSE event now sets
  ``threadRunState`` to "running" with an ``orchestration: true`` flag. The
  ``done`` event checks this flag — if true, it only stops streaming without
  setting a terminal state. The background job's ``run_done`` bus event sets
  the terminal state later. Previously, the SSE ``done`` event (sent by the
  controller's ``finally`` block after the orchestration generator exhausts)
  set the thread to "done" immediately after the background job was
  dispatched — before the job produced any result. This caused the "status
  shows OK but no result" symptom.

18.0.1.20.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] **``run_id`` tracking in ``threadRunState``:** the
  ``_handleOrchestrationBusEvent`` and ``setThreadRunState`` methods now
  track ``run_id`` (from ``payload.run_id``) in the per-thread run state.
  This fixes the status indicator jumping erratically
  (``done → ! → error → done``): the 60s safety-net poll in
  ``orchestration_bus_service.js`` was overriding the current run's
  ``running`` state with terminal states from old completed runs on the
  same thread. Now the poll skips stale runs (``run_id`` mismatch).
  OCB reference: ``discuss_channel.py:649-658`` (per-message bus delivery).

18.0.1.19.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] **OCB-canonical live-update architecture:** ``llm.thread`` now
  inherits ``bus.listener.mixin`` and broadcasts new threads + new
  messages via the WebSocket bus (``_bus_send_store`` →
  ``mail.record/insert``). New threads appear in the sidebar immediately;
  new messages (from user OR background job) appear without a manual
  reload. OCB reference: ``discuss_channel.py:1183`` (new channel),
  ``discuss_channel.py:649-658`` (new message).

* [IMP] **SSE polling loop removed for orchestration runs:** the
  ``_stream_orchestration_progress`` method (which held an HTTP worker
  for the entire background run duration via a ``while True`` +
  ``time.sleep(1)`` polling loop) has been removed. Orchestration
  progress is now delivered exclusively via the WebSocket bus. SSE is
  kept ONLY for direct LLM token streaming. The ``done`` SSE event now
  always sets terminal state (the ``_orchestrationThreads`` conditional
  is removed). The ``bus_event`` and ``run_terminal`` SSE cases are
  removed. The ``_onSSEOrchestrationEvent`` callback is removed.

* [IMP] **10s poll reduced to 60s safety net:** the RPC poll fallback
  in ``orchestration_bus_service.js`` is reduced from 10s to 60s. The
  WebSocket bus is now the PRIMARY delivery mechanism; the poll is a
  minimal safety net for dev mode (``workers=0``, no WebSocket).

18.0.1.18.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] **Optimistic user message UI (BUG-4 fix):** ``startLLMStreaming``
  now inserts the user message into the mail store BEFORE creating the
  ``EventSource``, so the user sees their message immediately. The
  ``message_create`` SSE event handler removes the optimistic message
  (negative temp ID) before adding the real one (with the real DB ID).
  If the SSE fails, the optimistic message stays — reconciled on next
  page reload. This eliminates the "I can't see my question initially"
  symptom when reusing empty threads.

18.0.1.17.0 (2026-07-19)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **SSE error message posting (regression):** the broad ``except
  Exception`` in ``_generate_assistant_response`` that posted an error
  message to the thread was removed in 18.0.1.16.0 (retry refactor). The
  SSE controller's ``except Exception`` handler now restores this behavior:
  it rolls back any poisoned transaction, posts an error message via
  ``_post_error_message``, commits, and then sends the error SSE event.
  Best-effort — non-fatal if the cursor is too poisoned to post.
* [ADD] **SSE callback hook:** ``llmStore._onSSEOrchestrationEvent`` is a
  generic hook slot (initially ``null``) that König-specific services can
  set to handle SSE-delivered bus events. The ``handleStreamMessage``
  ``bus_event`` case calls this callback after the shared
  ``_handleOrchestrationBusEvent`` handler. This enables the König
  ``orchestration_bus_service.js`` to handle ``run_paused`` events
  delivered via SSE (dev mode, no WebSocket).

18.0.1.16.0 (2026-07-17)
~~~~~~~~~~~~~~~~~~~~~~~~~~

* [ADD] **``_handleOrchestrationBusEvent`` shared handler:** the
  orchestration event → thread-state-transition switch statement is now a
  method on ``llmStore`` (in ``llm_store_service.js``). Both the WebSocket
  bus subscriber (König ``orchestration_bus_service.js``) and the SSE
  progress loop call this shared method. Eliminates duplication.
* [ADD] **``bus_event`` and ``run_terminal`` SSE event types:** the
  ``handleStreamMessage`` method now handles ``bus_event`` (a progress event
  from the background run, delivered via the continuous SSE channel) and
  ``run_terminal`` (the run reached a terminal state — the SSE is about to
  close). These enable the durable live-update architecture (no limbo state
  between SSE close and bus event delivery).

18.0.1.15.0 (2026-07-17)
~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **UI-1:** Thread run-state indicators (done/failed/cancelled) now
  persist permanently instead of flashing for 3 seconds. Previously, the
  indicators disappeared after 3s and were lost on page refresh. The
  ``threadFinishedFlash`` method no longer has a time-based expiration; the
  tick only runs for actively running threads (terminal states don't need
  the tick). Added ``_hasTimeSensitiveRunState()`` call from
  ``threadElapsedLabel`` to restart the tick when a new bus event arrives.
* [FIX] **UI-2:** Indicator icons on the selected (active) thread are now
  pure white on the red background. Previously, ``.text-success``,
  ``.text-danger``, and ``.text-primary`` kept their default colors —
  invisible on red. Now all indicator classes are overridden to white.
* [FIX] **UI-3:** Jump arrows now navigate to BOTH user messages AND AI
  answers. Previously, only AI answers were found (the
  ``.o-llm-message-user`` class was confirmed present but the query
  needed a fallback). Arrows moved from ``bottom: 88px`` to ``130px`` to
  clear the composer + OCB scroll-to-bottom button. ``scrollIntoView``
  uses ``block: "nearest"`` to prevent whitespace below the composer.
* [ADD] **UI-4:** Sidebar is now resizable (drag the right edge).
  Follows the König wiki pattern: CSS custom property
  ``--llm-sidebar-width``, localStorage persistence
  (``llm_thread.sidebar_width``), double-click to reset to 280px default.
* [ADD] **UI-5:** Collapsed sidebar section state (Today/This Week/Older)
  is now persisted in localStorage (``llm_thread.collapsed_buckets``).
  Survives page refresh.

18.0.1.14.0 (2026-07-16)
~~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] **BL-4:** Tag ACL fixed — users can now create AND edit tags
  (was create-only, ``1,0,1,0`` → ``1,1,1,0``). Users could create
  typo'd tags but not fix them.
* [FIX] **BL-7:** ``_serverSearch`` now shows a one-time notification on
  failure instead of silently falling back to client-side search.
* [FIX] **BL-6:** ``formatDate`` fallback now uses OCB ``formatDateTime``
  (was ``dt.toLocaleString()`` which used the browser locale instead of
  Odoo's user language setting).
* [ADD] **BL-8:** ``LLMBulkTagDialog`` now has an Add/Remove mode toggle.
  Remove mode uses ``[3, id]`` (unlink) instead of ``[4, id]`` (link).

18.0.1.13.9 (2026-07-15)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] ``deleteThread`` now calls ``clearThreadRunState(threadId)``
  after unlinking the thread. Previously, the ``threadRunState`` entry
  persisted as a memory leak (the Phase 7 tick fix stopped clearing
  terminal states via the tick, so ``deleteThread`` is the cleanup
  path). The map is still bounded by thread count, but now shrinks
  when threads are deleted.

18.0.1.13.8 (2026-07-15)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Sidebar run-state indicators (check/exclamation/ban icons) no
  longer vanish when clicking a thread. Root cause: the on-demand tick
  stopped after 1 second (no running threads found), leaving stale flash
  icons in the DOM until a manual re-render (e.g. clicking a thread)
  evaluated ``threadFinishedFlash`` → null and removed them. Fix: the
  tick now stays running while there are terminal states still within
  the 3-second flash window (``has = true`` for ``finishedAt <= 3s``),
  and increments ``elapsedTick`` one final time before stopping —
  triggering a re-render that removes the stale icon. Terminal states
  are also no longer deleted from ``threadRunState`` (previously
  ``clearThreadRunState`` was called after 3s), which prevents the
  bus-service poll from re-setting them every 10s — the feedback loop
  that caused indicators to flash on and off. The map grows at most one
  entry per thread (bounded by thread count).

18.0.1.13.7 (2026-07-15)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Composer ``stopStreaming()`` now also calls
  ``llmStore.stopOrchestration(threadId)`` (if it exists — added by
  ``koenig_ai_orchestrator``) so the stop button cancels background
  orchestration runs, not just closes the SSE stream. Guarded with
  ``typeof === "function"`` so it's a no-op when the orchestrator
  addon is not installed.

18.0.1.13.6 (2026-07-10)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Jump arrows now hop between **all user messages AND AI final
  answers** (``.o-llm-message-user, .o-llm-final-answer``), skipping the
  in-between thinking/tool-step messages — so they're a real "skim past
  the Thinking noise" tool. Previously they only targeted user messages,
  so the arrows skipped every AI answer. Button titles updated to
  "Previous/Next message (user / AI answer)".

18.0.1.13.5 (2026-07-10)
~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] Message spacing: a little more room between the author/timestamp
  header line (e.g. "glm-5.2 … Today at 6:28 AM") and the message
  body/bubble (``.o-mail-Message-contentContainer`` margin-top), and a
  bit more vertical separation between consecutive messages
  (``.o-llm-message`` margin-bottom 0.5rem → 1rem).

18.0.1.13.4 (2026-07-10)
~~~~~~~~~~~~~~~~~~~~~~~

* [IMP] Sidebar date-bucket headers (Today/Yesterday/This week/Older)
  are now visually-weighted section landmarks instead of blending into
  the thread items: subtle background tint + bottom divider + bolder,
  letter-spaced type in a darker grey for contrast, and ``position:
  sticky`` so the current group's header stays visible while the list
  scrolls (ChatGPT/Claude pattern). Thread items get more breathing
  room (padding + line-height) and a lighter hairline separator, and
  buckets get a small gap between them so the groups read as distinct
  sections. (BL-17 — the 18.0.1.13.2 padding bump was insufficient;
  the headers needed visual weight, not just padding.)

18.0.1.13.3 (2026-07-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] Live streaming (orchestration path): ``reloadThreadMessages``
  now calls ``thread.fetchNewMessages()`` instead of
  ``thread.fetchMessages()``. ``fetchMessages`` inserts records into
  the store but does NOT splice them into ``thread.messages``, so the
  Thread component never re-rendered — the assistant answer posted by
  a background orchestration run only appeared after navigating away
  and back (which calls ``fetchNewMessages`` via the Thread
  component's ``onWillUpdateProps``). ``fetchNewMessages`` splices the
  new messages into the reactive collection → the answer shows up live
  (combined with the ``koenig_ai_orchestrator`` polling-reconcile that
  triggers the reload when the ``run_done`` bus event is missed).

18.0.1.13.2 (2026-07-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] P-HUD: ``_get_thread_stats`` renamed to ``get_thread_stats``
  (public) — Odoo 18's ``get_public_method`` rejects ``_``-prefixed
  methods from RPC with ``AccessError: Private methods ... cannot be
  called remotely``, which silently hid the whole HUD (the catch set
  stats=null → hasStats=false → not rendered). The HUD now renders
  whenever stats are loaded (always shows at least the model name;
  tokens/cost/monthly show when non-zero).
* [FIX] Jump arrows: repositioned from ``bottom-0 end-0`` (overlapped
  the send button + the thread-area scrollbar) to float above the
  composer + P-HUD, offset from the right edge.
* [FIX] Cramped layout: date-bucket headers + thread items padding
  ``py-1`` → ``py-2``; message bubbles get ``margin-bottom`` for
  vertical breathing room.
* [FIX] Thinking/tool messages: the streaming "Thinking…" placeholder
  is now muted + italic with no bubble (was styled like a finished
  answer); step (tool / intermediate-assistant) content is lighter
  grey + smaller, to distinguish from the final answer + user question.

18.0.1.13.1 (2026-07-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [FIX] P-HUD: infinite RPC loop — `onWillUpdateProps` fired on every
  parent re-render (not just thread change), causing hundreds of
  `_get_thread_stats` RPCs per second. Fixed by tracking the last
  loaded threadId and skipping reloads when it hasn't changed.
* [FIX] P-HUD: `_get_thread_stats` method signature mismatch — the
  RPC passed `[[threadId]]` (list) but the method took no args. Fixed
  to accept a `thread_id` parameter and `browse()` the record.
* [FIX] SSE `done` event: for orchestration runs, `done` just means
  the SSE stream is finished (the background job was dispatched), NOT
  that the AI response is complete. Now tracks orchestration-mode
  threads and skips the terminal state for them — the bus events
  (run_done, run_failed) handle the terminal state.
* [FIX] SSE `orchestration_started` event: now handled (was an unknown
  type, logged as a warning). Marks the thread as orchestration-mode.
* [FIX] Auto-rename guard: frontend-generated thread names start with
  "Chat " (not "New Chat" or "AI Chat -"), so the guard never
  matched. Added "Chat " to the placeholder pattern check.

18.0.1.13.0 (2026-07-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P-UX Improvements Round 2 — 9 UX improvements for the
  LLM chat sidebar and message area:
* [ADD] Item 1: Removed per-card archive/delete buttons from thread
  items (caused height shift on hover). Archive/delete is now via
  select-mode toolbar only.
* [ADD] Item 2: Compact single-line thread cards (name + indicator +
  time, no model_id or tag badges). Auto-rename: after the first user
  message, an LLM generates a 3-5 word title via a new
  ``simple_completion`` provider method (non-streaming, no
  mail.message overhead). Works for both SSE and background
  orchestration paths.
* [FIX] Item 3: Running indicator (spinner + elapsed mm:ss + done/failed
  flash) now works for SSE streaming too — previously only background
  orchestration runs set ``threadRunState``. The SSE path now sets
  running/done/failed states. Stale terminal states are cleaned up
  after the 3-second flash window.
* [FIX] Item 4: Streaming content now updates live without F5. Root
  cause: ``Record.many().push()`` (plain Array method, no reactivity)
  changed to ``Record.many().add()`` (OCB pattern, triggers re-render).
  Also added a safety link check in message_chunk/message_update for
  SSE reconnection scenarios.
* [ADD] Item 6: Send button now shows ``fa-paper-plane-o`` icon instead
  of "Send" text for LLM threads. Composer area padding reduced.
* [ADD] Item 7: Message bubbles — user messages get a blue-tinted
  bubble, AI answers a grey bubble with border, tool/step messages are
  muted/smaller, error messages are red-tinted.
* [ADD] Item 7.1: Jump arrows (up/down) to skim between user messages
  in long conversations. Floating buttons at bottom-right of chat area.
* [ADD] Item 5 / P-HUD: Subtle stats display under the composer showing
  model name, dispatched experts, tokens, € cost for the thread, and
  month-to-date spend vs budget limit. Data from a single
  ``_get_thread_stats()`` RPC.
* [ADD] ``simple_completion()`` method on ``llm.provider`` + ``llm.model``
  for lightweight one-shot LLM calls (title generation, etc.) without
  mail.message overhead.
* [ADD] ``_maybe_generate_name()`` + ``generate_name()`` RPC on
  ``llm.thread`` — auto-generates a 3-5 word title from the first user
  message. Guarded: only fires when the name is still a default
  placeholder and exactly 1 user message exists.
* [ADD] ``thread_update`` SSE event type — the server yields this after
  auto-renaming a thread; the client merges it into the mail store.
* [ADD] GAP-D: ``generate()`` now sets ``llm_thread_id`` context so
  spend rows are tagged with the originating thread for per-thread
  cost attribution. The orchestrator sets it to the MAIN thread's ID
  before calling ``generate()`` on expert sub-threads.

18.0.1.12.2 (2026-07-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] P-UX template crash: ``llm_sidebar.xml`` used
  ``Boolean(state.selectedThreadIds[thread.id])`` in a ``t-att-checked``
  expression — ``Boolean`` is not in the QWeb expression scope (compiles to
  ``ctx['Boolean']`` = undefined). Fixed to ``!!state.selectedThreadIds[thread.id]``.

18.0.1.12.1 (2026-07-09)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] P-UX devil's advocate review — 8 fixes across Critical /
  High / Medium severity:
* [FIX] §1.4 (Critical): ``search_threads`` now caps the
  ``mail.message.body`` ilike scan at ``limit=100`` and the own-thread
  pre-scope at ``limit=500`` — prevents a full-table-scan performance
  landmine on common search terms.
* [FIX] §2.1 (Critical): ``llmThreadList`` sort now uses luxon
  ``deserializeDateTime`` instead of ``new Date(serverString)`` —
  fixes broken thread ordering on Safari/iOS where the space-separated
  server datetime parses as ``Invalid Date``.
* [FIX] §2.8 (High): Mobile routing — ``Thread.open()`` override for
  ``llm.thread`` navigates to the LLM chat client action on mobile
  instead of falling through to the backend form view (chat UI was
  unreachable on mobile).
* [FIX] §1.3 (High): ``_init_messaging`` now caps thread search at
  ``limit=200`` — prevents payload bloat for users with hundreds of
  archived threads.
* [FIX] §2.2 (Medium): P-UX custom fields (``active``, ``tag_ids``,
  ``provider_id``, ``model_id``, ``tool_ids``) are now declared as
  ``Record.attr`` on the Thread model via ``patch(Thread.prototype,
  setup)`` — enables proper store tracking + incremental updates.
* [FIX] §2.6 (Medium): Replaced direct store mutations
  (``Object.assign(thread, ...)``, ``thread.active = false``) with
  ``mailStore.insert({ "mail.thread": [...] })`` — OCB store update pattern.
* [FIX] §2.5 (Medium): ``setInterval`` elapsed-timer is now on-demand
  (``_maybeStartTick`` / ``_maybeStopTick``) — starts only when a run is
  active, stops when idle, instead of running permanently on every
  sidebar instance.
* [FIX] §1.5 (Medium): Bulk delete of >5 threads now requires a
  two-step confirmation dialog (first "Are you sure?", then "Last
  chance") to prevent accidental mass deletion.

18.0.1.12.0 (2026-07-08)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][ADD] P-UX: thread & memory management UX in the chat sidebar.
  The sidebar (previously a flat, duplicated mobile + desktop list) is now a
  single ``LLMSidebar`` component with date-bucket grouping
  (Today / Yesterday / This Week / Older — calendar-day based via luxon
  ``deserializeDateTime`` + ``startOf("day")``, not a rolling 24h window),
  client-side name search (instant, accent-insensitive via ``cleanTerm``)
  plus a debounced server-side message-content search (``llm.thread.
  search_threads``), colored tag badges, per-item archive/unarchive/delete
  with confirmation dialogs, a "Show archived" toggle, multi-select bulk
  archive/delete/tag, and richer thread items (model name + relative date).
  ``LLMChatContainer`` now delegates the sidebar to one ``<LLMSidebar>``
  instance per layout (mobile slide-in / desktop collapse).
* [KOENIG][ADD] ``llm.thread.tag`` model (user-manageable colored tags,
  ``project.tags`` pattern: ``name_uniq`` constraint, ``name_create`` dedup,
  ``color`` 0-11). ``tag_ids`` many2many on ``llm.thread`` (auto relation —
  required for compatibility with the ``llm.thread.mock`` prototype-inheriting
  transient in ``llm_assistant``; an explicit relation table would collide).
  Tag form/list/search views + a "Chat Tags" config menu.
* [KOENIG][ADD] ``_thread_to_store`` now sends ``active`` + ``tag_ids``
  unconditionally (no stale-store on last-tag-removal / unarchive).
  ``res.users._init_messaging`` loads BOTH active and archived threads
  (``active_test=False``) so the "Show archived" toggle is instant (no RPC).
* [KOENIG][ADD] ``llm.thread.search_threads(search_term)``: owner-scoped
  search by name or message content (messages are ``mail.message`` rows with
  ``model='llm.thread'`` + ``res_id``), ``active_test=False`` so archived
  threads are findable (search is the main way back to an archived thread).
* [KOENIG][ADD] ACL: users can now unlink ``llm.thread`` (``1,1,1,1`` — was
  ``1,1,1,0``) so bulk delete + the GDPR "delete my threads" right work; the
  existing owner-only record rule (``llm_thread_rule_personal``) scopes
  deletion to own threads. ``llm.thread.tag`` ACL: users read+create
  (``1,0,1,0``), managers full CRUD (curation of the shared ``name_uniq``
  namespace).
* [KOENIG][ADD] Hoot suite for the date-bucket helper
  (``utils/llm_date_bucket.js`` — pure function, 8 tests incl. the
  calendar-day-vs-24h boundary, timezone-independent).
* [KOENIG][ADD] Python tests: ``llm.thread.tag`` model (create, default
  color, ``name_uniq``, ``name_create`` dedup, thread relation),
  ``search_threads`` (name + content + archived + owner-scoped + result
  shape), ``_init_messaging`` archived loading, ACL (user unlinks own /
  cannot unlink others' / tag create-vs-unlink).

18.0.1.11.1 (2026-07-08)
~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][FIX] P-CHAT M3: ``reloadThreadMessages`` now calls
  ``thread.fetchMessages()`` (the OCB Thread model's canonical
  message-reload path, thread_model.js line 679) instead of
  ``mailStore.fetchData({ init_messaging: {} })``. The latter refreshes
  the thread LIST but does NOT re-fetch the specific thread's messages
  → the assistant answer (posted by a background orchestration run)
  stayed invisible without a manual page reload. This was the root cause
  of "no live update of the thread contents" — the bus events were
  posted correctly (verified: 32 bus.bus records for
  koenig.ai.orchestration) but the reload didn't fetch the messages.

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
