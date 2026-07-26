/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { LLMToolMessage } from "../components/llm_tool_message/llm_tool_message";
import { Message } from "@mail/core/common/message";
import { Message as MessageModel } from "@mail/core/common/message_model";
import { MessageActionMenuMobile } from "@mail/core/common/message_action_menu_mobile";
import { Record } from "@mail/core/common/record";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { url } from "@web/core/utils/urls";
import { status, useState } from "@odoo/owl";
import {
    isLLMErrorMessage as _isErrorMessage,
    isLLMFinalAnswer as _isFinalAnswer,
    isLLMFirstStepOfTurn as _isFirstStepOfTurn,
    isLLMProgressMessage as _isProgressMessage,
    isLLMStepMessage as _isStepMessage,
    llmStepCountInTurn as _stepCountInTurn,
    llmTurnIdForMessage as _turnIdFor,
} from "../utils/llm_message_classify";

// UI-07 — the branded König Intelligence app icon, used as the avatar for
// all AI-side messages (assistant / tool / progress). The same asset is
// already shipped as the app menu web_icon (llm/views/llm_menu_views.xml:11).
const LLM_AVATAR_URL = url("/llm/static/description/icon.png");

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

        // FIX-4d: reasoning ("Thinking") block collapse state. manualOpen
        // starts null = auto (open while this message is the active
        // reasoning target, collapsed otherwise); a summary click pins a
        // manual override that survives re-renders and stream end.
        this.llmReasoningState = useState({ manualOpen: null });

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
            // Clipboard API unavailable (non-secure context) — no-op.
        }
    },

    /**
     * Label for the assistant copy button — flips to "Copied" briefly.
     */
    get copiedLabel() {
        return this.llmCopyState.copied ? _t("Copied") : _t("Copy");
    },

    /**
     * Icon class for the corner copy affordance (UI-07) — flips to a check
     * while the "Copied" flash is active. Read-only getter (render-safe).
     */
    get copiedIcon() {
        return this.llmCopyState.copied ? "fa fa-check" : "fa fa-copy";
    },

    // ------------------------------------------------------------------------
    // FIX-4d — collapsible reasoning ("Thinking") block above the answer.
    // The llm store accumulates reasoning chunks on body_json.reasoning
    // (bus path: llm.thread/reasoning_chunk; SSE path: reasoning_chunk).
    // ------------------------------------------------------------------------

    /** Whether this message carries reasoning text to render. */
    get hasReasoning() {
        return (
            this.isLLMMessage &&
            this.llmRole === "assistant" &&
            Boolean(this.props.message?.body_json?.reasoning?.length)
        );
    },

    /** The accumulated reasoning text (plain text, escaped by t-out). */
    get reasoningText() {
        return this.props.message?.body_json?.reasoning || "";
    },

    /**
     * True while this message is the store's active reasoning target — the
     * block auto-opens so the user watches the "Thinking" live, and
     * auto-collapses when the run/stream ends (the store clears the target).
     */
    get isReasoningStreaming() {
        if (!this.hasReasoning || !this.llmStore) {
            return false;
        }
        return this.llmStore.activeReasoningMessageId === this.props.message?.id;
    },

    /**
     * Whether the reasoning <details> is open. Auto: open while streaming,
     * collapsed otherwise. A summary click pins a manual override (sticky
     * across re-renders and across stream end).
     */
    get isReasoningOpen() {
        if (this.llmReasoningState.manualOpen !== null) {
            return this.llmReasoningState.manualOpen;
        }
        return this.isReasoningStreaming;
    },

    /** Summary label — "Thinking…" while streaming, "Thought process" after. */
    get reasoningLabel() {
        return this.isReasoningStreaming ? _t("Thinking…") : _t("Thought process");
    },

    /**
     * Pin a manual open/close override. The native <details> toggles itself
     * on summary click; we record the flipped state BEFORE the toggle so the
     * next OWL render (which sets t-att-open from isReasoningOpen) keeps the
     * user's choice. Reading ``open`` from the toggle event is NOT usable
     * here: programmatic open-attribute changes also fire ``toggle``, so the
     * auto/streaming transitions would clobber the manual state.
     */
    onReasoningSummaryClick() {
        this.llmReasoningState.manualOpen = !this.isReasoningOpen;
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

    /**
     * UI-06 S1 — A "progress" message is a system-style notification posted by
     * the orchestration runtime (``message_type === 'notification'`` with no
     * ``llm_role``): "Analyzing your request…", "Dispatching expert…", etc.
     * These render as slim one-line status rows (no author header, no avatar
     * sidebar) via the ``o-llm-message-status`` CSS class. Error messages are
     * NOT progress messages.
     */
    get isLLMProgress() {
        return this.isLLMMessage && _isProgressMessage(this.props.message);
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

    /**
     * UI-10 — whether the current session runs in debug mode (?debug=1).
     *
     * OCB precedent: ``list_renderer.js:1793``
     * ``get isDebugMode() { return Boolean(odoo.debug); }`` — the
     * ``odoo.debug`` global is set by the web module from the URL/session
     * debug flag (``env.js:34`` mirrors it as ``env.debug``). Same global
     * check as ``export_data_dialog.js:177`` and ``view_button.js:73``.
     */
    get isDebugMode() {
        return Boolean(odoo.debug);
    },

    /**
     * Whether this turn's steps drawer is expanded (reactive on llmStore).
     *
     * UI-10 — debug-aware rendering (replaces the UI-06 S3 defaults):
     * - Regular users: the drawer NEVER opens. Tool-call details (args,
     *   results, tool badges) are completely hidden — the whole step
     *   message is removed from layout via the ``o-llm-step-hidden-user``
     *   CSS class. Progress status lines (UI-06 S1/S2) tell the story.
     * - Debug users (?debug=1): every step renders fully expanded — the
     *   pre-drawer behavior. An explicit click on the drawer toggle still
     *   folds a turn to "▸ N work steps" for focus.
     */
    get isStepDrawerOpen() {
        const turnId = this.llmTurnId;
        if (turnId === null || !this.llmStore) {
            return false;
        }
        if (!this.isDebugMode) {
            return false;
        }
        // Debug mode: default fully expanded; explicit toggle still honored.
        const explicit = this.llmStore.stepDrawerOpen[turnId];
        if (explicit !== undefined) {
            return Boolean(explicit);
        }
        return true;
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
                if (this.isDebugMode) {
                    className += this.isStepDrawerOpen
                        ? " o-llm-step-open"
                        : " o-llm-step-collapsed";
                } else {
                    // UI-10 — regular users: step messages are removed from
                    // layout entirely (progress status lines tell the story).
                    className += " o-llm-step-collapsed o-llm-step-hidden-user";
                }
            }
            if (this.isLLMFinalAnswer) {
                className += " o-llm-final-answer";
            }
            // UI-06 S1 — progress notifications render as slim status lines.
            if (this.isLLMProgress) {
                className += " o-llm-message-status";
            }
        }

        return className;
    },

    /**
     * UI-07 — König Intelligence avatar for AI-side messages. For
     * ``llm.thread`` messages where the AI is the author (assistant, tool, or
     * progress-notification without ``llm_role``), return the branded app icon
     * instead of the generic grey person avatar. User messages keep the user's
     * own avatar (delegates to ``super``).
     *
     * OCB precedent: ``message.js:238-253`` ``authorAvatarUrl`` getter —
     * delegates to ``message.author.avatarUrl`` when an author exists, else
     * ``store.DEFAULT_AVATAR``. We intercept only for AI-side ``llm.thread``
     * messages (no human author on those — ``author_id=False``).
     */
    get authorAvatarUrl() {
        if (this.isLLMMessage && this.llmRole !== "user") {
            return LLM_AVATAR_URL;
        }
        return super.authorAvatarUrl;
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
            if (this.llm_role === "assistant" && this.body_json?.tool_calls?.length > 0) {
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
