/** @odoo-module **/

import {
    buildOptimisticMessageBody,
    buildTransientStatusMessage,
    linkMessagesToThread,
    removeOptimisticMessageFromThread,
} from "../utils/llm_thread_messages";
import { getVisibleRunSummary } from "../utils/llm_phase";
import { Deferred } from "@web/core/utils/concurrency";
import { _t } from "@web/core/l10n/translation";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { reactive } from "@odoo/owl";
import { registry } from "@web/core/registry";

/**
 * LLM Store Service - Integrates with existing mail.store
 * Provides LLM-specific functionality without breaking mail components
 */
export const llmStoreService = {
    dependencies: ["orm", "mail.store", "notification", "bus_service"],

    start(env, { orm, "mail.store": mailStore, notification, bus_service }) {
        const llmStore = reactive({
            // NOTE: Threads are now loaded via standard mail.store, no need for separate Map
            // Map<id, LLMModel>
            llmModels: new Map(),
            // Map<id, LLMProvider>
            llmProviders: new Map(),
            // Map<id, LLMTool>
            llmTools: new Map(),
            // Set<threadId> currently streaming
            streamingThreads: new Set(),
            // Map<threadId, EventSource>
            eventSources: new Map(),
            // Resolves when LLM data is loaded
            isReady: new Deferred(),
            // Pending AI chat open from client action (bypasses unreliable bus)
            // { threadId, model, resId, autoGenerate }
            pendingOpenInChatter: null,

            // P-CHAT M2: steps-drawer expand state per turn. Keyed by turn id
            // (the user message id that opens the turn). Default = collapsed
            // (absent key) — tool/intermediate-assistant messages render as a
            // compact "Arbeitsschritte (N)" row; expanding reveals the step
            // details (args/result JSON via the tool-message component).
            stepDrawerOpen: {},

            // P-CHAT M3: live run-feedback state per thread. Keyed by thread id.
            // { state, label, startedAt, finishedAt, error }
            //   state: "running" | "done" | "failed" | "cancelled"
            //   label: human status (expert labels stream in via the bus)
            //   startedAt / finishedAt: epoch ms (for the sidebar elapsed mm:ss
            //   and the done/failed ✓/! flash)
            // Filled by the koenig_ai_orchestrator bus subscriber (the fork store
            // exposes the API; it does NOT know the bus channel name).
            threadRunState: {},

            // FIX-1 (TRACKER_2026-07-25_UI_RESEARCH.md §1 RC-1a): stable
            // sidebar ordering. Pinned array of thread IDs (most-recent-first).
            // Background write_date bumps (running threads) update tooltip/
            // bucket only — NOT position. Only these events may change order:
            //   (a) user creates/archives/deletes/tags a thread
            //   (b) a genuinely NEW thread arrives (goes top)
            //   (c) F5 / full reload (resets to write_date sort — natural on
            //       page reload since JS state is wiped)
            _threadOrderPin: null,

            // Computed properties - using mailStore as source of truth
            get activeLLMThread() {
                // Check if current active thread in mail.store is an LLM thread
                const activeThread = mailStore.discuss?.thread;
                return activeThread?.model === "llm.thread" ? activeThread : null;
            },

            get isLLMThread() {
                return this.activeLLMThread !== null;
            },

            get llmThreadList() {
                // Get all LLM threads from mailStore
                const allThreads = Object.values(mailStore.Thread.records || {});
                const llmThreads = allThreads.filter((thread) => thread.model === "llm.thread");

                // FIX-1: stable ordering. Sort by write_date DESC (as before)
                // but keep a pinned order for existing threads so background
                // write_date bumps (running threads) don't reshuffle the sidebar.
                const ts = (val) => {
                    if (!val) {
                        return 0;
                    }
                    // P-UX review §2.1: new Date("YYYY-MM-DD HH:MM:SS") is
                    // implementation-defined (Safari → Invalid Date). Use luxon
                    // deserializeDateTime for safe server-datetime parsing.
                    const dt = val instanceof luxon.DateTime ? val : deserializeDateTime(val);
                    return dt?.isValid ? dt.toMillis() : 0;
                };

                // Sort by write_date DESC to get the "natural" order.
                const sorted = [...llmThreads].sort((a, b) => ts(b.write_date) - ts(a.write_date));

                // Initialize or reconcile the pin with the current thread set.
                // On first access (or after threads disappeared): rebuild the
                // pin from the sorted order. This is the F5-equivalent — a
                // fresh page load wipes JS state, so the pin starts null and
                // is rebuilt here on first access.
                const currentIds = new Set(llmThreads.map((t) => t.id));
                if (!this._threadOrderPin) {
                    this._threadOrderPin = sorted.map((t) => t.id);
                } else {
                    // Drop IDs no longer present; add new IDs at the front
                    // (sorted by write_date among themselves).
                    const pinSet = new Set(this._threadOrderPin);
                    const newThreads = sorted.filter((t) => !pinSet.has(t.id));
                    const knownIds = this._threadOrderPin.filter((id) => currentIds.has(id));
                    this._threadOrderPin = [...newThreads.map((t) => t.id), ...knownIds];
                }

                // Build the result in pin order (stable), looking up each
                // thread from the store.
                const byId = new Map(llmThreads.map((t) => [t.id, t]));
                return this._threadOrderPin.map((id) => byId.get(id)).filter((t) => t); // Drop any stale pin entries
            },

            // LLM-specific methods using standard fetchData approach
            async ensureThreadLoaded(threadId) {
                // Check if thread already exists in mailStore
                const thread = mailStore.Thread.get({
                    model: "llm.thread",
                    id: threadId,
                });
                if (thread) {
                    return thread;
                }

                // If thread not found, it might not be accessible to current user
                // or wasn't loaded in init_messaging (e.g., old thread, different user)
                console.warn(`Thread ${threadId} not found in mailStore`);
                return null;
            },

            async sendLLMMessage(threadId, content, attachmentIds = []) {
                if (!threadId || (!content?.trim() && attachmentIds.length === 0)) {
                    return;
                }

                try {
                    await this.startLLMStreaming(threadId, content, attachmentIds);
                } catch (error) {
                    console.error("Error sending LLM message:", error);
                    notification.add(
                        _t(
                            "Could not send your message. Please check your connection and try again."
                        ),
                        { type: "danger" }
                    );
                }
            },

            async startLLMStreaming(threadId, message, attachmentIds = []) {
                this.stopStreaming(threadId);

                this.streamingThreads.add(threadId);

                // P-UX Item 3: also set threadRunState so the sidebar
                // indicator (elapsed time + done/failed flash) works for
                // SSE streaming, not just background orchestration runs.
                this.setThreadRunState(threadId, {
                    state: "running",
                    label: "AI is thinking...",
                    startedAt: Date.now(),
                    finishedAt: null,
                    error: false,
                });

                // BUG-4 fix: Optimistic UI — insert the user message into
                // the mail store BEFORE creating the EventSource, so the user
                // sees their message immediately. The real message arrives
                // via the ``message_create`` SSE event (with the real DB ID).
                // The ``message_create`` handler removes optimistic messages
                // (negative temp IDs) for this thread before adding the real
                // one. If the SSE fails (onerror) or EventSource creation
                // throws, ``_removeOptimisticMessage`` drops the temp message
                // too — ghosts never persist. If the server actually posted
                // the real message despite the SSE failure, it is re-linked
                // via the ``llm.thread/new_message`` bus subscriber or the
                // next reload.
                if (message) {
                    const tempId = -Date.now();
                    const optimisticMsg = {
                        id: tempId,
                        model: "llm.thread",
                        res_id: threadId,
                        // P0: escape raw user text before building the
                        // optimistic HTML body — pasted markup (e.g.
                        // ``<script>`` or ``&``) must render as text, not be
                        // parsed/executed. Mirrors OCB ``escape`` usage
                        // (@web/core/utils/strings).
                        body: buildOptimisticMessageBody(message),
                        llm_role: "user",
                        author_id: [
                            mailStore.currentUser?.partnerId || false,
                            mailStore.currentUser?.name || "",
                        ],
                        is_error: false,
                        date: new Date().toISOString(),
                        message_type: "comment",
                    };
                    mailStore.insert({ "mail.message": [optimisticMsg] }, { html: true });
                    const thread = mailStore.Thread.get({
                        model: "llm.thread",
                        id: threadId,
                    });
                    const tempMsg = mailStore.Message.get(tempId);
                    if (thread && tempMsg) {
                        thread.messages.add(tempMsg);
                    }
                    // Track the temp ID so message_create can remove it.
                    this._optimisticMsgIds = this._optimisticMsgIds || {};
                    this._optimisticMsgIds[threadId] = tempId;
                }

                try {
                    let url = `/llm/thread/generate?thread_id=${threadId}`;
                    if (message) {
                        url += `&message=${encodeURIComponent(message)}`;
                    }
                    if (attachmentIds.length > 0) {
                        url += `&attachment_ids=${attachmentIds.join(",")}`;
                    }
                    const eventSource = new EventSource(url);

                    this.eventSources.set(threadId, eventSource);

                    eventSource.onmessage = (event) => {
                        const data = JSON.parse(event.data);
                        this.handleStreamMessage(threadId, data);
                    };

                    eventSource.onerror = (error) => {
                        console.error("EventSource error:", error);
                        this.stopStreaming(threadId);
                        // P0: drop the optimistic temp message so a ghost of
                        // the user's text does not linger forever after the
                        // stream died. If the server actually posted the real
                        // message, it is re-linked via the ``llm.thread/
                        // new_message`` bus subscriber or the next reload.
                        this._removeOptimisticMessage(threadId);
                        // UI-05: clean up the transient status line too.
                        this._removeTransientStatusMessage(threadId);
                        // P-UX Item 3: set terminal state for the failed flash.
                        this.setThreadRunState(threadId, {
                            state: "failed",
                            label: "Connection lost",
                            error: true,
                        });
                        notification.add(
                            _t(
                                "Lost connection to AI service. Please try sending your message again."
                            ),
                            {
                                type: "danger",
                            }
                        );
                    };
                } catch (error) {
                    console.error("Error starting stream:", error);
                    this.stopStreaming(threadId);
                    // P0: EventSource creation failed — drop the optimistic
                    // temp message so the ghost does not persist (no stream
                    // means no ``message_create`` will ever reconcile it).
                    this._removeOptimisticMessage(threadId);
                    // UI-05: clean up the transient status line too.
                    this._removeTransientStatusMessage(threadId);
                    // P-UX Item 3: set terminal state so the indicator
                    // doesn't stay "running" forever if EventSource
                    // creation fails.
                    this.setThreadRunState(threadId, {
                        state: "failed",
                        label: "Failed to start",
                        error: true,
                    });
                    notification.add(
                        _t(
                            "Could not start AI response. Please check your connection and try again."
                        ),
                        { type: "danger" }
                    );
                }
            },

            stopStreaming(threadId) {
                const eventSource = this.eventSources.get(threadId);
                if (eventSource) {
                    eventSource.close();
                    this.eventSources.delete(threadId);
                }
                this.streamingThreads.delete(threadId);
            },

            /**
             * P0: remove the optimistic temp message tracked for ``threadId``
             * from that thread's reactive messages collection, then clear the
             * tracking. Idempotent — a no-op when no optimistic message is
             * tracked. Called from the SSE ``message_create`` handler (the
             * real message is arriving) AND from the EventSource error /
             * start-failure paths (the stream died, so no real message will
             * ever reconcile the ghost). Delegates the record-level work to
             * the pure ``removeOptimisticMessageFromThread`` helper so the
             * contract is unit-tested in Hoot.
             */
            _removeOptimisticMessage(threadId) {
                const tempId = this._optimisticMsgIds?.[threadId];
                if (tempId === undefined) {
                    return;
                }
                const thread = mailStore.Thread.get({
                    model: "llm.thread",
                    id: threadId,
                });
                removeOptimisticMessageFromThread({
                    thread,
                    tempId,
                    getMessage: (id) => mailStore.Message.get(id),
                });
                delete this._optimisticMsgIds[threadId];
            },

            /**
             * UI-05 — Insert a transient client-side status line ("Analyzing
             * your request…") into the thread's timeline on
             * ``orchestration_started``. Follows the OCB ``is_transient``
             * precedent (``discuss_core_common_service.js:53-69``): a
             * non-persisted ephemeral message that renders in the timeline
             * and is removed when the real progress message arrives via the
             * WebSocket bus (``llm.thread/new_message`` subscriber).
             *
             * The transient message is classified as a "progress
             * notification" (``message_type='notification'`` + no
             * ``llm_role``) so it picks up the ``o-llm-message-status`` CSS
             * class and renders as a slim one-line status row (UI-06 S2).
             *
             * Guard: skips if the thread already has a transient status line
             * (rapid double-submit). The transient is tracked in
             * ``_transientStatusMsgIds`` so it can be removed by
             * ``_removeTransientStatusMessage``.
             */
            _insertTransientStatusMessage(threadId) {
                if (!threadId) {
                    return;
                }
                // Guard: skip if a transient status already exists for this
                // thread (rapid double-submit). The existing one will be
                // replaced when the real progress message arrives.
                if (this._transientStatusMsgIds?.[threadId] !== undefined) {
                    return;
                }
                const thread = mailStore.Thread.get({
                    model: "llm.thread",
                    id: threadId,
                });
                if (!thread) {
                    return;
                }
                // Use a fractional ID (OCB pattern: lastMessageId + 0.01) so
                // the transient sorts after the last real message and after
                // the optimistic user message (negative temp id).
                const lastId = mailStore.getLastMessageId ? mailStore.getLastMessageId() : 0;
                const tempId = lastId + 0.01;
                const transientMsg = buildTransientStatusMessage({
                    id: tempId,
                    threadId,
                    text: _t("Analyzing your request…"),
                });
                mailStore.insert({ "mail.message": [transientMsg] }, { html: true });
                const transientRecord = mailStore.Message.get(tempId);
                if (transientRecord) {
                    thread.messages.add(transientRecord);
                }
                this._transientStatusMsgIds = this._transientStatusMsgIds || {};
                this._transientStatusMsgIds[threadId] = tempId;
            },

            /**
             * UI-05 — Remove the transient client-side status line for the
             * given thread. Idempotent: a no-op when no transient status
             * message is tracked. Delegates to the pure
             * ``removeOptimisticMessageFromThread`` helper (same contract:
             * check membership by id, then delete from the reactive
             * messages collection).
             *
             * Called from: ``llm.thread/new_message`` subscriber (real
             * progress message arriving), ``error`` SSE handler, terminal
             * orchestration bus events (safety net).
             */
            _removeTransientStatusMessage(threadId) {
                const tempId = this._transientStatusMsgIds?.[threadId];
                if (tempId === undefined) {
                    return;
                }
                const thread = mailStore.Thread.get({
                    model: "llm.thread",
                    id: threadId,
                });
                removeOptimisticMessageFromThread({
                    thread,
                    tempId,
                    getMessage: (id) => mailStore.Message.get(id),
                });
                delete this._transientStatusMsgIds[threadId];
            },

            /**
             * FIX-4c (TRACKER_2026-07-25_UI_RESEARCH.md §4): browser
             * staleness guard. Returns true if the incoming message body is
             * a strict downgrade — the incoming body is shorter than the
             * current rendered body while a stream/run is active on that
             * thread. This prevents the stale escaped "Thinking..." placeholder
             * (flushed at the main transaction's final commit, AFTER all
             * progressive chunk broadcasts) from overwriting the streamed
             * answer at the end of a run. Belt + suspenders alongside FIX-4b
             * (which suppresses the placeholder's bus broadcast at the source).
             */
            _isStaleBodyUpdate(threadId, incomingMsg) {
                if (!incomingMsg || typeof incomingMsg.id !== "number") {
                    return false;
                }
                const existing = mailStore.Message.get(incomingMsg.id);
                if (!existing) {
                    return false; // New message — always insert
                }
                const incomingBody = incomingMsg.body || "";
                const existingBody = existing.body || "";
                // Only guard when the thread is actively streaming or running.
                const isActive =
                    this.streamingThreads.has(threadId) ||
                    this.getThreadRunState(threadId)?.state === "running";
                if (!isActive) {
                    return false;
                }
                // Strict downgrade: incoming body is shorter than what's
                // already rendered. The placeholder ("Thinking...") is always
                // shorter than the streamed answer, so it gets filtered out.
                return incomingBody.length < existingBody.length;
            },

            handleStreamMessage(threadId, data) {
                switch (data.type) {
                    case "message_create": {
                        // P0: remove the optimistic user message (if any) for
                        // this thread before inserting the real one. The
                        // optimistic message has a negative temp id tracked in
                        // _optimisticMsgIds. Delegates to the shared
                        // _removeOptimisticMessage helper so the same cleanup
                        // runs on the SSE error / start-failure paths too —
                        // ghosts never persist.
                        this._removeOptimisticMessage(threadId);

                        // Handle all messages (user and AI) via EventSource
                        mailStore.insert({ "mail.message": [data.message] }, { html: true });

                        // Get the created message and add it to the thread's messages collection
                        const createdMessage = mailStore.Message.get(data.message.id);

                        // P-UX Item 4: use Record.many().add() not .push() —
                        // .push() is a plain Array method that does NOT trigger
                        // OWL reactivity. The OCB pattern (mail_core_web_service.js
                        // line 46: inbox.messages.add(message)) uses .add().
                        // Without this, new messages don't appear in the Thread
                        // component until a manual page reload.
                        const createThread = mailStore.Thread.get({
                            model: "llm.thread",
                            id: threadId,
                        });
                        if (
                            createThread &&
                            createdMessage &&
                            !createThread.messages.some((m) => m.id === createdMessage.id)
                        ) {
                            createThread.messages.add(createdMessage);
                        }
                        break;
                    }

                    case "message_chunk":
                    case "message_update":
                        // FIX-4c: staleness guard — skip body downgrades
                        // (stale placeholder overwriting streamed content).
                        if (this._isStaleBodyUpdate(threadId, data.message)) {
                            break;
                        }
                        // Update existing message using standard mail.store.insert() like Odoo does
                        mailStore.insert({ "mail.message": [data.message] }, { html: true });
                        // P-UX Item 4: ensure the message is linked to the thread's
                        // messages collection. If message_create was missed (e.g.
                        // SSE reconnection), the message won't be in the Thread
                        // component's reactive view. add() is idempotent if the
                        // message is already present (guarded by .some() check).
                        {
                            const updMsg = mailStore.Message.get(data.message.id);
                            const updThread = mailStore.Thread.get({
                                model: "llm.thread",
                                id: threadId,
                            });
                            if (
                                updThread &&
                                updMsg &&
                                !updThread.messages.some((m) => m.id === updMsg.id)
                            ) {
                                updThread.messages.add(updMsg);
                            }
                        }
                        break;

                    case "thread_update":
                        // P-UX Item 2: auto-rename — the server sends a
                        // thread_update event after auto-generating a title.
                        // Merge into the store so the sidebar + header update
                        // reactively.
                        mailStore.insert({ "mail.thread": [data.thread] });
                        break;

                    case "error":
                        console.error("Stream error:", data.error);
                        this.stopStreaming(threadId);
                        // UI-05: remove the transient status line if it
                        // hasn't been replaced by the real progress message.
                        this._removeTransientStatusMessage(threadId);
                        // P-UX Item 3: set terminal state for the failed flash.
                        this.setThreadRunState(threadId, {
                            state: "failed",
                            label: data.error || "Error",
                            error: true,
                        });
                        notification.add(data.error || _t("AI response error"), {
                            type: "danger",
                        });
                        break;

                    case "done":
                        this.stopStreaming(threadId);
                        // SSE stream finished. For orchestration threads,
                        // ``done`` just means "the SSE stream ended" (the
                        // background job was dispatched) — NOT "the AI is
                        // done answering". Do NOT set a terminal state; the
                        // background job will send ``run_done`` via the
                        // WebSocket bus to set the terminal state. For direct
                        // (non-orchestration) threads, ``done`` means the
                        // response is complete — set the terminal state.
                        {
                            const current = this.getThreadRunState(threadId);
                            if (current && current.orchestration) {
                                // Orchestration thread — leave state as
                                // "running" (set by orchestration_started).
                                // The bus event or 60s poll will reconcile.
                                break;
                            }
                            this.setThreadRunState(threadId, {
                                state: "done",
                                label: "Done",
                                error: false,
                            });
                        }
                        break;

                    case "orchestration_started":
                        // The orchestrator dispatched a background job.
                        // Progress is delivered via the WebSocket bus
                        // (orchestration_bus_service.js subscriber).
                        // Set the thread to "running" and flag it as an
                        // orchestration thread so the SSE ``done`` event
                        // below does NOT prematurely set a terminal state.
                        this.setThreadRunState(threadId, {
                            state: "running",
                            label: "Working...",
                            phase: "analyzing",
                            error: false,
                            run_id: data.run_id || null,
                            orchestration: true,
                            startedAt: Date.now(),
                            finishedAt: null,
                            expertCount: 0,
                            toolCount: 0,
                            lastRunSummary: null,
                        });
                        // UI-05 — instant client-side status line. The
                        // real "Analyzing your request…" progress message
                        // takes ~1–2 s (request → commit → job pickup →
                        // post → bus). Insert an ephemeral transient
                        // message NOW so the user sees sub-second feedback.
                        // It is removed when the real progress message
                        // arrives via ``llm.thread/new_message``, or on
                        // done/error/stop. OCB precedent: ``is_transient``
                        // (discuss_core_common_service.js:53-69).
                        this._insertTransientStatusMessage(threadId);
                        break;

                    case "tool_called":
                    case "tool_succeeded":
                    case "tool_failed":
                        // No-op: handled via message_update
                        console.log("[LLM] no-op event:", data.type);
                        break;

                    default:
                        console.warn("Unknown stream message type:", data.type);
                        break;
                }
            },

            async loadLLMModels() {
                try {
                    // Only chat/multimodal models are valid as a thread's chat model.
                    // Embedding/rerank models must never be selectable here (using one as
                    // the chat model yields a provider 400: "is an embedding model and
                    // cannot be used with the chat/completions endpoint").
                    const models = await orm.searchRead(
                        "llm.model",
                        [
                            ["active", "=", true],
                            ["model_use", "in", ["chat", "multimodal"]],
                        ],
                        ["id", "name", "provider_id", "default", "model_use"]
                    );

                    models.forEach((model) => {
                        this.llmModels.set(model.id, model);
                    });
                } catch (error) {
                    console.warn(
                        "LLM models not available - llm module may not be installed:",
                        error.message
                    );
                    // Don't throw error, just log warning
                }
            },

            async loadLLMProviders() {
                try {
                    // Check if llm.provider exists first - use correct field names
                    const providers = await orm.searchRead(
                        "llm.provider",
                        [["active", "=", true]],
                        ["id", "name", "service"]
                    );

                    providers.forEach((provider) => {
                        this.llmProviders.set(provider.id, provider);
                    });
                } catch (error) {
                    console.warn(
                        "LLM providers not available - llm module may not be installed:",
                        error.message
                    );
                    // Don't throw error, just log warning
                }
            },

            async loadLLMTools() {
                // Load available tools with minimal fields
                const tools = await orm.searchRead(
                    "llm.tool",
                    [["active", "=", true]],
                    ["id", "name"]
                );

                tools.forEach((tool) => {
                    this.llmTools.set(tool.id, tool);
                });
            },

            // Thread selection using standard Odoo patterns
            async selectThread(threadId) {
                try {
                    // Ensure thread is loaded using standard fetchData
                    const thread = await this.ensureThreadLoaded(threadId);
                    if (!thread) {
                        throw new Error("Thread not found or failed to load");
                    }

                    // Set as active thread in discuss - this is all we need!
                    thread.setAsDiscussThread();
                } catch (error) {
                    console.error("Error selecting thread:", error);
                    notification.add(
                        _t(
                            "Could not load this conversation. It may have been deleted or you may not have access."
                        ),
                        { type: "danger" }
                    );
                }
            },

            // Create new thread with default provider and model
            async createNewThread({ recordModel, recordId } = {}) {
                // FIX-1 (RC-1b): capture the active thread BEFORE the create
                // RPC. If the user switches to another thread while the RPC
                // is in flight, we must NOT yank them back to the new thread.
                const activeThreadAtClick = this.activeLLMThread;

                // Get first available provider and model
                const firstProvider = this.getFirstAvailableProvider();
                const firstModel = this.getFirstAvailableModel();

                // Check for null values and show notifications
                if (!firstProvider) {
                    notification.add(
                        _t(
                            "No AI providers are configured. Please contact your administrator to set up an AI provider."
                        ),
                        { type: "danger" }
                    );
                    return;
                }

                if (!firstModel) {
                    notification.add(
                        _t(
                            "No AI models are available. Please contact your administrator to configure AI models."
                        ),
                        { type: "danger" }
                    );
                    return;
                }

                // Create thread with auto-generated name
                const threadName = `Chat ${new Date().toLocaleString()}`;

                const threadData = {
                    name: threadName,
                    provider_id: firstProvider.id,
                    model_id: firstModel.id,
                };

                // Auto-link to record if context provided (e.g., from chatter)
                if (recordModel && recordId) {
                    threadData.model = recordModel;
                    threadData.res_id = recordId;
                }

                try {
                    const threadId = await orm.call("llm.thread", "create", [threadData]);

                    // FIX-3 (TRACKER_2026-07-25_UI_RESEARCH.md §3): deterministic
                    // insert. Fetch the new thread's store dict from the server
                    // (one lightweight RPC — NOT a full init_messaging) and
                    // insert it into mailStore directly. The thread is now
                    // visible + selectable regardless of bus delivery or
                    // worker exhaustion.
                    const storeData = await orm.call("llm.thread", "get_thread_store_data", [
                        [threadId],
                    ]);
                    mailStore.insert(storeData);

                    // FIX-1: pin the new thread at the top of the stable order.
                    if (this._threadOrderPin) {
                        this._threadOrderPin = [
                            threadId,
                            ...this._threadOrderPin.filter((id) => id !== threadId),
                        ];
                    }

                    // FIX-1 (RC-1b): only auto-select the new thread if the
                    // user hasn't manually switched threads since clicking +.
                    // If they did switch, respect their choice — don't yank.
                    const stillSameActive = this.activeLLMThread === activeThreadAtClick;
                    if (stillSameActive) {
                        await this.selectThread(threadId);
                    }

                    // Background reconciliation (non-blocking) — the bus
                    // broadcast + init_messaging refresh other tabs and fill
                    // in any store details the lightweight RPC didn't carry.
                    // We do NOT await this — it must not block selection.
                    mailStore
                        .fetchData({ init_messaging: {} })
                        .catch((err) =>
                            console.debug(
                                "Background init_messaging refresh after create failed:",
                                err
                            )
                        );
                } catch (error) {
                    console.error("Failed to create conversation:", error);
                    notification.add(
                        _t(
                            "Could not create the conversation. Please try again or contact your administrator."
                        ),
                        { type: "danger" }
                    );
                }
            },

            // Get first available provider
            getFirstAvailableProvider() {
                const providers = Array.from(this.llmProviders.values());
                return providers.length > 0 ? providers[0] : null;
            },

            // Get first available model — prefer the one flagged as default.
            getFirstAvailableModel() {
                const models = Array.from(this.llmModels.values());
                if (!models.length) {
                    return null;
                }
                return models.find((m) => m.default) || models[0];
            },

            // Refresh threads and select specific thread
            async refreshThreadsAndSelect(threadId) {
                // Use proper fetchData to refresh thread data
                // Will trigger proper reload of all threads
                await mailStore.fetchData({
                    init_messaging: {},
                });

                // Wait a moment for threads to be populated
                await new Promise((resolve) => setTimeout(resolve, 100));

                // Select the newly created thread
                await this.selectThread(threadId);
            },

            // Link a record to a thread
            async linkRecordToThread(threadId, model, recordId) {
                try {
                    // Update database
                    await orm.write("llm.thread", [threadId], {
                        model: model,
                        res_id: recordId,
                    });

                    // P-UX review §2.6: store.insert instead of Object.assign mutation.
                    mailStore.insert({
                        "mail.thread": [
                            {
                                id: threadId,
                                model: "llm.thread",
                                res_model: model,
                                res_id: recordId,
                            },
                        ],
                    });

                    notification.add(_t("Record linked to conversation successfully."), {
                        type: "success",
                    });
                    return true;
                } catch (error) {
                    console.error("Error linking record:", error);
                    notification.add(
                        _t("Could not link the record to this conversation. Please try again."),
                        { type: "danger" }
                    );
                    return false;
                }
            },

            // Unlink record from a thread
            async unlinkRecordFromThread(threadId) {
                try {
                    // Update database
                    await orm.write("llm.thread", [threadId], {
                        model: false,
                        res_id: false,
                    });

                    // P-UX review §2.6: store.insert instead of Object.assign mutation.
                    mailStore.insert({
                        "mail.thread": [
                            {
                                id: threadId,
                                model: "llm.thread",
                                res_model: false,
                                res_id: false,
                            },
                        ],
                    });

                    notification.add(_t("Record unlinked from conversation successfully."), {
                        type: "success",
                    });
                    return true;
                } catch (error) {
                    console.error("Error unlinking record:", error);
                    notification.add(
                        _t("Could not unlink the record from this conversation. Please try again."),
                        { type: "danger" }
                    );
                    return false;
                }
            },

            // Helper methods for components
            isStreamingThread(threadId) {
                // P-CHAT M3: a thread is "working" if it has an active SSE stream
                // OR a background orchestration run in the running state.
                return this.streamingThreads.has(threadId) || this.isThreadRunning(threadId);
            },

            getStreamingStatus() {
                const activeThread = mailStore.discuss?.thread;
                if (activeThread?.model === "llm.thread") {
                    return this.isStreamingThread(activeThread.id);
                }
                return false;
            },

            // Pending open methods - used by client action to bypass unreliable bus
            setPendingOpenInChatter(data) {
                this.pendingOpenInChatter = data;
            },

            // P-CHAT M2: steps-drawer toggle for a turn (keyed by user-message id).
            isStepDrawerOpen(turnId) {
                return Boolean(this.stepDrawerOpen[turnId]);
            },

            toggleStepDrawer(turnId) {
                this.stepDrawerOpen[turnId] = !this.stepDrawerOpen[turnId];
            },

            setStepDrawerOpen(turnId, open) {
                this.stepDrawerOpen[turnId] = Boolean(open);
            },

            // P-CHAT M3 — live run-feedback API (called by the koenig bus
            // subscriber; the fork store stays generic and channel-agnostic).
            _handleOrchestrationBusEvent(threadId, payload) {
                /** Handle an orchestration progress event (from SSE bus_event
                 * or from the WebSocket bus subscriber). Extracted here so
                 * both delivery paths share the same logic.
                 *
                 * The ``run_paused`` case sets the state to "paused" but does
                 * NOT call the interaction service — that is handled by the
                 * König ``orchestration_bus_service.js`` subscriber (which
                 * has access to the interaction service).
                 *
                 * ``run_id`` tracking (P1 fix 2026-07-19): the run_id is
                 * stored in threadRunState so the 60s safety-net poll can
                 * skip stale runs (old completed runs must NOT override the
                 * current run's running state). ``run_started`` sets a fresh
                 * run_id; all subsequent events for the same run carry the
                 * same run_id and are applied. Terminal events clear it.
                 */
                const data = payload || {};
                const event = data.event;
                const message = data.message;
                const runId = data.run_id || data.task_id || null;
                // Normalize task_* events to run_* for backward compat.
                const normalizedEvent =
                    event && event.startsWith("task_") ? "run_" + event.substring(5) : event;
                // UI-05: safety net — remove the transient status line on any
                // orchestration bus event except ``run_started`` (which fires
                // before the real progress message is posted). The
                // ``llm.thread/new_message`` subscriber is the primary removal
                // path; this catches terminal events where the subscriber
                // might have missed the message (e.g., WebSocket reconnect).
                if (normalizedEvent && normalizedEvent !== "run_started") {
                    this._removeTransientStatusMessage(threadId);
                }
                // UI-09 H1 — phase mapping. Each orchestration event maps to a
                // canonical phase label shown in the HUD:
                //   run_started      → "analyzing"
                //   expert_dispatched → "dispatching"
                //   expert_completed  → "gathering"
                //   (assistant msg arrives) → "synthesizing" (set in new_message sub)
                //   run_done          → "done"
                //   run_failed        → "failed"
                //   run_cancelled     → "cancelled"
                //   run_killed        → "killed"
                //   run_timed_out     → "timed_out"
                //   run_paused        → "paused"
                //   run_resumed       → "running" (keeps previous phase)
                // The phase field is additive to threadRunState — existing
                // consumers (sidebar) ignore it; the HUD reads it.
                //
                // UI-09 H2 — run counters. expertCount and toolCount are
                // incremented during the run and used for the run-summary chip.
                // They are reset on run_started and frozen into lastRunSummary
                // on run_done (auto-dismissed after 30s).
                const current = this.getThreadRunState(threadId) || {};
                switch (normalizedEvent) {
                    case "run_started":
                        this.setThreadRunState(threadId, {
                            state: "running",
                            label: message || "Working...",
                            phase: "analyzing",
                            startedAt: Date.now(),
                            finishedAt: null,
                            error: false,
                            run_id: runId,
                            expertCount: 0,
                            toolCount: 0,
                            // UI-12 — live health counters (HUD).
                            expertsRunning: 0,
                            errorCount: 0,
                            lastRunSummary: null,
                        });
                        break;
                    case "expert_dispatched":
                        this.setThreadRunState(threadId, {
                            state: "running",
                            label: message || "Working...",
                            phase: "dispatching",
                            run_id: runId,
                            expertCount: (current.expertCount || 0) + 1,
                            // UI-12 — live "N experts running" HUD counter.
                            expertsRunning: (current.expertsRunning || 0) + 1,
                        });
                        break;
                    // UI-12 — completed/failed split: both decrement the
                    // live experts-running counter; failures additionally
                    // bump the error counter shown in the HUD.
                    case "expert_completed":
                        this.setThreadRunState(threadId, {
                            state: "running",
                            label: message || "Working...",
                            phase: "gathering",
                            run_id: runId,
                            expertsRunning: Math.max(0, (current.expertsRunning || 0) - 1),
                        });
                        this.reloadThreadMessages(threadId);
                        break;
                    case "expert_failed":
                        this.setThreadRunState(threadId, {
                            state: "running",
                            label: message || "Working...",
                            phase: "gathering",
                            run_id: runId,
                            expertsRunning: Math.max(0, (current.expertsRunning || 0) - 1),
                            errorCount: (current.errorCount || 0) + 1,
                        });
                        this.reloadThreadMessages(threadId);
                        break;
                    case "run_done": {
                        this.setThreadRunState(threadId, {
                            state: "done",
                            label: message || "Done",
                            phase: "done",
                            error: false,
                            run_id: runId,
                            expertsRunning: 0,
                        });
                        // UI-09 H2 — compute the run-summary chip data.
                        // Duration from startedAt to now (setThreadRunState
                        // sets finishedAt). Experts/tool counts from the
                        // run counters. Auto-dismissed after 30s by the HUD.
                        const st = this.getThreadRunState(threadId) || {};
                        const durationSec = st.startedAt
                            ? Math.max(0, (st.finishedAt || Date.now()) - st.startedAt) / 1000
                            : 0;
                        this.setThreadRunState(threadId, {
                            lastRunSummary: {
                                durationSec: Math.round(durationSec * 10) / 10,
                                expertCount: st.expertCount || 0,
                                toolCount: st.toolCount || 0,
                                shownAt: Date.now(),
                            },
                        });
                        this.reloadThreadMessages(threadId);
                        break;
                    }
                    case "run_failed":
                        this.setThreadRunState(threadId, {
                            state: "failed",
                            label: message || "Failed",
                            phase: "failed",
                            error: true,
                            run_id: runId,
                            expertsRunning: 0,
                            // UI-12 — surface the failure in the HUD error counter.
                            errorCount: (current.errorCount || 0) + 1,
                        });
                        this.reloadThreadMessages(threadId);
                        break;
                    case "run_cancelled":
                        this.setThreadRunState(threadId, {
                            state: "cancelled",
                            label: message || "Cancelled",
                            phase: "cancelled",
                            error: false,
                            run_id: runId,
                            expertsRunning: 0,
                        });
                        this.reloadThreadMessages(threadId);
                        break;
                    case "run_killed":
                        this.setThreadRunState(threadId, {
                            state: "killed",
                            label: message || "Killed",
                            phase: "killed",
                            error: false,
                            run_id: runId,
                            expertsRunning: 0,
                        });
                        this.reloadThreadMessages(threadId);
                        break;
                    case "run_timed_out":
                        this.setThreadRunState(threadId, {
                            state: "timed_out",
                            label: message || "Timed out",
                            phase: "timed_out",
                            error: false,
                            run_id: runId,
                            expertsRunning: 0,
                        });
                        this.reloadThreadMessages(threadId);
                        break;
                    case "run_paused":
                        this.setThreadRunState(threadId, {
                            state: "paused",
                            label: message || "Awaiting approval",
                            phase: "paused",
                            error: false,
                            run_id: runId,
                        });
                        break;
                    case "run_resumed":
                        this.setThreadRunState(threadId, {
                            state: "running",
                            label: message || "Resuming...",
                            phase: current.phase || "analyzing",
                            error: false,
                            run_id: runId,
                        });
                        break;
                    default:
                        break;
                }
            },

            setThreadRunState(threadId, vals) {
                if (!threadId) {
                    return;
                }
                const prev = this.threadRunState[threadId] || {};
                const next = { ...prev, ...vals };
                if (
                    vals &&
                    (vals.state === "done" ||
                        vals.state === "failed" ||
                        vals.state === "cancelled") &&
                    !next.finishedAt
                ) {
                    next.finishedAt = Date.now();
                }
                this.threadRunState[threadId] = next;
            },

            getThreadRunState(threadId) {
                return this.threadRunState[threadId] || null;
            },

            isThreadRunning(threadId) {
                const st = this.threadRunState[threadId];
                return Boolean(st && st.state === "running");
            },

            /**
             * UI-09 H2 — get the run-summary chip data for a thread, with
             * 30-second auto-dismiss. Returns null if:
             *   - no run has completed (lastRunSummary is null)
             *   - the summary was shown >30s ago (auto-dismissed)
             *   - a new run has started (state is "running" again)
             *
             * The HUD calls this in a reactive getter (via ``useState``) so
             * the chip appears when ``run_done`` fires and disappears after
             * 30s. The auto-dismiss is time-based (not event-based) so it
             * works even if the user doesn't interact with the thread.
             */
            getRunSummary(threadId) {
                if (!threadId) {
                    return null;
                }
                return getVisibleRunSummary(this.threadRunState[threadId]);
            },

            clearThreadRunState(threadId) {
                delete this.threadRunState[threadId];
                // UI-05: also clear the transient status tracking so it
                // doesn't linger for a deleted/cleaned-up thread.
                if (this._transientStatusMsgIds) {
                    delete this._transientStatusMsgIds[threadId];
                }
            },

            /**
             * Re-fetch the thread's NEW messages so the assistant answer posted
             * by a background orchestration run appears without a manual reload.
             *
             * Uses the OCB Thread model's ``fetchNewMessages()`` — NOT
             * ``fetchMessages()``. The difference is the reactivity fix for
             * "answer only appears after clicking away and back": ``fetchMessages``
             * inserts records into the store but does NOT splice them into
             * ``thread.messages``, so the Thread component (which renders
             * ``thread.nonEmptyMessages``) never re-renders. ``fetchNewMessages``
             * (thread_model.js) explicitly ``this.messages.splice(start, 0, ...new)``
             * the newly-fetched messages into the reactive collection → the
             * component re-renders and the answer shows up live. This is the
             * same call OCB's Thread component makes on its
             * ``onWillUpdateProps`` (i.e. when the user navigates away and back),
             * which is why that workaround always showed the answer.
             */
            async reloadThreadMessages(threadId) {
                if (!threadId) {
                    return;
                }
                try {
                    const thread = mailStore.Thread.get({
                        model: "llm.thread",
                        id: threadId,
                    });
                    if (thread) {
                        await thread.fetchNewMessages();
                    }
                } catch (error) {
                    console.warn("[llm.store] reloadThreadMessages failed:", error);
                }
            },

            // ==================================================================
            // P-UX: archive / unarchive / delete / bulk actions
            // ==================================================================

            /**
             * Re-run ``init_messaging`` so the thread list reflects DB state
             * (active flag, tags, deletions). Used after bulk operations where
             * updating each store record in-place would be more code than a reload.
             */
            async _reloadThreads() {
                try {
                    await mailStore.fetchData({ init_messaging: {} });
                } catch (error) {
                    console.warn("[llm.store] thread reload failed:", error);
                }
            },

            _getThread(threadId) {
                return mailStore.Thread.get({ model: "llm.thread", id: threadId });
            },

            async archiveThread(threadId) {
                if (!threadId) {
                    return;
                }
                try {
                    await orm.call("llm.thread", "action_archive", [[threadId]]);
                    // P-UX review §2.6: store.insert for proper Record.attr tracking.
                    mailStore.insert({
                        "mail.thread": [{ id: threadId, model: "llm.thread", active: false }],
                    });
                } catch (error) {
                    console.warn("[llm.store] archiveThread failed:", error);
                    notification.add(_t("Could not archive the conversation. Please try again."), {
                        type: "danger",
                    });
                }
            },

            async unarchiveThread(threadId) {
                if (!threadId) {
                    return;
                }
                try {
                    await orm.call("llm.thread", "action_unarchive", [[threadId]]);
                    // P-UX review §2.6: store.insert for proper Record.attr tracking.
                    mailStore.insert({
                        "mail.thread": [{ id: threadId, model: "llm.thread", active: true }],
                    });
                } catch (error) {
                    console.warn("[llm.store] unarchiveThread failed:", error);
                    notification.add(
                        _t("Could not unarchive the conversation. Please try again."),
                        { type: "danger" }
                    );
                }
            },

            async deleteThread(threadId) {
                if (!threadId) {
                    return;
                }
                try {
                    await orm.unlink("llm.thread", [threadId]);
                    const thread = this._getThread(threadId);
                    // Clear the active discuss thread if it was the one deleted, so the
                    // chat area shows the empty state instead of a stale ghost.
                    if (
                        mailStore.discuss?.thread?.model === "llm.thread" &&
                        mailStore.discuss.thread.id === threadId
                    ) {
                        mailStore.discuss.thread = false;
                    }
                    if (thread) {
                        thread.delete();
                    }
                    // Clean up the threadRunState entry so the sidebar
                    // doesn't keep a stale state for a deleted thread
                    // (Phase 7: terminal states are no longer cleared
                    // by the tick, so this is the cleanup path).
                    this.clearThreadRunState(threadId);
                } catch (error) {
                    console.warn("[llm.store] deleteThread failed:", error);
                    notification.add(_t("Could not delete the conversation. Please try again."), {
                        type: "danger",
                    });
                }
            },

            async bulkArchive(threadIds) {
                if (!threadIds?.length) {
                    return;
                }
                try {
                    await orm.call("llm.thread", "action_archive", [threadIds]);
                    // P-UX review §2.6: batch store.insert instead of per-record mutation.
                    mailStore.insert({
                        "mail.thread": threadIds.map((id) => ({
                            id,
                            model: "llm.thread",
                            active: false,
                        })),
                    });
                } catch (error) {
                    console.warn("[llm.store] bulkArchive failed:", error);
                    notification.add(_t("Could not archive the selected conversations."), {
                        type: "danger",
                    });
                }
            },

            async bulkDelete(threadIds) {
                if (!threadIds?.length) {
                    return;
                }
                try {
                    await orm.unlink("llm.thread", threadIds);
                    const activeId =
                        mailStore.discuss?.thread?.model === "llm.thread"
                            ? mailStore.discuss.thread.id
                            : null;
                    if (activeId && threadIds.includes(activeId)) {
                        mailStore.discuss.thread = false;
                    }
                    for (const id of threadIds) {
                        const thread = this._getThread(id);
                        if (thread) {
                            thread.delete();
                        }
                    }
                } catch (error) {
                    console.warn("[llm.store] bulkDelete failed:", error);
                    notification.add(_t("Could not delete the selected conversations."), {
                        type: "danger",
                    });
                }
            },

            /**
             * Append the given tags to every selected thread (existing tags kept).
             * @param {Number[]} threadIds
             * @param {Number[]} tagIds tag ids to append or remove
             * @param {String} [mode="add"] "add" (4, id) or "remove" (3, id)
             */
            async bulkTag(threadIds, tagIds, mode = "add") {
                if (!threadIds?.length || !tagIds?.length) {
                    return;
                }
                try {
                    const tagCommands = tagIds.map((tagId) =>
                        mode === "remove" ? [3, tagId] : [4, tagId]
                    );
                    await orm.write("llm.thread", threadIds, { tag_ids: tagCommands });
                    // Reload so each thread's ``tag_ids`` badges reflect the merge
                    // (the write returns no tag-detail dicts to merge in-place).
                    await this._reloadThreads();
                } catch (error) {
                    console.warn("[llm.store] bulkTag failed:", error);
                    notification.add(
                        _t("Could not apply the tags to the selected conversations."),
                        { type: "danger" }
                    );
                }
            },

            consumePendingOpenInChatter(model, resId) {
                const pending = this.pendingOpenInChatter;
                if (pending && pending.model === model && pending.resId === resId) {
                    this.pendingOpenInChatter = null;
                    return pending;
                }
                return null;
            },

            // Get list of data loaders - can be extended by patches
            getDataLoaders() {
                return [this.loadLLMProviders, this.loadLLMModels, this.loadLLMTools];
            },

            // Initialize LLM store - threads now loaded via standard init_messaging
            async initialize() {
                try {
                    const loaders = this.getDataLoaders();
                    await Promise.all(loaders.map((loader) => loader.call(this)));
                    // NOTE: LLM threads are now loaded automatically via res.users._init_messaging()
                    this.isReady.resolve();
                } catch (error) {
                    console.error("Error initializing LLM store:", error);
                    this.isReady.reject(error);
                }
            },

            // Cleanup
            destroy() {
                // Close all event sources
                this.eventSources.forEach((eventSource) => eventSource.close());
                this.eventSources.clear();
                this.streamingThreads.clear();
            },
        });

        // Subscribe to llm.thread/new_message bus events for live message
        // delivery (OCB-canonical pattern, ref: discuss_channel.py:649-658 +
        // discuss_core_common_service.js:46-52, 147-160). When a new message
        // is posted (by user OR background job), the Python _notify_thread
        // broadcasts via bus.bus._sendone → WebSocket → this subscriber
        // inserts the message data into the OWL store AND links each inserted
        // mail.message into the target llm.thread's reactive messages
        // collection (dedupe by id). The link step mirrors the SSE
        // ``message_create`` path in handleStreamMessage and the OCB
        // ``discuss.channel/new_message`` handler: without it the message
        // lands in the store but the Thread component (which renders
        // ``thread.messages``) never re-renders, so the answer only appears
        // after a manual reload.
        bus_service.subscribe("llm.thread/new_message", (payload) => {
            if (!payload || !payload.data) {
                return;
            }
            // UI-05: remove the transient "Analyzing your request…" status
            // line before inserting the real progress message, so the
            // transient doesn't briefly appear alongside the real one.
            // The transient was inserted on ``orchestration_started`` and
            // is tracked in ``_transientStatusMsgIds``. No-op if none.
            llmStore._removeTransientStatusMessage(payload.id);
            // FIX-4c: staleness guard — filter out stale body downgrades
            // (the placeholder flushed at the main transaction's final commit
            // would overwrite the streamed answer). Ref: TRACKER §4.
            const threadId = payload.id;
            const rawMessages = payload.data["mail.message"];
            if (threadId && rawMessages?.length) {
                const filtered = rawMessages.filter(
                    (m) => !llmStore._isStaleBodyUpdate(threadId, m)
                );
                if (filtered.length < rawMessages.length) {
                    payload.data = { ...payload.data, "mail.message": filtered };
                }
            }
            // Insert should always be done before any other operation (OCB
            // invariant: awaiting before insertion could overwrite newer
            // state from more recent notifications).
            mailStore.insert(payload.data, { html: true });
            const messages = payload.data["mail.message"];
            if (!threadId || !messages?.length) {
                return;
            }
            const thread = mailStore.Thread.get({
                model: "llm.thread",
                id: threadId,
            });
            if (!thread) {
                return;
            }
            linkMessagesToThread({
                thread,
                messageIds: messages.map((m) => m.id),
                getMessage: (id) => mailStore.Message.get(id),
            });
            // UI-09 H1 — detect the "synthesizing" phase. When an assistant
            // (non-notification) message arrives while the run is still
            // "running", the master is writing the final answer — set the
            // phase to "synthesizing". This phase is brief: run_done
            // arrives shortly after and overwrites it with "done".
            // Also increment toolCount for each tool message (UI-09 H2).
            const runSt = llmStore.getThreadRunState(threadId);
            if (runSt && runSt.state === "running") {
                let hasAssistant = false;
                let toolDelta = 0;
                for (const m of messages) {
                    if (m.llm_role === "assistant" && m.message_type !== "notification") {
                        hasAssistant = true;
                    }
                    if (m.llm_role === "tool") {
                        toolDelta++;
                    }
                }
                const update = {};
                if (hasAssistant && runSt.phase !== "synthesizing") {
                    update.phase = "synthesizing";
                    update.label = "Synthesizing answer...";
                }
                if (toolDelta > 0) {
                    update.toolCount = (runSt.toolCount || 0) + toolDelta;
                }
                if (Object.keys(update).length) {
                    llmStore.setThreadRunState(threadId, update);
                }
            }
        });

        // Initialize LLM data after mailStore is ready (which calls init_messaging)
        mailStore.isReady.then(() => {
            llmStore.initialize();
        });

        // NOTE: No longer need thread subscription since threads load automatically via fetchData

        return llmStore;
    },
};

registry.category("services").add("llm.store", llmStoreService);
