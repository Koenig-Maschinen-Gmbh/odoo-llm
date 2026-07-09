/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { Deferred } from "@web/core/utils/concurrency";
import { reactive } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { deserializeDateTime } from "@web/core/l10n/dates";

/**
 * LLM Store Service - Integrates with existing mail.store
 * Provides LLM-specific functionality without breaking mail components
 */
export const llmStoreService = {
    dependencies: ["orm", "mail.store", "notification"],

    start(env, { orm, "mail.store": mailStore, notification }) {
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
                return allThreads
                    .filter((thread) => thread.model === "llm.thread")
                    .sort((a, b) => {
                        // P-UX review §2.1: new Date("YYYY-MM-DD HH:MM:SS") is
                        // implementation-defined (Safari → Invalid Date). Use luxon
                        // deserializeDateTime for safe server-datetime parsing.
                        const ts = (val) => {
                            if (!val) {
                                return 0;
                            }
                            const dt =
                                val instanceof luxon.DateTime ? val : deserializeDateTime(val);
                            return dt?.isValid ? dt.toMillis() : 0;
                        };
                        return ts(b.write_date) - ts(a.write_date);
                    });
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
                // Track whether this stream is an orchestration run. If so,
                // the SSE ``done`` event just means the stream is finished
                // (the background job was dispatched) — NOT that the AI
                // response is complete. The bus events (run_done, run_failed)
                // set the terminal state for orchestration runs.
                this._orchestrationThreads = this._orchestrationThreads || new Set();
                this._orchestrationThreads.delete(threadId);
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

            handleStreamMessage(threadId, data) {
                switch (data.type) {
                    case "message_create": {
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
                        // P-UX Item 3: set terminal state for the done flash
                        // — BUT only for direct SSE streaming. For orchestration
                        // runs, the ``done`` event just means the SSE stream is
                        // finished (the background job was dispatched). The bus
                        // events (run_done, run_failed) set the terminal state.
                        if (!this._orchestrationThreads?.has(threadId)) {
                            this.setThreadRunState(threadId, {
                                state: "done",
                                label: "Done",
                                error: false,
                            });
                        }
                        break;

                    case "orchestration_started":
                        // The orchestrator dispatched a background job.
                        // Mark this thread as orchestration-mode so the
                        // ``done`` SSE event doesn't set a terminal state
                        // (the bus events handle that for orchestration runs).
                        this._orchestrationThreads = this._orchestrationThreads || new Set();
                        this._orchestrationThreads.add(threadId);
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

                const threadId = await orm.call("llm.thread", "create", [threadData]);

                // Reload user threads and select the new one
                await this.refreshThreadsAndSelect(threadId);
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

            clearThreadRunState(threadId) {
                delete this.threadRunState[threadId];
            },

            /**
             * Re-fetch the thread's messages so new messages (e.g. the assistant
             * answer posted by a background orchestration run) appear without a
             * manual page reload. Uses the OCB Thread model's ``fetchMessages()``
             * — the canonical message-reload path (thread_model.js line 679). The
             * previous implementation called ``mailStore.fetchData({ init_messaging:
             * {} })`` which refreshes the thread LIST but does NOT re-fetch the
             * specific thread's messages → the new answer stayed invisible.
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
                        await thread.fetchMessages();
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
             * @param {Number[]} tagIds tag ids to append
             */
            async bulkTag(threadIds, tagIds) {
                if (!threadIds?.length || !tagIds?.length) {
                    return;
                }
                try {
                    const tagCommands = tagIds.map((tagId) => [4, tagId]);
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

        // Initialize LLM data after mailStore is ready (which calls init_messaging)
        mailStore.isReady.then(() => {
            llmStore.initialize();
        });

        // NOTE: No longer need thread subscription since threads load automatically via fetchData

        return llmStore;
    },
};

registry.category("services").add("llm.store", llmStoreService);
