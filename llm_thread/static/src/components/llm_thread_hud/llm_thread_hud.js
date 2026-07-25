/** @odoo-module **/

import {
    Component,
    onMounted,
    onWillStart,
    onWillUnmount,
    onWillUpdateProps,
    status,
    useState,
} from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { formatRunSummary, getPhaseLabel, getRunHealth } from "../../utils/llm_phase";

/**
 * P-HUD — subtle stats display under the composer.
 *
 * Shows: model name, dispatched experts, tokens, € cost for the thread,
 * and month-to-date spend vs limit. Data is fetched via a single RPC
 * (``llm.thread._get_thread_stats``) on mount and when the thread ID
 * actually changes (not on every parent re-render).
 *
 * UI-09 H1 — Live phase indicator: reads ``threadRunState.phase`` from the
 * llm.store service (reactive via ``useState``) and displays the current
 * orchestration phase: "Idle" → "Analyzing…" → "Dispatching expert…" →
 * "Gathering results…" → "Synthesizing answer…" → "Done". The phase is
 * driven by bus events already handled in ``llm_store_service.js``
 * (``_handleOrchestrationBusEvent``) — no additional RPC needed.
 *
 * UI-09 H2 — Run-summary chip: after a run completes, a compact chip
 * appears showing "✓ N tools · N experts · Ns". Data comes from
 * ``llmStore.getRunSummary(threadId)`` which auto-dismisses after 30s.
 * Clicking the chip opens the steps drawer of the last turn.
 */
