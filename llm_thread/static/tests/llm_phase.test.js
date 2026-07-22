/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import {
    eventToPhase,
    formatRunSummary,
    getPhaseLabel,
    getRunHealth,
    getVisibleRunSummary,
    PHASE_LABELS,
} from "../src/utils/llm_phase";

/**
 * UI-09 H1/H2 — unit tests for the phase mapping + run-summary formatting
 * helpers. These are pure functions (no OWL, no services) so they can be
 * tested directly without mounting components.
 *
 * Hoot API notes: ``_t()`` returns a ``LazyTranslatedString`` when the
 * translation cache is not loaded — tests assert on the *key* (which phase),
 * not the translated text. For label comparison, use ``getPhaseLabel(phase)``
 * which returns the lazy string — we assert on its existence, not its value.
 * See ODOO-js-owl-guide.md § "Hoot API gotchas".
 */

describe("eventToPhase", () => {
    test("run_started → analyzing", () => {
        expect(eventToPhase("run_started")).toBe("analyzing");
    });

    test("expert_dispatched → dispatching", () => {
        expect(eventToPhase("expert_dispatched")).toBe("dispatching");
    });

    test("expert_completed → gathering", () => {
        expect(eventToPhase("expert_completed")).toBe("gathering");
    });

    test("expert_failed → gathering (still gathering results)", () => {
        expect(eventToPhase("expert_failed")).toBe("gathering");
    });

    test("run_done → done", () => {
        expect(eventToPhase("run_done")).toBe("done");
    });

    test("run_failed → failed", () => {
        expect(eventToPhase("run_failed")).toBe("failed");
    });

    test("run_cancelled → cancelled", () => {
        expect(eventToPhase("run_cancelled")).toBe("cancelled");
    });

    test("run_killed → killed", () => {
        expect(eventToPhase("run_killed")).toBe("killed");
    });

    test("run_timed_out → timed_out", () => {
        expect(eventToPhase("run_timed_out")).toBe("timed_out");
    });

    test("run_paused → paused", () => {
        expect(eventToPhase("run_paused")).toBe("paused");
    });

    test("run_resumed → null (keeps previous phase, handled by caller)", () => {
        expect(eventToPhase("run_resumed")).toBe(null);
    });

    test("unknown event → null", () => {
        expect(eventToPhase("unknown_event")).toBe(null);
    });

    test("null/undefined event → null", () => {
        expect(eventToPhase(null)).toBe(null);
        expect(eventToPhase(undefined)).toBe(null);
        expect(eventToPhase("")).toBe(null);
    });
});

describe("PHASE_LABELS", () => {
    test("all expected phases have labels", () => {
        const expectedPhases = [
            "analyzing",
            "dispatching",
            "gathering",
            "synthesizing",
            "done",
            "failed",
            "cancelled",
            "paused",
            "killed",
            "timed_out",
        ];
        for (const phase of expectedPhases) {
            expect(Boolean(PHASE_LABELS[phase])).toBe(true);
        }
    });

    test("no extra phases in the map", () => {
        const keys = Object.keys(PHASE_LABELS);
        expect(keys.length).toBe(10);
    });
});

describe("getPhaseLabel", () => {
    test("returns a label for each known phase", () => {
        // Assert on truthiness, not the translated value (Hoot _t lazy string).
        expect(Boolean(getPhaseLabel("analyzing"))).toBe(true);
        expect(Boolean(getPhaseLabel("dispatching"))).toBe(true);
        expect(Boolean(getPhaseLabel("gathering"))).toBe(true);
        expect(Boolean(getPhaseLabel("synthesizing"))).toBe(true);
        expect(Boolean(getPhaseLabel("done"))).toBe(true);
        expect(Boolean(getPhaseLabel("failed"))).toBe(true);
        expect(Boolean(getPhaseLabel("cancelled"))).toBe(true);
        expect(Boolean(getPhaseLabel("paused"))).toBe(true);
        expect(Boolean(getPhaseLabel("killed"))).toBe(true);
        expect(Boolean(getPhaseLabel("timed_out"))).toBe(true);
    });

    test("null/undefined phase → Idle label", () => {
        expect(Boolean(getPhaseLabel(null))).toBe(true);
        expect(Boolean(getPhaseLabel(undefined))).toBe(true);
        expect(Boolean(getPhaseLabel(""))).toBe(true);
    });

    test("unknown phase → Idle label (fallback)", () => {
        expect(Boolean(getPhaseLabel("unknown_phase"))).toBe(true);
    });
});

