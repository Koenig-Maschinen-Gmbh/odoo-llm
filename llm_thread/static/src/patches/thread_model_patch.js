/** @odoo-module **/

import { Thread } from "@mail/core/common/thread_model";
import { Record } from "@mail/model/record";
import { patch } from "@web/core/utils/patch";
import { router } from "@web/core/browser/router";

/**
 * Patch Thread model to:
 *
 * 1. Declare LLM-specific fields as ``Record.attr`` (P-UX review §2.2) so
 *    the mail store tracks them properly — incremental updates via
 *    ``store.insert()`` instead of full ``_reloadThreads()``, reactive
 *    dependencies, no stale plain-property issues. Previously these were
 *    sent as opaque dict values via ``store.add("mail.thread", dict)`` and
 *    silently set as non-tracked properties on the record proxy.
 *
 * 2. Handle ``llm.thread`` URLs (``setActiveURL``).
 *
 * 3. Fix mobile routing (P-UX review §2.8): LLM threads have their own
 *    client action (``llm_thread.chat_client_action``), not a discuss chat
 *    window. On mobile, OCB's ``Thread.open()`` calls ``openChatWindow()``
 *    which is ``discuss.channel``-only and silently falls through to
 *    opening the backend ``llm.thread`` form view — making the chat UI
 *    unreachable on mobile. For ``llm.thread`` on mobile, either return
 *    (already in the chat UI) or navigate to the client action.
 */
patch(Thread.prototype, {
    setup() {
        super.setup(...arguments);
        // P-UX review §2.2: declare custom fields so the mail store tracks
        // them through Record.attr. These are plain attrs (not Record.one /
        // Record.many) because the fork sends them as plain values / dict
        // arrays, not as store record references.
        this.active = Record.attr(true);
        // UI-11: expert sub-threads are hidden from the sidebar by default.
        this.is_expert_subthread = Record.attr(false);
        this.tag_ids = Record.attr([]);
        this.provider_id = Record.attr(false);
        this.model_id = Record.attr(false);
        this.tool_ids = Record.attr([]);
    },

    /**
     * Override ``open()`` to handle ``llm.thread`` on mobile.
     *
     * On mobile, OCB's ``Thread.open()`` calls ``openChatWindow()`` which is
     * ``discuss.channel``-only. For ``llm.thread`` it silently falls through
     * to opening a form view. Fix: if we're already in the LLM chat client
     * action, just return (the thread is already set as ``discuss.thread``
     * by ``setAsDiscussThread``). If not, navigate to the client action. On
     * desktop, behave like OCB (``setAsDiscussThread``).
     */
    open(options) {
        if (this.model === "llm.thread") {
            if (this.store.env.services.ui.isSmall) {
                const currentAction = this.store.env.services.action?.currentController?.action;
                if (currentAction?.tag === "llm_thread.chat_client_action") {
                    // Already in the LLM chat UI — thread is already set.
                    return;
                }
                // Not in the chat UI — navigate to the client action.
                this.store.env.services.action.doAction({
                    type: "ir.actions.client",
                    tag: "llm_thread.chat_client_action",
                    params: { active_id: `llm.thread_${this.id}` },
                });
                return;
            }
            return this.setAsDiscussThread();
        }
        return super.open(...arguments);
    },

    /**
     * Update action context with active_id.
     * @param {String} activeId - Active ID to set
     */
    _updateActionContext(activeId) {
        if (
            !this.store?.action_discuss_id ||
            !this.store.env?.services?.action?.currentController?.action
        ) {
            return;
        }

        const currentAction = this.store.env.services.action.currentController.action;
        if (currentAction.id !== this.store.action_discuss_id) {
            return;
        }

        // Keep the action stack up to date (used by breadcrumbs).
        if (!currentAction.context) {
            currentAction.context = {};
        }
        currentAction.context.active_id = activeId;
    },

    /**
     * Override setActiveURL to handle llm.thread model.
     */
    setActiveURL() {
        // Handle llm.thread model specifically
        if (this.model === "llm.thread") {
            try {
                const activeId = `llm.thread_${this.id}`;

                // Safely update router state
                if (router && router.pushState) {
                    router.pushState({ active_id: activeId });
                }

                // Update action context if available
                this._updateActionContext(activeId);
            } catch (error) {
                console.warn("Error updating URL for LLM thread:", error);
                // Continue without failing
            }
        } else {
            // For all other models, use the original implementation
            super.setActiveURL();
        }
    },
});
