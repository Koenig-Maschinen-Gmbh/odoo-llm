/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { LLMToolMessage } from "../components/llm_tool_message/llm_tool_message";
import { Message } from "@mail/core/common/message";
import { Message as MessageModel } from "@mail/core/common/message_model";
import { MessageActionMenuMobile } from "@mail/core/common/message_action_menu_mobile";
import { Record } from "@mail/core/common/record";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { status, useState } from "@odoo/owl";
import {
  isLLMStepMessage as _isStepMessage,
  isLLMErrorMessage as _isErrorMessage,
  isLLMFinalAnswer as _isFinalAnswer,
  isLLMFirstStepOfTurn as _isFirstStepOfTurn,
  llmStepCountInTurn as _stepCountInTurn,
  llmTurnIdForMessage as _turnIdFor,
} from "../utils/llm_message_classify";

/**
 * PATCH 1: Message Component Static Properties
 * Adds LLMToolMessage to the available components registry
 */
patch(Message, {
  components: { ...Message.components, LLMToolMessage },
});

/**
 * PATCH 2: Message Component Prototype Methods
 * Adds UI rendering logic and getters for LLM message detection and styling
 * These methods are used by the component template and rendering logic
 */
patch(Message.prototype, {
  setup() {
    super.setup();
    // In an LLM (AI) conversation, the standard chatter message-action toolbar
    // (Add a Reaction, Mark as Todo/star, Reply, Edit, Delete, Copy Link,
    // Translate, the overflow "Expand" menu, …) is meaningless and only adds
    // noise. The mail Message component populates `this.messageActions` from
    // the global `mail.message/actions` registry in its own setup; here we
    // replace it with an empty, read-only action set for llm.thread messages
    // so no chatter affordances render. Regular mail/discuss messages are
    // untouched. The getter reads nothing reactive, so it is render-safe.
    if (this.props.message?.model === "llm.thread") {
      this.messageActions = {
        get actions() {
          return [];
        },
      };
    }
    // P-CHAT M1: copy-button "Copied" flash state. Hooks must be called
    // unconditionally, so this is created for every message (cheap); only
    // the assistant copy button reads it.
    this.llmCopyState = useState({ copied: false });

    // P-CHAT M2: llm store for the per-turn steps-drawer collapse state
    // and (M3) the run-feedback state. Wrapped in useState so reads of
    // stepDrawerOpen / threadRunState in this component's getters are
    // reactive — without useState the drawer toggle and run-state-driven
    // classes would never re-render (matches composer_patch.js +
    // llm_thread_header.js).
    try {
      this.llmStore = useState(useService("llm.store"));
    } catch (error) {
      this.llmStore = null;
    }
  },

  /**
   * Copy the assistant answer to the clipboard (P-CHAT M1).
   *
   * Prefers the markdown source when ``body_json.markdown`` is present
   * (future-proofing for paths that store the raw markdown); otherwise
   * strips the stored rendered HTML down to plain text. The clipboard API
   * requires a secure context — on failure the call is a no-op (no error
   * surfaced to the user for a copy button).
   */
  async copyBody() {
    const msg = this.props.message;
    if (!msg) {
      return;
    }
    let text = "";
    if (msg.body_json && msg.body_json.markdown) {
      text = msg.body_json.markdown;
    } else if (msg.body) {
      const tmp = document.createElement("div");
      tmp.innerHTML = msg.body;
      text = tmp.textContent || tmp.innerText || "";
    }
    try {
      await navigator.clipboard.writeText(text);
      if (status(this) === "destroyed") {
        return;
      }
      this.llmCopyState.copied = true;
      setTimeout(() => {
        if (status(this) === "destroyed") {
          return;
        }
        this.llmCopyState.copied = false;
      }, 1500);
    } catch {
      // clipboard API unavailable (non-secure context) — no-op.
    }
  },

  /**
   * Label for the assistant copy button — flips to "Copied" briefly.
   */
  get copiedLabel() {
    return this.llmCopyState.copied ? _t("Copied") : _t("Copy");
  },

  /**
   * Check if this message is in an LLM thread
   */
  get isLLMMessage() {
    return this.props.message?.model === "llm.thread";
  },

  /**
   * Get LLM role for this message
   */
  get llmRole() {
    return this.props.message?.llm_role;
  },

  /**
   * Check if message is a tool message
   */
  get isToolMessage() {
    return this.isLLMMessage && this.llmRole === "tool";
  },

  /**
   * Check if assistant message has tool calls
   */
  get hasToolCalls() {
    return (
      this.isLLMMessage &&
      this.llmRole === "assistant" &&
      this.props.message?.body_json?.tool_calls?.length > 0
    );
  },

  // ------------------------------------------------------------------------
  // P-CHAT M2 — foreground/background message hierarchy.
  // Classification + turn grouping delegate to pure helpers in
  // utils/llm_message_classify.js so they are unit-testable without mounting.
  // ------------------------------------------------------------------------

  /**
   * The thread's messages in ascending display order. The mail Thread
   * component renders `orderedMessages`; for llm.thread (created in id order,
   * displayed asc) sorting by id matches the rendered order.
   */
  get llmOrderedMessages() {
    const msgs = this.props.thread?.messages;
    if (!msgs) {
      return [];
    }
    return [...msgs].sort((a, b) => (a.id || 0) - (b.id || 0));
  },

  /** A failed-run error message (stays prominent, OUTSIDE the steps drawer). */
  get isLLMError() {
    return this.isLLMMessage && _isErrorMessage(this.props.message);
  },

  /** A "step" = tool message OR intermediate assistant (collapsed in drawer). */
  get isLLMStep() {
    return this.isLLMMessage && _isStepMessage(this.props.message);
  },

  /** The final answer = assistant message without tool calls (renders expanded). */
  get isLLMFinalAnswer() {
    return this.isLLMMessage && _isFinalAnswer(this.props.message);
  },

  /** Turn id = the user message id that opened this message's turn. */
  get llmTurnId() {
    if (!this.isLLMMessage) {
      return null;
    }
    return _turnIdFor(this.props.message, this.llmOrderedMessages);
  },

  /** Number of step messages in this message's turn (for the drawer label). */
  get llmStepCount() {
    return _stepCountInTurn(this.props.message, this.llmOrderedMessages);
  },

  /** True for the first step message of a turn — renders the drawer toggle. */
  get isFirstStepOfTurn() {
    return this.isLLMStep && _isFirstStepOfTurn(this.props.message, this.llmOrderedMessages);
  },

  /** Whether this turn's steps drawer is expanded (reactive on llmStore). */
  get isStepDrawerOpen() {
    const turnId = this.llmTurnId;
    if (turnId == null || !this.llmStore) {
      return false;
    }
    return this.llmStore.isStepDrawerOpen(turnId);
  },

  /** Toggle the steps drawer for this message's turn. */
  toggleStepDrawer() {
    const turnId = this.llmTurnId;
    if (turnId != null && this.llmStore) {
      this.llmStore.toggleStepDrawer(turnId);
    }
  },

  /** Label for the per-turn steps drawer ("Work steps" — DE via i18n). */
  get llmStepsLabel() {
    return _t("Work steps");
  },

  /**
   * A short one-line summary for a collapsed step (tool name / dispatched
   * expert), shown when the drawer is closed.
   */
  get llmStepSummary() {
    const msg = this.props.message;
    if (!msg) {
      return "";
    }
    if (this.isToolMessage) {
      const name = msg.body_json?.tool_name || msg.body_json?.name || _t("Tool");
      const status = msg.body_json?.status;
      const icon = status === "error" ? "⚠ " : status === "completed" ? "✓ " : "";
      return `${icon}${name}`;
    }
    if (this.hasToolCalls) {
      const names = (msg.body_json?.tool_calls || [])
        .map((tc) => tc?.function?.name || _t("tool"))
        .join(", ");
      return _t("Calling: %s", names);
    }
    return "";
  },

  /**
   * Add LLM-specific CSS classes
   */
  get className() {
    let className = super.className || "";

    if (this.isLLMMessage) {
      className += " o-llm-message";

      if (this.llmRole) {
        className += ` o-llm-message-${this.llmRole}`;
      }

      // Add streaming class for assistant messages that are still being generated
      if (this.llmRole === "assistant" && this.props.message?.isPending) {
        className += " o-llm-message-streaming";
      }

      // P-CHAT M2 — hierarchy classes.
      if (this.isLLMError) {
        className += " o-llm-message-error";
      }
      if (this.isLLMStep) {
        className += " o-llm-step";
        className += this.isStepDrawerOpen
          ? " o-llm-step-open"
          : " o-llm-step-collapsed";
      }
      if (this.isLLMFinalAnswer) {
        className += " o-llm-final-answer";
      }
    }

    return className;
  },

  /**
   * P-CHAT M2: merge this component's ``className`` getter into the root
   * class set. The base ``attClass`` only forwards ``props.className``
   * (from the Thread's ``getMessageClass``); it does NOT include this
   * component's ``className`` getter, so without this override the
   * hierarchy classes added there (``o-llm-step``, ``o-llm-step-collapsed``
   * / ``-open``, ``o-llm-message-error``, ``o-llm-final-answer``) would
   * never reach the DOM. Non-LLM messages: ``className`` returns ``""``
   * → base ``attClass`` returned unchanged.
   */
  get attClass() {
    const base = super.attClass;
    const extra = this.className;
    if (!extra) {
      return base;
    }
    return { ...base, [extra]: true };
  },
});

