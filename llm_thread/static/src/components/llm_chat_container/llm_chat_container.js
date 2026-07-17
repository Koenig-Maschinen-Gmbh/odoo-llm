/** @odoo-module **/

import { Component, onWillUnmount, status, useRef, useState } from "@odoo/owl";
import { Composer } from "@mail/core/common/composer";
import { LLMThreadHeader } from "../llm_thread_header/llm_thread_header";
import { LLMSidebar } from "../llm_sidebar/llm_sidebar";
import { LLMThreadHud } from "../llm_thread_hud/llm_thread_hud";
import { Thread } from "@mail/core/common/thread";
import { useService } from "@web/core/utils/hooks";
import { browser } from "@web/core/browser/browser";

// Sidebar resize constants (wiki pattern: localStorage + CSS custom property).
const SIDEBAR_WIDTH_KEY = "llm_thread.sidebar_width";
const SIDEBAR_WIDTH_DEFAULT = 280;
const SIDEBAR_WIDTH_MIN = 200;
const SIDEBAR_WIDTH_MAX = 500;

// P-CHAT link-navigation: intercept /odoo/<model>/<id> record links in AI
// answer bodies + route them through the action service (doAction) so the
// record form opens via the SPA action stack — giving a breadcrumb back to
// the AI thread. A plain <a href="/odoo/..."> click would otherwise be
// SPA-navigated by the router (router.js global handler) but the router's
// loadState REPLACES the action stack → no breadcrumb back. doAction PUSHES
// onto the stack → breadcrumb back works. ev.preventDefault() prevents the
// router's handler from double-firing (it checks ev.defaultPrevented).
// Pattern: koenig_wiki/static/src/wiki_editor.js (_handleMentionClick /
// _anchorClickTarget, lines 808-832 + 926-932). PDF preview links
// (/web/content/...) + external links → browser default.
const _ODOO_RECORD_LINK_RE = /^\/odoo\/([a-z][a-z0-9_.]+)\/(\d+)\b/;

/**
 * LLM Chat Container - Main container for LLM chat UI
 * Uses existing mail Thread and Composer components with LLM patches.
 *
 * P-UX: the sidebar (grouping / search / tags / archive / bulk) now lives in
 * ``LLMSidebar``. This container keeps the layout (mobile slide-in vs desktop
 * collapse wrapper) + the main chat area, and delegates the sidebar to a
 * single ``<LLMSidebar>`` instance per layout.
 */
export class LLMChatContainer extends Component {
    static components = { Thread, Composer, LLMThreadHeader, LLMSidebar, LLMThreadHud };
    static template = "llm_thread.LLMChatContainer";
    static props = {
        recordModel: { type: String, optional: true },
        recordId: { type: Number, optional: true },
    };

    setup() {
        this.llmStore = useState(useService("llm.store"));
        this.mailStore = useState(useService("mail.store"));
        this.action = useService("action");
        this.ui = useState(useService("ui")); // Wrap with useState to make it reactive

        // Reference to the scrollable thread container for proper jump-to-present behavior
        this.threadScrollableRef = useRef("threadScrollable");

        // Read persisted sidebar width (wiki pattern).
        let initialWidth = SIDEBAR_WIDTH_DEFAULT;
        try {
            const raw = browser.localStorage.getItem(SIDEBAR_WIDTH_KEY);
            const parsed = parseInt(raw, 10);
            if (Number.isFinite(parsed)) {
                initialWidth = Math.min(SIDEBAR_WIDTH_MAX, Math.max(SIDEBAR_WIDTH_MIN, parsed));
            }
        } catch {
            // Non-fatal — use default width.
        }

        // Sidebar layout state (the sidebar CONTENT state — search, archive,
        // bulk, buckets — lives inside LLMSidebar).
        this.state = useState({
            // Desktop: collapse/expand state (default collapsed in chatter mode)
            isSidebarCollapsed: Boolean(this.props.recordModel && this.props.recordId),
            // Mobile: slide-in modal visibility
            isMobileSidebarVisible: false,
            // Resizable sidebar width (persisted in localStorage).
            sidebarWidth: initialWidth,
            resizingSidebar: false,
        });

        // Bind resize handlers once (wiki pattern).
        this._onSidebarResizeMove = this._onSidebarResizeMove.bind(this);
        this._onSidebarResizeEnd = this._onSidebarResizeEnd.bind(this);
        onWillUnmount(() => this._detachSidebarResizeListeners());
    }

