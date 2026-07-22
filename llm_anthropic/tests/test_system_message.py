"""Parity tests: ``llm_role="system"`` messages must reach the Anthropic payload.

Regression coverage for the silent-drop class fixed in ``llm_openai``
(``_openai_format_llm_system_message``) and ported here: without a dedicated
branch, ``anthropic_format_message`` fell through to ``return None`` for
system messages and the model answered as if injected context (e.g. a
media-description bridge result) did not exist.
"""

from markupsafe import Markup

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestAnthropicSystemMessage(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Anthropic Format Test Provider",
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
                    "name": "anthropic-format-test-model",
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
                    "name": "Anthropic format test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def test_system_message_serialized_as_attributed_user(self):
        """A mid-conversation system note must NOT be silently dropped."""
        msg = self.thread.with_context(mail_create_nosubscribe=True).message_post(
            body=Markup(
                "<p>[Expert media_describe | attachment_id=1] The image shows a chart.</p>"
            ),
            llm_role="system",
        )
        formatted = msg.anthropic_format_message()
        self.assertIsNotNone(
            formatted,
            "system message must not fall through to None (silent drop)",
        )
        self.assertEqual(formatted["role"], "user")
        self.assertIn("media_describe", formatted["content"])

    def test_user_and_assistant_messages_unchanged(self):
        """The new branch must not alter the existing role paths."""
        user_msg = self.thread.with_context(mail_create_nosubscribe=True).message_post(
            body="Hello",
            llm_role="user",
        )
        self.assertEqual(
            user_msg.anthropic_format_message(),
            {"role": "user", "content": "Hello"},
        )
        # Assistant bodies are Markup HTML in production (stream handler posts).
        assistant_msg = self.thread.with_context(
            mail_create_nosubscribe=True
        ).message_post(
            body=Markup("<p>Hi there</p>"),
            llm_role="assistant",
        )
        formatted = assistant_msg.anthropic_format_message()
        self.assertEqual(formatted["role"], "assistant")
        self.assertEqual(
            formatted["content"],
            [{"type": "text", "text": "Hi there"}],
        )

    def test_empty_system_body_returns_none(self):
        """Empty system bodies stay dropped (no payload noise)."""
        Message = self.env["mail.message"]
        self.assertIsNone(Message._anthropic_format_llm_system_message(""))
        self.assertIsNone(Message._anthropic_format_llm_system_message("   "))
