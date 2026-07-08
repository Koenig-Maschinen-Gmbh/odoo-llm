/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { Component, onWillDestroy, onWillStart, useState } from "@odoo/owl";
import { LLMChatContainer } from "@llm_thread/components/llm_chat_container/llm_chat_container";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useSetupAction } from "@web/search/action_hook";

/**
 * LLM Chat Client Action - Main entry point for LLM chat functionality
 * Follows Odoo 18.0 client action pattern similar to DiscussClientAction
 */
export class LLMChatClientAction extends Component {
  static components = { LLMChatContainer };
  static props = ["*"];
  static template = "llm_thread.LLMChatClientAction";

  setup() {
    this.llmStore = useState(useService("llm.store"));
    this.mailStore = useState(useService("mail.store"));
    this.orm = useService("orm");
    this.notification = useService("notification");

    // P-CHAT link-navigation: save the active thread id when the user navigates
    // away (e.g. clicks a record link → doAction pushes the form onto the
    // action stack). On breadcrumb-back, the action service feeds the saved
    // state back as props.state → we re-select the same thread. Mirrors the
    // OCB pattern used by every controller (form/list/kanban/…) + the König
    // wiki client action (wiki_client_action.js useSetupAction).
    useSetupAction({
      getLocalState: () => this._getLocalState(),
    });

    onWillStart(() => {
      return this.initializeLLMChat(this.props);
    });

    onWillDestroy(() => {
      this.cleanup();
    });
  }

  /**
   * Snapshot the active thread id for breadcrumb-back restoration.
   * Called by the action service via useSetupAction when this controller
   * is about to be left for another action.
   */
  _getLocalState() {
    return { activeThreadId: this._activeThreadId() };
  }

  /**
   * The active llm.thread id (or false). The active thread lives on
   * mail.store.discuss.thread when it's an llm.thread.
   */
  _activeThreadId() {
    const thread = this.mailStore.discuss?.thread;
    return thread?.model === "llm.thread" ? thread.id : false;
  }

  /**
   * Initialize LLM chat based on action context
   * Similar to how DiscussClientAction handles thread restoration
   * @param {Object} props - Component props
   */
  async initializeLLMChat(props) {
    try {
      // Wait for both mailStore and llmStore to be ready
      // mailStore.isReady ensures threads are loaded via init_messaging
      // llmStore.isReady ensures providers, models, tools are loaded
      await Promise.all([this.mailStore.isReady, this.llmStore.isReady]);

      // P-CHAT link-navigation: breadcrumb-restore. When the user navigates
      // back to this controller via the breadcrumb (after opening a record
      // from an AI answer), the action service feeds the saved state back as
      // props.state. Re-select the same thread instead of loading the default.
      const restoredThreadId = props.state?.activeThreadId;
      if (restoredThreadId) {
        await this.selectLLMThread(restoredThreadId);
        return;
      }

      const activeId = this.getActiveId(props);

      if (!activeId) {
        // No specific context, load user's recent threads
        await this.loadUserThreads();
        return;
      }

      if (activeId.startsWith("llm.thread_")) {
        await this.handleThreadSelection(activeId);
      } else {
        // Open form to create new LLM thread for the referenced record
        await this.openCreateThreadForm(props);
      }
    } catch (error) {
      console.error("Error initializing LLM chat:", error);
      this.notification.add(
        _t("Could not start AI chat. Please refresh the page and try again."),
        { type: "danger" }
      );
    }
  }

  /**
   * Get active ID from action context, similar to DiscussClientAction
   * @param {Object} props - Component props
   * @returns {String|null} Active ID or null
   */
  getActiveId(props) {
    return (
      props.action.context?.active_id ??
      props.action.params?.active_id ??
      props.action.context?.default_active_id
    );
  }

  /**
   * Handle thread selection from activeId
   * @param {String} activeId - Active ID string in format "llm.thread_123"
   */
  async handleThreadSelection(activeId) {
    const threadId = parseInt(activeId.split("_")[1], 10);
    const existingThread = this.mailStore.Thread.get({
      model: "llm.thread",
      id: threadId,
    });

    if (!existingThread) {
      await this.loadUserThreads();
      const threadAfterLoad = this.mailStore.Thread.get({
        model: "llm.thread",
        id: threadId,
      });
      if (!threadAfterLoad) {
        this.notification.add(
          _t(
            "The requested conversation could not be found. Showing your recent conversations instead."
          ),
          { type: "warning" }
        );
        return;
      }
    }

    await this.selectLLMThread(threadId);
  }

  /**
   * Select an existing LLM thread - delegates to service
   * @param {Number} threadId - Thread ID to select
   */
  async selectLLMThread(threadId) {
    // Use the consolidated service method
    await this.llmStore.selectThread(threadId);
  }

  /**
   * Open llm.thread form to create new thread for a specific record
   * @param {Object} props - Component props
   */
  async openCreateThreadForm(props) {
    try {
      const context = props.action.context || {};
      const resModel = context.default_res_model;
      const resId = context.default_res_id;

      await this.action.doAction({
        name: "Create AI Chat",
        type: "ir.actions.act_window",
        res_model: "llm.thread",
        view_mode: "form",
        views: [[false, "form"]],
        target: "new",
        context: {
          // No default_name - backend will generate it from record.display_name
          default_model: resModel,
          default_res_id: resId,
        },
      });
    } catch (error) {
      console.error("Error opening create thread form:", error);
      this.notification.add(
        _t("Could not open the new conversation form. Please try again."),
        {
          type: "danger",
        }
      );
    }
  }

  /**
   * Load user's existing LLM threads
   */
  async loadUserThreads() {
    try {
      // Threads are automatically loaded via init_messaging
      // Just get the most recent one from mailStore
      const threads = this.llmStore.llmThreadList;

      if (threads.length > 0) {
        await this.selectLLMThread(threads[0].id);
      }
      // No auto-creation - let user create threads via form
    } catch (error) {
      console.error("Error loading user threads:", error);
      this.notification.add(
        _t(
          "Could not load your conversations. Please refresh the page and try again."
        ),
        { type: "danger" }
      );
    }
  }

  /**
   * Cleanup when component is destroyed
   */
  cleanup() {
    // Stop any streaming
    this.llmStore.destroy();
  }
}

// Register client action
registry
  .category("actions")
  .add("llm_thread.chat_client_action", LLMChatClientAction);
