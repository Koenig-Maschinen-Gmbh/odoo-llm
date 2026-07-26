/** @odoo-module **/

import { llmStoreService } from "../src/services/llm_store_service";
import { accumulateReasoningText } from "../src/utils/llm_thread_messages";
import { translatedTerms, translationLoaded } from "@web/core/l10n/translation";
import { beforeEach, describe, expect, test } from "@odoo/hoot";

// Hoot does not load the translation cache — ``_t(str)`` would return a
// LazyTranslatedString whose coercion throws "translation error" (OCB
// translation.js:36-62). Mark translations as loaded so ``_t`` returns the
// English term (idempotent one-off setup; matches the production env).
translatedTerms[translationLoaded] = true;

/**
 * FIX-4c/4d/4g (TRACKER_2026-07-25_UI_RESEARCH.md) — handler-level tests for
 * the llm store service's bus/SSE handlers. The real service ``start()`` is
 * invoked with a minimal mock ``mail.store`` (Map-backed records), a
 * subscriber-capturing mock ``bus_service``, and a recording mock
 * ``notification`` — no OWL store mount needed.
 *
 * Covered contracts:
 *  1. FIX-4c: the staleness guard skips the stale "Thinking..." placeholder
 *     while a run is active, but a shorter NON-placeholder body (the legit
 *     final ``_strip_preamble`` + linkify broadcast, V7) is still applied.
 *  2. FIX-4g: a terminal ``run_killed`` event removes the transient
 *     "Analyzing your request…" status line and stops streaming.
 *  3. FIX-4d: ``reasoning_chunk`` events (bus + SSE) accumulate text on
 *     ``body_json.reasoning``, track the active reasoning message, and the
 *     target is cleared on run_done / stopStreaming (thread-scoped).
 *
 * Hoot API notes: no ``.toContain`` / ``.toBeTruthy`` — use
 * ``expect(str.includes(x)).toBe(true)`` / ``expect(Boolean(x)).toBe(true)``.
 */

/**
 * Minimal Map-backed stand-in for the OCB ``mail.store`` service. Mirrors
 * the interface the llm store service touches: ``Message.get``,
 * ``Thread.get``, ``insert`` (merge semantics), ``getLastMessageId``,
 * ``currentUser``, ``isReady`` (never resolves → ``initialize()`` is never
 * called, so no ORM is needed), and ``discuss``.
 */
function makeMockMailStore() {
    const messages = new Map();
    const threads = new Map();
    const key = (model, id) => `${model},${id}`;
    return {
        Message: {
            get: (id) => messages.get(id) || null,
        },
        Thread: {
            get: ({ model, id }) => threads.get(key(model, id)) || null,
        },
        currentUser: { partnerId: 1, name: "AI Tester" },
        // Never-resolving thenable: initialize() (ORM loading) never runs.
        isReady: {
            then() {
                // Intentionally never calls back — initialize() must not run.
            },
        },
        discuss: {},
        getLastMessageId: () => (messages.size ? Math.max(...messages.keys()) : 0),
        insert(data) {
            for (const [modelName, records] of Object.entries(data)) {
                for (const rec of records) {
                    if (modelName === "mail.message") {
                        const existing = messages.get(rec.id) || {};
                        messages.set(rec.id, { ...existing, ...rec });
                    } else if (modelName === "mail.thread") {
                        const k = key(rec.model, rec.id);
                        const existing = threads.get(k) || {};
                        threads.set(k, { ...existing, ...rec });
                    }
                }
            }
        },
        /** Test helper: register a thread with a reactive-style messages collection. */
        _addThread(threadId) {
            const arr = [];
            const thread = {
                model: "llm.thread",
                id: threadId,
                messages: {
                    add(m) {
                        if (!arr.some((x) => x.id === m.id)) {
                            arr.push(m);
                        }
                    },
                    delete(m) {
                        const i = arr.findIndex((x) => x.id === m.id);
                        if (i >= 0) {
                            arr.splice(i, 1);
                            return true;
                        }
                        return false;
                    },
                    some(pred) {
                        return arr.some(pred);
                    },
                    _arr: arr,
                },
                fetchNewMessages: async () => {
                    // no-op in the mock — the real store fetches via ORM
                },
            };
            threads.set(key("llm.thread", threadId), thread);
            return thread;
        },
        /** Test helper: register a message record directly. */
        _addMessage(msg) {
            messages.set(msg.id, msg);
        },
        _messages: messages,
    };
}

/**
 * Start the real llm store service with mock dependencies.
 * Returns the store plus the captured bus subscribers and notifications.
 */
