"""Adapted from https://gist.github.com/jzempel/1552816

MIT License

Copyright (c) 2025, Marc Zempel

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import logging
import re
import time
from collections import namedtuple

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CAP-03: deterministic prompt-fragment assembler.
#
# The system prompt is assembled by ONE method (``_build_system_messages``)
# that collects ordered fragments from contributors, sorts by ``sequence``,
# applies a token budget, and composes the final message list.
#
# Each fragment is a ``SystemPromptFragment`` namedtuple. Contributors override
# ``_system_prompt_fragments()`` (super() + append) to add their own fragments
# with explicit ``sequence`` numbers — the assembler sorts, so order is
# deterministic regardless of MRO (fixes the ordering bug of the old
# ``get_prepend_messages`` override chain).
#
# OCB precedent: ``ir.ui.view`` arch composition — multiple inheriting views
# (contributors) apply XPaths in ``priority`` order (deterministic, not MRO).
# ---------------------------------------------------------------------------
SystemPromptFragment = namedtuple(
    "SystemPromptFragment",
    ["sequence", "role", "content", "droppable", "source"],
)
# sequence: int — determines order (lower = earlier in the prompt).
# role: str — usually "system".
# content: str — the prompt text.
# droppable: bool — True = may be dropped by the token budget (R4).
# source: str — identifier for dedup + debugging (e.g. "guardrails", "web_research").


class GenerationCancelled(BaseException):
    """Raised by the loop-control hook to abort ``generate_messages`` cooperatively.

    A caller passes an optional ``loop_control_check`` callable to
    :meth:`generate_messages`; when it returns ``{"cancel": True}`` the loop
    raises this exception at the next check site (loop iteration, before a tool
    call, between stream chunks). The orchestrator's ``finally`` block catches it
    to mark the run cancelled and post the terminal bus event. Pure control-flow
    signal — the cancel flag is already persisted on the task row by the canceler
    before this fires, so the exception carries no payload.

    Inherits from :class:`BaseException` (not :class:`Exception`) so it
    propagates through all ``except Exception as e:`` blocks in the tool
    execution pipeline (``_generate_assistant_response``, ``_execute_tool_call``,
    ``mail_message.execute_tool_call``).  This is the Python convention for
    control-flow signals — the same hierarchy as :class:`KeyboardInterrupt`,
    :class:`SystemExit`, and :class:`GeneratorExit`.  Without this, the
    between-chunks cancel check in ``_handle_streaming_response`` is silently
    swallowed by ``_generate_assistant_response``'s error handler, and the
    expert's cancel signal is swallowed by ``_execute_tool_call``'s error
    handler — the cancel never reaches the orchestrator.
    """


class GenerationPaused(BaseException):
    """Raised by the loop-control hook to pause ``generate_messages`` before a
    write-tool executes (human-in-the-loop approval, Phase 4a).

    A caller's ``loop_control_check`` callable returns ``{"pause": True}`` when
    the upcoming tool call is a write-tool that needs human approval. The loop
    raises this exception at the per-tool-call boundary — BEFORE
    ``post_tool_call`` / ``execute_tool_call`` — so no tool side-effect occurs.
    The orchestrator catches it, marks the run ``paused``, posts a
    ``run_paused`` bus event, and releases the worker (the generator ends
    gracefully; the ``queue.job`` completes). On approval, the real result is
    injected as a tool message via :meth:`post_tool_result`, and the run is
    re-enqueued — ``generate_messages(last_message=None)`` re-reads the
    injected tool result and the LLM chains naturally. On rejection, the
    rejection reason is injected instead.

    Inherits from :class:`BaseException` (not :class:`Exception`) for the same
    reason as :class:`GenerationCancelled`: the tool execution pipeline has
    ``except Exception as e:`` blocks in ``_generate_assistant_response``,
    ``_execute_tool_call``, and ``mail_message.execute_tool_call`` that would
    otherwise swallow the pause signal. Like cancel, pause is a control-flow
    signal — the assistant message with ``tool_calls`` is already committed to
    the DB before this fires, so the exception carries no payload.
    """


class TransientLLMError(Exception):
    """An LLM API error that may succeed on retry (5xx, timeout, connection).

    The OpenAI SDK already retries at the HTTP level (``max_retries=3`` in
    ``openai_get_client``).  When this error is raised (or classified from a
    raw SDK exception by ``_is_transient_llm_error``), it means the SDK's own
    retries were exhausted.  The retry loop in
    ``_generate_assistant_response`` catches it and retries the entire
    ``chat()`` call with exponential backoff — a second layer on top of the
    SDK's first layer.

    Non-transient errors (400, 401, 403, Odoo ``UserError``, etc.) are NOT
    this class — they propagate as-is and mark the run "failed".
    """


# ---------------------------------------------------------------------------
# Provider error taxonomy — single source of truth for the retry decision
# (:meth:`LLMThread._is_transient_llm_error`), telemetry
# (``koenig.ai.llm.trace.error_category``), and the user-facing error message
# (:meth:`LLMThread._describe_llm_error`). Modeled on the SAP
# ``classify_sap_error`` idiom (koenig_sap_business_partner) — named constants
# + one classifier + an explicit transient set.
#
# The category is STATUS-DRIVEN (the HTTP status code determines retry-ability),
# so a gateway-shaped 400 (Scaleway ``category: GATEWAY, upstreamStatus: 400``)
# stays FATAL like any other 400 — retrying it inflates the context window and
# the model then fabricates (the 2026-07-23 hardening finding). A gateway 5xx is
# transient. ``gateway`` is a descriptive category whose transient-ness is
# resolved from the status by ``_is_transient_llm_error`` (documented special
# case below).
# ---------------------------------------------------------------------------
LLM_ERR_RATE_LIMIT = "rate_limit"  # HTTP 429 — transient
LLM_ERR_SERVER = "server"  # HTTP 5xx (not 501) — transient
LLM_ERR_TIMEOUT = "timeout"  # request timeout — transient
LLM_ERR_CONNECTION = "connection"  # connection reset/refused — transient
LLM_ERR_EMPTY = (
    "empty_response"  # TransientLLMError (empty/reasoning-only/PERF-09) — transient
)
LLM_ERR_GATEWAY = "gateway"  # gateway/upstream error — transient iff status is 429/5xx
LLM_ERR_BAD_REQUEST = "bad_request"  # HTTP 400 — fatal
LLM_ERR_AUTH = "auth"  # HTTP 401 — fatal
LLM_ERR_FORBIDDEN = "forbidden"  # HTTP 403 — fatal
LLM_ERR_NOT_FOUND = "not_found"  # HTTP 404 — fatal
LLM_ERR_NOT_IMPLEMENTED = "not_implemented"  # HTTP 501 — fatal
LLM_ERR_UNKNOWN = "unknown"  # unclassified — fatal (never retry blindly)

# Categories that the retry loop treats as transient (retryable). ``gateway``
# is NOT here — its transient-ness depends on the status code (see
# ``_is_transient_llm_error``).
_LLM_TRANSIENT_CATEGORIES = frozenset(
    {
        LLM_ERR_RATE_LIMIT,
        LLM_ERR_SERVER,
        LLM_ERR_TIMEOUT,
        LLM_ERR_CONNECTION,
        LLM_ERR_EMPTY,
    }
)

# Extract a 3-digit HTTP status from a provider error's text when the exception
# does not expose a ``status_code`` attribute (belt-and-suspenders for wrapped
# errors; the OpenAI SDK sets ``status_code`` directly for the real cases).
_LLM_STATUS_TEXT_RE = re.compile(
    r"(?:error code|httpstatus|status[_ ]?code|upstreamstatus)['\"\s:]*?(\d{3})",
    re.IGNORECASE,
)


_FOLD_PROMPT_TEMPLATE = """\
You are merging new conversation content into an existing summary.

EXISTING SUMMARY:
{existing_anchor}

NEW CONVERSATION SEGMENT TO MERGE:
{span_text}

Merge the new segment into the existing summary. Update facts that changed, \
add new decisions and open items, and keep the summary under 1500 words. \
Use these sections:
- Intent: what the user is trying to achieve
- Facts & decisions: established facts and made decisions
- Open items: unresolved questions and pending tasks
- User preferences observed: communication style, language, format preferences

Do NOT include task-transient details, secrets, or verbatim quotes. \
Write in the user's language. Output ONLY the merged summary (no commentary)."""