describe("formatRunSummary", () => {
    test("null summary → empty string", () => {
        expect(formatRunSummary(null)).toBe("");
    });

    test("undefined summary → empty string", () => {
        expect(formatRunSummary(undefined)).toBe("");
    });

    test("all zeros → empty string (nothing to show)", () => {
        const summary = { durationSec: 0, expertCount: 0, toolCount: 0, shownAt: Date.now() };
        expect(formatRunSummary(summary)).toBe("");
    });

    test("tools only", () => {
        const summary = { durationSec: 0, expertCount: 0, toolCount: 3, shownAt: Date.now() };
        const result = formatRunSummary(summary);
        // Plain string output — can assert on exact content.
        expect(result.startsWith("✓")).toBe(true);
        expect(result.includes("3 tools")).toBe(true);
    });

    test("experts only", () => {
        const summary = { durationSec: 0, expertCount: 2, toolCount: 0, shownAt: Date.now() };
        const result = formatRunSummary(summary);
        expect(result.startsWith("✓")).toBe(true);
        expect(result.includes("2 experts")).toBe(true);
    });

    test("duration only", () => {
        const summary = { durationSec: 4.2, expertCount: 0, toolCount: 0, shownAt: Date.now() };
        const result = formatRunSummary(summary);
        expect(result.startsWith("✓")).toBe(true);
        expect(result.includes("4.2s")).toBe(true);
    });

    test("all three — combined chip", () => {
        const summary = { durationSec: 4.2, expertCount: 2, toolCount: 3, shownAt: Date.now() };
        const result = formatRunSummary(summary);
        expect(result.startsWith("✓")).toBe(true);
        expect(result.includes("3 tools")).toBe(true);
        expect(result.includes("2 experts")).toBe(true);
        expect(result.includes("4.2s")).toBe(true);
        // Parts are joined with " · "
        expect(result.includes(" · ")).toBe(true);
    });

    test("single tool (singular form)", () => {
        const summary = { durationSec: 0, expertCount: 0, toolCount: 1, shownAt: Date.now() };
        const result = formatRunSummary(summary);
        // "1 tool" — singular, no "s" suffix.
        expect(result.includes("1 tool")).toBe(true);
        expect(result.includes("1 tools")).toBe(false);
    });

    test("single expert (singular form)", () => {
        const summary = { durationSec: 0, expertCount: 1, toolCount: 0, shownAt: Date.now() };
        const result = formatRunSummary(summary);
        expect(result.includes("1 expert")).toBe(true);
        expect(result.includes("1 experts")).toBe(false);
    });

    test("exact combined output", () => {
        const summary = { durationSec: 4.2, expertCount: 2, toolCount: 3, shownAt: Date.now() };
        expect(formatRunSummary(summary)).toBe("✓ 3 tools · 2 experts · 4.2s");
    });
});