function startLlmStore(mailStore) {
    const subscribers = {};
    const notifications = [];
    const bus_service = {
        subscribe: (type, cb) => {
            subscribers[type] = cb;
        },
        addEventListener: () => {
            // no-op in the mock — only subscribe() is captured
        },
    };
    const notification = {
        add: (message, options) => notifications.push({ message, options }),
    };
    const orm = { call: async () => [] };
    const llmStore = llmStoreService.start(
        {},
        { orm, "mail.store": mailStore, notification, bus_service }
    );
    return { llmStore, subscribers, notifications };
}

let mailStore = null;
let llmStore = null;
let subscribers = null;

beforeEach(() => {
    mailStore = makeMockMailStore();
    ({ llmStore, subscribers } = startLlmStore(mailStore));
});

// ---------------------------------------------------------------------------
// FIX-4c — staleness guard (browser side, V7 narrowed)
// ---------------------------------------------------------------------------

describe("FIX-4c: llm.thread/new_message staleness guard", () => {
    test("stale placeholder body is skipped while a run is active", () => {
        const thread = mailStore._addThread(1);
        const longBody = `<p>${"Streamed answer content. ".repeat(30)}</p>`;
        mailStore._addMessage({ id: 100, model: "llm.thread", res_id: 1, body: longBody });
        thread.messages.add(mailStore.Message.get(100));
        llmStore.setThreadRunState(1, { state: "running", label: "Working..." });

        subscribers["llm.thread/new_message"]({
            id: 1,
            data: {
                "mail.message": [
                    { id: 100, model: "llm.thread", res_id: 1, body: "<p>Thinking...</p>" },
                ],
            },
        });

        // The placeholder downgrade was filtered out — the streamed body stays.
        expect(mailStore.Message.get(100).body).toBe(longBody);
    });

    test("legacy double-escaped placeholder is also skipped", () => {
        const thread = mailStore._addThread(1);
        const longBody = `<p>${"Streamed answer content. ".repeat(30)}</p>`;
        mailStore._addMessage({ id: 100, model: "llm.thread", res_id: 1, body: longBody });
        thread.messages.add(mailStore.Message.get(100));
        llmStore.setThreadRunState(1, { state: "running", label: "Working..." });

        subscribers["llm.thread/new_message"]({
            id: 1,
            data: {
                "mail.message": [
                    {
                        id: 100,
                        model: "llm.thread",
                        res_id: 1,
                        body: "<p>&lt;p&gt;Thinking...&lt;/p&gt;\n</p>",
                    },
                ],
            },
        });

        expect(mailStore.Message.get(100).body).toBe(longBody);
    });

    test("placeholder body is applied when NO run is active", () => {
        const thread = mailStore._addThread(1);
        const longBody = `<p>${"Old answer. ".repeat(20)}</p>`;
        mailStore._addMessage({ id: 100, model: "llm.thread", res_id: 1, body: longBody });
        thread.messages.add(mailStore.Message.get(100));
        // No run state, no streaming thread → guard inactive.

        subscribers["llm.thread/new_message"]({
            id: 1,
            data: {
                "mail.message": [
                    { id: 100, model: "llm.thread", res_id: 1, body: "<p>Thinking...</p>" },
                ],
            },
        });

        expect(mailStore.Message.get(100).body).toBe("<p>Thinking...</p>");
    });

    test("V7: shorter NON-placeholder final body is applied while running (legit strip/linkify)", () => {
        const thread = mailStore._addThread(1);
        // The streamed body contains a preamble that _strip_preamble removes,
        // so the legitimate final body is SHORTER than the streamed one.
        const streamedBody = `<p>Let me compile the answer…</p><p>${"Final content. ".repeat(
            30
        )}</p>`;
        const finalBody = `<p>${"Final content. ".repeat(30)}</p>`;
        mailStore._addMessage({ id: 100, model: "llm.thread", res_id: 1, body: streamedBody });
        thread.messages.add(mailStore.Message.get(100));
        llmStore.setThreadRunState(1, { state: "running", label: "Working..." });

        subscribers["llm.thread/new_message"]({
            id: 1,
            data: {
                "mail.message": [
                    {
                        id: 100,
                        model: "llm.thread",
                        res_id: 1,
                        body: finalBody,
                        llm_role: "assistant",
                        message_type: "comment",
                    },
                ],
            },
        });

        // The guard must NOT skip the legitimate final message just because
        // it is shorter (V7 false-positive regression).
        expect(mailStore.Message.get(100).body).toBe(finalBody);
    });

    test("SSE message_chunk with placeholder body is skipped while streaming", () => {
        const thread = mailStore._addThread(1);
        const longBody = `<p>${"Streamed answer content. ".repeat(30)}</p>`;
        mailStore._addMessage({ id: 100, model: "llm.thread", res_id: 1, body: longBody });
        thread.messages.add(mailStore.Message.get(100));
        llmStore.streamingThreads.add(1);

        llmStore.handleStreamMessage(1, {
            type: "message_chunk",
            message: { id: 100, model: "llm.thread", res_id: 1, body: "<p>Thinking...</p>" },
        });

        expect(mailStore.Message.get(100).body).toBe(longBody);
    });
});

