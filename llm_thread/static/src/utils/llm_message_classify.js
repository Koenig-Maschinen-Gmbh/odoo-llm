/** @odoo-module **/

/**
 * P-CHAT M2 — LLM message classification + turn grouping (pure helpers).
 *
 * Classification (per plan §2 "Verified grounding", no schema change):
 * - tool messages:          ``llm_role === 'tool'`` (equiv. ``body_json.type
 *   === 'tool_execution'``).
 * - intermediate assistant:  ``llm_role === 'assistant'`` WITH ``body_json.tool_calls``.
 * - final answer:            ``llm_role === 'assistant'`` WITHOUT ``body_json.tool_calls``.
 * - error:                   ``is_error`` (failed-run error messages stay
 *   prominent, outside the steps drawer).
 * - progress notification:   ``message_type === 'notification'`` WITHOUT
 *   ``llm_role`` — system-style progress messages ("Analyzing…",
 *   "Dispatching expert…", "…returned results.") posted by the
 *   orchestration runtime. These render as slim one-line status rows
 *   (no author header, no avatar sidebar) via ``o-llm-message-status``.
 *
 * Turn grouping: a turn = a user message + every assistant/tool message that
 * follows it, up to the next user message. The steps drawer groups the tool +
 * intermediate-assistant messages of one turn under a single
 * "Arbeitsschritte (N)" toggle.
 *
 * These functions are pure (no OWL, no services) so they can be unit-tested
 * in Hoot without mounting components. The Message patch getters delegate to
 * them with the thread's ordered message list.
 */

export function isLLMToolMessage(msg) {
    return msg?.llm_role === "tool" || msg?.body_json?.type === "tool_execution";
}

export function isLLMIntermediateAssistant(msg) {
    return msg?.llm_role === "assistant" && Boolean(msg?.body_json?.tool_calls?.length);
}

export function isLLMFinalAnswer(msg) {
    return msg?.llm_role === "assistant" && !msg?.body_json?.tool_calls?.length;
}

export function isLLMErrorMessage(msg) {
    return Boolean(msg?.is_error);
}

/**
 * UI-06 S1 — A "progress" message is a system-style notification posted by the
 * orchestration runtime: ``message_type === 'notification'`` with NO
 * ``llm_role`` (not a tool call, not an assistant turn). These are the
 * "Analyzing your request…", "Dispatching expert…", "…returned results."
 * messages that should render as slim one-line status rows — no author
 * header, no avatar sidebar, muted text.
 *
 * Error messages (``is_error``) are NOT progress messages — they stay
 * prominent with the error styling.
 */
export function isLLMProgressMessage(msg) {
    if (isLLMErrorMessage(msg)) {
        return false;
    }
    return msg?.message_type === "notification" && !msg?.llm_role;
}

/**
 * A "step" message is a tool message OR an intermediate assistant message
 * (the master thinking aloud / calling tools). Final answers and errors are
 * NOT steps — the final answer renders expanded, errors render prominent,
 * both outside the drawer.
 */
export function isLLMStepMessage(msg) {
    if (isLLMErrorMessage(msg)) {
        return false;
    }
    return isLLMToolMessage(msg) || isLLMIntermediateAssistant(msg);
}

/**
 * The turn id = id of the most recent USER message at or before ``msg`` in
 * display order. Returns ``null`` if there is no preceding user message.
 */
export function llmTurnIdForMessage(msg, orderedMessages) {
    if (!msg || !orderedMessages) {
        return null;
    }
    const idx = orderedMessages.findIndex((m) => m.id === msg.id);
    if (idx < 0) {
        return null;
    }
    for (let i = idx; i >= 0; i--) {
        if (orderedMessages[i].llm_role === "user") {
            return orderedMessages[i].id;
        }
    }
    return null;
}

/**
 * Number of step messages that share ``msg``'s turn.
 */
export function llmStepCountInTurn(msg, orderedMessages) {
    const turnId = llmTurnIdForMessage(msg, orderedMessages);
    if (turnId === null) {
        return 0;
    }
    return orderedMessages.filter(
        (m) => llmTurnIdForMessage(m, orderedMessages) === turnId && isLLMStepMessage(m)
    ).length;
}

/**
 * True if ``msg`` is the FIRST step message of its turn — the one that
 * renders the "Arbeitsschritte (N)" toggle for the whole turn.
 */
export function isLLMFirstStepOfTurn(msg, orderedMessages) {
    if (!isLLMStepMessage(msg) || !orderedMessages) {
        return false;
    }
    const idx = orderedMessages.findIndex((m) => m.id === msg.id);
    if (idx < 0) {
        return false;
    }
    const turnId = llmTurnIdForMessage(msg, orderedMessages);
    for (let i = idx - 1; i >= 0; i--) {
        if (orderedMessages[i].llm_role === "user") {
            break; // Reached the user message that opens this turn
        }
        if (
            llmTurnIdForMessage(orderedMessages[i], orderedMessages) === turnId &&
            isLLMStepMessage(orderedMessages[i])
        ) {
            return false; // An earlier step in the same turn exists
        }
    }
    return true;
}
