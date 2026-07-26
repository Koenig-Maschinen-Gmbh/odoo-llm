import contextlib
import json
import logging
import threading

import emoji
import markdown2
from markupsafe import Markup
from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class RelatedRecordProxy:
    """
    A proxy object that provides clean access to related record fields in Jinja templates.
    Usage in templates: {{ related_record.get_field('field_name', 'default_value') }}
    When called directly, returns JSON with model name, id, and display name.
    """

    def __init__(self, record):
        self._record = record

    def get_field(self, field_name, default=""):
        """
        Get a field value from the related record.

        Args:
            field_name (str): The field name to access
            default: Default value if field doesn't exist or is empty

        Returns:
            The field value, or default if not available
        """
        if not self._record:
            return default

        try:
            if hasattr(self._record, field_name):
                value = getattr(self._record, field_name)

                # Handle different field types
                if value is None:
                    return default
                if isinstance(value, bool):
                    return value  # Keep as boolean for Jinja
                if hasattr(value, "name"):  # Many2one field
                    return value.name
                if hasattr(value, "mapped"):  # Many2many/One2many field
                    return value.mapped("name")
                return value
            _logger.debug(
                "Field '%s' not found on record %s",
                field_name,
                self._record,
            )
            return default

        except Exception as e:
            _logger.error(
                "Error getting field '%s' from record: %s",
                field_name,
                e,
            )
            return default

    def __getattr__(self, name):
        """Allow direct attribute access as fallback"""
        return self.get_field(name)

    def __bool__(self):
        """Return True if we have a record"""
        return bool(self._record)

    def __str__(self):
        """When called by itself, return JSON of model name, id, and display name"""
        if not self._record:
            return json.dumps({"model": None, "id": None, "display_name": None})

        return json.dumps(
            {
                "model": self._record._name,
                "id": self._record.id,
                "display_name": getattr(
                    self._record,
                    "display_name",
                    str(self._record),
                ),
            },
        )

    def __repr__(self):
        """Same as __str__ for consistency"""
        return self.__str__()


