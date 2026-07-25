from markupsafe import Markup

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMessagePostEscape(TransactionCase):
    """FIX-4a: assistant ``message_post`` bodies must NOT be double-escaped.

    Root cause (TRACKER_2026-07-25_UI_RESEARCH.md §4): the fork's
    ``message_post`` passed the ``str`` output of ``_process_llm_body`` to
    OCB's ``mail.thread.message_post``, which calls ``escape(str)`` on plain
    strings (mail_thread.py:2366) before sanitizing. The escaped text then
    gets wrapped in ``<p>`` by the Html-field sanitizer, producing the
    double-escaped ``<p>&lt;p&gt;Thinking...&lt;/p&gt;</p>`` the user saw
    replacing streamed answers at the end of a run.

    Fix: wrap the renderer output in ``Markup`` so OCB's ``escape`` is a
    no-op.  These tests pin the fix and guard against regression.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Test Provider",
                    "service": "openai",
                    "api_base": "https://test.example.com/v1",
                    "api_key": "sk-test-key",
                }
            )
        )
        cls.model = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "test-model",
                    "provider_id": cls.provider.id,
                    "model_use": "chat",
                }
            )
        )
        cls.thread = (
            cls.env["llm.thread"]
            .sudo()
            .create(
                {
                    "name": "Escape Test Thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def test_assistant_placeholder_body_not_double_escaped(self):
        """The 'Thinking...' placeholder survives as clean HTML, NOT as
        ``<p>&lt;p&gt;Thinking...&lt;/p&gt;</p>``.

        This is the exact body that replaces streamed answers at the end of
        a run when the stale placeholder payload flushes after the
        progressive chunks (TRACKER §4 'replaced in the end'). Before
        FIX-4a the user saw literal ``<p>Thinking...</p>`` text in place of
        the answer.
        """
        msg = self.thread.message_post(
            body="Thinking...",
            llm_role="assistant",
            author_id=False,
        )
        self.assertIn("Thinking...", msg.body)
        self.assertIn("<p>", msg.body)
        # The double-escape signature: '&lt;p&gt;' must NOT appear.
        self.assertNotIn("&lt;p&gt;", msg.body)
        self.assertNotIn("&lt;", msg.body)

    def test_assistant_markdown_body_renders_clean(self):
        """A markdown body renders as clean HTML, not as escaped text."""
        msg = self.thread.message_post(
            body="## Heading\n\nSome **bold** text.",
            llm_role="assistant",
            author_id=False,
        )
        self.assertIn("<h2>", msg.body)
        self.assertIn("<strong>bold</strong>", msg.body)
        # No escaped angle brackets anywhere.
        self.assertNotIn("&lt;", msg.body)

    def test_assistant_html_body_passthrough(self):
        """An already-HTML Markup body passes through untouched (the
        ``_process_llm_body`` Markup short-circuit is preserved)."""
        html = Markup("<p>Already <strong>HTML</strong></p>")
        msg = self.thread.message_post(
            body=html,
            llm_role="assistant",
            author_id=False,
        )
        self.assertIn("<strong>HTML</strong>", msg.body)
        self.assertNotIn("&lt;", msg.body)

    def test_user_message_body_is_plain_text(self):
        """User messages are plain text (no markdown processing); the
        escape fix only applies to assistant bodies."""
        msg = self.thread.message_post(
            body="Just plain text <not html>",
            llm_role="user",
            author_id=self.env.user.partner_id.id,
        )
        # Plain-text user bodies SHOULD be escaped by OCB (they are not
        # pre-rendered HTML); the fix only wraps the assistant path.
        self.assertIn("Just plain text", msg.body)
