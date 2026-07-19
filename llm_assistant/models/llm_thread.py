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
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


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
        """Hook: return a list of formatted messages to prepend to the conversation."""
        self.ensure_one()

        messages = []
        if self.prompt_id:
            try:
                # Get messages from the prompt with enhanced context
                messages = self.prompt_id.get_messages(self.get_context())
            except Exception as e:
                _logger.error(
                    "Error getting messages from prompt '%s': %s",
                    self.prompt_id.name,
                    e,
                )
                # Continue without prompt messages rather than failing completely
                # Post a user-friendly warning to the thread
                self.message_post(
                    body=_(
                        "Note: The prompt '%s' could not be loaded. "
                        "Continuing without it. (Error: %s)",
                    )
                    % (self.prompt_id.name, str(e)),
                )

        # D8 (P-MEM): append rolling summary as a system block when set.
        # Folded messages are excluded from get_llm_messages() so summary +
        # raw never overlap.
        if self.llm_summary:
            messages = list(messages) + [
                {
                    "role": "system",
                    "content": (
                        "Summary of the earlier conversation "
                        "(messages before this point were removed from context):\n"
                        + self.llm_summary
                    ),
                }
            ]

        return messages

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

    def _generate_assistant_response(
        self, final_answer=False, *, loop_control_check=None
    ):
        """Generate assistant response with transient-error retry.

        Calls ``model_id.chat()`` with an exponential-backoff retry loop for
        transient LLM API failures (5xx, timeout, connection).  The OpenAI SDK
        already retries at the HTTP level (``max_retries=3`` in
        ``openai_get_client``); this is a second layer that retries the entire
        ``chat()`` call when the SDK exhausts its own retries.

        **Error propagation design:**
        - Transient errors (after all retries exhausted) → propagate → the
          orchestrator's ``_execute`` handler marks the run "failed".
        - Non-transient errors (400, 401, 403) → propagate immediately →
          same "failed" path.
        - ``GenerationCancelled`` (``BaseException``) → never caught →
          propagates cleanly to the cancel handler.
        - Mid-stream errors → propagate immediately (no retry for partial
          state; ``_handle_streaming_response`` handles error chunks
          internally and does not raise ``TransientLLMError``).

        ``final_answer=True`` (used once the agentic loop hits
        ``tool_calls_max``) appends a transient nudge instructing the model to
        answer now without calling more tools — while keeping tools available,
        so models that emit native tool-call syntax as text when constrained
        still behave.
        """
        self.env.flush_all()
        message_history = self.get_llm_messages()
        use_streaming = getattr(self.model_id, "supports_streaming", True)
        chat_kwargs = self._prepare_chat_kwargs(
            message_history, use_streaming, final_answer=final_answer
        )

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

        for attempt in range(max_retries):
            # Phase 1: call the LLM API (retryable on transient errors).
            # TransientLLMError is only raised by chat() — BEFORE any stream
            # chunks are processed.  Mid-stream connection drops are raw SDK
            # exceptions that propagate immediately (no partial-state retry).
            try:
                raw_response = self.sudo().model_id.chat(**chat_kwargs)
            except Exception as exc:
                if self._is_transient_llm_error(exc) and attempt < max_retries - 1:
                    wait = backoff_base * (2**attempt)
                    _logger.warning(
                        "LLM API transient error (attempt %d/%d): %s — retrying in %ss",
                        attempt + 1,
                        max_retries,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                raise

            # Phase 2: process the response (NOT retryable).
            # If the stream fails mid-way, the error propagates immediately —
            # the message has already been partially posted and cannot be
            # retried.  _handle_streaming_response handles error chunks
            # internally (yields error events, returns the partial message).
            if use_streaming:
                assistant_message = yield from self._handle_streaming_response(
                    raw_response,
                    loop_control_check=loop_control_check,
                )
            else:
                assistant_message = yield from self._handle_non_streaming_response(
                    raw_response,
                )
            return assistant_message

    def _is_transient_llm_error(self, exc):
        """Classify an LLM API exception as transient (retryable) or not.

        The OpenAI SDK retries at the HTTP level (``max_retries=3`` in
        ``openai_get_client``).  If the SDK raises after exhausting its own
        retries, this method classifies the error so the caller's retry loop
        can retry the entire ``chat()`` call.

        Classification is generic (no SDK-specific imports) — it checks the
        exception class name and the ``status_code`` attribute.  Works with
        the OpenAI SDK and any OpenAI-compatible provider.

        Transient (retry):
            - ``TransientLLMError`` (explicitly classified)
            - Connection / timeout errors (class name heuristic)
            - HTTP 5xx (``status_code >= 500``)
            - HTTP 429 rate limit (``status_code == 429``)

        Non-transient (propagate):
            - HTTP 400 (bad request), 401 (auth), 403 (forbidden), 404
            - Odoo ``UserError`` / ``ValidationError`` (data issues)
            - Any other ``Exception`` not matching the transient criteria
            - ``GenerationCancelled`` (``BaseException`` — never reaches here)
        """
        if isinstance(exc, TransientLLMError):
            return True
        exc_name = type(exc).__name__
        if "Timeout" in exc_name or "Connection" in exc_name:
            return True
        status = getattr(exc, "status_code", None)
        if status is not None and (status >= 500 or status == 429):
            return True
        return False

    def _prepare_chat_kwargs(self, message_history, use_streaming, final_answer=False):
        """Prepare chat kwargs for provider. Can be overridden by extensions.

        `final_answer=True` appends a transient nudge (agentic loop cap reached)
        so the model answers from what it has instead of calling more tools. Tools
        stay available on purpose (see generate_messages).
        """
        kwargs = {
            "messages": message_history,
            "tools": self.tool_ids,
            "stream": use_streaming,
            "prepend_messages": self.get_prepend_messages(),
        }
        if final_answer:
            kwargs["append_messages"] = [
                {
                    "role": "system",
                    "content": (
                        "You now have enough information from the tools. Answer my "
                        "question directly and completely using the tool results "
                        "above, in my language. Do NOT call any more tools."
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

    def _handle_streaming_response(self, stream_response, *, loop_control_check=None):
        """Handle streaming response from LLM provider with tool call processing.

        The between-chunks cancel check is **mode-aware** (Phase 3): in
        ``after_turn`` mode the check does NOT raise between chunks — the LLM
        response is allowed to finish so the in-flight tool call's savepoint
        commits or rolls back cleanly. The cancel is then caught at the next
        tool-call boundary or ``while`` iteration boundary. In ``immediate``
        mode (or when no ``mode`` is returned), the check raises at every
        chunk boundary (the Phase 2 behaviour, reserved for hard kills).
        """
        message = None
        accumulated_content = ""
        collected_tool_calls = []

        for chunk in stream_response:
            if self._check_loop_control_streaming(loop_control_check):
                raise GenerationCancelled(
                    _("Generation cancelled by loop-control hook."),
                )
            # Initialize message on first content
            if message is None and chunk.get("content"):
                message = self.message_post(
                    body="Thinking...",
                    llm_role="assistant",
                    author_id=False,
                )
                yield {"type": "message_create", "message": message.to_store_format()}

            # Handle content streaming
            if chunk.get("content"):
                accumulated_content += chunk["content"]
                message.write({"body": self._process_llm_body(accumulated_content)})
                yield {"type": "message_chunk", "message": message.to_store_format()}

            # Collect tool calls for processing
            if chunk.get("tool_calls"):
                collected_tool_calls.extend(chunk["tool_calls"])
                _logger.debug(
                    f"Collected {len(chunk['tool_calls'])} tool calls from chunk",
                )

            # Handle errors
            if chunk.get("error"):
                yield {"type": "error", "error": chunk["error"]}
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

        return message

    def _handle_non_streaming_response(self, response):
        """Handle non-streaming response from LLM provider."""
        # Extract content and tool calls from response
        content = response.get("content", "")
        tool_calls = response.get("tool_calls", [])

        if not content and not tool_calls:
            content = "No response from model"

        # Prepare body_json with tool calls if present
        body_json = {"tool_calls": tool_calls} if tool_calls else None

        # Create assistant message with body_json (handled by message_post override)
        assistant_message = self.message_post(
            body=self._process_llm_body(content) if content else "",
            body_json=body_json,
            llm_role="assistant",
            author_id=False,
        )

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
