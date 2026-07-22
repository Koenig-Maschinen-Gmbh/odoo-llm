/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/**
 * UI-09 H1/H2 — Pure helpers for the HUD live phase indicator + run-summary chip.
 *
 * Extracted from ``llm_store_service.js`` (phase mapping) and
 * ``llm_thread_hud.js`` (label formatting) so the logic can be Hoot-tested
 * without mounting services. The store and HUD import these helpers; the
 * tests exercise them directly.
 *
 * Hoot API notes: ``_t()`` returns a ``LazyTranslatedString`` when the
 * translation cache is not loaded — do NOT coerce it (no ``String(s)``,
 * no ``s + ""``, no template literal). The tests assert on the *key*
 * (which phase), not the translated text.
 */

/**
 * Map a normalized orchestration bus event to a canonical phase string.
 *
 * The phase is an additive field on ``threadRunState`` — existing consumers
 * (sidebar) ignore it; the HUD reads it. The phases form a linear progression:
 *   analyzing → dispatching → gathering → synthesizing → done
 *
 * @param {string} normalizedEvent - The event name (run_started, expert_dispatched, etc.)
 * @returns {string|null} The phase string, or null for unrecognized events.
 */
export function eventToPhase(normalizedEvent) {
    const map = {
        run_started: "analyzing",
        expert_dispatched: "dispatching",
        expert_completed: "gathering",
        expert_failed: "gathering",
        run_done: "done",
        run_failed: "failed",
        run_cancelled: "cancelled",
        run_killed: "killed",
        run_timed_out: "timed_out",
        run_paused: "paused",
    };
    return map[normalizedEvent] || null;
}

/**
 * The canonical phase labels shown in the HUD.
 *
 * Returns ``_t()`` lazy strings — callers must use ``t-out`` in templates
 * (NOT string comparison). For boolean checks, use ``phaseIsDone`` /
 * ``phaseIsFailed`` getters instead of comparing the label.
 */
export const PHASE_LABELS = {
    analyzing: _t("Analyzing…"),
    dispatching: _t("Dispatching expert…"),
    gathering: _t("Gathering results…"),
    synthesizing: _t("Synthesizing answer…"),
    done: _t("Done"),
    failed: _t("Failed"),
    cancelled: _t("Cancelled"),
    paused: _t("Awaiting approval"),
    killed: _t("Killed"),
    timed_out: _t("Timed out"),
};

/**
 * Get the phase label for a phase string. Returns "Idle" for null/undefined.
 *
 * @param {string|null|undefined} phase - The phase string.
 * @returns {string} The translated label (a ``_t()`` lazy string).
 */
export function getPhaseLabel(phase) {
    if (!phase) {
        return _t("Idle");
    }
    return PHASE_LABELS[phase] || _t("Idle");
}

/**
 * Format the run-summary chip text: "✓ 3 tools · 2 experts · 4.2s".
 *
 * Uses plain English strings (NOT ``_t()``) so the utility is pure and
 * testable in Hoot without the translation cache. The HUD component wraps
 * the result in ``t-out`` — if German translations are needed for the
 * "tool"/"expert"/"s" labels later, they can be added at the component
 * level via a label map. For now the chip is short enough that English
 * labels are acceptable (the ✓ and numbers are language-neutral).
 *
 * @param {Object|null} summary - The run summary object from ``getRunSummary``.
 *   { durationSec, expertCount, toolCount, shownAt }
 * @returns {string} The formatted chip text, or "" if summary is null/empty.
 */
export function formatRunSummary(summary) {
    if (!summary) {
        return "";
    }
    const parts = [];
    if (summary.toolCount > 0) {
        const suffix = summary.toolCount === 1 ? " tool" : " tools";
        parts.push(`${summary.toolCount}${suffix}`);
    }
    if (summary.expertCount > 0) {
        const suffix = summary.expertCount === 1 ? " expert" : " experts";
        parts.push(`${summary.expertCount}${suffix}`);
    }
    if (summary.durationSec > 0) {
        parts.push(`${summary.durationSec}s`);
    }
    if (!parts.length) {
        return "";
    }
    return `✓ ${parts.join(" · ")}`;
}

/**
 * UI-12 — normalize the HUD run-health indicators from a threadRunState.
 *
 * Returns three counts rendered as subtle HUD spans (only non-zero values
 * are shown — same pattern as the existing tokens/cost spans):
 *   - ``experts``: live "N experts running" (dispatched minus completed/
 *     failed) — only meaningful while the run is active.
 *   - ``tools``: cumulative tool calls for the current run — live during
 *     the run; after completion the run-summary chip covers this.
 *   - ``errors``: expert/tool failures for the current/last run — persists
 *     after a failed run until the next ``run_started`` resets it.
 *
 * These are NOT debug-only: every user gets a "system health" sense while
 * waiting for the answer.
 *
 * @param {Object|null} threadRunState - The thread's run state object.
 * @returns {{experts: number, tools: number, errors: number}}
 */
export function getRunHealth(threadRunState) {
    if (!threadRunState) {
        return { experts: 0, tools: 0, errors: 0 };
    }
    const running = threadRunState.state === "running";
    return {
        experts: running ? threadRunState.expertsRunning || 0 : 0,
        tools: running ? threadRunState.toolCount || 0 : 0,
        errors: threadRunState.errorCount || 0,
    };
}

/**
 * Check if the run-summary should be visible (30s auto-dismiss).
 *
 * @param {Object} threadRunState - The thread's run state object.
 * @param {number} now - Current timestamp (epoch ms). Defaults to Date.now().
 * @returns {Object|null} The summary object if visible, null otherwise.
 */
export function getVisibleRunSummary(threadRunState, now = Date.now()) {
    if (!threadRunState || !threadRunState.lastRunSummary) {
        return null;
    }
    // Hide if a new run has started.
    if (threadRunState.state === "running") {
        return null;
    }
    // Auto-dismiss after 30s.
    const elapsed = now - (threadRunState.lastRunSummary.shownAt || 0);
    if (elapsed > 30000) {
        return null;
    }
    return threadRunState.lastRunSummary;
}