    /**
     * Check if should use mobile layout:
     * - On actual mobile devices (window < 768px)
     * - In chatter positioned on the side (narrow panel)
     *
     * Note: Chatter below form is full-width, so uses desktop layout
     */
    get isSmall() {
        const isActuallySmall = this.ui.isSmall;
        const isChatterAside = this.env.inChatter?.aside ?? false;
        const shouldUseMobileLayout = isActuallySmall || isChatterAside;
        return shouldUseMobileLayout;
    }

    /**
     * Get the active thread from standard mail.store.discuss
     */
    get activeThread() {
        const thread = this.mailStore.discuss?.thread;
        return thread;
    }

    /**
     * P-CHAT link-navigation: delegated click handler on the thread message
     * area. Intercepts <a href="/odoo/<model>/<id>"> record links (from M4
     * enrich_html / the record-link [[model:id label]] markers) + routes them
     * through the action service so the record opens via the SPA action stack
     * (breadcrumb back to the AI thread). Ctrl/Cmd/middle-click → new tab.
     * External links, #anchors, and /web/content/ PDF preview links → browser
     * default. Bound on the threadScrollable div (t-on-click in the template).
     */
    onThreadAreaClick(ev) {
        if (ev.defaultPrevented || ev.target.closest("[contenteditable]")) {
            return;
        }
        const a = ev.target.closest("a");
        if (!a) {
            return;
        }
        let href = a.getAttribute("href") || "";
        if (!href || href.startsWith("#")) {
            return;
        }
        // Strip our own origin so absolute same-origin links route in-app.
        const origin = browser.location.origin;
        if (origin && href.startsWith(origin)) {
            href = href.slice(origin.length) || "/";
        }
        // Only intercept /odoo/<model>/<id> record links. PDF preview links
        // (/web/content/...) + external links → browser default.
        const match = href.match(_ODOO_RECORD_LINK_RE);
        if (!match) {
            return;
        }
        const model = match[1];
        const id = parseInt(match[2], 10);
        if (!model || !Number.isFinite(id)) {
            return;
        }
        ev.preventDefault();
        const newTab = ev.ctrlKey || ev.metaKey || ev.button === 1;
        if (newTab) {
            browser.open(a.href, "_blank", "noopener");
            return;
        }
        // Push the record form onto the action stack → breadcrumb back to the chat.
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    /**
     * P-UX Item 7.1: Jump to the previous/next "main" message in the chat —
     * a user request OR an AI final answer — skipping the in-between
     * thinking/tool-step messages (`.o-llm-step`), which can be long in
     * agentic threads. Used by the floating jump arrows.
     *
     * The combined set (user messages + final answers) is queried in document
     * order, so "up" lands on the previous main message and "down" on the
     * next one relative to the current scroll position.
     * @param {'up'|'down'} direction - 'up' for previous, 'down' for next
     */
    jumpToMainMessage(direction) {
        const container = this.threadScrollableRef?.el;
        if (!container) {
            return;
        }
        // User requests + AI final answers (NOT the muted step/thinking
        // messages — those are the noise this tool lets you jump over).
        // Use both class-based and role-based queries for robustness: the
        // class is added by message_patch.js className getter, but if the
        // patch hasn't applied yet (timing), the role-based fallback
        // catches user messages via the data attribute.
        let mainMessages = Array.from(
            container.querySelectorAll(".o-llm-message-user, .o-llm-final-answer")
        );
        // Fallback: also check for mail's standard message containers that
        // contain user-authored content (llm_role === "user").
        if (!mainMessages.length) {
            mainMessages = Array.from(container.querySelectorAll(".o-mail-Message")).filter(
                (el) => {
                    const role = el.dataset?.llmRole || el.getAttribute?.("data-llm-role");
                    return role === "user" || role === "assistant";
                }
            );
        }
        if (!mainMessages.length) {
            return;
        }
        const containerRect = container.getBoundingClientRect();
        // Use a threshold slightly below the top of the visible area so
        // the "previous" message isn't the one currently at the top.
        const threshold = containerRect.top + 50;
        let target = null;
        if (direction === "up") {
            for (let i = mainMessages.length - 1; i >= 0; i--) {
                const rect = mainMessages[i].getBoundingClientRect();
                if (rect.top < threshold) {
                    target = mainMessages[i];
                    break;
                }
            }
        } else {
            for (const msg of mainMessages) {
                const rect = msg.getBoundingClientRect();
                if (rect.top > threshold) {
                    target = msg;
                    break;
                }
            }
        }
        if (target) {
            // Use "nearest" instead of "start" to avoid creating whitespace
            // below the composer when scrolling to the last message.
            target.scrollIntoView({
                behavior: "smooth",
                block: "nearest",
            });
        }
    }

    /**
     * Check if we have an active LLM thread
     */
    get hasActiveThread() {
        return this.activeThread?.model === "llm.thread";
    }

    /**
     * Get composer for the active thread
     */
    get threadComposer() {
        return this.activeThread?.composer;
    }

    /**
     * Check if this thread is currently streaming
     */
    get isStreaming() {
        return this.llmStore.getStreamingStatus();
    }

    /**
     * Select thread - delegates to LLM store service.
     * On mobile, closes the sidebar after selection.
     * @param {Number} threadId - Thread ID to select
     */
    async selectThread(threadId) {
        await this.llmStore.selectThread(threadId);
        // Close mobile sidebar after selecting thread
        if (this.ui.isSmall) {
            this.closeMobileSidebar();
        }
    }

    /**
     * Create new thread - delegates to llm store service
     * Passes record context if available (e.g., from chatter)
     */
    async createNewThread() {
        await this.llmStore.createNewThread({
            recordModel: this.props.recordModel,
            recordId: this.props.recordId,
        });
    }

    /**
     * Toggle sidebar collapse/expand state (desktop only)
     */
    toggleSidebar() {
        this.state.isSidebarCollapsed = !this.state.isSidebarCollapsed;
    }

    /**
     * Open mobile sidebar (slide in from left)
     */
    openMobileSidebar() {
        this.state.isMobileSidebarVisible = true;
    }

    /**
     * Close mobile sidebar (slide out to left)
     */
    closeMobileSidebar() {
        this.state.isMobileSidebarVisible = false;
    }

    /**
     * Handle backdrop click - closes mobile sidebar
     */
    onBackdropClick() {
        this.closeMobileSidebar();
    }

    /**
     * Open thread settings form view (following 16.0 pattern)
     * Opens llm.thread form in dialog for editing all settings
     */
    async openThreadSettings() {
        if (!this.activeThread) {
            console.warn("[LLMChatContainer] No active thread to open settings for");
            return;
        }

        await this.action.doAction(
            {
                type: "ir.actions.act_window",
                res_model: "llm.thread",
                res_id: this.activeThread.id,
                views: [[false, "form"]],
                target: "new",
                context: { form_view_initial_mode: "edit" },
            },
            {
                onClose: async () => {
                    // Refresh thread data after closing form
                    await this.activeThread.fetchData([
                        "name",
                        "provider_id",
                        "model_id",
                        "tool_ids",
                        "assistant_id",
                    ]);
                },
            }
        );
    }

    // ------------------------------------------------------------------
    // Sidebar resize (Bug 4 — wiki pattern: localStorage + CSS var)
    // ------------------------------------------------------------------

    onSidebarResizeStart(ev) {
        if (ev.button !== 0 || this.state.isSidebarCollapsed) {
            return;
        }
        ev.preventDefault();
        this._sidebarResizeStart = {
            startX: ev.clientX,
            startWidth: this.state.sidebarWidth,
        };
        this.state.resizingSidebar = true;
        document.addEventListener("mousemove", this._onSidebarResizeMove);
        document.addEventListener("mouseup", this._onSidebarResizeEnd);
    }

    _onSidebarResizeMove(ev) {
        if (!this._sidebarResizeStart || status(this) === "destroyed") {
            return;
        }
        const delta = ev.clientX - this._sidebarResizeStart.startX;
        const next = this._sidebarResizeStart.startWidth + delta;
        this.state.sidebarWidth = Math.min(SIDEBAR_WIDTH_MAX, Math.max(SIDEBAR_WIDTH_MIN, next));
    }

    _onSidebarResizeEnd() {
        if (!this._sidebarResizeStart) {
            return;
        }
        this._sidebarResizeStart = null;
        if (status(this) !== "destroyed") {
            this.state.resizingSidebar = false;
            try {
                browser.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(this.state.sidebarWidth));
            } catch {
                // Quota exceeded — non-fatal.
            }
        }
        this._detachSidebarResizeListeners();
    }

    _detachSidebarResizeListeners() {
        document.removeEventListener("mousemove", this._onSidebarResizeMove);
        document.removeEventListener("mouseup", this._onSidebarResizeEnd);
    }

    onSidebarResizeReset() {
        this.state.sidebarWidth = SIDEBAR_WIDTH_DEFAULT;
        try {
            browser.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(SIDEBAR_WIDTH_DEFAULT));
        } catch {
            // Quota exceeded — non-fatal.
        }
    }
}

// Accept any props (like updateActionState)
LLMChatContainer.props = {
    "*": true,
};
