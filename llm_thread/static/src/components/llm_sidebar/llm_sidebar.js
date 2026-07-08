/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import {
  Component,
  onMounted,
  onWillUnmount,
  status,
  useState,
} from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { debounce } from "@web/core/utils/timing";
import { cleanTerm } from "@mail/utils/common/format";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { LLMBulkTagDialog } from "../llm_bulk_tag_dialog/llm_bulk_tag_dialog";
import { llmDateBucket, LLM_DATE_BUCKETS } from "../../utils/llm_date_bucket";

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

    this.state = useState({
      searchVal: "",
      showArchived: false,
      collapsedBuckets: {},
      selectMode: false,
      selectedThreadIds: {}, // {id: true} — plain object for reactivity
      serverMatchedIds: {}, // {id: true} from the debounced server search
      elapsedTick: 0, // P-CHAT M3: drives the sidebar mm:ss + done/failed flash
    });

    // Debounced server-side content search (message body). Client-side
    // name filtering is instant (see ``filteredThreadList``); this merges
    // content-matched threads into the store so they appear even when the
    // name doesn't contain the term.
    this._debouncedServerSearch = debounce(this._serverSearch.bind(this), 300);

    // P-CHAT M3 — gated 1s tick (only bumps while a thread is running or
    // freshly finished, so idle sidebars don't re-render every second).
    this._elapsedTimer = null;
    onMounted(() => {
      this._elapsedTimer = setInterval(() => {
        if (this._hasTimeSensitiveRunState()) {
          this.state.elapsedTick++;
        }
      }, 1000);
    });
    onWillUnmount(() => {
      if (this._elapsedTimer) {
        clearInterval(this._elapsedTimer);
        this._elapsedTimer = null;
      }
      this._debouncedServerSearch.cancel?.();
    });
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
        (t) =>
          t.res_model === this.props.recordModel &&
          t.res_id === this.props.recordId
      );
    }
    // Archive filter: hide archived unless "Show archived" is on.
    if (!this.state.showArchived) {
      threads = threads.filter((t) => t.active !== false);
    }
    // Search: instant client-side name match OR a server content match.
    const term = cleanTerm(this.state.searchVal);
    if (term) {
      const serverMatches = this.state.serverMatchedIds;
      threads = threads.filter(
        (t) =>
          cleanTerm(t.name || "").includes(term) || Boolean(serverMatches[t.id])
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
      const results = await this.orm.call("llm.thread", "search_threads", [
        term,
      ]);
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
    this.dialog.add(ConfirmationDialog, {
      body: _t(
        "Permanently delete %s threads? This cannot be undone.",
        String(ids.length)
      ),
      confirmLabel: _t("Delete"),
      confirmClass: "btn-danger",
      confirm: async () => {
        await this.llmStore.bulkDelete(ids);
        this.state.selectedThreadIds = {};
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
      onConfirm: async (tagIds) => {
        await this.llmStore.bulkTag(ids, tagIds);
        this.state.selectedThreadIds = {};
      },
    });
  }

  // ------------------------------------------------------------------
  // P-CHAT M3 — run-state indicators (moved from LLMChatContainer)
  // ------------------------------------------------------------------
  _hasTimeSensitiveRunState() {
    const states = Object.values(this.llmStore.threadRunState || {});
    const now = Date.now();
    return states.some(
      (s) =>
        s.state === "running" || (s.finishedAt && now - s.finishedAt < 3000)
    );
  }

  threadRunState(threadId) {
    return this.llmStore.getThreadRunState(threadId);
  }

  threadElapsedLabel(threadId) {
    // touch the tick so OWL re-evaluates each second while a run is active
    void this.state.elapsedTick;
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
    if (Date.now() - st.finishedAt > 3000) {
      return null;
    }
    return st.state; // "done" | "failed" | "cancelled"
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
    const dt =
      dateString instanceof DateTime
        ? dateString
        : deserializeDateTime(dateString);
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
    return dt.toLocaleString();
  }
}
