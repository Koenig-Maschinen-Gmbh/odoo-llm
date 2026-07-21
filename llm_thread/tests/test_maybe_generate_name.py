"""P1-3 test: ``_maybe_generate_name`` passes ``reasoning_effort='none'``
and ``max_tokens=50`` to ``simple_completion``.

Measured: ``reasoning_effort='none'`` cuts the title call from 8–55 s to
~0.4 s (RESEARCH_2026-07-20 §1.1 — 1849 tokens → 12). The explicit
call-site override (D2) stays ``none`` even when the model record is
configured ``low``.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMaybeGenerateNameEffort(TransactionCase):
    """P1-3: title generation uses ``reasoning_effort='none'`` + ``max_tokens=50``."""

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
                    "api_base": "https://api.scaleway.ai/v1",
                    "api_key": "sk-test-key",
                },
            )
        )
        cls.model = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "glm-5.2",
                    "provider_id": cls.provider.id,
                    "model_use": "chat",
                    "reasoning_effort": "low",  # Model configured 'low' — call must still use 'none'
                },
            )
        )
        cls.thread = (
            cls.env["llm.thread"]
            .sudo()
            .create(
                {
                    "name": "New Chat #123",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                },
            )
        )

    def test_maybe_generate_name_passes_none_effort_and_max_tokens(self):
        """The title call must pass ``reasoning_effort='none'`` (D2 explicit
        call-site override — stays ``none`` even when model is ``low``) and
        ``max_tokens=50`` (safety cap).
        """
        # Post the first user message so _maybe_generate_name fires
        self.thread.with_context(mail_create_nosubscribe=True).message_post(
            body="<p>Kundenübersicht für SAS JV AGENIAA</p>",
            llm_role="user",
            author_id=False,
        )
        captured_kwargs = {}

        def fake_simple_completion(
            self, prompt, system_prompt=None, model=None, **kwargs
        ):
            captured_kwargs.update(kwargs)
            return "Kundenübersicht SAS JV AGENIAA"

        with patch.object(
            type(self.env["llm.model"]),
            "simple_completion",
            fake_simple_completion,
        ):
            result = self.thread._maybe_generate_name()

        self.assertEqual(captured_kwargs.get("reasoning_effort"), "none")
        self.assertEqual(captured_kwargs.get("max_tokens"), 50)
        self.assertTrue(result)