/**
 * PATCH 3: Message Model (Data Layer)
 * Patches the Message data model to handle LLM-specific isEmpty computation
 * This ensures LLM messages with tool calls or body_json are never filtered out
 * NOTE: This is NOT the component - this is the data model that holds message data
 */
patch(MessageModel.prototype, {
  setup() {
    super.setup(...arguments);
    // P-CHAT M2: register is_error as a Mail Store field so the value
    // sent by _extras_to_store is populated on the record (and defaults
    // to undefined for non-error messages). Without this declaration the
    // field-detection scan in make_store.js does not see it and the
    // frontend isLLMError classification silently never fires. Declared
    // inside setup() per the mail-store field-registration rule.
    this.is_error = Record.attr();
  },

  /**
   * Override computeIsEmpty for LLM messages with tool calls or body_json
   * @returns {Boolean} True if message is empty
   */
  computeIsEmpty() {
    // For LLM messages, apply custom logic
    if (this.model === "llm.thread") {
      // Assistant messages with tool calls are never empty
      if (
        this.llm_role === "assistant" &&
        this.body_json?.tool_calls?.length > 0
      ) {
        return false;
      }

      // Tool messages with body_json are never empty
      if (this.llm_role === "tool" && this.body_json) {
        return false;
      }
    }

    // Use original computation for other messages
    return super.computeIsEmpty();
  },
});

/**
 * PATCH 4: Mobile message action menu (data layer)
 * The mobile overflow menu (MessageActionMenuMobile) builds its OWN action set
 * via useMessageActions() and is NOT driven by the Message component's
 * this.messageActions. Without this patch, the desktop toolbar would be empty
 * for llm.thread messages (PATCH 2) but the mobile "⋮" menu would still list
 * the chatter actions. Empty it for llm.thread messages too so AI threads are
 * clean on every form factor. Regular mail/discuss messages are untouched.
 */
patch(MessageActionMenuMobile.prototype, {
  setup() {
    super.setup();
    if (this.props.message?.model === "llm.thread") {
      this.messageActions = {
        get actions() {
          return [];
        },
      };
    }
  },
});
