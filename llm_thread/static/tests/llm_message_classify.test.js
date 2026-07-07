/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import {
    isLLMToolMessage,
    isLLMIntermediateAssistant,
    isLLMFinalAnswer,
    isLLMErrorMessage,
    isLLMStepMessage,
    llmTurnIdForMessage,
    llmStepCountInTurn,
    isLLMFirstStepOfTurn,
} from "../src/utils/llm_message_classify";

/**
 * P-CHAT M2 — unit tests for the LLM message classification + turn-grouping
 * helpers. These are pure functions (no OWL, no services) so they can be
 * tested directly without mounting components. The Message patch getters
 * delegate to them, so getting them right is the contract for the
 * foreground/background hierarchy (steps drawer + final-answer + errors).
 */

describe("llm_message_classify", () => {
    // Mock message factories — mirror the mail store message shape
    // (id + llm_role + body_json + is_error).
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

    test("step = tool OR intermediate, NOT final/error", () => {
        expect(isLLMStepMessage(tool(1))).toBe(true);
        expect(isLLMStepMessage(intermediate(2))).toBe(true);
        expect(isLLMStepMessage(final(3))).toBe(false);
        expect(isLLMStepMessage(error(4))).toBe(false);
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
        expect(llmStepCountInTurn(tool(11), msgs)).toBe(2); // tool(11), intermediate(12)
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
        // the error is not counted as a step
        expect(llmStepCountInTurn(tool(11), msgs)).toBe(1);
    });

    test("message with no preceding user has null turn", () => {
        const msgs = [tool(1), final(2)];
        expect(llmTurnIdForMessage(tool(1), msgs)).toBe(null);
        expect(llmStepCountInTurn(tool(1), msgs)).toBe(0);
    });
});