class LLMThread(models.Model):
    _inherit = "llm.thread"

    assistant_id = fields.Many2one(
        "llm.assistant",
        string="Assistant",
        ondelete="restrict",
        help="The assistant used for this thread",
    )

    prompt_id = fields.Many2one(
        "llm.prompt",
        string="Prompt for workflow",
        ondelete="restrict",
        tracking=True,
        help="Prompt to use for workflow",
    )

    # D8 (P-MEM): rolling thread summarization — anchored iterative fold.
    # When the summary is set, folded messages (id <= llm_summary_upto_message_id)
    # are excluded from get_llm_messages() so summary + raw never overlap.
    llm_summary = fields.Text(
        help="Persistent rolling summary of earlier conversation (anchored iterative fold).",
    )
    llm_summary_upto_message_id = fields.Many2one(
        "mail.message",
        ondelete="set null",
        help="Watermark: messages with id <= this are folded into llm_summary.",
    )

    # CAP-01 per-thread tool deviation (override-based, replaces the snapshot).
    # ``_effective_tools()`` = (assistant.tool_ids | tool_ids_extra) - tool_ids_disabled,
    # resolved LIVE each turn. These two sets are normally EMPTY — a plain user
    # chat carries neither, so it always sees its assistant's current tools
    # (killing the 97%-stale snapshot). They exist for deliberate per-thread
    # curation (e.g. an orchestrator expert sub-thread that drops the dispatch
    # tool from the inherited master set).
    #
    # ⚠ RELATION TABLES — explicit + distinct, NOT auto. ``llm.thread`` already
    # has ``tool_ids`` (M2M → ``llm.tool``, AUTO table ``llm_thread_llm_tool_rel``).
    # A SECOND/THIRD auto M2M to the same comodel on the same model is impossible:
    # Odoo derives the auto table name from the two model tables only (no field
    # name), so all three would claim ``llm_thread_llm_tool_rel`` and Odoo raises
    # "Many2many fields ... use the same table and columns" (OCB fields.py
    # ~4988-5004). Hence EXPLICIT, distinct ``relation=`` names here.
    #
    # Columns are left AUTO on purpose (no explicit column1/column2): the
    # prototype child ``llm.thread.mock`` (``_name`` + ``_inherit="llm.thread"``,
    # llm_prompt_test.py) copies every M2M. With an explicit relation the child
    # would reuse the SAME physical table; auto columns make the child's copy
    # resolve ``column1`` from its OWN table (``llm_thread_mock_id``), so it does
    # not clash with the parent on the shared-table key — and the mock redefines
    # these two fields as non-stored (store=False) so it never creates/reads the
    # relation table at all. See llm_prompt_test.py.
    tool_ids_disabled = fields.Many2many(
        "llm.tool",
        relation="llm_thread_tool_disabled_rel",
        string="Disabled Tools",
        help="Tools removed from this thread's effective set even though its "
        "assistant offers them (per-thread deviation).",
    )
    tool_ids_extra = fields.Many2many(
        "llm.tool",
        relation="llm_thread_tool_extra_rel",
        string="Extra Tools",
        help="Tools added to this thread's effective set on top of what its "
        "assistant offers (per-thread deviation).",
    )

    @api.onchange("assistant_id")
    def _onchange_assistant_id(self):
        """Update provider, model and tools when assistant changes"""
        if self.assistant_id:
            self.provider_id = self.assistant_id.provider_id
            self.model_id = self.assistant_id.model_id
            self.tool_ids = self.assistant_id.tool_ids
            self.prompt_id = self.assistant_id.prompt_id
        else:
            # Clear prompt when assistant is cleared
            self.prompt_id = False

    def set_assistant(self, assistant_id):
        """Set the assistant for this thread and update related fields

        Args:
            assistant_id (int): The ID of the assistant to set

        Returns:
            bool: True if successful, False otherwise
        """
        self.ensure_one()

        # If assistant_id is False or 0, clear the assistant and its prompt
        if not assistant_id:
            return self.write({"assistant_id": False, "prompt_id": False})

        # Get the assistant record
        assistant = self.env["llm.assistant"].browse(assistant_id)
        if not assistant.exists():
            return False

        # Update the thread with the assistant and related fields
        update_vals = {
            "assistant_id": assistant_id,
            "tool_ids": [(6, 0, assistant.tool_ids.ids)],
        }
        if assistant.provider_id.id:
            update_vals["provider_id"] = assistant.provider_id.id
        if assistant.model_id.id:
            update_vals["model_id"] = assistant.model_id.id
        if assistant.prompt_id.id:
            update_vals["prompt_id"] = assistant.prompt_id.id
        return self.write(update_vals)

    def _candidate_tools(self):
        """Resolve the candidate tool set LIVE from the bound assistant + deviation.

        ``candidate = (base | tool_ids_extra) - tool_ids_disabled`` where
        ``base`` is the assistant's live resolved set
        (``assistant._resolved_tools()`` = explicit ``tool_ids`` ∪ subscribed
        bundles) when an assistant is bound, else the base fork's value (the raw
        ``tool_ids`` column, via ``super()``). Because the base is read live from
        the assistant, a tool added to the assistant (or to a bundle it
        subscribes to) reaches all its threads at once — the per-thread
        ``tool_ids`` snapshot is no longer an execution source (it survives only
        as legacy display data and is ignored here when an assistant is bound).

        The base ``_effective_tools`` then filters this candidate set through
        each tool's ``_ai_is_available_for`` gate (active / capability / consent)
        for the calling user. This override is the assistant/bundle/deviation
        half; the environment gating lives in the base so it applies to
        no-assistant threads too.

        Runs on the hot path (every turn) → stays cheap: in-memory recordset set
        operations over already-prefetched M2M relations, no search/read_group.

        Odoo-aligned live capability resolution (see the base docstring).

        Returns an ``llm.tool`` recordset.
        """
        self.ensure_one()
        base = (
            self.assistant_id._resolved_tools()
            if self.assistant_id
            else super()._candidate_tools()
        )
        return (base | self.tool_ids_extra) - self.tool_ids_disabled

    def action_open_thread(self):
        """Open the thread in the chat client interface

        Returns:
            dict: Action to open the thread in the chat client
        """
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "llm_thread.chat_client_action",
            "params": {
                "default_active_id": self.id,
            },
            "context": {
                "active_id": self.id,
            },
            "target": "current",
        }

    def get_context(self, base_context=None):
        """
        Get the context to pass to prompt rendering with thread-specific enhancements.
        This is the canonical method for creating prompt context in both production and testing.

        Args:
            base_context (dict): Additional context from caller (optional)

        Returns:
            dict: Context ready for prompt rendering
        """
        context = super().get_context(base_context or {})

        # If we have an assistant with default values, add them to the context
        if self.assistant_id:
            # Get assistant's evaluated default values using the current context
            assistant_defaults = self.assistant_id.get_evaluated_default_values(context)

            # Merge assistant defaults into context
            # Assistant defaults are added first, so thread context takes precedence
            if assistant_defaults:
                context = {**assistant_defaults, **context}

        return context

    @api.model
    def get_thread_by_id(self, thread_id):
        """Get a thread record by its ID

        Args:
            thread_id (int): ID of the thread

        Returns:
            tuple: (thread, error_response)
                  If successful, error_response will be None
                  If error, thread will be None
        """
        thread = self.browse(int(thread_id))
        if not thread.exists():
            return None, {"success": False, "error": "Thread not found"}
        return thread, None

    @api.model
    def get_thread_and_assistant(self, thread_id, assistant_id=False):
        """Get thread and assistant records by their IDs

        Args:
            thread_id (int): ID of the thread
            assistant_id (int, optional): ID of the assistant, or False to clear

        Returns:
            tuple: (thread, assistant, error_response)
                  If successful, error_response will be None
                  If error, thread and/or assistant will be None
        """
        # Get thread
        thread, error = self.get_thread_by_id(thread_id)
        if error:
            return None, None, error

        # If no assistant_id, return just the thread
        if not assistant_id:
            return thread, None, None

        # Get assistant from the assistant model
        assistant, error = self.env["llm.assistant"].get_assistant_by_id(assistant_id)
        if error:
            return thread, None, error

        return thread, assistant, None

    def _thread_to_store(self, store, **kwargs):
        """Extend base _thread_to_store to include assistant_id and prompt_id."""
        super()._thread_to_store(store, **kwargs)

        # Always add assistant_id and prompt_id to thread data (either value or False)
        for thread in self:
            thread_data = {
                "id": thread.id,
                "model": "llm.thread",
                "assistant_id": {
                    "id": thread.assistant_id.id,
                    "name": thread.assistant_id.name,
                    "model": "llm.assistant",
                }
                if thread.assistant_id
                else False,
                # prompt_id is defined in this module, so handle it here
                "prompt_id": {
                    "id": thread.prompt_id.id,
                    "name": thread.prompt_id.name,
                    "model": "llm.prompt",
                }
                if thread.prompt_id
                else False,
            }
            store.add("mail.thread", thread_data)

    def _extract_message_content(self, message):
        """Extract text content from a message regardless of format"""
        content = message.get("content", "")

        if isinstance(content, list) and len(content) > 0:
            return content[0].get("text", "")
        if isinstance(content, str):
            return content
        return ""

    def get_prepend_messages(self):
        """Hook: return a list of formatted messages to prepend to the conversation.

        CAP-03: delegates to ``_build_system_messages()``, the deterministic
        prompt-fragment assembler. Kept as a backward-compat shim so external
        callers and tests that call ``get_prepend_messages()`` still work.
        Koenig contributors now override ``_system_prompt_fragments()`` (not
        this method) to add their fragments.
        """
        self.ensure_one()
        return self._build_system_messages()

    # ------------------------------------------------------------------
    # CAP-03: deterministic prompt-fragment assembler.
    # ------------------------------------------------------------------

    def _build_system_messages(self):
        """ONE deterministic, ordered assembler for the system prompt.

        Collects fragments from three sources:
        1. ``_system_prompt_fragments()`` — model-level contributors (super()
           chain: persona, summary, guardrails, brief, domain, context, memory,
           anchor). Each contributor appends ``SystemPromptFragment`` namedtuples
           with an explicit ``sequence`` number.
        2. ``tool._system_prompt_fragment(self)`` — per-tool guidance, called
           for each tool in ``_effective_tools()`` (CAP-02 fix: effective set,
           not ``assistant.tool_ids``). Fragments deduplicated by ``source``.
        3. ``_consent_prompt_fragment()`` — consent instruction for
           ``requires_user_consent`` tools in the effective set.

        All fragments are sorted by ``sequence`` (deterministic order,
        regardless of MRO), then the token budget drops droppable fragments
        in reverse sequence order when over cap (R4).

        Returns a list of ``{"role": ..., "content": ...}`` dicts — the same
        format as the old ``get_prepend_messages()``.
        """
        self.ensure_one()
        fragments = list(self._system_prompt_fragments())

        # Per-tool fragments (effective tools only — CAP-02 fix).
        seen_sources = {f.source for f in fragments}
        for tool in self._effective_tools():
            tool_fragment = tool._system_prompt_fragment(self)
            if tool_fragment and tool_fragment.source not in seen_sources:
                fragments.append(tool_fragment)
                seen_sources.add(tool_fragment.source)

        # Consent fragment (from effective tools with requires_user_consent).
        consent_fragment = self._consent_prompt_fragment()
        if consent_fragment:
            fragments.append(consent_fragment)

        # Deterministic order: sort by sequence (stable — preserves relative
        # order of fragments with the same sequence, e.g. multiple persona msgs).
        fragments.sort(key=lambda f: f.sequence)

        # Token budget: drop droppable fragments in reverse sequence order.
        fragments = self._apply_token_budget(fragments)

        # Compose the final message list (skip empty fragments).
        return [{"role": f.role, "content": f.content} for f in fragments if f.content]

    def _system_prompt_fragments(self):
        """Return the base system-prompt fragments (persona + rolling summary).

        Koenig contributors override this (super() + append) to add their own
        fragments with explicit ``sequence`` numbers. The assembler sorts by
        sequence, so the order is deterministic regardless of MRO.

        Base fragments (fork-only, no koenig):
        - Persona (seq=30, not droppable) — from ``prompt_id.get_messages()``.
        - Rolling summary (seq=90, droppable) — D8 ``llm_summary``.
        """
        fragments = []

        # Persona (from prompt_id).
        if self.prompt_id:
            try:
                messages = self.prompt_id.get_messages(self.get_context())
                for i, msg in enumerate(messages):
                    # get_messages() may return content as a string or a
                    # multimodal list (``[{"type": "text", "text": ...}]``).
                    # Extract the text — system messages are always text.
                    raw_content = msg.get("content", "")
                    if isinstance(raw_content, list):
                        text_content = self._extract_message_content(msg)
                    else:
                        text_content = raw_content
                    fragments.append(
                        SystemPromptFragment(
                            sequence=30 + i,
                            role=msg.get("role", "system"),
                            content=text_content,
                            droppable=False,
                            source="persona",
                        )
                    )
            except Exception as e:
                _logger.error(
                    "Error getting messages from prompt '%s': %s",
                    self.prompt_id.name,
                    e,
                )
                self.message_post(
                    body=_(
                        "Note: The prompt '%s' could not be loaded. "
                        "Continuing without it. (Error: %s)",
                    )
                    % (self.prompt_id.name, str(e)),
                )

        # D8 (P-MEM): rolling summary as a system block when set.
        # Folded messages are excluded from get_llm_messages() so summary +
        # raw never overlap.
        if self.llm_summary:
            fragments.append(
                SystemPromptFragment(
                    sequence=90,
                    role="system",
                    content=(
                        "Summary of the earlier conversation "
                        "(messages before this point were removed from context):\n"
                        + self.llm_summary
                    ),
                    droppable=True,
                    source="summary",
                )
            )

        return fragments

    def _consent_prompt_fragment(self):
        """Consent instruction for ``requires_user_consent`` tools in the
        effective set (R5 — single source, from effective tools).

        Returns a ``SystemPromptFragment`` (seq=100, not droppable) or ``None``
        when no consent-requiring tool is offered this turn.
        """
        consent_tools = self._effective_tools().filtered(
            lambda t: t.requires_user_consent
        )
        if not consent_tools:
            return None
        config = self.env["llm.tool.consent.config"].get_active_config()
        tool_names = ", ".join([f"'{t.name}'" for t in consent_tools])
        content = config.system_message_template.format(tool_names=tool_names)
        return SystemPromptFragment(
            sequence=100,
            role="system",
            content=content,
            droppable=False,
            source="consent",
        )

    def _apply_token_budget(self, fragments):
        """Drop droppable fragments in reverse sequence order when over cap (R4).

        The cap is configurable via ICP ``llm_assistant.prompt_token_budget``
        (default 8000 tokens ≈ 32000 chars). Fragments with ``droppable=False``
        (guardrails, brief, persona, consent) are NEVER dropped.

        Uses a chars//4 token estimate (``_koenig_tokens_from_chars`` when
        available from koenig_ai_core; falls back to the same formula).
        """
        if not fragments:
            return fragments
        try:
            cap_str = (
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("llm_assistant.prompt_token_budget")
            )
            cap_tokens = int(cap_str) if cap_str else 8000
        except Exception:
            cap_tokens = 8000
        cap_chars = cap_tokens * 4

        total_chars = sum(len(f.content or "") for f in fragments)
        if total_chars <= cap_chars:
            return fragments

        # Drop droppable fragments in reverse sequence order (highest first).
        result = list(fragments)
        for frag in sorted(
            [f for f in result if f.droppable],
            key=lambda f: f.sequence,
            reverse=True,
        ):
            if total_chars <= cap_chars:
                break
            result.remove(frag)
            total_chars -= len(frag.content or "")
        return result

    def generate_messages(self, last_message, *, loop_control_check=None):
        """Generate messages with actual AI intelligence.

        ``loop_control_check`` is an optional callable returning a dict with a
        ``cancel`` boolean (``{"cancel": True}``). When provided, the loop
        consults it at three cooperative check sites — before each ``while``
        iteration, before each tool-call execution, and between stream chunks
        — and raises :class:`GenerationCancelled` as soon as it reports a
        cancel. This bounds cancellation to one tool/LLM-call granularity even
        when a single ``next(gen)`` step blocks inside a long tool call or
        stream (a between-``next(gen)`` poll alone cannot interrupt that).
        Pure addition: with no hook (or a hook that never cancels) the loop is
        byte-for-byte the previous behaviour.
        """
        self.ensure_one()

        # Get last message if not provided
        if not last_message:
            try:
                last_message = self.get_latest_llm_message()
            except UserError:
                # No DB messages found - check if prepended messages have a user message
                prepend_msgs = self.get_prepend_messages()
                user_msg = next(
                    (msg for msg in prepend_msgs if msg.get("role") == "user"),
                    None,
                )

                if user_msg:
                    # Extract content from prepended user message
                    content = user_msg.get("content", [])
                    if isinstance(content, list) and content:
                        body = content[0].get("text", "")
                    else:
                        body = str(content)

                    # Create actual user message from prepended content
                    last_message = self.message_post(
                        body=body,
                        llm_role="user",
                        author_id=self.env.user.partner_id.id,
                    )
                else:
                    # No user message in prepended messages either
                    raise

        # Bound the agentic loop: a model can keep requesting tools indefinitely
        # (each round costs an LLM call + tool execution). `tool_calls_max` on the
        # assistant caps the tool-execution rounds. Once reached, the next assistant
        # turn is nudged to answer now (a transient "you have enough info, don't
        # call more tools" message) while KEEPING tools available.
        # NB: do NOT drop tools / force tool_choice="none" to stop the loop — some
        # models (e.g. DeepSeek) then emit their native tool-call syntax as plain
        # text instead of answering. A hard cap is the final backstop.
        tool_rounds = 0
        max_tool_rounds = (
            (self.assistant_id.tool_calls_max or 5) if self.assistant_id else 8
        )
        hard_cap = max_tool_rounds + 3

        # Continue generation loop
        while self._should_continue(last_message):
            if self._check_loop_control(loop_control_check):
                raise GenerationCancelled(
                    _("Generation cancelled by loop-control hook."),
                )
            if last_message.llm_role in ("user", "tool"):
                if self.model_id.model_use in ("image_generation", "generation"):
                    last_message = yield from self._generate_response(last_message)
                else:
                    # Nudge toward a final answer once the soft cap is hit.
                    last_message = yield from self._generate_assistant_response(
                        final_answer=tool_rounds >= max_tool_rounds,
                        loop_control_check=loop_control_check,
                    )
            elif last_message.llm_role == "assistant" and last_message.has_tool_calls():
                if tool_rounds >= hard_cap:
                    _logger.warning(
                        "Thread %s: hard tool-call cap (%s) reached; stopping loop.",
                        self.id,
                        hard_cap,
                    )
                    break
                tool_rounds += 1
                # Double-execution guard (Phase 4a): filter out tool calls
                # that already have a completed/error tool message. Prevents
                # accidental re-execution on structured resume (the tool
                # result was injected before re-entering the loop).
                tool_calls = last_message.get_unexecuted_tool_calls()
                if not tool_calls:
                    _logger.info(
                        "Thread %s: all tool calls already have results; "
                        "breaking to avoid re-execution.",
                        self.id,
                    )
                    break
                for tool_call in tool_calls:
                    control = self._check_loop_control_tool(
                        loop_control_check,
                        tool_call,
                    )
                    if control.get("cancel"):
                        raise GenerationCancelled(
                            _("Generation cancelled by loop-control hook."),
                        )
                    if control.get("pause"):
                        raise GenerationPaused(
                            _("Generation paused for tool approval."),
                        )
                    tool_message = yield from self._execute_tool_call(
                        tool_call,
                        last_message,
                    )
                    last_message = tool_message
                    # Commit to persist the tool result + flush bus events.
                    # Sub-agents run on an independent cursor (self.pool.cursor()
                    # in _run_subagent), so this commit goes to the sub-cr, not
                    # the master's savepoint. See ODOO-transaction-savepoint-commit.md.
                    self.env.cr.commit()  # pylint: disable=invalid-commit
            else:
                _logger.info(
                    f"Breaking loop. Last message role: {last_message.llm_role}, "
                    f"has_tool_calls: {last_message.has_tool_calls()}",
                )
                break

        return last_message

    def _check_loop_control(self, loop_control_check):
        """Invoke the loop-control hook; return ``True`` when the loop must abort.

        ``loop_control_check`` is an optional callable returning a dict with a
        ``cancel`` boolean (``{"cancel": True}``) and, optionally, a ``mode``
        string (``"after_turn"`` or ``"immediate"``). A falsy/missing hook, a
        falsy return, or a dict without a truthy ``cancel`` all mean "keep
        going". The hook is invoked synchronously at each non-streaming check
        site in :meth:`generate_messages` (before each ``while`` iteration and
        before each tool-call execution); an exception raised by the hook
        propagates — the discipline of not silently swallowing a failed poll
        into a no-cancel lives in the caller's check, not here.

        This method always returns the cancel flag regardless of ``mode`` — it
        is used at boundaries where raising is always correct (a new turn is
        about to start / a new tool call is about to execute). For the
        between-stream-chunks site (where ``after_turn`` mode must defer), use
        :meth:`_check_loop_control_streaming` instead.
        """
        if not loop_control_check:
            return False
        result = loop_control_check() or {}
        return bool(result.get("cancel"))

    def _check_loop_control_streaming(self, loop_control_check):
        """Mode-aware between-chunks cancel check (Phase 3).

        In ``after_turn`` mode (the default for user cancels), do NOT raise
        between stream chunks — let the LLM response finish so the in-flight
        tool call's savepoint commits or rolls back cleanly. The cancel is
        caught at the next tool-call boundary (before
        :meth:`_execute_tool_call`) or at the next ``while`` iteration
        boundary (before :meth:`_generate_assistant_response`).

        In ``immediate`` mode (or when no ``mode`` key is present), raise at
        all check sites — the current Phase 2 behaviour, reserved for hard
        kills (zombie reclaim).

        Returns ``True`` when the between-chunks check should raise
        :class:`GenerationCancelled`.
        """
        if not loop_control_check:
            return False
        result = loop_control_check() or {}
        return bool(result.get("cancel")) and result.get("mode") != "after_turn"

    def _check_loop_control_tool(self, loop_control_check, tool_call):
        """Per-tool-call control check: cancel OR pause (Phase 4a).

        Calls the hook with the ``tool_call`` so the hook can decide whether
        to pause for approval (write-tools). Returns the full result dict so
        the caller can check both ``cancel`` and ``pause`` keys.

        At non-tool-call boundaries (top of ``while`` loop, between stream
        chunks), use :meth:`_check_loop_control` /
        :meth:`_check_loop_control_streaming` instead — those don't pass the
        ``tool_call`` and check only for cancel.
        """
        if not loop_control_check:
            return {}
        return loop_control_check(tool_call) or {}

    def _generate_response(self, last_message):
        raise NotImplementedError

    def _record_llm_call_trace(self, trace):
        """Extension point: react to a completed provider attempt's trace.

        No-op (logging) in the base fork; koenig overrides persist it
        (``koenig.ai.llm.trace``). Called for EVERY attempt outcome (ok,
        transient, fatal) by ``_finalize_llm_trace``. Returns None in the
        fork; koenig overrides may return the trace record.

        OCB hook precedent: ``mail_thread._get_customer_information``
        (``OCB/addons/mail/models/mail_thread.py:2129-2138``) — "extension
        point to subclasses". Fork hooks stay koenig-agnostic.

        Never raises — telemetry must not break the observed path.
        """
        _logger.info(
            "llm trace: thread=%s model=%s attempt=%s status=%s chunks=%s finish=%s",
            self.id,
            trace.get("model"),
            trace.get("attempt"),
            trace.get("status"),
            trace.get("chunks"),
            trace.get("finish_reason"),
        )

    def _finalize_llm_trace(self, request_trace, sink, status, exc):
        """Merge request_trace + sink, set status/error, call the hook.

        Called at the attempt boundary in ``_generate_assistant_response``
        for all three outcomes (ok / transient / fatal). The hook
        (``_record_llm_call_trace``) is itself wrapped in try/except +
        debug log, so this method never raises into the observed path.

        Sets ``error_class`` / ``error_message`` (capped 500) from ``exc``
        when present, plus ``error_category`` (the normalized provider-error
        taxonomy from :meth:`_classify_llm_error`, for PROV telemetry).
        ``duration_ms`` is taken from the sink (set by the handler's
        ``_fill_end``); when the handler never ran (chat raised before any
        chunk), it stays 0 — the trace still records the attempt + error
        class, which is the forensic value.
        """
        try:
            trace = dict(request_trace)
            trace.update(sink)
            trace["status"] = status
            if exc is not None:
                trace["error_class"] = type(exc).__name__
                try:
                    trace["error_category"] = self._classify_llm_error(exc)
                except Exception:  # noqa: BLE001 — classification must never break capture
                    trace["error_category"] = LLM_ERR_UNKNOWN
                try:
                    msg = str(exc)
                except Exception:  # noqa: BLE001 — never crash on a bad __str__
                    msg = type(exc).__name__
                if len(msg) > 500:
                    msg = msg[:500] + "…"
                trace["error_message"] = msg
            self._record_llm_call_trace(trace)
        except Exception:  # noqa: BLE001 — telemetry must never raise
            _logger.debug("llm trace finalize failed", exc_info=True)

    def _get_fallback_model(self, model):
        """Return the next fallback model after ``model`` (RES-01), or an
        empty ``llm.model`` recordset for no fallback.

        Base fork: NO fallback (empty). Extensions (e.g. the koenig
        orchestrator) override to consult their fallback-chain config.
        ONE hop only — the caller tries ``[primary, fallback]`` and stops
        (no chains, no loops). The fallback model's OWN effort profile
        applies via the provider's D2 precedence (per-model
        ``reasoning_effort`` field), so chain entries with different effort
        support are safe.
        """
        return self.env["llm.model"]

    def _generate_assistant_response(
        self,
        final_answer=False,
        *,
        loop_control_check=None,
        max_stream_duration_s=None,
        reasoning_effort=None,
    ):
        """Generate assistant response with transient-error retry.

        Calls ``model_id.chat()`` with an exponential-backoff retry loop for
        transient LLM API failures (5xx, timeout, connection).  The OpenAI SDK
        already retries at the HTTP level (``max_retries=3`` in
        ``openai_get_client``); this is a second layer that retries the entire
        ``chat()`` call when the SDK exhausts its own retries.

        RES-01 (fallback chain): after the retry loop is exhausted on a
        TRANSIENT error (incl. PERF-09 stream-cap kills), the turn is retried
        ONCE with the next chain model from :meth:`_get_fallback_model`
        (empty in the base fork = no fallback; koenig overrides consult the
        master fallback chain). One hop per turn, no loops. Non-transient
        errors (400/401/403) never hop — the same request would fail on the
        fallback too. The fallback attempt's traces carry the fallback
        provider/model automatically (per-attempt ``request_trace``).

        **Error propagation design:**
        - Transient errors (after all retries + the fallback hop exhausted) →
          propagate → the orchestrator's ``_execute`` handler marks the run
          "failed".
        - Non-transient errors (400, 401, 403) → propagate immediately →
          same "failed" path.
        - ``GenerationCancelled`` (``BaseException``) → never caught →
          propagates cleanly to the cancel handler.
        - Mid-stream errors after partial content → return partial message
          (no retry; the partial message cannot be un-posted).
        - Error chunk before any content → ``TransientLLMError`` (no
          partial state, safe to retry).

        ``final_answer=True`` (used once the agentic loop hits
        ``tool_calls_max``) appends a transient nudge instructing the model to
        answer now without calling more tools — while keeping tools available,
        so models that emit native tool-call syntax as text when constrained
        still behave.

        ``max_stream_duration_s`` (PERF-09) optionally overrides the
        total-duration streaming cap for THIS call (seconds). ``None`` →
        resolved per attempt by the handler via
        :meth:`_get_max_stream_duration_s` (ICP, default 180). A value
        ``<= 0`` disables the cap for this call.

        ``reasoning_effort`` (P2-a / EFF-02) optionally overrides the
        reasoning effort for THIS call (e.g. the explicit synthesis
        stage). Passed through to the provider's D2 precedence (per-call
        kwarg > model field > provider default). Chain models each get
        the same per-call value — their own effort profile applies when
        it is unset.

        TEL-01: every attempt is traced. ``request_trace`` (fingerprint) is
        built per attempt; ``sink`` is filled incrementally by the response
        handler; ``_finalize_llm_trace`` fires at the attempt boundary for
        all three outcomes (ok / transient / fatal). Capture is purely
        additive — retry/raise semantics are byte-for-byte unchanged.
        """
        self.env.flush_all()
        message_history = self.get_llm_messages()

        max_retries = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "llm_assistant.max_retries",
                3,
            )
        )
        backoff_base = float(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "llm_assistant.backoff_base",
                1.0,
            )
        )
        # Phase-3 Item 5-residual (2026-07-25): 429 (rate_limit) gets a
        # potentially longer backoff base — the per-minute token quota needs
        # time to reset, and the default 1s base is too short for some
        # providers (observed: Scaleway glm-5.2 INSUFFICIENT QUOTA retried
        # 3× within the same second → all hit the same rate limit). Default
        # is the same as the general backoff_base (backward compatible);
        # operators can set it to 2.0+ to give the quota more time.
        backoff_base_rate_limit = float(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "llm_assistant.backoff_base_rate_limit",
                backoff_base,
            )
        )

        # RES-01: the model chain for this turn — primary + ONE fallback hop
        # (empty hook in the base fork = chain of 1, unchanged behavior).
        model_chain = [self.sudo().model_id]
        fallback_model = self._get_fallback_model(model_chain[0])
        if fallback_model:
            model_chain.append(fallback_model)

        last_exc = None
        for chain_idx, chain_model in enumerate(model_chain):
            model_su = chain_model
            if last_exc is not None:
                _logger.warning(
                    "Thread %s: falling back to model %s after %s exhausted "
                    "retries (%s)",
                    self.id,
                    model_su.name,
                    model_chain[0].name,
                    last_exc,
                )
            use_streaming = getattr(model_su, "supports_streaming", True)
            chat_kwargs = self._prepare_chat_kwargs(
                message_history, use_streaming, final_answer=final_answer
            )
            # P2-a (EFF-02): per-call effort override — threaded into the
            # provider's D2 precedence (per-call kwarg > model field).
            if reasoning_effort:
                chat_kwargs["reasoning_effort"] = reasoning_effort
            for attempt in range(max_retries):
                # TEL-01: per-attempt request trace (fingerprint). Built inside
                # the retry loop so each attempt gets its own row.
                request_trace = {
                    "ts": fields.Datetime.to_string(fields.Datetime.now()),
                    "provider": (
                        model_su.provider_id.service if model_su.provider_id else None
                    ),
                    "provider_id": (
                        model_su.provider_id.id if model_su.provider_id else None
                    ),
                    "model": model_su.name,
                    "model_id": model_su.id,
                    "streaming": bool(use_streaming),
                    "attempt": attempt + 1,
                    "request": {
                        "messages": len(chat_kwargs.get("messages") or []),
                        "tools": len(chat_kwargs.get("tools") or []),
                        "reasoning_effort": (
                            chat_kwargs.get("reasoning_effort")
                            or getattr(model_su, "reasoning_effort", "")
                            or ""
                        ),
                        "final_answer": bool(final_answer),
                    },
                }
                sink = {}

                # Phase 1: call the LLM API (retryable on transient errors).
                # TransientLLMError is only raised by chat() — BEFORE any stream
                # chunks are processed.  Mid-stream connection drops are raw SDK
                # exceptions that propagate immediately (no partial-state retry).
                try:
                    raw_response = model_su.chat(**chat_kwargs)
                except Exception as exc:
                    # TEL-01: trace the chat-phase failure before retry/raise.
                    self._finalize_llm_trace(request_trace, sink, "error", exc)
                    if self._is_transient_llm_error(exc):
                        if attempt < max_retries - 1:
                            # Phase-3 Item 5-residual: use the rate-limit-
                            # specific backoff base for 429 errors — the
                            # per-minute token quota needs time to reset.
                            err_cat = self._classify_llm_error(exc)
                            base = (
                                backoff_base_rate_limit
                                if err_cat == LLM_ERR_RATE_LIMIT
                                else backoff_base
                            )
                            wait = base * (2**attempt)
                            _logger.warning(
                                "LLM API transient error (attempt %d/%d): %s — retrying in %ss",
                                attempt + 1,
                                max_retries,
                                exc,
                                wait,
                            )
                            time.sleep(wait)
                            continue
                        # Transient exhausted on this model → RES-01 hop.
                        last_exc = exc
                        break
                    # Non-transient → propagate immediately (never hops).
                    raise

                # Phase 2: process the response.
                # An empty response (no content, no tool_calls) raises
                # TransientLLMError from the handler BEFORE any assistant message
                # is created, so there is no partial state — safe to retry the
                # whole chat() call.  Other exceptions (mid-stream errors after a
                # partial message was posted) propagate immediately.
                try:
                    if use_streaming:
                        assistant_message = yield from self._handle_streaming_response(
                            raw_response,
                            loop_control_check=loop_control_check,
                            trace_sink=sink,
                            max_stream_duration_s=max_stream_duration_s,
                        )
                    else:
                        assistant_message = yield from self._handle_non_streaming_response(
                            raw_response,
                            trace_sink=sink,
                        )
                except TransientLLMError as exc:
                    # TEL-01: trace the handler-phase failure before retry/raise.
                    self._finalize_llm_trace(request_trace, sink, "error", exc)
                    # Phase-3 (2026-07-24): a duration-cap kill is near-
                    # deterministic for a given (context, model, effort) triple —
                    # re-issuing the identical request reproduces the same kill
                    # while burning the full cap per attempt (observed on
                    # 2026-07-24: 3×180s dead retries; the RES-01 fallback then
                    # completed the identical turn in ~100s). On a cap kill,
                    # skip the remaining same-model retries and hop to the
                    # fallback model immediately. With NO fallback configured,
                    # keep the legacy same-model retry (a genuinely stalled
                    # provider stream can recover on a later attempt).
                    if (
                        sink.get("duration_cap_kill")
                        and chain_idx < len(model_chain) - 1
                    ):
                        _logger.warning(
                            "Thread %s: stream killed by the total-duration cap — "
                            "hopping to fallback model %s instead of identical "
                            "retries (%s)",
                            self.id,
                            model_chain[chain_idx + 1].name,
                            exc,
                        )
                        last_exc = exc
                        break
                    if attempt < max_retries - 1:
                        wait = backoff_base * (2**attempt)
                        _logger.warning(
                            "LLM empty response (attempt %d/%d): %s — retrying in %ss",
                            attempt + 1,
                            max_retries,
                            exc,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    # Transient exhausted on this model → RES-01 hop.
                    last_exc = exc
                    break
                # TEL-01: trace the successful attempt.
                self._finalize_llm_trace(request_trace, sink, "ok", None)
                return assistant_message
            # Attempt loop broke without returning → transient exhaustion on
            # this chain model → hop to the next one (if any).
            continue
        # Chain exhausted — propagate the last transient error (same
        # semantics as the pre-RES-01 retry-exhausted raise).
        if last_exc is not None:
            raise last_exc

    def _is_transient_llm_error(self, exc):
        """Return True when ``exc`` should be retried (transient), else False.

        Delegates to :meth:`_classify_llm_error` (the single source of truth for
        the provider-error taxonomy) so the retry decision, the telemetry
        ``error_category``, and the user-facing message never drift apart.

        Transient (retry): rate_limit (429), server (5xx≠501), timeout,
        connection, empty_response (``TransientLLMError``), and gateway errors
        whose effective HTTP status is 429/5xx.

        Non-transient (propagate → run marked "failed"): bad_request (400),
        auth (401), forbidden (403), not_found (404), not_implemented (501),
        gateway errors with a 4xx status, and unknown. ``GenerationCancelled``
        / ``GenerationPaused`` are ``BaseException`` and never reach here.

        The OpenAI SDK already retries at the HTTP level (``max_retries=3`` in
        ``openai_get_client``); this classifies the error the SDK surfaces AFTER
        it exhausts its own retries, so the caller's loop can retry the whole
        ``chat()`` call.
        """
        cat = self._classify_llm_error(exc)
        if cat in _LLM_TRANSIENT_CATEGORIES:
            return True
        if cat == LLM_ERR_GATEWAY:
            # A gateway error is transient only when its status is a server-side
            # hiccup (429/5xx≠501). A gateway-wrapped 400 (upstream rejected the
            # request as malformed) is FATAL — retrying inflates the context and
            # the model fabricates.
            status = self._llm_error_status(exc)
            return status is not None and (
                status == 429 or (500 <= status < 600 and status != 501)
            )
        return False

    @staticmethod
    def _llm_error_status(exc):
        """Extract the HTTP status code from a provider error, or ``None``.

        Prefers the exception's ``status_code`` attribute (set by the OpenAI SDK
        for ``APIStatusError`` subclasses); falls back to parsing a 3-digit code
        out of the message text (``Error code: 400``, ``httpStatus: 400``,
        ``upstreamStatus: 400``) for wrapped errors. Never raises.
        """
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return status
        try:
            text = str(exc)
        except Exception:  # noqa: BLE001 — never crash on a bad __str__
            return None
        match = _LLM_STATUS_TEXT_RE.search(text)
        return int(match.group(1)) if match else None

    def _classify_llm_error(self, exc):
        """Classify a provider/LLM exception into an ``LLM_ERR_*`` category.

        Single source of truth for the provider-error taxonomy — consumed by
        :meth:`_is_transient_llm_error` (retry decision), ``_finalize_llm_trace``
        (``koenig.ai.llm.trace.error_category`` telemetry), and
        :meth:`_describe_llm_error` (user-facing message). Modeled on the SAP
        ``classify_sap_error`` idiom.

        Classification is generic (no SDK-specific imports) — it inspects the
        exception class name, ``status_code``, and the message text, so it works
        with the OpenAI SDK and any OpenAI-compatible provider. Never raises.
        """
        if isinstance(exc, TransientLLMError):
            return LLM_ERR_EMPTY
        exc_name = type(exc).__name__
        if "Timeout" in exc_name:
            return LLM_ERR_TIMEOUT
        if "Connection" in exc_name:
            return LLM_ERR_CONNECTION
        try:
            text = str(exc).lower()
        except Exception:  # noqa: BLE001 — never crash on a bad __str__
            text = ""
        # Gateway / upstream-service errors: the provider's edge failed to reach
        # or got an error from the model backend. Detected by text so the
        # transient-ness can be resolved from the status (a gateway 400 is fatal,
        # a gateway 5xx is transient) — see _is_transient_llm_error. "Bad
        # Gateway" (502) is correctly caught here and stays transient via status.
        if (
            "gateway" in text
            or "upstream-service-error" in text
            or "upstreamstatus" in text
        ):
            return LLM_ERR_GATEWAY
        status = self._llm_error_status(exc)
        if status == 429:
            return LLM_ERR_RATE_LIMIT
        if status == 501:
            return LLM_ERR_NOT_IMPLEMENTED
        if status is not None and 500 <= status < 600:
            return LLM_ERR_SERVER
        if status == 400:
            return LLM_ERR_BAD_REQUEST
        if status == 401:
            return LLM_ERR_AUTH
        if status == 403:
            return LLM_ERR_FORBIDDEN
        if status == 404:
            return LLM_ERR_NOT_FOUND
        # Text heuristics for providers that don't expose status_code cleanly.
        if "rate limit" in text or "too many requests" in text:
            return LLM_ERR_RATE_LIMIT
        return LLM_ERR_UNKNOWN

    def _describe_llm_error(self, exc):
        """Return a clean, translatable user-facing message for a provider error.

        Provider errors get a concise summary keyed off :meth:`_classify_llm_error`
        (the raw provider payload stays in the forensic ``error`` field + the
        trace row for admin diagnosis). An UNKNOWN category (e.g. a genuine code
        bug, not a provider error) falls back to ``str(exc)`` so real bugs are
        NOT hidden from the user. Never raises.
        """
        cat = self._classify_llm_error(exc)
        messages = {
            LLM_ERR_RATE_LIMIT: _(
                "The AI provider is rate-limiting requests right now. Please try again in a moment."
            ),
            LLM_ERR_SERVER: _(
                "The AI provider had a temporary server error. Please try again."
            ),
            LLM_ERR_TIMEOUT: _("The AI provider timed out. Please try again."),
            LLM_ERR_CONNECTION: _(
                "Could not reach the AI provider (connection error). Please try again."
            ),
            LLM_ERR_GATEWAY: _(
                "The AI provider's gateway returned an error (upstream service problem). "
                "Please try again in a moment."
            ),
            LLM_ERR_BAD_REQUEST: _(
                "The AI provider rejected the request. This usually indicates a "
                "configuration issue rather than a temporary problem — please contact an administrator."
            ),
            LLM_ERR_AUTH: _(
                "The AI provider rejected the credentials. Please check the provider API key."
            ),
            LLM_ERR_FORBIDDEN: _(
                "The AI provider denied access to this model. Please check the provider configuration."
            ),
            LLM_ERR_NOT_FOUND: _(
                "The AI provider could not find the requested model or endpoint."
            ),
            LLM_ERR_NOT_IMPLEMENTED: _(
                "The AI provider does not support this request."
            ),
        }
        try:
            return messages.get(cat) or str(exc)
        except Exception:  # noqa: BLE001 — never crash on a bad __str__
            return _("The AI request failed.")

    def _prepare_chat_kwargs(self, message_history, use_streaming, final_answer=False):
        """Prepare chat kwargs for provider. Can be overridden by extensions.

        `final_answer=True` appends a transient nudge (agentic loop cap reached)
        so the model answers from what it has instead of calling more tools. Tools
        stay available on purpose (see generate_messages).

        CAP-03: ``prepend_messages`` is built by ``_build_system_messages()``
        (the deterministic prompt-fragment assembler) instead of the old
        ``get_prepend_messages()`` hook chain.

        Phase-3 (2026-07-24): strengthened the nudge to explicitly forbid
        reasoning preamble ("Now I have all the data…", "Let me compile…").
        Models (esp. GLM-5.2) were emitting meta-commentary before the actual
        answer on the synthesis turn — the judge penalises it and it wastes
        output tokens.
        """
        kwargs = {
            "messages": message_history,
            # CAP-01: resolve the tool set LIVE (assistant-derived) instead of
            # the stale per-thread ``tool_ids`` snapshot — see _effective_tools.
            "tools": self._effective_tools(),
            "stream": use_streaming,
            # CAP-03: deterministic ordered assembler (persona, guardrails,
            # per-tool guidance, memory, domain, consent — all in one place).
            "prepend_messages": self._build_system_messages(),
        }
        if final_answer:
            kwargs["append_messages"] = [
                {
                    "role": "system",
                    "content": (
                        "You now have enough information from the tools. "
                        "Provide ONLY your final answer — no preamble, no "
                        "meta-commentary about having gathered data, and no "
                        "transitional phrases such as 'Now I have all the "
                        "data' or 'Let me compile the results'. Start "
                        "directly with the answer content. Answer completely "
                        "using the tool results above, in my language. Do "
                        "NOT call any more tools."
                    ),
                }
            ]
        return kwargs

    def get_llm_messages(self, limit=25):
        """Get the most recent LLM messages in chronological order.

        This method is optimized for LLM context preparation:
        - Always returns messages in chronological order (ASC)
        - Limits to the most recent N messages for context window management
        - Uses efficient database queries with proper indexing
        - Excludes error messages (is_error=True) from context
        - D8 (P-MEM): excludes folded messages (id <= llm_summary_upto_message_id)
          so the summary block + raw messages never overlap.

        Args:
            limit (int): Maximum number of recent messages to retrieve (default: 25)

        Returns:
            mail.message recordset: Recent LLM messages in chronological order
        """
        self.ensure_one()

        # Domain for filtering LLM messages only (excluding error messages)
        domain = [
            ("model", "=", self._name),
            ("res_id", "=", self.id),
            ("llm_role", "!=", False),  # Only messages with LLM roles
            ("is_error", "=", False),  # Exclude error messages from LLM context
        ]

        # D8: exclude folded messages when a summary watermark is set
        if self.llm_summary_upto_message_id:
            domain.append(("id", ">", self.llm_summary_upto_message_id.id))

        if limit:
            # Two-step approach for efficiency:
            # 1. Get the N most recent messages (DESC order)
            recent_messages = self.env["mail.message"].search(
                domain,
                order="create_date DESC, write_date DESC, id DESC",
                limit=limit,
            )
            # Phase-3 (2026-07-24): pin the LATEST user message. In long
            # tool-call runs the N-message window can scroll the user's
            # question out of context; the model then silently drifts from the
            # original intent (observed: "list ALL Swiss customers" narrowed to
            # "Top 10 by volume" once the question had scrolled out of a
            # 25-message window). The pin costs at most one extra message of
            # context; folded (summarized) user messages are not re-pinned
            # (the watermark domain above already excludes them).
            latest_user = self.env["mail.message"].search(
                domain + [("llm_role", "=", "user")],
                order="id DESC",
                limit=1,
            )
            if latest_user and latest_user not in recent_messages:
                recent_messages |= latest_user
            # 2. Sort them chronologically for LLM context (ASC order)
            return recent_messages.sorted(lambda m: (m.create_date, m.write_date, m.id))
        # If no limit, get all messages in chronological order
        return self.env["mail.message"].search(
            domain,
            order="create_date ASC, write_date ASC, id ASC",
        )

    def _llm_fold_into_summary(self, model, keep_last=10):
        """Fold older messages into the persistent summary (anchored iterative).

        D8 (P-MEM): when history exceeds the context window, fold the dropped
        prefix into a persistent **anchored** summary. Only the newly-dropped
        span is summarized and MERGED into the existing anchor — never
        regenerated from scratch. Folded messages leave the context (excluded
        by ``get_llm_messages`` via the watermark).

        Fail-soft: ANY error keeps the old anchor and watermark unchanged
        (the anchor must never be lost).

        Args:
            model: an ``llm.model`` recordset (caller picks the cheap model
                and sets the env context — the fork stays koenig-free).
            keep_last (int): number of newest messages to keep raw (default 10).
        """
        self.ensure_one()
        watermark_id = (
            self.llm_summary_upto_message_id.id
            if self.llm_summary_upto_message_id
            else 0
        )

        # Select foldable span: LLM messages after the watermark, excluding
        # the newest keep_last, excluding error messages.
        domain = [
            ("model", "=", self._name),
            ("res_id", "=", self.id),
            ("llm_role", "!=", False),
            ("is_error", "=", False),
            ("id", ">", watermark_id),
        ]
        all_after = self.env["mail.message"].search(
            domain,
            order="create_date ASC, write_date ASC, id ASC",
        )

        if len(all_after) <= keep_last:
            return  # Not enough messages to fold

        foldable = all_after[:-keep_last]
        if not foldable:
            return

        last_folded_id = foldable[-1].id

        # Build the anchored merge prompt
        existing_anchor = self.llm_summary or "(no existing summary)"
        span_lines = []
        for m in foldable:
            role = m.llm_role or "unknown"
            text = html2plaintext(m.body or "")[:500]
            span_lines.append(f"{role}: {text}")
        span_text = "\n\n".join(span_lines)

        prompt = _FOLD_PROMPT_TEMPLATE.format(
            existing_anchor=existing_anchor,
            span_text=span_text,
        )

        try:
            result = model.chat(
                [],
                stream=False,
                prepend_messages=[{"role": "user", "content": prompt}],
            )
            content = result.get("content", "") if isinstance(result, dict) else ""
            if not content:
                return  # No summary generated, keep old anchor
        except Exception:
            _logger.exception(
                "llm_assistant: _llm_fold_into_summary chat call failed for thread %s",
                self.id,
            )
            return  # Fail-soft: keep old anchor

        # Write the new summary + watermark in one write (fail-soft: if the
        # write fails, the old anchor is still in the DB from the last fold).
        self.sudo().write(
            {
                "llm_summary": content,
                "llm_summary_upto_message_id": last_folded_id,
            }
        )

    def get_latest_llm_message(self):
        """Get the most recent LLM message for flow control.

        Returns:
            mail.message: The latest LLM message

        Raises:
            UserError: If no LLM messages exist
        """
        self.ensure_one()

        domain = [
            ("model", "=", self._name),
            ("res_id", "=", self.id),
            ("llm_role", "!=", False),
        ]

        result = self.env["mail.message"].search(
            domain,
            order="create_date DESC, write_date DESC, id DESC",
            limit=1,
        )

        if not result:
            raise UserError("No LLM messages found in this thread.")

        return result[0]

    def _should_continue(self, last_message):
        """Simplified continue logic based on message history."""
        if not last_message:
            return False

        # Continue if:
        # 1. Last message is user message → generate assistant response
        # 2. Last message is tool message → generate assistant response
        # 3. Last message is assistant with tool calls → execute tools
        if last_message.llm_role in ("user", "tool") or (
            last_message.llm_role == "assistant" and last_message.has_tool_calls()
        ):
            return True

        return False

    def _get_max_stream_duration_s(self):
        """Resolve the total-duration streaming cap in seconds (PERF-09).

        Default source: ICP ``llm_assistant.max_stream_duration_s`` (180).
        Extensions (e.g. the koenig orchestrator) override this hook to read
        their own config namespace. A resolved value ``<= 0`` disables the
        cap. Never raises — a broken config read falls back to 180.
        """
        try:
            return float(
                self.env["ir.config_parameter"]
                .sudo()
                .get_param("llm_assistant.max_stream_duration_s", 180)
            )
        except Exception:  # noqa: BLE001 — config read must not break generation
            _logger.debug("max_stream_duration_s ICP read failed", exc_info=True)
            return 180.0

    def _handle_streaming_response(  # noqa: C901
        self,
        stream_response,
        *,
        loop_control_check=None,
        trace_sink=None,
        max_stream_duration_s=None,
    ):
        """Handle streaming response from LLM provider with tool call processing.

        The between-chunks cancel check is **mode-aware** (Phase 3): in
        ``after_turn`` mode the check does NOT raise between chunks — the LLM
        response is allowed to finish so the in-flight tool call's savepoint
        commits or rolls back cleanly. The cancel is then caught at the next
        tool-call boundary or ``while`` iteration boundary. In ``immediate``
        mode (or when no ``mode`` is returned), the check raises at every
        chunk boundary (the Phase 2 behaviour, reserved for hard kills).

        PERF-09 (total-duration cap): at each chunk boundary, when the
        stream's total elapsed time exceeds ``max_stream_duration_s``
        (kwarg → :meth:`_get_max_stream_duration_s` ICP fallback, default
        180; ``<= 0`` disables), the stream is closed and a
        ``TransientLLMError`` is raised. This kills pathological
        reasoning explosions (observed 200–1200 reasoning chunks over
        48–325 s on 2026-07-22) that no per-read SDK timeout can catch —
        chunks keep arriving, the stream just never finishes. The raise is
        classified transient, so the retry loop in
        ``_generate_assistant_response`` retries with backoff (a warm
        provider instance usually answers in time). Partial-content
        handling: any already-posted partial assistant message is KEPT
        (never deleted); the error is annotated on the trace
        (``error_class``/``error_message`` via ``_finalize_llm_trace``).
        When a kill happens mid-content and the retry then succeeds, the
        retry posts a FRESH assistant message (the partial one stays as
        the visible record of the killed attempt). The cancel check keeps
        precedence — ``GenerationCancelled`` is never swallowed by the
        cap path.

        TEL-01: when ``trace_sink`` (a dict) is passed by the caller, it is
        filled incrementally with the stream histogram (content/reasoning/tool
        chunk counts + char lengths), finish_reason, usage (from the terminal
        metadata chunk), first-token latency, and message_created/id. The sink
        lives in the caller (``_generate_assistant_response``) so it survives
        both the return and the raise paths — the caller's
        ``_finalize_llm_trace`` reads it at the attempt boundary. Filling is
        purely additive: existing yield/raise semantics are byte-for-byte
        unchanged. Never raises from sink writes (guarded).
        """
        message = None
        accumulated_content = ""
        collected_tool_calls = []
        # TEL-01: sink init (keys only — no behavior change). t0 is the
        # monotonic clock for ttft/duration; the sink carries it for _fill_end.
        # HARD-07: prev_chunk_mono tracks the previous chunk's monotonic time
        # for inter-chunk gap forensics.
        t0 = time.monotonic()
        prev_chunk_mono = None
        # PERF-09: resolve the cap once per attempt (not per chunk).
        max_stream_s = (
            max_stream_duration_s
            if max_stream_duration_s is not None
            else self._get_max_stream_duration_s()
        )
        if trace_sink is not None:
            trace_sink.setdefault("chunks", {"content": 0, "reasoning": 0, "tool": 0})
            trace_sink.setdefault("content_length", 0)
            trace_sink.setdefault("reasoning_length", 0)
            trace_sink.setdefault("tool_count", 0)
            trace_sink.setdefault("tool_names", "")

        def _sink_fill_end(msg):
            """TEL-01: record finish timestamps + message link on the sink.

            Called at every exit site (both TransientLLMError raises and both
            return message sites). Never raises.
            """
            if trace_sink is None:
                return
            try:
                # Odoo Datetime fields reject the ISO 'T' separator; use
                # to_string() which produces 'YYYY-MM-DD HH:MM:SS'.
                trace_sink["finish_ts"] = fields.Datetime.to_string(
                    fields.Datetime.now()
                )
                trace_sink["duration_ms"] = int((time.monotonic() - t0) * 1000)
                trace_sink["message_created"] = bool(msg)
                trace_sink["message_id"] = msg.id if msg else None
            except Exception:  # noqa: BLE001 — telemetry must never raise
                _logger.debug("trace_sink fill_end failed", exc_info=True)

        for chunk in stream_response:
            if self._check_loop_control_streaming(loop_control_check):
                raise GenerationCancelled(
                    _("Generation cancelled by loop-control hook."),
                )

            # HARD-07: inter-chunk gap forensics (durations only — privacy-
            # safe). Tracks max gap + cumulative total/count so the trace can
            # expose avg. Metadata chunks (finish_reason/usage) count too —
            # consistent with the ttft "any chunk" semantics below.
            now_mono = time.monotonic()
            if trace_sink is not None and prev_chunk_mono is not None:
                try:
                    gap_ms = int((now_mono - prev_chunk_mono) * 1000)
                    if gap_ms > trace_sink.get("inter_chunk_max_ms", 0):
                        trace_sink["inter_chunk_max_ms"] = gap_ms
                    trace_sink["inter_chunk_gap_total_ms"] = (
                        trace_sink.get("inter_chunk_gap_total_ms", 0) + gap_ms
                    )
                    trace_sink["inter_chunk_gap_count"] = (
                        trace_sink.get("inter_chunk_gap_count", 0) + 1
                    )
                except Exception:  # noqa: BLE001 — telemetry must never raise
                    _logger.debug("trace_sink inter-chunk fill failed", exc_info=True)
            prev_chunk_mono = now_mono

            # PERF-09: total-duration cap. Fires regardless of cancel mode —
            # it is a safety net, not a cancel. The cancel check above keeps
            # precedence (a cancelled run never reports a duration kill).
            if max_stream_s > 0 and (time.monotonic() - t0) > max_stream_s:
                elapsed = time.monotonic() - t0
                _logger.warning(
                    "Thread %s: stream killed by total-duration cap "
                    "(%.1fs elapsed > %.1fs cap)",
                    self.id,
                    elapsed,
                    max_stream_s,
                )
                close = getattr(stream_response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 — best-effort close
                        _logger.debug(
                            "stream close after duration cap failed", exc_info=True
                        )
                # Partial content is KEPT (message stays posted); the error
                # is annotated on the trace by the caller's finalize step.
                if trace_sink is not None:
                    # Phase-3 (2026-07-24): flag the kill so the retry loop can
                    # skip futile identical retries and hop to the fallback
                    # model immediately (see _generate_assistant_response).
                    trace_sink["duration_cap_kill"] = True
                _sink_fill_end(message)
                raise TransientLLMError(
                    _(
                        "LLM stream exceeded the total-duration cap of %(cap)s s "
                        "(killed after %(elapsed).1f s).",
                        cap=max_stream_s,
                        elapsed=elapsed,
                    ),
                )

            # TEL-01: record first-chunk timestamp (any chunk type counts).
            if trace_sink is not None and "first_chunk_ts" not in trace_sink:
                try:
                    trace_sink["first_chunk_ts"] = fields.Datetime.to_string(
                        fields.Datetime.now()
                    )
                    trace_sink["ttft_ms"] = int((time.monotonic() - t0) * 1000)
                except Exception:  # noqa: BLE001 — telemetry must never raise
                    _logger.debug("trace_sink first-chunk fill failed", exc_info=True)

            # TEL-01: handle terminal metadata chunks FIRST (finish_reason,
            # usage) — record into sink, continue. These keys never collide
            # with content/tool_calls/error keys.
            if trace_sink is not None:
                if "finish_reason" in chunk:
                    trace_sink["finish_reason"] = chunk["finish_reason"]
                    continue
                if "usage" in chunk:
                    trace_sink["usage"] = chunk["usage"]
                    continue

            # Initialize message on first content
            if message is None and chunk.get("content"):
                # FIX-4b: post the placeholder with llm_streaming_placeholder=True
                # so _notify_thread skips the bus broadcast. The placeholder's
                # bus payload is baked at post time (precommit) but flushed at
                # the final commit — AFTER all progressive chunk broadcasts.
                # If broadcast here, the stale placeholder overwrites the
                # streamed answer at the end of the run. The orchestration
                # bridge handles message_create itself (independent cursor,
                # CURRENT body) so the placeholder still appears live.
                # Ref: TRACKER_2026-07-25_UI_RESEARCH.md §4.
                message = self.with_context(
                    llm_streaming_placeholder=True
                ).message_post(
                    body="Thinking...",
                    llm_role="assistant",
                    author_id=False,
                )
                yield {"type": "message_create", "message": message.to_store_format()}

            # Handle content streaming
            if chunk.get("content"):
                accumulated_content += chunk["content"]
                if trace_sink is not None:
                    try:
                        trace_sink["chunks"]["content"] += 1
                        trace_sink["content_length"] += len(chunk["content"])
                    except Exception:  # noqa: BLE001 — telemetry must never raise
                        _logger.debug("trace_sink content fill failed", exc_info=True)
                message.write({"body": self._process_llm_body(accumulated_content)})
                yield {"type": "message_chunk", "message": message.to_store_format()}

            # Collect tool calls for processing
            if chunk.get("tool_calls"):
                collected_tool_calls.extend(chunk["tool_calls"])
                if trace_sink is not None:
                    try:
                        trace_sink["chunks"]["tool"] += 1
                        trace_sink["tool_count"] = len(chunk["tool_calls"])
                        names = [
                            (tc.get("function") or {}).get("name", "")
                            for tc in chunk["tool_calls"]
                        ]
                        trace_sink["tool_names"] = ",".join(n for n in names if n)
                    except Exception:  # noqa: BLE001 — telemetry must never raise
                        _logger.debug("trace_sink tool fill failed", exc_info=True)
                _logger.debug(
                    f"Collected {len(chunk['tool_calls'])} tool calls from chunk",
                )

            # TEL-01: count reasoning chunks (they carry no content/tool_calls
            # keys, so they fall through the above checks — count them here).
            # FIX-4d (2026-07-25): forward reasoning chunks as a
            # ``reasoning_chunk`` event so the orchestration bridge can
            # relay them to the UI (Kilo-Code-style "Thinking" section).
            # Ref: TRACKER_2026-07-25_UI_RESEARCH.md §4.
            if chunk.get("reasoning"):
                if trace_sink is not None:
                    try:
                        trace_sink["chunks"]["reasoning"] += 1
                        trace_sink["reasoning_length"] += len(chunk["reasoning"])
                    except Exception:  # noqa: BLE001 — telemetry must never raise
                        _logger.debug("trace_sink reasoning fill failed", exc_info=True)
                if message is not None:
                    yield {
                        "type": "reasoning_chunk",
                        "message": message.to_store_format(),
                        "reasoning": chunk["reasoning"],
                    }

            # Handle errors
            if chunk.get("error"):
                yield {"type": "error", "error": chunk["error"]}
                if message is None:
                    # Error chunk arrived before any content was posted —
                    # no partial state exists, safe to retry the whole
                    # chat() call.  Treat as transient so the retry loop
                    # re-attempts; after retries exhausted the error
                    # propagates to the orchestrator instead of silently
                    # returning None (false success).
                    _sink_fill_end(None)
                    raise TransientLLMError(
                        _(
                            "LLM stream error before any content: %s",
                            chunk["error"],
                        ),
                    )
                # Partial content was already posted — preserve existing
                # semantics: return the partial message so the caller can
                # decide what to do.  Mid-stream errors are NOT retried
                # because the partial message cannot be un-posted.
                _sink_fill_end(message)
                return message

        # CRITICAL FIX: Create assistant message IMMEDIATELY if we have tool calls
        if collected_tool_calls:
            body_json = {"tool_calls": collected_tool_calls}

            if not message:
                # Create assistant message with body_json (handled by message_post override)
                message = self.message_post(
                    body="",  # Empty body for tool-only responses
                    body_json=body_json,
                    llm_role="assistant",
                    author_id=False,
                )
                # Commit to ensure message is saved before tool execution
                self.env.cr.commit()  # pylint: disable=invalid-commit
                yield {"type": "message_create", "message": message.to_store_format()}
            else:
                # Update existing message with tool calls
                message.write({"body_json": body_json})
                # Commit to ensure update is saved
                self.env.cr.commit()  # pylint: disable=invalid-commit
                yield {"type": "message_update", "message": message.to_store_format()}
        elif message and accumulated_content:
            # Final update for assistant message without tool calls
            message.write({"body": self._process_llm_body(accumulated_content)})
            yield {"type": "message_update", "message": message.to_store_format()}

        if message is None:
            # Stream completed with no content and no tool_calls (e.g.
            # reasoning-only chunks that carry no ``content`` key).  No
            # assistant message was created, so there is no partial state to
            # preserve.  Treat as transient so the retry loop in
            # _generate_assistant_response re-calls chat() with backoff;
            # after retries are exhausted the error propagates to the
            # orchestrator instead of silently returning None.
            _sink_fill_end(None)
            raise TransientLLMError(
                _("LLM stream completed with no content and no tool calls."),
            )
        _sink_fill_end(message)
        return message

    def _handle_non_streaming_response(self, response, *, trace_sink=None):
        """Handle non-streaming response from LLM provider.

        Guards against a provider ``chat()`` call that returns a raw string
        instead of a dict (observed with the Mistral provider used by the
        ``media_describe`` expert — root cause of the 3 historical
        ``"'str' object has no attribute 'get'"`` expert failures). Mirrors
        the ``isinstance(result, dict)`` guard already present at line 823
        in ``_llm_fold_into_summary``.

        TEL-01: when ``trace_sink`` is passed, fills content_length,
        tool_count/names, usage from ``response.get("usage")``, and
        message_created/id before both the raise and the yield path. Purely
        additive — never raises from sink writes.
        """
        t0 = time.monotonic()
        # Extract content and tool calls from response. Some providers
        # return a raw string instead of a dict on edge cases (empty
        # response, rate-limit fallback). Guard with isinstance to avoid
        # AttributeError cascading into an expert failure.
        if not isinstance(response, dict):
            _logger.warning(
                "llm_assistant: non-streaming response was %s, expected dict — "
                "treating as plain content",
                type(response).__name__,
            )
            content = str(response) if response else ""
            tool_calls = []
        else:
            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

        # TEL-01: fill the sink with what we extracted (before the raise so
        # the trace records the empty-response signature).
        if trace_sink is not None:
            try:
                # Odoo Datetime fields reject the ISO 'T' separator; use
                # to_string() which produces 'YYYY-MM-DD HH:MM:SS'.
                trace_sink["finish_ts"] = fields.Datetime.to_string(
                    fields.Datetime.now()
                )
                trace_sink["duration_ms"] = int((time.monotonic() - t0) * 1000)
                trace_sink["content_length"] = len(content or "")
                trace_sink["tool_count"] = len(tool_calls or [])
                if tool_calls:
                    names = [
                        (tc.get("function") or {}).get("name", "") for tc in tool_calls
                    ]
                    trace_sink["tool_names"] = ",".join(n for n in names if n)
                usage = response.get("usage") if isinstance(response, dict) else None
                if usage:
                    trace_sink["usage"] = usage
                # finish_reason is not reliably present on non-streaming
                # responses; leave unset when absent.
                if isinstance(response, dict) and response.get("finish_reason"):
                    trace_sink["finish_reason"] = response["finish_reason"]
            except Exception:  # noqa: BLE001 — telemetry must never raise
                _logger.debug("trace_sink non-streaming fill failed", exc_info=True)

        if not content and not tool_calls:
            # Provider returned no content and no tool_calls.  Do NOT
            # fabricate a fake "No response from model" assistant message
            # (false success).  Treat as transient so the retry loop
            # re-calls chat(); after retries are exhausted the error
            # propagates to the orchestrator.
            if trace_sink is not None:
                try:
                    trace_sink["message_created"] = False
                    trace_sink["message_id"] = None
                except Exception:  # noqa: BLE001 — telemetry must never raise
                    _logger.debug(
                        "trace_sink message_created fill failed", exc_info=True
                    )
            raise TransientLLMError(
                _("LLM returned no content and no tool calls."),
            )

        # Prepare body_json with tool calls if present
        body_json = {"tool_calls": tool_calls} if tool_calls else None

        # Create assistant message with body_json (handled by message_post override)
        assistant_message = self.message_post(
            body=self._process_llm_body(content) if content else "",
            body_json=body_json,
            llm_role="assistant",
            author_id=False,
        )

        # TEL-01: record the created message link.
        if trace_sink is not None:
            try:
                trace_sink["message_created"] = True
                trace_sink["message_id"] = assistant_message.id
            except Exception:  # noqa: BLE001 — telemetry must never raise
                _logger.debug("trace_sink message link fill failed", exc_info=True)

        yield {
            "type": "message_create",
            "message": assistant_message.to_store_format(),
        }
        return assistant_message

    def _execute_tool_call(self, tool_call, assistant_message):
        """Execute a single tool call and return the tool message.

        Uses an outer savepoint so that if the inner ``execute_tool_call``
        savepoint fails to clear a poisoned transaction (e.g., an SQL error
        occurred before the inner savepoint was created), the outer savepoint
        rollback clears it. Without this, the error handler's
        ``create_tool_error_message`` (which needs SQL) also fails with
        ``InFailedSqlTransaction``, cascading into an un-recoverable state.

        Args:
            tool_call (dict): Tool call data from assistant message
            assistant_message (mail.message): The assistant message that contains the tool calls

        Yields:
            dict: Status updates for streaming

        Returns:
            mail.message: The tool message with execution result
        """
        try:
            with self.env.cr.savepoint():
                # Create tool message using the post_tool_call method
                tool_msg = self.env["mail.message"].post_tool_call(
                    tool_call,
                    thread_model=self,
                )
                yield {"type": "message_create", "message": tool_msg.to_store_format()}

                # Execute the tool call (inner method has its own savepoint)
                result_msg = yield from tool_msg.execute_tool_call(thread_model=self)
            return result_msg

        except Exception as e:
            _logger.error(f"Error executing tool call: {e}")

            # The outer savepoint was rolled back — the transaction should be
            # clean now, even if the inner savepoint failed to clear a poisoned
            # state. Create the error message safely.
            try:
                error_msg = self.env["mail.message"].create_tool_error_message(
                    tool_call,
                    str(e),
                    thread_model=self,
                )
                yield {
                    "type": "message_create",
                    "message": error_msg.to_store_format(),
                }
                return error_msg
            except Exception as e2:
                _logger.error(f"Failed to create error message: {e2}")
                # Yield error event so frontend knows something went wrong
                yield {
                    "type": "error",
                    "error": f"Tool execution failed: {e!s}",
                }
                # Re-raise the original exception - don't silently return None
                raise e from e2