// ---------------------------------------------------------------------------
// FIX-4g — killed-run transient cleanup
// ---------------------------------------------------------------------------

describe("FIX-4g: terminal run events clean up the transient status line", () => {
    test("run_killed removes the transient status + stops streaming + sets killed state", () => {
        const thread = mailStore._addThread(1);
        llmStore.streamingThreads.add(1);
        llmStore._insertTransientStatusMessage(1);
        const tempId = llmStore._transientStatusMsgIds[1];
        expect(typeof tempId).toBe("number");
        expect(thread.messages.some((m) => m.id === tempId)).toBe(true);

        llmStore._handleOrchestrationBusEvent(1, {
            event: "run_killed",
            message: "Killed",
            run_id: 42,
        });

        // Transient status line removed from the thread + tracking cleared.
        expect(thread.messages.some((m) => m.id === tempId)).toBe(false);
        expect(llmStore._transientStatusMsgIds[1]).toBe(undefined);
        // Streaming stopped + terminal state recorded.
        expect(llmStore.streamingThreads.has(1)).toBe(false);
        expect(llmStore.getThreadRunState(1).state).toBe("killed");
    });

    test("run_failed also removes the transient status line", () => {
        const thread = mailStore._addThread(1);
        llmStore._insertTransientStatusMessage(1);
        const tempId = llmStore._transientStatusMsgIds[1];
        expect(thread.messages.some((m) => m.id === tempId)).toBe(true);

        llmStore._handleOrchestrationBusEvent(1, {
            event: "run_failed",
            message: "Failed",
            run_id: 43,
        });

        expect(thread.messages.some((m) => m.id === tempId)).toBe(false);
        expect(llmStore._transientStatusMsgIds[1]).toBe(undefined);
        expect(llmStore.getThreadRunState(1).state).toBe("failed");
    });
});

// ---------------------------------------------------------------------------
// FIX-4d — reasoning accumulation (bus + SSE) and active-target lifecycle
// ---------------------------------------------------------------------------

describe("FIX-4d: reasoning_chunk accumulation", () => {
    test("bus reasoning_chunk accumulates on body_json.reasoning + tracks active target", () => {
        mailStore._addThread(1);
        mailStore._addMessage({
            id: 100,
            model: "llm.thread",
            res_id: 1,
            body: "<p>answer</p>",
            llm_role: "assistant",
        });

        subscribers["llm.thread/reasoning_chunk"]({ message_id: 100, reasoning: "First, " });
        subscribers["llm.thread/reasoning_chunk"]({ message_id: 100, reasoning: "then this." });

        expect(mailStore.Message.get(100).body_json.reasoning).toBe("First, then this.");
        expect(llmStore.activeReasoningMessageId).toBe(100);
    });

    test("reasoning_chunk for an unknown message is a no-op (no throw)", () => {
        mailStore._addThread(1);
        subscribers["llm.thread/reasoning_chunk"]({ message_id: 999, reasoning: "orphan" });
        expect(mailStore.Message.get(999)).toBe(null);
        expect(llmStore.activeReasoningMessageId).toBe(null);
    });

    test("malformed reasoning_chunk payloads are ignored", () => {
        subscribers["llm.thread/reasoning_chunk"](null);
        subscribers["llm.thread/reasoning_chunk"]({ reasoning: "no id" });
        subscribers["llm.thread/reasoning_chunk"]({ message_id: "not-a-number", reasoning: "x" });
        expect(llmStore.activeReasoningMessageId).toBe(null);
    });

    test("SSE reasoning_chunk accumulates via handleStreamMessage (direct-LLM path)", () => {
        mailStore._addThread(1);
        mailStore._addMessage({
            id: 100,
            model: "llm.thread",
            res_id: 1,
            body: "<p>answer</p>",
            llm_role: "assistant",
        });

        llmStore.handleStreamMessage(1, {
            type: "reasoning_chunk",
            message: { id: 100, model: "llm.thread", res_id: 1 },
            reasoning: "SSE thinking…",
        });

        expect(mailStore.Message.get(100).body_json.reasoning).toBe("SSE thinking…");
        expect(llmStore.activeReasoningMessageId).toBe(100);
    });

    test("run_done clears the active reasoning target (block auto-collapses)", () => {
        mailStore._addThread(1);
        mailStore._addMessage({ id: 100, model: "llm.thread", res_id: 1, body: "<p>a</p>" });
        llmStore.activeReasoningMessageId = 100;

        llmStore._handleOrchestrationBusEvent(1, { event: "run_done", message: "Done" });

        expect(llmStore.activeReasoningMessageId).toBe(null);
    });

    test("stopStreaming clears the target only for the SAME thread", () => {
        mailStore._addThread(1);
        mailStore._addThread(2);
        mailStore._addMessage({ id: 100, model: "llm.thread", res_id: 2, body: "<p>a</p>" });
        llmStore.activeReasoningMessageId = 100;

        // Thread 1's stream stops — the reasoning target belongs to thread 2.
        llmStore.stopStreaming(1);
        expect(llmStore.activeReasoningMessageId).toBe(100);

        // Thread 2's stream stops — now it clears.
        llmStore.stopStreaming(2);
        expect(llmStore.activeReasoningMessageId).toBe(null);
    });

    test("accumulateReasoningText is null-safe and concatenates", () => {
        expect(accumulateReasoningText(undefined, "a")).toBe("a");
        expect(accumulateReasoningText("a", "b")).toBe("ab");
        expect(accumulateReasoningText("a", undefined)).toBe("a");
        expect(accumulateReasoningText(undefined, undefined)).toBe("");
    });
});

