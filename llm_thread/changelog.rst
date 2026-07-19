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