describe("getVisibleRunSummary", () => {
    test("null state → null", () => {
        expect(getVisibleRunSummary(null)).toBe(null);
    });

    test("no lastRunSummary → null", () => {
        expect(getVisibleRunSummary({ state: "done" })).toBe(null);
    });

    test("state running → null (new run started, hide old summary)", () => {
        const st = {
            state: "running",
            lastRunSummary: { durationSec: 4.2, expertCount: 2, toolCount: 3, shownAt: Date.now() },
        };
        expect(getVisibleRunSummary(st)).toBe(null);
    });

    test("state done + within 30s → returns summary", () => {
        const summary = { durationSec: 4.2, expertCount: 2, toolCount: 3, shownAt: Date.now() };
        const st = { state: "done", lastRunSummary: summary };
        expect(getVisibleRunSummary(st)).toBe(summary);
    });

    test("state done + exactly 30s → still visible (boundary)", () => {
        const now = Date.now();
        const summary = {
            durationSec: 4.2,
            expertCount: 2,
            toolCount: 3,
            shownAt: now - 30000,
        };
        const st = { state: "done", lastRunSummary: summary };
        expect(getVisibleRunSummary(st, now)).toBe(summary);
    });

    test("state done + >30s → null (auto-dismissed)", () => {
        const now = Date.now();
        const summary = {
            durationSec: 4.2,
            expertCount: 2,
            toolCount: 3,
            shownAt: now - 30001,
        };
        const st = { state: "done", lastRunSummary: summary };
        expect(getVisibleRunSummary(st, now)).toBe(null);
    });

    test("state failed + within 30s → returns summary (failed runs also get a chip)", () => {
        const summary = { durationSec: 2.1, expertCount: 1, toolCount: 0, shownAt: Date.now() };
        const st = { state: "failed", lastRunSummary: summary };
        expect(getVisibleRunSummary(st)).toBe(summary);
    });

    test("missing shownAt → treated as 0 → auto-dismissed (safety)", () => {
        const summary = { durationSec: 4.2, expertCount: 2, toolCount: 3 };
        const st = { state: "done", lastRunSummary: summary };
        // shownAt is undefined → elapsed = Date.now() - 0 = huge → > 30000 → null
        expect(getVisibleRunSummary(st)).toBe(null);
    });

    test("custom now parameter controls visibility", () => {
        const shownAt = 1000000;
        const summary = { durationSec: 4.2, expertCount: 2, toolCount: 3, shownAt: shownAt };
        const st = { state: "done", lastRunSummary: summary };
        // At shownAt + 10s → visible
        expect(getVisibleRunSummary(st, shownAt + 10000)).toBe(summary);
        // At shownAt + 31s → dismissed
        expect(getVisibleRunSummary(st, shownAt + 31000)).toBe(null);
    });
});

// UI-12 — getRunHealth: normalize the HUD run-health indicators.
describe("getRunHealth", () => {
    test("null/undefined state → all zeros", () => {
        expect(getRunHealth(null)).toEqual({ experts: 0, tools: 0, errors: 0 });
        expect(getRunHealth(undefined)).toEqual({ experts: 0, tools: 0, errors: 0 });
    });

    test("empty state object → all zeros", () => {
        expect(getRunHealth({})).toEqual({ experts: 0, tools: 0, errors: 0 });
    });

    test("running state → live experts + tools + errors", () => {
        const st = {
            state: "running",
            expertsRunning: 2,
            toolCount: 5,
            errorCount: 1,
        };
        expect(getRunHealth(st)).toEqual({ experts: 2, tools: 5, errors: 1 });
    });

    test("running state with missing counters → zeros (no crash)", () => {
        expect(getRunHealth({ state: "running" })).toEqual({
            experts: 0,
            tools: 0,
            errors: 0,
        });
    });

    test("done state → experts/tools zeroed, errors persist", () => {
        const st = {
            state: "done",
            expertsRunning: 0,
            toolCount: 7,
            errorCount: 2,
        };
        // After the run the live indicators zero out (the run-summary chip
        // covers the totals); errors stay visible until the next run.
        expect(getRunHealth(st)).toEqual({ experts: 0, tools: 0, errors: 2 });
    });

    test("failed state → errors persist", () => {
        const st = { state: "failed", errorCount: 3 };
        expect(getRunHealth(st)).toEqual({ experts: 0, tools: 0, errors: 3 });
    });

    test("cancelled state → experts/tools zeroed", () => {
        const st = { state: "cancelled", expertsRunning: 0, toolCount: 4 };
        expect(getRunHealth(st)).toEqual({ experts: 0, tools: 0, errors: 0 });
    });

    test("paused state → experts/tools zeroed (not running)", () => {
        const st = { state: "paused", expertsRunning: 1, toolCount: 2 };
        expect(getRunHealth(st)).toEqual({ experts: 0, tools: 0, errors: 0 });
    });
});