export class LLMThreadHud extends Component {
    static template = "llm_thread.LLMThreadHud";
    static props = {
        threadId: { type: Number, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        // UI-09 H1/H2 — reactive store read for the live phase + run-summary.
        // ``useState`` wraps the store's reactive object so the HUD re-renders
        // when ``threadRunState`` changes (pattern proven in LLMSidebar).
        this.llmStore = useState(useService("llm.store"));
        this.state = useState({
            stats: null,
            // FIX-2: context usage stats (ring + popover breakdown).
            contextStats: null,
            contextPopoverOpen: false,
            loading: false,
            // UI-09 H2 — tick to force re-evaluation of the 30s auto-dismiss.
            // The store's ``getRunSummary`` checks elapsed time, but without
            // a tick the getter doesn't re-evaluate. The tick starts when a
            // run-summary appears and stops after it auto-dismisses.
            summaryTick: 0,
        });
        this._lastLoadedThreadId = null;
        this._tickTimer = null;
        onWillStart(() => this._loadStats());
        // Only reload when the thread ID actually changes — NOT on every
        // parent re-render. The parent (LLMChatContainer) re-renders on
        // every llmStore state change (setThreadRunState, streaming, etc.).
        // Without this guard, onWillUpdateProps fires on every re-render,
        // creating an infinite RPC loop: stats RPC → state change → parent
        // re-render → onWillUpdateProps → stats RPC → …
        onWillUpdateProps((nextProps) => {
            if (nextProps.threadId !== this._lastLoadedThreadId) {
                this._loadStats();
            }
        });
        // UI-09 H1/H2 — on-demand tick for the live elapsed counter + the
        // 30s auto-dismiss. Starts when a run is active or a run-summary
        // chip is showing; stops when idle. Same pattern as LLMSidebar.
        onMounted(() => this._maybeStartTick());
        onWillUnmount(() => this._maybeStopTick());
    }

    /**
     * UI-09 H1/H2 — on-demand tick. The tick re-evaluates:
     *   - the elapsed mm:ss counter (while a run is active)
     *   - the 30s auto-dismiss of the run-summary chip
     * It starts when either is needed and stops when both are idle.
     */
    _maybeStartTick() {
        if (this._tickTimer) {
            return;
        }
        this._tickTimer = setInterval(() => {
            const threadId = this.props.threadId;
            if (!threadId) {
                this._maybeStopTick();
                return;
            }
            const st = this.llmStore.getThreadRunState(threadId);
            const summary = this.llmStore.getRunSummary(threadId);
            if (st?.state === "running" || summary) {
                this.state.summaryTick++;
            } else {
                // Final re-render to remove stale elements, then stop.
                this.state.summaryTick++;
                this._maybeStopTick();
            }
        }, 1000);
    }

    _maybeStopTick() {
        if (this._tickTimer) {
            clearInterval(this._tickTimer);
            this._tickTimer = null;
        }
    }

    async _loadStats() {
        const threadId = this.props.threadId;
        if (!threadId) {
            this.state.stats = null;
            this._lastLoadedThreadId = null;
            return;
        }
        this._lastLoadedThreadId = threadId;
        this.state.loading = true;
        try {
            // Public method (no leading underscore): Odoo 18's
            // ``get_public_method`` rejects ``_``-prefixed methods from RPC
            // with ``AccessError: Private methods ... cannot be called
            // remotely`` — that silently hid the whole HUD (the catch set
            // stats=null → hasStats=false → not rendered).
            const stats = await this.orm.call("llm.thread", "get_thread_stats", [threadId]);
            if (status(this) === "destroyed") {
                return;
            }
            // Staleness guard: if the thread changed during the RPC,
            // discard the result (the new thread's _loadStats is already
            // in flight via onWillUpdateProps).
            if (this.props.threadId !== threadId) {
                return;
            }
            this.state.stats = stats;
            // FIX-2: fetch context stats alongside the spend stats (single
            // mount/switch — not on every re-render). Graceful: if the
            // endpoint returns zeros (koenig_ai_core not installed), the
            // context segment simply hides.
            try {
                const ctxStats = await this.orm.call("llm.thread", "get_context_stats", [threadId]);
                if (status(this) === "destroyed" || this.props.threadId !== threadId) {
                    return;
                }
                this.state.contextStats = ctxStats;
            } catch {
                this.state.contextStats = null;
            }
        } catch {
            if (status(this) === "destroyed") {
                return;
            }
            this.state.stats = null;
        }
        if (status(this) !== "destroyed") {
            this.state.loading = false;
        }
    }

    /**
     * The HUD renders whenever stats have loaded (non-null). It always shows
     * at least the model name; tokens/cost/monthly spans show only when their
     * value is non-zero (see the *Label getters + template t-if). This keeps
     * the HUD visible on every thread (so the user sees the stats bar) instead
     * of hiding entirely when a thread has no spend yet.
     */
    get hasStats() {
        return Boolean(this.state.stats);
    }

    get costLabel() {
        const s = this.state.stats;
        if (!s || !s.cost) {
            return "";
        }
        const cur = s.currency || "";
        return `${s.cost.toFixed(4)} ${cur}`.trim();
    }

    get monthlyLabel() {
        const s = this.state.stats;
        if (!s || !s.monthly_budget) {
            return "";
        }
        const cur = s.currency || "";
        return `${s.monthly_spend.toFixed(2)} / ${s.monthly_budget.toFixed(2)} ${cur}`.trim();
    }

    get tokensLabel() {
        const s = this.state.stats;
        if (!s || !s.tokens) {
            return "";
        }
        if (s.tokens >= 1000) {
            return `${(s.tokens / 1000).toFixed(1)}k`;
        }
        return String(s.tokens);
    }

    // ------------------------------------------------------------------
    // FIX-2 — context usage meter (Kilo-Code-style)
    // ------------------------------------------------------------------

    /**
     * Returns the context usage data for the ring/bar, or null when no
     * context stats are available (koenig_ai_core not installed or no
     * trace yet). The headline number is ``last_prompt_tokens`` (real
     * usage_input from the latest LLM call), NOT the estimate total.
     */
    get contextData() {
        const c = this.state.contextStats;
        if (!c || !c.context_window || !c.last_prompt_tokens) {
            return null;
        }
        const used = c.last_prompt_tokens;
        const window = c.context_window;
        const reserved = c.reserved_output || 0;
        const available = Math.max(0, window - used - reserved);
        const pct = window > 0 ? Math.round((used / window) * 100) : 0;
        return {
            used,
            window,
            reserved,
            available,
            pct,
            // Thresholds for coloring (Kilo-Code convention).
            level: pct >= 90 ? "danger" : pct >= 70 ? "warning" : "ok",
            estimate: c.estimate || {},
            lastPromptAt: c.last_prompt_at,
        };
    }

    get contextLabel() {
        const d = this.contextData;
        if (!d) {
            return "";
        }
        const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
        return `${fmt(d.used)} / ${fmt(d.window)} (${d.pct}%)`;
    }

    _toggleContextPopover() {
        this.state.contextPopoverOpen = !this.state.contextPopoverOpen;
    }

    // ------------------------------------------------------------------
    // UI-09 H1 — Live phase indicator
    // ------------------------------------------------------------------

    /**
     * The current orchestration phase label for the active thread.
     * Returns a human-readable string driven by ``threadRunState.phase``
     * from the store. When no run is active, returns "Idle".
     *
     * Phase mapping (set in ``_handleOrchestrationBusEvent``):
     *   analyzing   → "Analyzing…"
     *   dispatching → "Dispatching expert…"
     *   gathering   → "Gathering results…"
     *   synthesizing → "Synthesizing answer…"
     *   done        → "Done"
     *   failed      → "Failed"
     *   cancelled   → "Cancelled"
     *   paused      → "Awaiting approval"
     *   killed      → "Killed"
     *   timed_out   → "Timed out"
     */
    get phaseLabel() {
        const threadId = this.props.threadId;
        if (!threadId) {
            return "";
        }
        const st = this.llmStore.getThreadRunState(threadId);
        if (!st) {
            return getPhaseLabel(null);
        }
        // Touch the tick so the elapsed timer re-evaluates (same pattern
        // as LLMSidebar.threadElapsedLabel).
        void this.state.summaryTick;
        const phase = st.phase || (st.state === "running" ? "analyzing" : st.state);
        return getPhaseLabel(phase);
    }

    /**
     * Whether the phase indicator should show a spinner icon.
     * True for any active (non-terminal) phase.
     */
    get phaseIsActive() {
        const threadId = this.props.threadId;
        if (!threadId) {
            return false;
        }
        const st = this.llmStore.getThreadRunState(threadId);
        if (!st) {
            return false;
        }
        return st.state === "running" || st.state === "paused";
    }

    /**
     * Whether the phase is "done" (green check icon).
     * Checked against the raw phase/state — NOT the translated label
     * (``_t()`` returns a ``LazyTranslatedString`` which can't be compared
     * with ``===`` in the template).
     */
    get phaseIsDone() {
        const threadId = this.props.threadId;
        if (!threadId) {
            return false;
        }
        const st = this.llmStore.getThreadRunState(threadId);
        return Boolean(st && (st.phase === "done" || st.state === "done"));
    }

    /**
     * Whether the phase is "failed" (red exclamation icon).
     */
    get phaseIsFailed() {
        const threadId = this.props.threadId;
        if (!threadId) {
            return false;
        }
        const st = this.llmStore.getThreadRunState(threadId);
        return Boolean(st && (st.phase === "failed" || st.state === "failed"));
    }

    /**
     * The elapsed time label (mm:ss) for the active run. Re-evaluated on
     * every render via the tick. Pattern mirrors LLMSidebar.threadElapsedLabel.
     */
    get elapsedLabel() {
        const threadId = this.props.threadId;
        if (!threadId) {
            return "";
        }
        void this.state.summaryTick;
        const st = this.llmStore.getThreadRunState(threadId);
        if (!st || st.state !== "running" || !st.startedAt) {
            return "";
        }
        const ms = Date.now() - st.startedAt;
        const totalSec = Math.max(0, Math.floor(ms / 1000));
        const mm = String(Math.floor(totalSec / 60)).padStart(2, "0");
        const ss = String(totalSec % 60).padStart(2, "0");
        return `${mm}:${ss}`;
    }

    // ------------------------------------------------------------------
    // UI-12 — Run-health indicators (experts running / tool calls / errors)
    // ------------------------------------------------------------------

    /**
     * The run-health counts for the active thread:
     * ``{ experts, tools, errors }`` — the template shows only non-zero
     * values (same pattern as the existing tokens/cost spans). NOT
     * debug-only: every user gets a subtle "system health" sense while
     * waiting for the answer. Logic lives in the pure ``getRunHealth``
     * helper (Hoot-tested); this getter is the reactive store read.
     */
    get runHealth() {
        const threadId = this.props.threadId;
        if (!threadId) {
            return { experts: 0, tools: 0, errors: 0 };
        }
        // Touch the tick so the indicators re-evaluate every second while
        // a run is active (same pattern as ``phaseLabel``/``elapsedLabel``).
        void this.state.summaryTick;
        return getRunHealth(this.llmStore.getThreadRunState(threadId));
    }

    // ------------------------------------------------------------------
    // UI-09 H2 — Run-summary chip
    // ------------------------------------------------------------------

    /**
     * The run-summary chip data for the active thread, or null if the
     * chip should not be shown (no completed run, auto-dismissed after
     * 30s, or a new run has started).
     *
     * Delegates to ``llmStore.getRunSummary(threadId)`` which handles the
     * 30s auto-dismiss. The getter is reactive (``useState``) so the chip
     * appears/disappears automatically.
     */
    get runSummary() {
        const threadId = this.props.threadId;
        if (!threadId) {
            return null;
        }
        // Touch the tick so the 30s auto-dismiss re-evaluates.
        void this.state.summaryTick;
        return this.llmStore.getRunSummary(threadId);
    }

    /**
     * The formatted run-summary chip text: "✓ 3 tools · 2 experts · 4.2s".
     * Returns an empty string if runSummary is null.
     */
    get runSummaryLabel() {
        return formatRunSummary(this.runSummary);
    }

    /**
     * UI-09 H2 — click handler for the run-summary chip. Opens the steps
     * drawer of the last turn (the most recent user message). The turn ID
     * is the user message ID that precedes the completed run's messages.
     */
    _onSummaryClick() {
        const thread = this.llmStore.activeLLMThread;
        if (!thread || !thread.messages) {
            return;
        }
        // Find the most recent user message — its ID is the turn ID.
        const messages = thread.messages;
        let lastUserMsgId = null;
        for (let i = messages.length - 1; i >= 0; i--) {
            const m = messages[i];
            if (m && m.llm_role === "user") {
                lastUserMsgId = m.id;
                break;
            }
        }
        if (lastUserMsgId) {
            this.llmStore.setStepDrawerOpen(lastUserMsgId, true);
        }
    }
}
