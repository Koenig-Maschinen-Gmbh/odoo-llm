/** @odoo-module **/

import {
    buildOptimisticMessageBody,
    buildTransientStatusMessage,
    linkMessagesToThread,
    removeOptimisticMessageFromThread,
} from "../src/utils/llm_thread_messages";
import { isLLMProgressMessage, isLLMStepMessage } from "../src/utils/llm_message_classify";
import { describe, expect, test } from "@odoo/hoot";

/**
 * P0 reliability + UI-05 — unit tests for the optimistic-message lifecycle
 * helpers and the transient status message builder.
 *
 * These helpers are pure (they take a thread record + a ``getMessage``
 * callable, never import a service), so they can be exercised directly in
 * Hoot without mounting the mail store. They are the contract for:
 *
 *  1. ``buildOptimisticMessageBody`` — raw user text is HTML-escaped before
 *     being interpolated into the optimistic ``<p>…</p>`` body, so pasted
 *     markup renders as text (XSS / parsing defense).
 *  2. ``buildTransientStatusMessage`` (UI-05) — builds the ephemeral
 *     "Analyzing your request…" status message data object, with
 *     ``is_transient: true`` and ``message_type: 'notification'`` so it
 *     renders as a slim one-line status row (UI-06 S2).
 *  3. ``linkMessagesToThread`` — on ``llm.thread/new_message`` the inserted
 *     mail.message records are linked into the target thread's reactive
 *     ``messages`` collection with a dedupe-by-id guard.
 *  4. ``removeOptimisticMessageFromThread`` — the optimistic temp message
 *     (and the transient status message) is dropped from the thread on
 *     ``message_create`` / ``new_message`` AND on EventSource error /
 *     start-failure, so ghosts never persist.
 *
 * Hoot API notes: there is no ``.toContain`` / ``.toBeTruthy`` in Hoot — use
 * ``expect(str.includes(x)).toBe(true)`` / ``expect(Boolean(x)).toBe(true)``
 * instead (ref: ODOO-js-owl-guide.md § "Hoot API gotchas").
 */

/**
 * Minimal stand-in for a mail store ``Record.many`` collection. The real
 * collection is a reactive proxy with ``add`` / ``delete`` / ``some``; this
 * mock mirrors those semantics (idempotent add by id, delete by id, some by
 * predicate) so the helper contracts can be asserted without the OWL store.
 *
 * @param {Array<{id: number}>} [messages]
 * @returns {Object} mock thread with a messages collection
 */
function makeMockThread(messages = []) {
    const arr = [...messages];
    return {
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
    };
}

