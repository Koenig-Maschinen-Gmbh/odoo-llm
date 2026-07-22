/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { Component, onMounted, onWillUnmount, status, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { debounce } from "@web/core/utils/timing";
import { cleanTerm } from "@mail/utils/common/format";
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { browser } from "@web/core/browser/browser";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { LLMBulkTagDialog } from "../llm_bulk_tag_dialog/llm_bulk_tag_dialog";
import { LLM_DATE_BUCKETS, llmDateBucket } from "../../utils/llm_date_bucket";

const BUCKET_COLLAPSE_KEY = "llm_thread.collapsed_buckets";

const { DateTime } = luxon;

const BUCKET_LABELS = {
    today: _t("Today"),
    yesterday: _t("Yesterday"),
    this_week: _t("This Week"),
    older: _t("Older"),
};

/**
 * LLMSidebar — thread & memory management UX (P-UX).
 *
 * Extracted from ``LLMChatContainer`` (Step 17) so grouping / search / tags /
 * archive / bulk actions live in ONE component instead of the previous
 * duplicated mobile + desktop sidebar markup. The container delegates with
 * props; CSS / the container's wrapper handle mobile slide-in vs desktop
 * collapse.
 *
 * Owns: date-bucket grouping, client-side name search + debounced
 * server-side content search (``llm.thread.search_threads``), tag badges,
 * per-item archive/unarchive + delete, "Show archived" toggle, multi-select
 * bulk archive/delete/tag, the P-CHAT M3 run-state indicators (moved here
 * from the container — the sidebar is where thread items live).
 */
export class LLMSidebar extends Component {
    static template = "llm_thread.LLMSidebar";
    static components = { LLMBulkTagDialog };
    static props = {
        recordModel: { type: String, optional: true },
        recordId: { type: Number, optional: true },
        isCollapsed: { type: Boolean, optional: true },
        isMobile: { type: Boolean, optional: true },
        onSelectThread: { type: Function, optional: true },
        onCreateThread: { type: Function, optional: true },
        onToggleSidebar: { type: Function, optional: true },
    };

    setup() {
        this.llmStore = useState(useService("llm.store"));
        this.mailStore = useState(useService("mail.store"));
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.bucketLabels = BUCKET_LABELS;

        // Read persisted collapsed-bucket state so the user's section
        // preferences survive page refreshes.
        let persistedBuckets = {};
        try {
            const raw = browser.localStorage.getItem(BUCKET_COLLAPSE_KEY);
            if (raw) {
                persistedBuckets = JSON.parse(raw);
            }
        } catch {
            // Non-fatal — start with all buckets expanded.
        }

        this.state = useState({
            searchVal: "",
            showArchived: false,
            // UI-11: expert sub-threads are hidden by default; the toggle
            // reveals them for debugging (mirrors the archive toggle).
            showExpertThreads: false,
            collapsedBuckets: persistedBuckets,
            selectMode: false,
            selectedThreadIds: {}, // {id: true} — plain object for reactivity
            serverMatchedIds: {}, // {id: true} from the debounced server search
            elapsedTick: 0, // P-CHAT M3: drives the sidebar mm:ss + done/failed flash
            _serverSearchFailed: false, // BL-7: one-time notification flag for server search failures
        });

        // Debounced server-side content search (message body). Client-side
        // name filtering is instant (see ``filteredThreadList``); this merges
        // content-matched threads into the store so they appear even when the
        // name doesn't contain the term.
        this._debouncedServerSearch = debounce(this._serverSearch.bind(this), 300);

        // P-UX review §2.5: on-demand tick — starts only when a run is active,
        // stops when idle. Avoids a permanent 1s interval on every sidebar.
        this._elapsedTimer = null;
        onMounted(() => {
            this._maybeStartTick();
        });
        onWillUnmount(() => {
            this._maybeStopTick();
            this._debouncedServerSearch.cancel?.();
        });
    }

    // ------------------------------------------------------------------
    // P-CHAT M3 — on-demand elapsed timer (P-UX review §2.5)
    // ------------------------------------------------------------------
    _maybeStartTick() {
        if (this._elapsedTimer) {
            return;
        }
        this._elapsedTimer = setInterval(() => {
            if (this._hasTimeSensitiveRunState()) {
                this.state.elapsedTick++;
            } else {
                // Final re-render to remove stale flash icons before
                // stopping the tick. Without this, the last render (while
                // the flash was still active) leaves a stale icon in the
                // DOM — the tick stops without triggering the re-render
                // that would evaluate threadFinishedFlash → null.
                this.state.elapsedTick++;
                this._maybeStopTick();
            }
        }, 1000);
    }

    _maybeStopTick() {
        if (this._elapsedTimer) {
            clearInterval(this._elapsedTimer);
            this._elapsedTimer = null;
        }
    }

    // ------------------------------------------------------------------
    // Active thread (highlight)
    // ------------------------------------------------------------------
    get activeThread() {
        return this.mailStore.discuss?.thread;
    }

    // ------------------------------------------------------------------
    // Filtered + grouped thread list
    // ------------------------------------------------------------------
    get filteredThreadList() {
        let threads = this.llmStore.llmThreadList;
        // Chatter mode: only threads linked to this record.
        if (this.props.recordModel && this.props.recordId) {
            threads = threads.filter(
                (t) => t.res_model === this.props.recordModel && t.res_id === this.props.recordId
            );
        }
        // Archive filter: hide archived unless "Show archived" is on.
        if (!this.state.showArchived) {
            threads = threads.filter((t) => t.active !== false);
        }
        // UI-11: hide expert sub-threads unless "Show expert threads" is on.
        // The flag is set at creation time, so sub-threads are hidden even
        // while the expert is still running (before active=False archiving).
        if (!this.state.showExpertThreads) {
            threads = threads.filter((t) => !t.is_expert_subthread);
        }
        // Search: instant client-side name match OR a server content match.
        const term = cleanTerm(this.state.searchVal);
        if (term) {
            const serverMatches = this.state.serverMatchedIds;
            threads = threads.filter(
                (t) => cleanTerm(t.name || "").includes(term) || Boolean(serverMatches[t.id])
            );
        }
        return threads;
    }

    get groupedThreadList() {
        const buckets = { today: [], yesterday: [], this_week: [], older: [] };
        for (const thread of this.filteredThreadList) {
            buckets[llmDateBucket(thread.write_date)].push(thread);
        }
        // Preserve fixed bucket order; drop empty buckets.
        return LLM_DATE_BUCKETS.filter((key) => buckets[key].length).map((key) => ({
            key,
            label: this.bucketLabels[key],
            threads: buckets[key],
        }));
    }

    isBucketCollapsed(key) {
        return Boolean(this.state.collapsedBuckets[key]);
    }

    toggleBucket(key) {
        this.state.collapsedBuckets[key] = !this.state.collapsedBuckets[key];
        // Persist the collapsed state so it survives page refreshes.
        try {
            browser.localStorage.setItem(
                BUCKET_COLLAPSE_KEY,
                JSON.stringify(this.state.collapsedBuckets)
            );
        } catch {
            // Quota exceeded / disabled localStorage — non-fatal.
        }
    }

    // ------------------------------------------------------------------
    // Search
    // ------------------------------------------------------------------
    onSearchInput(ev) {
        this.state.searchVal = ev.target.value;
        this.state.serverMatchedIds = {};
        const term = this.state.searchVal.trim();
        if (term.length >= 2) {
            this._debouncedServerSearch(term);
        }
    }

    clearSearch() {
        this.state.searchVal = "";
        this.state.serverMatchedIds = {};
    }

    async _serverSearch(term) {
        // OWL lifecycle guard: don't touch state after destroy.
        if (status(this) === "destroyed") {
            return;
        }
        try {
            const results = await this.orm.call("llm.thread", "search_threads", [term]);
            if (status(this) === "destroyed") {
                return;
            }
            const matched = {};
            for (const r of results) {
                matched[r.id] = true;
            }
            // Merge results into the mail store so they appear in
            // ``llmThreadList`` (content-matched threads may not be loaded yet).
            this.mailStore.insert({ "mail.thread": results });
            this.state.serverMatchedIds = matched;
        } catch (e) {
            // Fail soft — instant client-side name search still works.
            console.warn("[LLMSidebar] server search failed", e);
            if (!this.state._serverSearchFailed) {
                this.state._serverSearchFailed = true;
                this.notification.add(_t("Search encountered an error. Showing basic results."), {
                    type: "warning",
                    sticky: false,
                });
            }
        }
    }

    // ------------------------------------------------------------------
    // Selection + thread actions
    // ------------------------------------------------------------------
    async selectThread(threadId) {
        if (this.state.selectMode) {
            this.toggleSelectThread(threadId);
            return;
        }
        await this.props.onSelectThread?.(threadId);
    }

    createNewThread() {
        this.props.onCreateThread?.();
    }

    toggleSidebar() {
        this.props.onToggleSidebar?.();
    }

    // ------------------------------------------------------------------
    // Archive / unarchive / delete (per item)
    // ------------------------------------------------------------------
    archiveThread(thread) {
        this.dialog.add(ConfirmationDialog, {
            body: _t("Are you sure you want to archive this thread?"),
            confirmLabel: _t("Archive"),
            confirm: () => this.llmStore.archiveThread(thread.id),
        });
    }

    async unarchiveThread(thread) {
        await this.llmStore.unarchiveThread(thread.id);
    }

    deleteThread(thread) {
        this.dialog.add(ConfirmationDialog, {
            body: _t("Permanently delete this thread? This cannot be undone."),
            confirmLabel: _t("Delete"),
            confirmClass: "btn-danger",
            confirm: () => this.llmStore.deleteThread(thread.id),
        });
    }

    // ------------------------------------------------------------------
    // Bulk actions (select mode)
    // ------------------------------------------------------------------
    toggleSelectMode() {
        this.state.selectMode = !this.state.selectMode;
        this.state.selectedThreadIds = {};
    }

    toggleSelectThread(threadId) {
        if (this.state.selectedThreadIds[threadId]) {
            const next = { ...this.state.selectedThreadIds };
            delete next[threadId];
            this.state.selectedThreadIds = next;
        } else {
            this.state.selectedThreadIds = {
                ...this.state.selectedThreadIds,
                [threadId]: true,
            };
        }
    }

    get selectedIds() {
        return Object.keys(this.state.selectedThreadIds).map(Number);
    }

    get selectedCount() {
        return this.selectedIds.length;
    }

    selectAllVisible() {
        const next = {};
        for (const t of this.filteredThreadList) {
            next[t.id] = true;
        }
        this.state.selectedThreadIds = next;
    }

    bulkArchive() {
        const ids = this.selectedIds;
        if (!ids.length) {
            return;
        }
        this.dialog.add(ConfirmationDialog, {
            body: _t("Archive %s threads?", String(ids.length)),
            confirmLabel: _t("Archive"),
            confirm: async () => {
                await this.llmStore.bulkArchive(ids);
                this.state.selectedThreadIds = {};
            },
        });
    }

    bulkDelete() {
        const ids = this.selectedIds;
        if (!ids.length) {
            return;
        }
        // P-UX review §1.5: for >5 threads, require a second confirmation to
        // prevent accidental mass deletion (unlink is permanent, no undo).
        const isMassDelete = ids.length > 5;
        const body = isMassDelete
            ? _t(
                  "You are about to permanently delete %s threads and all their messages. This CANNOT be undone. Are you absolutely sure?",
                  String(ids.length)
              )
            : _t("Permanently delete %s threads? This cannot be undone.", String(ids.length));
        this.dialog.add(ConfirmationDialog, {
            body,
            confirmLabel: _t("Delete"),
            confirmClass: "btn-danger",
            confirm: () => {
                if (isMassDelete) {
                    // Second confirmation for mass deletes.
                    this.dialog.add(ConfirmationDialog, {
                        body: _t(
                            "Last chance: %s threads will be permanently deleted. Click Delete again to proceed.",
                            String(ids.length)
                        ),
                        confirmLabel: _t("Delete"),
                        confirmClass: "btn-danger",
                        confirm: async () => {
                            await this.llmStore.bulkDelete(ids);
                            this.state.selectedThreadIds = {};
                        },
                    });
                } else {
                    this.llmStore.bulkDelete(ids).then(() => {
                        this.state.selectedThreadIds = {};
                    });
                }
            },
        });
    }

    bulkTag() {
        const ids = this.selectedIds;
        if (!ids.length) {
            return;
        }
        this.dialog.add(LLMBulkTagDialog, {
            threadIds: ids,
            onConfirm: async (tagIds, mode) => {
                await this.llmStore.bulkTag(ids, tagIds, mode);
                this.state.selectedThreadIds = {};
            },
        });
    }

    // ------------------------------------------------------------------
    // P-CHAT M3 — run-state indicators (moved from LLMChatContainer)
    // ------------------------------------------------------------------
    _hasTimeSensitiveRunState() {
        let has = false;
        // The tick is only needed for RUNNING threads (to update the
        // elapsed mm:ss counter every second). Terminal states are now
        // persistent (threadFinishedFlash no longer expires), so they
        // don't need the tick — they render once and stay until a new
        // run starts or the page is refreshed.
        for (const s of Object.values(this.llmStore.threadRunState || {})) {
            if (s.state === "running") {
                has = true;
                break;
            }
        }
        if (has) {
            this._maybeStartTick();
        }
        return has;
    }

    threadRunState(threadId) {
        return this.llmStore.getThreadRunState(threadId);
    }

    threadElapsedLabel(threadId) {
        // Touch the tick so OWL re-evaluates each second while a run is active
        void this.state.elapsedTick;
        // Ensure the tick is running if any thread is active — the tick
        // may have stopped after all threads reached terminal state, and
        // a new bus event (run_started) won't restart it without this
        // call (the tick only self-restarts from inside its own callback).
        this._hasTimeSensitiveRunState();
        const st = this.threadRunState(threadId);
        if (!st || st.state !== "running" || !st.startedAt) {
            return "";
        }
        const ms = Date.now() - st.startedAt;
        const totalSec = Math.max(0, Math.floor(ms / 1000));
        const mm = String(Math.floor(totalSec / 60)).padStart(2, "0");
        const ss = String(totalSec % 60).padStart(2, "0");
        return `${mm}:${ss}`;
    }

    threadFinishedFlash(threadId) {
        void this.state.elapsedTick;
        const st = this.threadRunState(threadId);
        if (!st || !st.finishedAt) {
            return null;
        }
        // Terminal states persist until a new run starts (which clears
        // finishedAt via setThreadRunState). The previous 3-second flash
        // window caused indicators to vanish after clicking a thread or
        // after a page refresh (the poll re-sets finishedAt to the poll
        // time, not the actual completion time, so the flash expired
        // almost immediately).
        return st.state; // "done" | "failed" | "cancelled" | "killed" | "timed_out"
    }

    isStreamingThread(threadId) {
        return this.llmStore.isStreamingThread(threadId);
    }

    // ------------------------------------------------------------------
    // Formatting
    // ------------------------------------------------------------------
    formatDate(dateString) {
        if (!dateString) {
            return "";
        }
        const dt = dateString instanceof DateTime ? dateString : deserializeDateTime(dateString);
        if (!dt || !dt.isValid) {
            return "";
        }
        const now = DateTime.local();
        const diffMin = now.diff(dt, "minutes").minutes;
        if (diffMin < 60) {
            return _t("Just now");
        }
        const diffH = now.diff(dt, "hours").hours;
        if (diffH < 24) {
            return _t("%sh ago", Math.floor(diffH));
        }
        const diffD = now.diff(dt, "days").days;
        if (diffD < 7) {
            return _t("%sd ago", Math.floor(diffD));
        }
        return formatDateTime(dt);
    }

    /**
     * UI-08 B1 — build a rich tooltip for a sidebar thread item.
     *
     * Replaces the old persistent per-item relative-date label (which was
     * redundant noise — the date-bucket headers already carry the date
     * context). The tooltip shows the thread name, created date, and last
     * activity date as full datetimes — zero visual noise, full info on hover.
     */
    threadTooltip(thread) {
        if (!thread) {
            return "";
        }
        const parts = [thread.name || ""];
        if (thread.create_date) {
            const created = formatDateTime(deserializeDateTime(thread.create_date));
            if (created) {
                parts.push(_t("Created: %s", created));
            }
        }
        if (thread.write_date) {
            const lastActivity = formatDateTime(deserializeDateTime(thread.write_date));
            if (lastActivity) {
                parts.push(_t("Last activity: %s", lastActivity));
            }
        }
        return parts.join("\n");
    }
}