class LLMThread(models.Model):
    _name = "llm.thread"
    _description = "LLM Chat Thread"
    _inherit = ["mail.thread", "bus.listener.mixin"]
    _order = "write_date DESC"

    name = fields.Char(
        string="Title",
        required=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        default=lambda self: self.env.user,
        required=True,
        ondelete="restrict",
    )
    provider_id = fields.Many2one(
        "llm.provider",
        string="Provider",
        required=True,
        ondelete="restrict",
    )
    model_id = fields.Many2one(
        "llm.model",
        string="Model",
        required=True,
        domain="[('provider_id', '=', provider_id), ('model_use', 'in', ['chat', 'multimodal'])]",
        ondelete="restrict",
    )
    active = fields.Boolean(default=True)

    # UI-11 — expert sub-threads (created by koenig_ai_orchestrator's
    # ``_create_and_drain_sub_thread``) are hidden from the sidebar by
    # default. Set at creation time so the thread is filtered out
    # immediately — even while the expert is still running and before the
    # ``active = False`` archiving lands in the ``finally`` block. The flag
    # lives here (not in the orchestrator) because the sidebar filter and
    # the store payload read it from the base thread model.
    is_expert_subthread = fields.Boolean(
        string="Expert Sub-Thread",
        default=False,
        help="Thread created by an orchestration expert run. Hidden from the "
        "sidebar by default; reveal via the 'Show expert threads' toggle.",
    )

    # Updated fields for related record reference
    model = fields.Char(
        string="Related Document Model",
        help="Technical name of the related model",
    )
    res_id = fields.Many2oneReference(
        string="Related Document ID",
        model_field="model",
        help="ID of the related record",
    )

    tool_ids = fields.Many2many(
        "llm.tool",
        string="Available Tools",
        help="Tools that can be used by the LLM in this thread",
    )

    # P-UX: user-manageable colored tags (sidebar grouping + badges).
    # NOTE: no explicit relation table here on purpose. ``llm.thread`` has a
    # prototype-inheriting transient child ``llm.thread.mock`` (``_name`` +
    # ``_inherit = "llm.thread"`` in ``llm_assistant``) which copies every
    # M2M field. With an EXPLICIT relation table the copy would collide
    # ("Many2many fields ... use the same table and columns"); with an AUTO
    # relation each model gets its own table (``llm_thread_llm_thread_tag_rel``
    # here, ``llm_thread_mock_llm_thread_tag_rel`` on the mock) — same reason
    # the existing ``tool_ids`` M2M is auto. There is intentionally no inverse
    # ``thread_ids`` field on ``llm.thread.tag``: an inverse would need the
    # same explicit relation and would collide the same way.
    tag_ids = fields.Many2many(
        "llm.thread.tag",
        string="Tags",
    )

    attachment_ids = fields.Many2many(
        "ir.attachment",
        string="All Thread Attachments",
        compute="_compute_attachment_ids",
        store=True,
        help="All attachments from all messages in this thread",
    )

    attachment_count = fields.Integer(
        string="Thread Attachments",
        compute="_compute_attachment_count",
        store=True,
        help="Total number of attachments in this thread",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Set default title if not provided, then broadcast via bus.

        After creation, each new thread is broadcast to its owner via
        ``user._bus_send_store(thread, as_thread=True)`` →
        ``mail.record/insert`` → WebSocket bus → the owner's sidebar
        updates immediately (OCB-canonical pattern, ref:
        ``discuss_channel.py:1183``).
        """
        needs_unique_name = []

        for vals in vals_list:
            if not vals.get("name"):
                # If linked to a record, use its display name
                if vals.get("model") and vals.get("res_id"):
                    try:
                        record = self.env[vals["model"]].browse(vals["res_id"])
                        if record.exists():
                            vals["name"] = f"AI Chat - {record.display_name}"
                        else:
                            # Record doesn't exist, use technical format
                            vals["name"] = f"AI Chat - {vals['model']}#{vals['res_id']}"
                    except Exception:
                        # Model doesn't exist or access error, use technical format
                        vals["name"] = f"AI Chat - {vals['model']}#{vals['res_id']}"
                else:
                    # Generic name - will add unique ID after creation
                    vals["name"] = "New Chat"
                    needs_unique_name.append(True)
            else:
                needs_unique_name.append(False)

        records = super().create(vals_list)

        # Update generic thread names to include unique ID
        for record, needs_update in zip(records, needs_unique_name):
            if needs_update:
                record.name = f"New Chat #{record.id}"

        # Broadcast new threads to their owners via the WebSocket bus so the
        # sidebar updates immediately (OCB ref: discuss_channel.py:1183).
        # Skip in test mode — TransactionCase forbids commits and the bus
        # precommit hooks would raise.
        if not getattr(threading.current_thread(), "testing", False):
            for record in records:
                if record.user_id:
                    try:
                        record.user_id._bus_send_store(record, as_thread=True)
                    except Exception:
                        _logger.debug(
                            "Failed to broadcast new llm.thread %s via bus",
                            record.id,
                            exc_info=True,
                        )

        return records

    @api.depends("message_ids.attachment_ids")
    def _compute_attachment_ids(self):
        """Compute all attachments from all messages in this thread."""
        for thread in self:
            # Get all attachments from all messages in this thread
            all_attachments = thread.message_ids.mapped("attachment_ids")
            thread.attachment_ids = [(6, 0, all_attachments.ids)]

    @api.depends("attachment_ids")
    def _compute_attachment_count(self):
        """Compute the total number of attachments in this thread."""
        for thread in self:
            thread.attachment_count = len(thread.attachment_ids)

    # ============================================================================
    # MESSAGE POST OVERRIDES - Clean integration with mail.thread
    # ============================================================================

    @api.returns("mail.message", lambda value: value.id)
    def message_post(
        self,
        *,
        llm_role=None,
        message_type="comment",
        body_json=None,
        is_error=False,
        **kwargs,
    ):
        """Override to handle LLM-specific message types and metadata.

        Args:
            llm_role (str): The LLM role ('user', 'assistant', 'tool', 'system')
                           If provided, will automatically set the appropriate subtype
            body_json (dict): JSON body for tool calls - will be set after message creation
            is_error (bool): If True, marks message as error (excluded from LLM context)
        """

        # P-CHAT M6: AI chat threads don't have meaningful followers — the
        # user interacts live via the chat UI (SSE / bus reload), never via
        # inbox/email. Suppress the follower-notification pipeline:
        # ``mail_create_nosubscribe`` prevents auto-subscribing the poster,
        # and ``_notify_thread`` (overridden below) is a no-op so no
        # ``mail.notification`` / ``mail.mail`` / bus push is created.
        # Generic — any AI chat product wants this, not just König.
        if not self.env.context.get("mail_create_nosubscribe"):
            self = self.with_context(mail_create_nosubscribe=True)

        # Convert LLM role to subtype_xmlid if provided
        if llm_role:
            _, role_to_id = self.env["mail.message"].get_llm_roles()
            if llm_role in role_to_id:
                # Get the xmlid from the role
                subtype_xmlid = f"llm.mt_{llm_role}"
                kwargs["subtype_xmlid"] = subtype_xmlid

        # Handle LLM-specific subtypes and email_from generation
        if not kwargs.get("author_id") and not kwargs.get("email_from"):
            kwargs["email_from"] = self._get_llm_email_from(
                kwargs.get("subtype_xmlid"),
                kwargs.get("author_id"),
                llm_role,
            )

        # Convert markdown to HTML if needed (only for assistant messages).
        # User messages should be plain text, tool messages use body_json.
        # Wrap in Markup so OCB's mail.thread.message_post (mail_thread.py:2366)
        # does NOT escape the HTML we just produced — passing a plain ``str``
        # body would yield ``<p>&lt;p&gt;Thinking...&lt;/p&gt;</p>`` (double
        # escape, since ``escape(str)`` + sanitize wraps the escaped text in
        # ``<p>``).  ``_process_llm_body`` already skips ``Markup`` input, so
        # wrapping the OUTPUT here is the safe single point of truth.
        # Ref: TRACKER_2026-07-25_UI_RESEARCH.md §4 (FIX-4a).
        if kwargs.get("body") and llm_role == "assistant":
            kwargs["body"] = Markup(self._process_llm_body(kwargs["body"]))

        # Create the message using standard mail.thread flow (without body_json)
        message = super().message_post(message_type=message_type, **kwargs)

        # Set additional fields after message creation
        write_vals = {}
        if body_json:
            write_vals["body_json"] = body_json
        if is_error:
            write_vals["is_error"] = True
        if write_vals:
            message.write(write_vals)

        return message

    def _get_llm_email_from(self, subtype_xmlid, author_id, llm_role=None):
        """Generate appropriate email_from for LLM messages."""
        if author_id:
            return None  # Let standard flow handle it

        provider_name = self.provider_id.name
        model_name = self.model_id.name

        if subtype_xmlid == "llm.mt_tool" or llm_role == "tool":
            return f"Tool <tool@{provider_name.lower().replace(' ', '')}.ai>"
        if subtype_xmlid == "llm.mt_assistant" or llm_role == "assistant":
            return f"{model_name} <ai@{provider_name.lower().replace(' ', '')}.ai>"

        return None

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """P-CHAT M6: no follower notifications, but broadcast via bus for live UI.

        FULL OVERRIDE (no ``super``): an AI chat thread's "followers" are
        not meaningful — the asking user reads answers live through the
        chat UI (WebSocket bus + SSE stream), not through the inbox/email
        notification pipeline. Calling ``super`` would create
        ``mail.notification`` / ``mail.mail`` / bus push records and email
        followers, which is exactly the spam an AI chat must avoid.
        Combined with ``mail_create_nosubscribe`` (set in ``message_post``)
        this guarantees zero follower notifications for any AI message
        (user / assistant / tool / error). Other ``mail.thread`` models
        are unaffected — this override is on ``llm.thread`` only.

        **Bus broadcast (2026-07-19):** Although we skip ``super`` (no
        email/inbox), we DO broadcast the new message via the WebSocket
        bus so the chat UI updates live. This follows the OCB-canonical
        pattern (ref: ``discuss_channel.py:649-658``): send the message
        data via ``_bus_send_store`` (→ ``mail.record/insert``) and a
        custom ``llm.thread/new_message`` event. The browser receives
        both via WebSocket and inserts the message into the OWL store
        → the message appears immediately without a manual reload.
        """
        from odoo.addons.mail.tools.discuss import Store

        # No super() — suppress email/inbox/follower notifications.
        # But broadcast the new message via the WebSocket bus for live UI.

        # FIX-4b (TRACKER_2026-07-25_UI_RESEARCH.md §4): skip the bus broadcast
        # for streaming placeholders. The placeholder is posted with
        # ``llm_streaming_placeholder=True`` context; its body is stale by the
        # time the main transaction commits (the precommit payload is baked at
        # post time but flushed at the final commit — AFTER all progressive
        # chunk broadcasts). If we broadcast it here, the stale escaped
        # placeholder overwrites the streamed answer at the end of the run.
        # The orchestration bridge handles ``message_create`` itself (independent
        # cursor, CURRENT body) so the placeholder still appears live — just
        # without the stale end-of-run flush.
        if self.env.context.get("llm_streaming_placeholder"):
            _logger.debug(
                "Skipping bus broadcast for streaming placeholder msg %s",
                message.id if message else None,
            )
            return None

        try:
            # mail.record/insert — inserts message into the OWL store
            self._bus_send_store(message)
            # llm.thread/new_message — custom event for the JS to trigger
            # reloadThreadMessages as a belt-and-suspenders fallback
            payload = {"data": Store(message).get_result(), "id": self.id}
            self._bus_send("llm.thread/new_message", payload)
        except Exception:
            # V9 (2026-07-26): was _logger.debug — a broadcast failure here is
            # SILENT data loss for the live UI (the message never reaches the
            # store without a manual refetch) and must be visible in the log.
            _logger.warning(
                "Failed to broadcast llm.thread message %s via bus",
                message.id if message else None,
                exc_info=True,
            )
        return None

    def _message_compute_author(
        self, author_id=None, email_from=None, raise_on_email=True
    ):
        """Override to allow authorless notification messages without a configured sender email.

        LLM threads intentionally suppress email/inbox follower notifications
        (see ``_notify_thread`` override — no ``super()``, no email pipeline).
        Instead, messages are broadcast live via the WebSocket bus
        (``_bus_send_store`` → ``mail.record/insert``).  Progress and system
        messages (``message_type='notification'``, ``subtype_xmlid='mail.mt_note'``)
        are posted with ``author_id=False`` and no ``email_from`` — they have no
        human sender.  Without this override, ``mail.thread._message_compute_author``
        raises ``UserError("Unable to send message, please configure the sender's
        email address.")`` whenever ``email_from`` is empty and
        ``raise_on_email=True`` (the default), which breaks every authorless
        notification on ``llm.thread``.

        The fix mirrors the OCB ``discuss.channel`` precedent
        (ref: ``discuss_channel.py:696-697``): delegate to ``super()`` with
        ``raise_on_email=False`` so that authorless messages are accepted
        silently instead of raising.

        This is safe because:

        * ``_notify_thread`` is already a no-op for email — no ``mail.mail``
          or ``mail.notification`` records are created, so a missing
          ``email_from`` cannot cause an email-sending failure.
        * The bus broadcast (the real delivery path) does not use
          ``email_from`` at all.
        * Callers that explicitly pass ``author_id`` or ``email_from``
          (e.g. ``message_post`` with ``llm_role``) are unaffected —
          ``super()`` still resolves those normally.
        """
        return super()._message_compute_author(
            author_id=author_id, email_from=email_from, raise_on_email=False
        )

    def _bus_channel(self):
        """Route bus events to the thread owner's partner channel.

        This follows the OCB delegation pattern (ref:
        ``discuss_channel_member.py:244`` — delegates to partner). Every
        authenticated user is auto-subscribed to their own partner channel
        (``ir_websocket._build_bus_channel_list`` line 70), so sending to
        ``user_id.partner_id`` delivers to the owner's browser via WebSocket.
        """
        self.ensure_one()
        return self.user_id.partner_id

    def _process_llm_body(self, body):
        """Process body content for LLM messages (markdown to HTML conversion).

        Skips processing if body is already Markup (pre-formatted HTML).

        KOENIG fork fixes (2026-07-07):
        - ``emoji.emojize`` instead of ``demojize``: models emit real emoji;
          demojize converted them INTO ``:factory:``-style text markers in
          the rendered answer. emojize renders shortcodes to emoji and
          leaves real emoji untouched.
        - ``tables`` extra: pipe tables (the models' standard table format)
          previously rendered as raw ``| a | b |`` text. ``html-classes``
          maps them onto Bootstrap table styling.
        - P-CHAT M1 (2026-07-07): extras aligned with the wiki content
          converter (``koenig_wiki_outline_importer`` ``ContentConverter``)
          so chat answers render at the same markdown fidelity as wiki pages.
          Added ``task_list`` (``- [ ]`` / ``- [x]`` checkboxes),
          ``code-friendly`` (disables ``__bold__`` / ``_italic_`` so code
          identifiers with underscores survive), and ``break-on-newline``
          (single newlines render as ``<br>`` — the ChatGPT/Claude chat
          convention). ``header-ids`` is deliberately NOT enabled: the
          ``id`` anchors it injects on every heading are noise in a chat
          bubble (no in-page navigation target) and would clutter the
          stored HTML. ``html-classes`` table mapping kept.
        """
        if not body or isinstance(body, Markup):
            return body
        return markdown2.markdown(
            emoji.emojize(body, language="alias"),
            extras={
                "tables": None,
                "fenced-code-blocks": None,
                "strike": None,
                "task_list": None,
                "code-friendly": None,
                "break-on-newline": None,
                "html-classes": {"table": "table table-sm"},
            },
        )

    # ============================================================================
    # STREAMING MESSAGE CREATION
    # ============================================================================

    def message_post_from_stream(
        self,
        stream,
        llm_role,
        placeholder_text="…",
        **kwargs,
    ):
        """Create and update a message from a streaming response.

        Args:
            stream: Generator yielding chunks of response data
            llm_role (str): The LLM role ('user', 'assistant', 'tool', 'system')
            placeholder_text (str): Text to show while streaming

        Returns:
            message: The created/updated message record
        """
        message = None
        accumulated_content = ""

        for chunk in stream:
            # Initialize message on first content
            if message is None and chunk.get("content"):
                # FIX-4b: suppress the placeholder's bus broadcast — see
                # _notify_thread (llm_streaming_placeholder context key).
                # Ref: TRACKER_2026-07-25_UI_RESEARCH.md §4.
                message = self.with_context(
                    llm_streaming_placeholder=True
                ).message_post(
                    body=placeholder_text,
                    llm_role=llm_role,
                    author_id=False,
                    **kwargs,
                )
                yield {"type": "message_create", "message": message.to_store_format()}

            # Handle content streaming
            if chunk.get("content"):
                accumulated_content += chunk["content"]
                message.write({"body": self._process_llm_body(accumulated_content)})
                yield {"type": "message_chunk", "message": message.to_store_format()}

            # Handle errors
            if chunk.get("error"):
                yield {"type": "error", "error": chunk["error"]}
                return message

        # Final update for assistant message
        if message and accumulated_content:
            message.write({"body": self._process_llm_body(accumulated_content)})
            yield {"type": "message_update", "message": message.to_store_format()}

        return message

    # ============================================================================
    # GENERATION FLOW - Refactored to use message_post with roles
    # ============================================================================

    def generate(self, user_message_body=None, attachment_ids=None, **kwargs):
        """Main generation method with PostgreSQL advisory locking.

        Args:
            user_message_body: Optional message body. If not provided, will use
                              the latest message in the thread to start generation.
            attachment_ids: Optional list of ir.attachment IDs to attach to user message.
        """
        self.ensure_one()
        # GAP-D: tag spend rows with this thread's ID for per-thread cost
        # attribution (P-HUD). Don't override if already set (the orchestrator
        # sets it to the MAIN thread's ID before calling generate() on expert
        # sub-threads, so all spend rows in an orchestration run aggregate
        # under the main thread).
        if not self.env.context.get("llm_thread_id"):
            self = self.with_context(llm_thread_id=self.id)

        with self._generation_lock():
            last_message = False
            if user_message_body or attachment_ids:
                post_kwargs = {
                    "body": user_message_body or "",
                    "llm_role": "user",
                    "author_id": self.env.user.partner_id.id,
                }
                if attachment_ids:
                    post_kwargs["attachment_ids"] = attachment_ids
                last_message = self.message_post(**post_kwargs)
                yield {
                    "type": "message_create",
                    "message": last_message.to_store_format(),
                }

                # Check for unsupported attachments in the new message
                if last_message.attachment_ids:
                    unsupported = self._check_unsupported_attachments(last_message)
                    if unsupported:
                        # Mark the user message as error (excluded from LLM context)
                        last_message.write({"is_error": True})
                        # Show warning and return - don't call LLM
                        yield from self._handle_unsupported_attachments(unsupported)
                        return last_message

            last_message = yield from self.generate_messages(last_message)
            # P-UX Item 2: auto-rename thread from first user message.
            new_name = self._maybe_generate_name()
            if new_name:
                yield {
                    "type": "thread_update",
                    "thread": {"id": self.id, "model": "llm.thread", "name": new_name},
                }
            return last_message

    def _get_context_messages(self, limit=25):
        """Get recent LLM messages that will be sent as context.

        This is used to validate attachments in the ENTIRE context before
        sending to the LLM, not just the new message.

        Note: Error messages (is_error=True) are excluded from context.

        Args:
            limit: Maximum number of messages to retrieve (default: 25)

        Returns:
            mail.message recordset of recent LLM messages
        """
        self.ensure_one()
        domain = [
            ("model", "=", self._name),
            ("res_id", "=", self.id),
            ("llm_role", "!=", False),
            ("is_error", "=", False),  # Exclude error messages from context
        ]
        return self.env["mail.message"].search(
            domain,
            order="create_date DESC, id DESC",
            limit=limit,
        )

    def _check_unsupported_attachments(self, message=None):
        """Check a message for unsupported attachments.

        Args:
            message: Specific message to check. If None, checks entire context.

        Returns:
            List of unsupported attachments or empty list
        """
        self.ensure_one()

        if message:
            # Check only the specific message
            messages_to_check = message
        else:
            # Check all context messages (for model switch scenarios)
            context_messages = self._get_context_messages()
            messages_to_check = context_messages.filtered(
                lambda m: m.attachment_ids,
            )

        if not messages_to_check:
            return []

        provider_service = self.provider_id.service
        is_multimodal = self.model_id.model_use == "multimodal"

        return messages_to_check._get_unsupported_attachments(
            provider_service=provider_service,
            is_multimodal=is_multimodal,
        )

    def _handle_unsupported_attachments(self, unsupported):
        """Create an info message for unsupported attachments.

        The message has is_error=True so it's excluded from LLM context,
        allowing the conversation to continue normally.

        Args:
            unsupported: List of dicts with name, mimetype, reason

        Yields:
            Message events for the info message
        """
        # Build file list items
        file_items = "".join(
            f"<li><strong>{att['name']}</strong>: {att['reason']}</li>"
            for att in unsupported
        )

        # Build HTML message
        error_html = (
            f"<p>⚠️ <strong>{_('Unsupported file(s)')}</strong></p>"
            f"<ul>{file_items}</ul>"
            f"<p><em>{_('These files will be skipped.')}</em></p>"
        )

        # Post as info message (excluded from LLM context)
        error_message = self.message_post(
            body=Markup(error_html),
            llm_role="assistant",
            author_id=False,
            is_error=True,
        )
        yield {
            "type": "message_create",
            "message": error_message.to_store_format(),
        }
        return error_message

    def _post_error_message(self, error, title=None):
        """Post an error message to the thread (excluded from LLM context).

        This allows users to see API errors directly in the chat instead of
        only in server logs.

        Args:
            error: The exception or error string
            title: Optional title for the error message

        Returns:
            tuple: (error_message, event_dict) for yielding to the client
        """
        title = title or _("Error")
        error_str = str(error)

        # Build HTML error message
        error_html = (
            f"<p>❌ <strong>{title}</strong></p><p><code>{error_str}</code></p>"
        )

        error_message = self.message_post(
            body=Markup(error_html),
            llm_role="assistant",
            author_id=False,
            is_error=True,
        )

        event = {
            "type": "message_create",
            "message": error_message.to_store_format(),
        }
        return error_message, event

    def generate_messages(self, last_message=None):
        """Generate messages - to be overridden by llm_assistant module."""
        raise UserError(
            _("Please install the llm_assistant module for actual AI generation."),
        )

    def _candidate_tools(self):
        """Resolve the tool set for THIS thread BEFORE per-turn availability gates.

        Base fork (no assistant concept): the raw per-thread ``tool_ids`` column.
        ``llm_assistant`` overrides this to derive the candidate set LIVE from the
        bound assistant (explicit picks ∪ subscribed bundles) plus the tiny
        per-thread deviation (``tool_ids_extra`` / ``tool_ids_disabled``).

        The candidate set is then filtered by ``_effective_tools`` through each
        tool's ``_ai_is_available_for`` gate (active / capability / consent).

        Returns an ``llm.tool`` recordset.
        """
        self.ensure_one()
        return self.tool_ids

    def _effective_tools(self):
        """Resolve the tools available to the LLM for THIS turn (single seam).

        This is the ONE method the execution path reads to decide which tools
        the model may call — the chat request (``_prepare_chat_kwargs``) and the
        tool-call validation/execution in ``llm_tool.mail_message``. Reading a
        method (not the raw ``tool_ids`` column) lets ``llm_assistant`` resolve
        the set LIVE from the bound assistant, so a tool added to an assistant
        reaches every one of its threads immediately — there is no per-thread
        snapshot left to rot.

        CAP-02: the seam is now ``candidate ∩ per-turn availability gates``.
        ``_candidate_tools()`` yields the assistant/bundle/deviation-resolved set;
        each tool's ``_ai_is_available_for(self)`` gate (active / capability /
        consent) then filters it for this thread + calling user. The gate runs
        as the caller (never sudo) and stays cheap (prefetched-field reads only),
        so the hot path adds no query.

        Odoo-aligned: capability is resolved at the point of use, never copied
        per record — the same idiom as ``ir.model.access`` / ``ir.rule`` /
        group membership, which are evaluated live on every request.

        Returns an ``llm.tool`` recordset.
        """
        self.ensure_one()
        return self._candidate_tools().filtered(lambda t: t._ai_is_available_for(self))

    def get_context(self, base_context=None):
        context = {
            **(base_context or {}),
            "thread_id": self.id,
        }
        # Guard clause: skip if model or res_id not set
        if not self.model or not self.res_id:
            return context

        try:
            related_record = self.env[self.model].browse(self.res_id)
            if related_record:
                context["related_record"] = RelatedRecordProxy(related_record)
                context["related_model"] = self.model
                context["related_res_id"] = self.res_id
            else:
                context["related_record"] = None
                context["related_model"] = None
                context["related_res_id"] = None
        except Exception as e:
            _logger.warning(
                "Error accessing related record %s,%s: %s",
                self.model,
                self.res_id,
                e,
            )

        return context

    # ============================================================================
    # POSTGRESQL ADVISORY LOCK IMPLEMENTATION
    # ============================================================================

    def _acquire_thread_lock(self):
        """Acquire PostgreSQL advisory lock for this thread."""
        self.ensure_one()

        try:
            query = "SELECT pg_try_advisory_lock(%s)"
            self.env.cr.execute(query, (self.id,))
            result = self.env.cr.fetchone()

            if not result or not result[0]:
                raise UserError(
                    _(
                        "This conversation is currently generating a response. "
                        "Please wait for it to complete before sending another message.",
                    ),
                )

            _logger.info(f"Acquired advisory lock for thread {self.id}")

        except UserError:
            raise
        except OperationalError as e:
            _logger.error("Database error acquiring lock for thread %s: %s", self.id, e)
            raise UserError(
                _(
                    "Unable to process your request due to a system conflict. "
                    "Please wait a moment and try again.",
                ),
            ) from e
        except Exception as e:
            _logger.error(
                "Unexpected error acquiring lock for thread %s: %s",
                self.id,
                e,
            )
            raise UserError(
                _(
                    "Your request could not be processed. Please refresh the page and try again.",
                ),
            ) from e

    def _release_thread_lock(self):
        """Release PostgreSQL advisory lock for this thread."""
        self.ensure_one()

        try:
            query = "SELECT pg_advisory_unlock(%s)"
            self.env.cr.execute(query, (self.id,))
            result = self.env.cr.fetchone()

            success = result and result[0]
            if success:
                _logger.info(f"Released advisory lock for thread {self.id}")
            else:
                _logger.warning(f"Advisory lock for thread {self.id} was not held")

            return success

        except Exception as e:
            _logger.error(f"Error releasing lock for thread {self.id}: {e}")
            return False

    @contextlib.contextmanager
    def _generation_lock(self):
        """Context manager for thread generation with automatic lock cleanup."""
        self.ensure_one()

        self._acquire_thread_lock()

        try:
            _logger.info(f"Starting locked generation for thread {self.id}")
            yield self

        finally:
            released = self._release_thread_lock()
            if released:
                _logger.info(f"Finished locked generation for thread {self.id}")
            else:
                _logger.warning(f"Lock release failed for thread {self.id}")

    # ============================================================================
    # ODOO HOOKS AND CLEANUP
    # ============================================================================

    # ============================================================================
    # STORE INTEGRATION - For mail.store compatibility
    # ============================================================================

    def _thread_store_dict(self, thread):
        """Build the per-thread data dict shipped to the mail store / RPC.

        Shared by ``_thread_to_store`` (store.add) and ``search_threads``
        (RPC return) so the two paths never drift in shape.

        P-UX: ``active`` and ``tag_ids`` are sent **unconditionally**. The
        store merges inserts key-by-key, so omitting a key when empty would
        leave a stale value on the JS record after the last tag is removed
        or the thread is unarchived.
        """
        thread_data = {
            "id": thread.id,
            "model": "llm.thread",
            "name": thread.name,  # Essential for UI display
            "write_date": thread.write_date,  # For sorting in thread list
            "channel_type": "llm_chat",  # Custom type for LLM threads
            "active": thread.active,  # P-UX: archive filter
            # UI-11: sidebar expert-subthread filter. Sent unconditionally
            # (same store-merge rationale as ``active`` above).
            "is_expert_subthread": thread.is_expert_subthread,
            "tag_ids": [  # P-UX: sidebar badges + tag filtering
                {
                    "id": tag.id,
                    "name": tag.name,
                    "color": tag.color,
                    "model": "llm.thread.tag",
                }
                for tag in thread.tag_ids
            ],
        }

        # Related record fields (for linking threads to Odoo records)
        # Use res_model to avoid conflict with "model": "llm.thread"
        if thread.model:
            thread_data["res_model"] = thread.model
        if thread.res_id:
            thread_data["res_id"] = thread.res_id

        # Add LLM-specific fields using proper Store.one/Store.many format
        if thread.provider_id:
            thread_data["provider_id"] = {
                "id": thread.provider_id.id,
                "name": thread.provider_id.name,
                "model": "llm.provider",
            }

        if thread.model_id:
            thread_data["model_id"] = {
                "id": thread.model_id.id,
                "name": thread.model_id.name,
                "model": "llm.model",
            }

        if thread.tool_ids:
            thread_data["tool_ids"] = [
                {"id": tool.id, "name": tool.name, "model": "llm.tool"}
                for tool in thread.tool_ids
            ]

        return thread_data

    # pylint: disable=missing-return  # void store hook: mutates `store`, no return value
    def _thread_to_store(self, store, **kwargs):
        """Extend base _thread_to_store to include LLM-specific fields."""
        super()._thread_to_store(store, **kwargs)
        for thread in self:
            store.add("mail.thread", self._thread_store_dict(thread))

    def _thread_to_store_data(self):
        """Return the list of per-thread store dicts (RPC-friendly).

        Same shape as ``_thread_to_store`` but for ``search_threads`` return
        value — the JS caller merges the results into the mail store via
        ``mailStore.insert()``.
        """
        return [self._thread_store_dict(thread) for thread in self]

    @api.model
    def get_thread_store_data(self, thread_ids):
        """Return the store dicts for the given thread IDs (FIX-3).

        Called via RPC from the client ``createNewThread`` flow right after
        ``create`` returns the new thread ID. The client inserts the result
        into ``mailStore`` and calls ``selectThread`` immediately — no
        dependence on the bus broadcast or a heavy ``init_messaging``
        re-fetch (which under load takes 50+ seconds and yanks the active
        thread; see TRACKER_2026-07-25_UI_RESEARCH.md §1 RC-1b + §3).

        Returns ``{"mail.thread": [store_dict, ...]}`` ready for
        ``mailStore.insert()``.
        """
        threads = self.browse(thread_ids).exists()
        return {"mail.thread": threads._thread_to_store_data()}

    def get_context_stats(self, thread_id=None):
        """FIX-2 (TRACKER_2026-07-25_UI_RESEARCH.md §2): context usage stats
        for the HUD. Returns the context window, last real prompt tokens,
        and an estimated breakdown so the user can see how full the context
        is and what it's used for (Kilo-Code-style context meter).

        Called via RPC from the LLMThreadHud component alongside
        ``get_thread_stats``. Gracefully degrades when ``koenig_ai_core``
        (trace model, context window field, estimator) is not installed —
        returns zeros so the HUD simply hides the context segment.

        Public (no leading underscore) so it can be called remotely.

        Args:
            thread_id (int|list|None): The thread ID (may be wrapped in a
                list by the RPC layer).

        Returns:
            dict: ``{context_window, reserved_output, last_prompt_tokens,
            last_prompt_at, estimate: {system, tools, summary, window_msgs,
            window_tokens, total}}``. ``last_prompt_tokens`` is the ONLY
            truthful number for "how full is the context right now" — the
            estimate breakdown is labeled "~" in the UI.
        """
        if thread_id is not None:
            if isinstance(thread_id, (list, tuple)):
                thread_id = thread_id[0] if thread_id else None
            thread = self.browse(thread_id) if thread_id else self
        else:
            thread = self
        thread = thread.sudo()
        thread.ensure_one()

        # Context window + reserved output from the model record (added by
        # koenig_ai_core — may be absent if koenig_ai_core is not installed).
        # F4 (2026-07-26): resolve via the ASSISTANT's configured model first
        # — the assistant is the configuration source of truth (the thread's
        # model_id is only synced from it at creation/onchange and can drift
        # when the admin reconfigures the assistant). Fall back to the
        # thread's own model when no assistant is set.
        context_window = 0
        reserved_output = 0
        # ``assistant_id`` comes from llm_assistant — guard its existence so
        # a bare llm_thread install keeps working.
        assistant = thread.assistant_id if "assistant_id" in thread._fields else None
        effective_model = (
            assistant.model_id if assistant and assistant.model_id else thread.model_id
        )
        if effective_model:
            context_window = getattr(
                effective_model, "koenig_context_window", 0
            ) or 0
            reserved_output = getattr(
                effective_model, "koenig_max_output_tokens", 0
            ) or 0

        # Last real prompt tokens from the trace model (koenig.ai.llm.trace
        # in koenig_ai_core). This is the headline number — real usage_input
        # from the latest LLM call, not an estimate.
        last_prompt_tokens = 0
        last_prompt_at = None
        Trace = thread.env.get("koenig.ai.llm.trace")
        if Trace is not None:
            try:
                trace = Trace.sudo()._current_for_thread(thread.id)
                if trace:
                    last_prompt_tokens = trace.usage_input or 0
                    last_prompt_at = trace.create_date
            except Exception:
                _logger.debug(
                    "get_context_stats: trace lookup failed", exc_info=True
                )

        # Estimated breakdown (labeled "~" in the UI — chars/4 underestimates
        # ~2x vs real usage_input, so categories are indicative only).
        estimate = {
            "system": 0,
            "tools": 0,
            "summary": 0,
            "window_msgs": 0,
            "window_tokens": 0,
            "total": 0,
        }
        try:
            # System + prepend messages.
            provider = thread.provider_id
            est_tokens = getattr(provider, "_koenig_estimate_message_tokens", None)
            if est_tokens:
                # System/prompt tokens.
                prompt_text = ""
                if thread.assistant_id and thread.assistant_id.prompt_id:
                    prompt_text = thread.assistant_id.prompt_id.content or ""
                if prompt_text:
                    estimate["system"] = est_tokens(
                        [{"role": "system", "content": prompt_text}], {}
                    )
                # Message window tokens.
                messages = thread.message_ids.filtered(
                    lambda m: not m.is_error
                    and m.llm_role in ("user", "assistant", "tool")
                ).sorted("id")[-25:]
                estimate["window_msgs"] = len(messages)
                estimate["window_tokens"] = est_tokens(messages, {})
                # Summary tokens (if llm_summary exists).
                summary = getattr(thread, "llm_summary", False)
                if summary:
                    estimate["summary"] = est_tokens(
                        [{"role": "system", "content": summary}], {}
                    )
                # Tools schema tokens.
                tools = thread.tool_ids if hasattr(thread, "tool_ids") else None
                if tools:
                    tool_chars = sum(
                        len(t.get_tool_definition().get("function", {}).get("description", "") or "")
                        + len(str(t.get_tool_definition().get("function", {}).get("parameters", {})))
                        for t in tools
                        if hasattr(t, "get_tool_definition")
                    )
                    estimate["tools"] = max(1, tool_chars // 4)
                estimate["total"] = (
                    estimate["system"]
                    + estimate["tools"]
                    + estimate["summary"]
                    + estimate["window_tokens"]
                )
        except Exception:
            _logger.debug(
                "get_context_stats: estimate breakdown failed", exc_info=True
            )

        return {
            "context_window": context_window,
            "reserved_output": reserved_output,
            "last_prompt_tokens": last_prompt_tokens,
            "last_prompt_at": last_prompt_at,
            "estimate": estimate,
        }

    def get_thread_stats(self, thread_id=None):
        """Return cost/token/expert statistics for the P-HUD display.

        Called via RPC from the LLMThreadHud component. Accepts a thread_id
        argument (the RPC layer passes it as a positional arg).

        Aggregates ``koenig.ai.spend`` rows tagged with this thread's ID
        (GAP-D: spend rows are tagged via the ``llm_thread_id`` context).
        Falls back to 0 if the spend model isn't installed.

        Public (no leading underscore) so it can be called remotely — Odoo 18's
        ``get_public_method`` rejects ``_``-prefixed methods from RPC with
        ``AccessError: Private methods ... cannot be called remotely``.

        Args:
            thread_id (int|list|None): The thread ID. May be wrapped in a
                list by the RPC layer.

        Returns:
            dict: stats for the P-HUD component.
        """
        if thread_id is not None:
            if isinstance(thread_id, (list, tuple)):
                thread_id = thread_id[0] if thread_id else None
            thread = self.browse(thread_id) if thread_id else self
        else:
            thread = self
        thread.ensure_one()
        Spend = thread.env.get("koenig.ai.spend")
        tokens = 0
        cost = 0.0
        currency = ""
        monthly_spend = 0.0
        monthly_budget = 0.0
        expert_count = 0
        is_running = False

        if Spend is not None:
            # Per-thread spend (tagged via GAP-D context).
            rows = Spend.sudo().search([("thread_id", "=", thread.id)])
            tokens = sum(r.tokens for r in rows if r.tokens)
            cost = sum(r.cost for r in rows if r.cost)
            if rows:
                currency = rows[0].currency_label or ""

            # Monthly spend for the current user (all providers).
            period = Spend._current_period()
            monthly_groups = Spend.sudo().read_group(
                [("user_id", "=", thread.env.uid), ("period", "=", period)],
                ["cost:sum"],
                [],
            )
            monthly_spend = (monthly_groups and monthly_groups[0].get("cost")) or 0.0

            # Monthly budget (from the thread's provider).
            if thread.provider_id and hasattr(
                thread.provider_id, "koenig_user_monthly_budget"
            ):
                monthly_budget = thread.provider_id.koenig_user_monthly_budget or 0.0
                if not currency and thread.provider_id.koenig_currency:
                    currency = thread.provider_id.koenig_currency

        # Expert dispatch count (if the orchestrator is installed).
        RunModel = thread.env.get("koenig.ai.orchestration.run")
        if RunModel is not None:
            try:
                runs = RunModel.sudo().search([("thread_id", "=", thread.id)])
                expert_count = sum(len(r.expert_run_ids) for r in runs) if runs else 0
                is_running = bool(
                    runs.filtered(lambda r: r.state in ("pending", "running"))
                )
            except Exception:
                pass

        return {
            "model_name": thread.model_id.name if thread.model_id else "",
            "tokens": tokens,
            "cost": round(cost, 6),
            "currency": currency,
            "monthly_spend": round(monthly_spend, 4),
            "monthly_budget": monthly_budget,
            "expert_count": expert_count,
            "is_running": is_running,
        }

    def _maybe_generate_name(self):
        """Auto-generate a concise thread title from the first user message.

        P-UX Item 2: ChatGPT-style auto-rename. After the first user message
        in a new thread, the LLM generates a 3-5 word title. Only fires when:
        - The thread has exactly 1 non-error user message (first interaction)
        - The name is still a default placeholder ("New Chat #…" or "AI Chat -…")

        Uses a non-streaming LLM call via ``simple_completion`` — lightweight,
        no mail.message overhead. Failures are logged, never interrupting
        the generation flow.

        P1-3 (tracker §3): passes ``reasoning_effort='none'`` + ``max_tokens=50``
        (explicit call-site override per D2 — stays ``none`` even when the model
        record is configured ``low``). Measured: ``none`` cuts the title call
        from 8–55 s to ~0.4 s (RESEARCH_2026-07-20 §1.1 — 1849 tokens → 12).

        Returns:
            str|None: The new title if generated, None otherwise.
        """
        self.ensure_one()
        # Guard: only rename if the name is still a default placeholder.
        # Frontend-generated names: "Chat 7/9/2026, 12:11:31 PM"
        # Backend-generated names: "New Chat #123", "AI Chat - ..."
        if not (
            self.name.startswith("New Chat")
            or self.name.startswith("AI Chat -")
            or self.name.startswith("Chat ")
        ):
            return None
        # Only if exactly 1 non-error user message exists (first interaction).
        user_msgs = self.env["mail.message"].search(
            [
                ("model", "=", self._name),
                ("res_id", "=", self.id),
                ("llm_role", "=", "user"),
                ("is_error", "=", False),
            ],
        )
        if len(user_msgs) != 1:
            return None
        body_text = html2plaintext(user_msgs[0].body or "")[:500]
        if not body_text.strip():
            return None
        try:
            title = self.sudo().model_id.simple_completion(
                prompt=f"Generate a concise 3-5 word title for this conversation:\n\n{body_text}",
                system_prompt=(
                    "You are a title generator. Generate a concise 3-5 word "
                    "title that summarizes the user's question or request. "
                    "Respond with ONLY the title, no quotes, no explanations, "
                    "no trailing punctuation."
                ),
                reasoning_effort="none",
                max_tokens=50,
            )
            if title:
                title = title.strip().strip("\"'").strip()
                if title and 3 <= len(title) <= 100:
                    self.sudo().write({"name": title})
                    return title
        except Exception as e:
            _logger.warning(
                "Failed to auto-generate thread name for thread %s: %s",
                self.id,
                e,
            )
        return None

    def generate_name(self):
        """Public RPC entry point for auto-generating a thread title.

        Called by the frontend after a thread's first message if the SSE
        path didn't yield a ``thread_update`` event (e.g., background
        orchestration runs).
        """
        for thread in self:
            thread._maybe_generate_name()
        return True

    @api.model
    def search_threads(self, search_term, limit=50):
        """Search the current user's threads by name or message content.

        Name matches are fast and owner-scoped by the ``llm_thread_rule_personal``
        record rule. Content matches scan ``mail.message`` rows
        (``model='llm.thread'`` + ``res_id``) — pre-scoped to the user's own
        threads first so the (expensive, document-based) ``mail.message``
        search stays both fast and unambiguous. ``active_test=False`` so
        archived threads are findable (search is the main way back to an
        archived thread). ``body`` is HTML, so a plain ``ilike`` can also
        match markup — acceptable for v1.
        """
        if not search_term or len(search_term.strip()) < 2:
            return []
        term = search_term.strip()
        # Name match (fast; record rule scopes to owner). ``active_test=False``
        # so archived threads are findable by name — search is the main way back
        # to an archived thread.
        name_matches = self.with_context(active_test=False).search(
            [("user_id", "=", self.env.uid), ("name", "ilike", term)],
            limit=limit,
        )
        # Content match — pre-scope to the user's own threads (cap at 500
        # so the IN clause stays bounded for users with many threads).
        if len(name_matches) < limit:
            own_thread_ids = (
                self.with_context(active_test=False)
                .search([("user_id", "=", self.env.uid)], limit=500)
                .ids
            )
            content_thread_ids = []
            if own_thread_ids:
                # mail.message.body is an unindexed HTML text column — cap
                # the scan at 100 matches to avoid a performance landmine on
                # common search terms ("the", "report", etc.).
                content_thread_ids = (
                    self.env["mail.message"]
                    .search(
                        [
                            ("model", "=", "llm.thread"),
                            ("res_id", "in", own_thread_ids),
                            ("body", "ilike", term),
                        ],
                        limit=100,
                    )
                    .mapped("res_id")
                )
            if content_thread_ids:
                remaining = limit - len(name_matches)
                # active_test=False so archived threads found via content match
                # are returned (search is the main way back to an archived thread).
                content_matches = self.with_context(active_test=False).search(
                    [("id", "in", content_thread_ids)],
                    limit=remaining,
                )
                # De-duplicate while preserving order (name matches first).
                seen = set(name_matches.ids)
                extras = self.browse()
                for thread in content_matches:
                    if thread.id not in seen:
                        extras |= thread
                        seen.add(thread.id)
                name_matches |= extras
        return name_matches[:limit]._thread_to_store_data()

    @api.ondelete(at_uninstall=False)
    def _unlink_llm_thread(self):
        unlink_ids = [record.id for record in self]
        self.env["bus.bus"]._sendone(
            self.env.user.partner_id,
            "llm.thread/delete",
            {"ids": unlink_ids},
        )
