/** @odoo-module **/

import { Component, onWillStart, onWillUpdateProps, status, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * P-HUD — subtle stats display under the composer.
 *
 * Shows: model name, dispatched experts, tokens, € cost for the thread,
 * and month-to-date spend vs limit. Data is fetched via a single RPC
 * (``llm.thread._get_thread_stats``) on mount and when the thread ID
 * actually changes (not on every parent re-render).
 */
export class LLMThreadHud extends Component {
    static template = "llm_thread.LLMThreadHud";
    static props = {
        threadId: { type: Number, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            stats: null,
            loading: false,
        });
        this._lastLoadedThreadId = null;
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
}
