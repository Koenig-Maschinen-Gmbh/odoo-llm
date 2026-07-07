/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";
import { Composer } from "@mail/core/common/composer";
import { LLMThreadHeader } from "../llm_thread_header/llm_thread_header";
import { Thread } from "@mail/core/common/thread";
import { useService } from "@web/core/utils/hooks";

/**
 * LLM Chat Container - Main container for LLM chat UI
 * Uses existing mail Thread and Composer components with LLM patches
 */
export class LLMChatContainer extends Component {
  static components = { Thread, Composer, LLMThreadHeader };
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

    // Sidebar state
    this.state = useState({
      // Desktop: collapse/expand state (default collapsed in chatter mode)
      isSidebarCollapsed: Boolean(
        this.props.recordModel && this.props.recordId
      ),
      // Mobile: slide-in modal visibility
      isMobileSidebarVisible: false,
      // P-CHAT M3: 1s tick that drives the sidebar elapsed-mm:ss counter
      // and the done/failed ✓/! flash. Bumped only while at least one
      // thread has an active or recently-finished run (see the interval
      // gate), so idle chats don't re-render every second.
      elapsedTick: 0,
    });

    // No need for local thread tracking - use mail.store.discuss.thread

    // P-CHAT M3 — elapsed timer for the working-indicator. The interval
    // runs while the container is mounted; the callback only bumps the
    // reactive tick when there is something time-sensitive to show.
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
    });
  }

  /**
   * P-CHAT M3 — true if any tracked thread is running or freshly finished
   * (within the flash window). Used to gate the 1s tick so idle chats
   * don't re-render.
   */
  _hasTimeSensitiveRunState() {
    const states = Object.values(this.llmStore.threadRunState || {});
    const now = Date.now();
    return states.some(
      (s) =>
        s.state === "running" ||
        (s.finishedAt && now - s.finishedAt < 3000)
    );
  }

  /**
   * P-CHAT M3 — run state for a thread (or null).
   */
  threadRunState(threadId) {
    return this.llmStore.getThreadRunState(threadId);
  }

  /**
   * P-CHAT M3 — elapsed "mm:ss" since the run started, or "" when not
   * running. Reads `elapsedTick` so the getter is reactive on the tick.
   */
  threadElapsedLabel(threadId) {
    // touch the tick so OWL re-evaluates each second
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

  /**
   * P-CHAT M3 — "done" / "failed" / "cancelled" for ~3s after the run
   * terminates (the ✓/! flash), then null. Reactive on the tick.
   */
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
   * Get filtered thread list based on context
   * - In chatter mode (recordModel + recordId provided): show only threads for current record
   * - In standalone mode: show all user's threads
   * @returns {Array} Filtered thread list
   */
  get filteredThreadList() {
    const allThreads = this.llmStore.llmThreadList;

    // If in chatter mode (record context provided), filter by record
    if (this.props.recordModel && this.props.recordId) {
      return allThreads.filter(
        (thread) =>
          thread.res_model === this.props.recordModel &&
          thread.res_id === this.props.recordId
      );
    }

    // Standalone mode - show all threads
    return allThreads;
  }

  /**
   * Select thread - delegates to LLM store service
   * On mobile, closes the sidebar after selection
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
   * Check if a thread is currently streaming
   * @param {Number} threadId - Thread ID to check
   * @returns {Boolean} True if thread is streaming
   */
  isStreamingThread(threadId) {
    return this.llmStore.isStreamingThread(threadId);
  }

  /**
   * Format date for display
   * @param {String} dateString - Date string to format
   * @returns {String} Formatted date string
   */
  formatDate(dateString) {
    if (!dateString) return "";
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now - date;
    const diffHours = Math.floor(diffMs / (1000 * 60 * 60));
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffHours < 1) {
      return _t("Just now");
    } else if (diffHours < 24) {
      return _t("%sh ago", diffHours);
    } else if (diffDays < 7) {
      return _t("%sd ago", diffDays);
    }
    return date.toLocaleDateString();
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
}

// Accept any props (like updateActionState)
LLMChatContainer.props = {
  "*": true,
};
