/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import {
    isLLMErrorMessage,
    isLLMFinalAnswer,
    isLLMFirstStepOfTurn,
    isLLMIntermediateAssistant,
    isLLMProgressMessage,
    isLLMStepMessage,
    isLLMToolMessage,
    llmStepCountInTurn,
    llmTurnIdForMessage,
} from "../src/utils/llm_message_classify";

/**
 * P-CHAT M2 + UI-06 S1 — unit tests for the LLM message classification +
 * turn-grouping helpers. These are pure functions (no OWL, no services) so
 * they can be tested directly without mounting components. The Message patch
 * getters delegate to them, so getting them right is the contract for the
 * foreground/background hierarchy (steps drawer + final-answer + errors +
 * progress status lines).
 *
 * Hoot API notes: there is no ``.toContain`` / ``.toBeTruthy`` in Hoot — use
 * ``expect(str.includes(x)).toBe(true)`` / ``expect(Boolean(x)).toBe(true)``
 * instead (ref: ODOO-js-owl-guide.md § "Hoot API gotchas").
 */

describe("llm_message_classify", () => {
    // Mock message factories — mirror the mail store message shape
    // (id + llm_role + body_json + is_error + message_type).
    const user = (id) => ({ id, llm_role: "user" });
    const tool = (id) => ({
        id,
        llm_role: "tool",
        body_json: { type: "tool_execution", tool_name: "koenig_sap_query" },
    });
    const intermediate = (id) => ({
        id,
        llm_role: "assistant",
        body_json: { tool_calls: [{ id: "c1", function: { name: "x" } }] },
    });
    const final = (id) => ({ id, llm_role: "assistant", body_json: {} });
    const error = (id) => ({ id, llm_role: "assistant", is_error: true, body_json: {} });
    // UI-06 S1 — progress notification: message_type='notification', no llm_role.
    // These are the "Analyzing…", "Dispatching expert…", "…returned results."
    // messages posted by the orchestration runtime.
    const progress = (id) => ({
        id,
        message_type: "notification",
        llm_role: false,
    });

    test("tool message classification", () => {
        expect(isLLMToolMessage(tool(1))).toBe(true);
        expect(isLLMToolMessage(final(2))).toBe(false);
    });

    test("intermediate assistant carries tool_calls", () => {
        expect(isLLMIntermediateAssistant(intermediate(1))).toBe(true);
        expect(isLLMIntermediateAssistant(final(2))).toBe(false);
    });

    test("final answer = assistant without tool_calls", () => {
        expect(isLLMFinalAnswer(final(1))).toBe(true);
        expect(isLLMFinalAnswer(intermediate(2))).toBe(false);
    });

    test("error message classification", () => {
        expect(isLLMErrorMessage(error(1))).toBe(true);
        expect(isLLMErrorMessage(final(2))).toBe(false);
    });

    // --- UI-06 S1: progress notification classification -----------------

    describe("isLLMProgressMessage", () => {
        test("notification without llm_role is a progress message", () => {
            expect(isLLMProgressMessage(progress(1))).toBe(true);
        });

        test("notification with llm_role is NOT a progress message", () => {
            const msg = { id: 1, message_type: "notification", llm_role: "assistant" };
            expect(isLLMProgressMessage(msg)).toBe(false);
        });

        test("non-notification message is NOT a progress message", () => {
            expect(isLLMProgressMessage(user(1))).toBe(false);
            expect(isLLMProgressMessage(tool(1))).toBe(false);
            expect(isLLMProgressMessage(intermediate(1))).toBe(false);
            expect(isLLMProgressMessage(final(1))).toBe(false);
        });

        test("error message is NOT a progress message (errors stay prominent)", () => {
            // Even if an error happens to be a notification without llm_role,
            // it should NOT be classified as a progress message — errors keep
            // their prominent error styling.
            const errorNotification = {
                id: 1,
                message_type: "notification",
                llm_role: false,
                is_error: true,
            };
            expect(isLLMProgressMessage(errorNotification)).toBe(false);
            expect(isLLMErrorMessage(errorNotification)).toBe(true);
        });

        test("null safety", () => {
            expect(isLLMProgressMessage(null)).toBe(false);
            expect(isLLMProgressMessage(undefined)).toBe(false);
            expect(isLLMProgressMessage({})).toBe(false);
        });

        test("progress messages are NOT steps (excluded from drawer)", () => {
            // Progress messages render as standalone status lines, NOT inside
            // the steps drawer. They should not be classified as steps.
            expect(isLLMStepMessage(progress(1))).toBe(false);
        });

        test("message_type undefined with no llm_role is NOT progress", () => {
            // A message with no message_type and no llm_role is ambiguous —
            // only explicit 'notification' type qualifies.
            const ambiguous = { id: 1, llm_role: false };
            expect(isLLMProgressMessage(ambiguous)).toBe(false);
        });
    });

    test("step = tool OR intermediate, NOT final/error/progress", () => {
        expect(isLLMStepMessage(tool(1))).toBe(true);
        expect(isLLMStepMessage(intermediate(2))).toBe(true);
        expect(isLLMStepMessage(final(3))).toBe(false);
        expect(isLLMStepMessage(error(4))).toBe(false);
        expect(isLLMStepMessage(progress(5))).toBe(false);
    });

    test("null safety", () => {
        expect(isLLMToolMessage(null)).toBe(false);
        expect(isLLMStepMessage(null)).toBe(false);
        expect(llmTurnIdForMessage(null, [])).toBe(null);
    });

    test("turn id = most recent user message at or before", () => {
        const msgs = [
            user(10),
            tool(11),
            intermediate(12),
            final(13),
            user(20),
            tool(21),
            final(22),
        ];
        expect(llmTurnIdForMessage(tool(11), msgs)).toBe(10);
        expect(llmTurnIdForMessage(intermediate(12), msgs)).toBe(10);
        expect(llmTurnIdForMessage(final(13), msgs)).toBe(10);
        expect(llmTurnIdForMessage(user(10), msgs)).toBe(10);
        expect(llmTurnIdForMessage(tool(21), msgs)).toBe(20);
        expect(llmTurnIdForMessage(final(22), msgs)).toBe(20);
    });

    test("step count per turn", () => {
        const msgs = [
            user(10),
            tool(11),
            intermediate(12),
            final(13),
            user(20),
            tool(21),
            final(22),
        ];
        expect(llmStepCountInTurn(tool(11), msgs)).toBe(2); // Tool(11), intermediate(12)
        expect(llmStepCountInTurn(intermediate(12), msgs)).toBe(2);
        expect(llmStepCountInTurn(final(13), msgs)).toBe(2);
        expect(llmStepCountInTurn(tool(21), msgs)).toBe(1);
    });

    test("first step of turn", () => {
        const msgs = [
            user(10),
            tool(11),
            intermediate(12),
            final(13),
            user(20),
            tool(21),
            final(22),
        ];
        expect(isLLMFirstStepOfTurn(tool(11), msgs)).toBe(true);
        expect(isLLMFirstStepOfTurn(intermediate(12), msgs)).toBe(false);
        expect(isLLMFirstStepOfTurn(tool(21), msgs)).toBe(true);
        expect(isLLMFirstStepOfTurn(final(13), msgs)).toBe(false);
    });

    test("errors stay out of the drawer", () => {
        const msgs = [user(10), tool(11), error(12), final(13)];
        expect(isLLMStepMessage(error(12))).toBe(false);
        expect(isLLMFirstStepOfTurn(error(12), msgs)).toBe(false);
        // The error is not counted as a step
        expect(llmStepCountInTurn(tool(11), msgs)).toBe(1);
    });

    test("message with no preceding user has null turn", () => {
        const msgs = [tool(1), final(2)];
        expect(llmTurnIdForMessage(tool(1), msgs)).toBe(null);
        expect(llmStepCountInTurn(tool(1), msgs)).toBe(0);
    });
});