// ---------------------------------------------------------------------------
// FREEZE-FIX (TRACKER_2026-07-26_UI_BUS_HARDENING.md) — llmThreadList must
// not rewrite the reactive ``_threadOrderPin`` on every call. The previous
// unconditional write (fresh array identity per call) was a reactive write
// during render → invalidated the observing sidebar → re-render → getter
// again → write again → infinite render loop that pegged the main thread
// (~100% CPU) and froze the whole chat page. These tests pin the stability
// contract: the pin is only replaced when the thread MEMBERSHIP changes.
// ---------------------------------------------------------------------------

describe("FREEZE-FIX: llmThreadList pin is write-stable across calls", () => {
    /**
     * Register three llm threads with ascending write_dates and expose them
     * via the ``Thread.records`` dict the getter reads.
     */
    function addThreadsWithDates() {
        mailStore.Thread.records = {};
        for (const [id, wd] of [
            [1, "2026-07-26 10:00:00"],
            [2, "2026-07-26 11:00:00"],
            [3, "2026-07-26 12:00:00"],
        ]) {
            const thread = mailStore._addThread(id);
            thread.write_date = wd;
            mailStore.Thread.records[`llm.thread,${id}`] = thread;
        }
    }

    test("initial order is write_date DESC and pin identity is stable across calls", () => {
        addThreadsWithDates();
        const first = llmStore.llmThreadList;
        expect(first.map((t) => t.id)).toEqual([3, 2, 1]);
        const pin1 = llmStore._threadOrderPin;
        // Repeated calls (as happens on every render) must NOT replace the
        // pin with a fresh array — identity stability is what stops the
        // reactive render loop.
        llmStore.llmThreadList;
        llmStore.llmThreadList;
        llmStore.llmThreadList;
        expect(llmStore._threadOrderPin).toBe(pin1);
    });

    test("background write_date bump keeps position AND pin identity (FIX-1 contract)", () => {
        addThreadsWithDates();
        llmStore.llmThreadList;
        const pin1 = llmStore._threadOrderPin;
        // A running thread's write_date bumps in the background — the
        // sidebar position must NOT reshuffle (FIX-1) and the pin must not
        // be rewritten (FREEZE-FIX).
        mailStore.Thread.records["llm.thread,1"].write_date = "2026-07-26 13:00:00";
        const order = llmStore.llmThreadList.map((t) => t.id);
        expect(order).toEqual([3, 2, 1]);
        expect(llmStore._threadOrderPin).toBe(pin1);
        // Repeated calls after the bump: still stable.
        llmStore.llmThreadList;
        expect(llmStore._threadOrderPin).toBe(pin1);
    });

    test("new thread goes on top; pin changes exactly once, then is stable again", () => {
        addThreadsWithDates();
        llmStore.llmThreadList;
        const pin1 = llmStore._threadOrderPin;
        const t4 = mailStore._addThread(4);
        t4.write_date = "2026-07-26 12:30:00";
        mailStore.Thread.records["llm.thread,4"] = t4;
        const order = llmStore.llmThreadList.map((t) => t.id);
        expect(order).toEqual([4, 3, 2, 1]);
        const pin2 = llmStore._threadOrderPin;
        expect(pin2 === pin1).toBe(false);
        llmStore.llmThreadList;
        llmStore.llmThreadList;
        expect(llmStore._threadOrderPin).toBe(pin2);
    });

    test("removed thread is dropped from the pin; pin then stable", () => {
        addThreadsWithDates();
        llmStore.llmThreadList;
        delete mailStore.Thread.records["llm.thread,2"];
        const order = llmStore.llmThreadList.map((t) => t.id);
        expect(order).toEqual([3, 1]);
        const pin = llmStore._threadOrderPin;
        llmStore.llmThreadList;
        expect(llmStore._threadOrderPin).toBe(pin);
    });
});
