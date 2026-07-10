import contextlib
import json
import logging

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
    _inherit = ["mail.thread"]
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
        """Set default title if not provided"""
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

        # Convert markdown to HTML if needed (only for assistant messages)
        # User messages should be plain text, tool messages use body_json
        if kwargs.get("body") and llm_role == "assistant":
            kwargs["body"] = self._process_llm_body(kwargs["body"])

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
        """P-CHAT M6: no-op — AI chat threads never notify followers.

        FULL OVERRIDE (no ``super``): an AI chat thread's "followers" are
        not meaningful — the asking user reads answers live through the
        chat UI (SSE stream + the P-CHAT M3 bus reload), not through the
        inbox/email notification pipeline. Calling ``super`` would create
        ``mail.notification`` / ``mail.mail`` / bus push records and email
        followers, which is exactly the spam an AI chat must avoid.
        Combined with ``mail_create_nosubscribe`` (set in ``message_post``)
        this guarantees zero follower notifications for any AI message
        (user / assistant / tool / error). Other ``mail.thread`` models
        are unaffected — this override is on ``llm.thread`` only.
        """
        return None

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
                message = self.message_post(
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
                expert_count = (
                    sum(len(r.expert_run_ids) for r in runs) if runs else 0
                )
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
        - The name is still a default placeholder ("New Chat #\u2026" or "AI Chat -\u2026")

        Uses a non-streaming LLM call via ``simple_completion`` \u2014 lightweight,
        no mail.message overhead. Failures are logged, never interrupting
        the generation flow.

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