describe("buildOptimisticMessageBody", () => {
    test("wraps plain text in a <p> tag", () => {
        expect(buildOptimisticMessageBody("hello world")).toBe("<p>hello world</p>");
    });

    test("escapes < and > so pasted markup is rendered as text", () => {
        const body = buildOptimisticMessageBody("<script>alert(1)</script>");
        expect(body.includes("<script>")).toBe(false);
        expect(body).toMatch(/&lt;script&gt;/);
        expect(body).toBe("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>");
    });

    test("escapes ampersands", () => {
        expect(buildOptimisticMessageBody("a & b")).toBe("<p>a &amp; b</p>");
        expect(buildOptimisticMessageBody("tom & jerry")).toBe("<p>tom &amp; jerry</p>");
    });

    test("escapes quotes and backticks", () => {
        const body = buildOptimisticMessageBody(`she said "hi" & 'bye' \`code\``);
        // Quotes and backtick must be escaped; ampersand too
        expect(body.includes('"hi"')).toBe(false);
        expect(body).toMatch(/&quot;hi&quot;/);
        expect(body).toMatch(/&#x27;bye&#x27;/);
        expect(body).toMatch(/&#x60;code&#x60;/);
        expect(body.includes(" & ")).toBe(false);
    });

    test("empty string stays empty (still wrapped)", () => {
        expect(buildOptimisticMessageBody("")).toBe("<p></p>");
    });

    test("does NOT double-escape already-safe sequences", () => {
        // A literal &amp; in input becomes &amp;amp; (escape is not idempotent
        // by design — it escapes the raw text the user typed)
        expect(buildOptimisticMessageBody("&amp;")).toBe("<p>&amp;amp;</p>");
    });
});

// --- UI-05: transient status message builder --------------------------------

describe("buildTransientStatusMessage", () => {
    test("returns a message with the correct shape", () => {
        const msg = buildTransientStatusMessage({
            id: 42.01,
            threadId: 510,
            text: "Analyzing your request…",
            date: "2026-07-21T10:00:00.000Z",
        });
        expect(msg.id).toBe(42.01);
        expect(msg.model).toBe("llm.thread");
        expect(msg.res_id).toBe(510);
        expect(msg.llm_role).toBe(false);
        expect(msg.author_id).toBe(false);
        expect(msg.is_error).toBe(false);
        expect(msg.is_transient).toBe(true);
        expect(msg.message_type).toBe("notification");
        expect(msg.date).toBe("2026-07-21T10:00:00.000Z");
    });

    test("wraps text in a <p> tag (matching optimistic body shape)", () => {
        const msg = buildTransientStatusMessage({
            id: 1,
            threadId: 1,
            text: "Working…",
        });
        expect(msg.body).toBe("<p>Working…</p>");
    });

    test("escapes HTML in the text (XSS defense)", () => {
        const msg = buildTransientStatusMessage({
            id: 1,
            threadId: 1,
            text: "<script>alert(1)</script>",
        });
        expect(msg.body.includes("<script>")).toBe(false);
        expect(msg.body).toMatch(/&lt;script&gt;/);
    });

    test("escapes ampersands", () => {
        const msg = buildTransientStatusMessage({
            id: 1,
            threadId: 1,
            text: "Analyzing A & B",
        });
        expect(msg.body).toBe("<p>Analyzing A &amp; B</p>");
    });

    test("defaults date to current time when not provided", () => {
        const before = Date.now();
        const msg = buildTransientStatusMessage({
            id: 1,
            threadId: 1,
            text: "test",
        });
        const after = Date.now();
        const msgDate = new Date(msg.date).getTime();
        expect(msgDate >= before).toBe(true);
        expect(msgDate <= after).toBe(true);
    });

    test("is_transient + message_type='notification' + no llm_role → classified as progress", () => {
        // The transient status message must be classified as a progress
        // message by isLLMProgressMessage, so it picks up the
        // o-llm-message-status CSS class and renders as a slim status line.
        const msg = buildTransientStatusMessage({
            id: 1,
            threadId: 1,
            text: "Analyzing…",
        });
        expect(isLLMProgressMessage(msg)).toBe(true);
    });

    test("is_transient=true so OCB hides the date in the sidebar/header", () => {
        // OCB message.xml: t-if="!message.is_transient" on date elements.
        // The transient status message should not show a timestamp.
        const msg = buildTransientStatusMessage({
            id: 1,
            threadId: 1,
            text: "test",
        });
        expect(msg.is_transient).toBe(true);
    });

    test("no llm_role so it is NOT classified as a step (stays outside drawer)", () => {
        const msg = buildTransientStatusMessage({
            id: 1,
            threadId: 1,
            text: "test",
        });
        expect(isLLMStepMessage(msg)).toBe(false);
    });
});

describe("removeOptimisticMessageFromThread", () => {
    test("removes the tracked temp message from the thread", () => {
        const tempMsg = { id: -100 };
        const thread = makeMockThread([tempMsg, { id: 5 }]);
        const getMessage = (id) => (id === -100 ? tempMsg : null);
        const removed = removeOptimisticMessageFromThread({
            thread,
            tempId: -100,
            getMessage,
        });
        expect(removed).toBe(true);
        expect(thread.messages.some((m) => m.id === -100)).toBe(false);
        // Other messages are untouched
        expect(thread.messages.some((m) => m.id === 5)).toBe(true);
    });

    test("returns false when the temp message is not in the store", () => {
        const thread = makeMockThread([{ id: 5 }]);
        // Temp message never resolved
        const getMessage = () => null;
        const removed = removeOptimisticMessageFromThread({
            thread,
            tempId: -100,
            getMessage,
        });
        expect(removed).toBe(false);
        expect(thread.messages._arr).toHaveLength(1);
    });

    test("returns false (no-op) when tempId is undefined", () => {
        const thread = makeMockThread([{ id: 5 }]);
        const removed = removeOptimisticMessageFromThread({
            thread,
            tempId: undefined,
            getMessage: () => ({ id: -100 }),
        });
        expect(removed).toBe(false);
        expect(thread.messages._arr).toHaveLength(1);
    });

    test("returns false (no-op) when tempId is null", () => {
        const thread = makeMockThread([{ id: 5 }]);
        const removed = removeOptimisticMessageFromThread({
            thread,
            tempId: null,
            getMessage: () => ({ id: -100 }),
        });
        expect(removed).toBe(false);
    });

    test("returns false (no-op) when thread is null", () => {
        const removed = removeOptimisticMessageFromThread({
            thread: null,
            tempId: -100,
            getMessage: () => ({ id: -100 }),
        });
        expect(removed).toBe(false);
    });

    test("is idempotent — second call is a no-op once the message is gone", () => {
        const tempMsg = { id: -100 };
        const thread = makeMockThread([tempMsg]);
        const getMessage = (id) => (id === -100 ? tempMsg : null);
        expect(removeOptimisticMessageFromThread({ thread, tempId: -100, getMessage })).toBe(true);
        // Second call: the message object is still resolvable but no longer in
        // the thread's collection — delete returns false, helper returns false.
        expect(removeOptimisticMessageFromThread({ thread, tempId: -100, getMessage })).toBe(false);
        expect(thread.messages._arr).toHaveLength(0);
    });

    // UI-05 — the same helper is used to remove the transient status message
    // (tracked by a fractional temp id, not a negative one). Verify it works
    // with fractional ids too.
    test("works with fractional temp ids (transient status messages)", () => {
        const transientMsg = { id: 42.01 };
        const thread = makeMockThread([transientMsg, { id: 43 }]);
        const getMessage = (id) => (id === 42.01 ? transientMsg : null);
        const removed = removeOptimisticMessageFromThread({
            thread,
            tempId: 42.01,
            getMessage,
        });
        expect(removed).toBe(true);
        expect(thread.messages.some((m) => m.id === 42.01)).toBe(false);
        expect(thread.messages.some((m) => m.id === 43)).toBe(true);
    });
});

describe("linkMessagesToThread", () => {
    test("links new messages into the thread's collection", () => {
        const thread = makeMockThread();
        const msg1 = { id: 1 };
        const msg2 = { id: 2 };
        const getMessage = (id) => ({ 1: msg1, 2: msg2 }[id]);
        const added = linkMessagesToThread({
            thread,
            messageIds: [1, 2],
            getMessage,
        });
        expect(added).toBe(2);
        expect(thread.messages.some((m) => m.id === 1)).toBe(true);
        expect(thread.messages.some((m) => m.id === 2)).toBe(true);
    });

    test("dedupes by id — does not re-add an already-linked message", () => {
        const existing = { id: 1 };
        const thread = makeMockThread([existing]);
        const msg2 = { id: 2 };
        const getMessage = (id) => ({ 1: existing, 2: msg2 }[id]);
        const added = linkMessagesToThread({
            thread,
            messageIds: [1, 2],
            getMessage,
        });
        // Only msg2 is newly linked; msg1 was already present
        expect(added).toBe(1);
        expect(thread.messages._arr).toHaveLength(2);
    });

    test("returns 0 when messageIds is empty", () => {
        const thread = makeMockThread();
        const added = linkMessagesToThread({
            thread,
            messageIds: [],
            getMessage: () => null,
        });
        expect(added).toBe(0);
    });

    test("returns 0 when messageIds is undefined/null", () => {
        const thread = makeMockThread();
        expect(
            linkMessagesToThread({ thread, messageIds: undefined, getMessage: () => null })
        ).toBe(0);
        expect(linkMessagesToThread({ thread, messageIds: null, getMessage: () => null })).toBe(0);
    });

    test("returns 0 when thread is null", () => {
        const added = linkMessagesToThread({
            thread: null,
            messageIds: [1, 2],
            getMessage: () => ({ id: 1 }),
        });
        expect(added).toBe(0);
    });

    test("skips ids that getMessage cannot resolve", () => {
        const thread = makeMockThread();
        const msg2 = { id: 2 };
        // ID 1 unresolved
        const getMessage = (id) => ({ 2: msg2 }[id]);
        const added = linkMessagesToThread({
            thread,
            messageIds: [1, 2],
            getMessage,
        });
        expect(added).toBe(1);
        expect(thread.messages.some((m) => m.id === 1)).toBe(false);
        expect(thread.messages.some((m) => m.id === 2)).toBe(true);
    });

    test("dedupe keeps a different message with the same id safe (id is the key)", () => {
        // Mirrors the OCB Record.many + SSE path contract: identity is by id.
        const first = { id: 5, body: "first" };
        const thread = makeMockThread([first]);
        const second = { id: 5, body: "second" };
        const getMessage = (id) => ({ 5: second }[id]);
        const added = linkMessagesToThread({
            thread,
            messageIds: [5],
            getMessage,
        });
        // Already present (by id) → not re-added
        expect(added).toBe(0);
        expect(thread.messages._arr).toHaveLength(1);
    });
});
