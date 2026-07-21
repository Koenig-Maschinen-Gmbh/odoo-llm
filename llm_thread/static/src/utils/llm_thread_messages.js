/** @odoo-module **/

import { escape } from "@web/core/utils/strings";

/**
 * P0 reliability helpers for the LLM thread message lifecycle.
 *
 * These three helpers are *pure* with respect to the OWL/services layer: they
 * take plain params (a thread record, a ``getMessage`` callable, a temp id, …)
 * and never import a service themselves. That makes the optimistic-message
 * contract — escape on insert, dedupe on link, clean on failure — unit-testable
 * in Hoot without mounting the mail store. The store service delegates to them.
 *
 * Grounding:
 *  - ``escape``: OCB ``@web/core/utils/strings`` (escapes ``& < > ' " ` ``).
 *  - new-message linking: OCB ``discuss.channel/new_message`` handler
 *    (``discuss_core_common_service.js:46-52, 147-160``) inserts then links the
 *    message into the thread's reactive ``messages`` collection with a dedupe
 *    guard (``message.notIn(channel.messages)``). The SSE ``message_create``
 *    path in ``llm_store_service.js`` mirrors the same contract with
 *    ``thread.messages.add()`` + a ``.some()`` id guard.
 */

/**
 * Build the optimistic HTML body shown for a user's message *before* the
 * server confirms it. The raw user text is HTML-escaped so pasted markup
 * (``<script>``, ``&``, …) renders as text instead of being parsed/executed.
 *
 * @param {String} text raw user-typed message
 * @returns {String} safe HTML string, e.g. ``<p>hi &amp; bye</p>``
 */
export function buildOptimisticMessageBody(text) {
    return `<p>${escape(text)}</p>`;
}

/**
 * UI-05 — Build the transient client-side status message object inserted on
 * ``orchestration_started``. Follows the OCB ``is_transient`` precedent
 * (``discuss_core_common_service.js:53-69``): a non-persisted ephemeral
 * message that renders in the timeline and is removed when the real
 * progress message arrives via the WebSocket bus.
 *
 * The message is classified as a "progress notification" by
 * ``isLLMProgressMessage`` (``message_type === 'notification'`` + no
 * ``llm_role``), so it picks up the ``o-llm-message-status`` CSS class and
 * renders as a slim one-line status row (UI-06 S2).
 *
 * @param {Object} params
 * @param {Number} params.id ephemeral id (fractional, e.g. ``lastMessageId + 0.01``)
 * @param {Number} params.threadId the llm.thread id
 * @param {String} params.text the status text (already translated by the caller)
 * @param {String} [params.date] ISO date string (defaults to now)
 * @returns {Object} the transient message data object for ``mailStore.insert``
 */
export function buildTransientStatusMessage({ id, threadId, text, date }) {
    return {
        id,
        model: "llm.thread",
        res_id: threadId,
        body: `<p>${escape(text)}</p>`,
        llm_role: false,
        author_id: false,
        is_error: false,
        is_transient: true,
        message_type: "notification",
        date: date || new Date().toISOString(),
    };
}

/**
 * Remove a tracked optimistic temp message from a thread's reactive messages
 * collection. Idempotent: returns ``false`` (no-op) when there is nothing to
 * remove — either the temp message cannot be resolved, OR it resolves but is
 * no longer linked to the thread's collection (already cleaned up by a prior
 * call). Membership is checked by id before delete, mirroring the OCB
 * ``message.notIn(channel.messages)`` guard in
 * ``discuss_core_common_service.js:160``.
 *
 * @param {Object} params
 * @param {Object|null} params.thread the llm.thread store record (or null)
 * @param {Number} params.tempId the negative temp id tracked for this thread
 * @param {Function} params.getMessage ``(id) => mail.message record | falsy``
 * @returns {Boolean} whether a message was removed from the thread
 */
export function removeOptimisticMessageFromThread({ thread, tempId, getMessage }) {
    if (tempId === undefined || tempId === null || !thread) {
        return false;
    }
    const tempMsg = getMessage(tempId);
    if (!tempMsg) {
        return false;
    }
    // Idempotence: only delete (and report a real removal) when the message is
    // still linked to the thread's reactive messages collection. A temp message
    // that resolves but is no longer in the collection is already cleaned up —
    // return false so callers can tell a no-op from a real removal.
    if (!thread.messages.some((m) => m.id === tempMsg.id)) {
        return false;
    }
    thread.messages.delete(tempMsg);
    return true;
}

/**
 * Link a list of inserted ``mail.message`` records into a target thread's
 * reactive ``messages`` collection, skipping any already present (dedupe by
 * id). Mirrors the OCB ``discuss.channel/new_message`` handler and the
 * existing SSE ``message_create`` path.
 *
 * @param {Object} params
 * @param {Object|null} params.thread the target llm.thread store record
 * @param {Number[]} params.messageIds ids of the inserted messages
 * @param {Function} params.getMessage ``(id) => mail.message record | falsy``
 * @returns {Number} count of messages newly linked to the thread
 */
export function linkMessagesToThread({ thread, messageIds, getMessage }) {
    if (!thread || !messageIds?.length) {
        return 0;
    }
    let added = 0;
    for (const id of messageIds) {
        const message = getMessage(id);
        if (message && !thread.messages.some((m) => m.id === message.id)) {
            thread.messages.add(message);
            added += 1;
        }
    }
    return added;
}
