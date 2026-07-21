"""P1-1 tests: reasoning-effort parameter branching in the OpenAI provider.

Verifies:
- D1: Scaleway/IONOS (non-OpenRouter) get native top-level ``reasoning_effort``;
  OpenRouter gets ``extra_body.reasoning``.
- D2: per-call kwarg > model field > no param (provider default).
- P1-1.2: ``openai_simple_completion`` forwards effort + max_tokens.
"""

from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestReasoningEffortParams(TransactionCase):
    """P1-1: reasoning-effort parameter branching (D1) and precedence (D2)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Non-OpenRouter provider (Scaleway/IONOS style)
        cls.provider_scw = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Scaleway Test",
                    "service": "openai",
                    "api_base": "https://api.scaleway.ai/v1",
                    "api_key": "sk-test-key",
                },
            )
        )
        cls.model_scw = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "glm-5.2",
                    "provider_id": cls.provider_scw.id,
                    "model_use": "chat",
                },
            )
        )
        # OpenRouter provider
        cls.provider_or = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "OpenRouter Test",
                    "service": "openai",
                    "api_base": "https://openrouter.ai/api/v1",
                    "api_key": "sk-test-key",
                },
            )
        )
        cls.model_or = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "z-ai/glm-5.2",
                    "provider_id": cls.provider_or.id,
                    "model_use": "chat",
                },
            )
        )

    def _mock_chat_create(self):
        """Mock ``client.chat.completions.create`` to capture params."""
        mock_response = MagicMock()
        mock_response.choices = []
        mock_response.usage = None
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        return mock_client

    def test_scw_sends_native_reasoning_effort(self):
        """D1: Scaleway (non-OpenRouter) gets native top-level ``reasoning_effort``."""
        self.model_scw.sudo().write({"reasoning_effort": "low"})
        mock_client = self._mock_chat_create()
        with patch.object(type(self.provider_scw), "client", mock_client):
            self.provider_scw.openai_chat(
                messages=self.env["mail.message"],
                model=self.model_scw,
                stream=False,
            )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs.get("reasoning_effort"), "low")
        self.assertNotIn("extra_body", call_kwargs)

    def test_openrouter_sends_reasoning_dict(self):
        """D1: OpenRouter gets ``extra_body.reasoning`` dict."""
        self.model_or.sudo().write({"reasoning_effort": "low"})
        mock_client = self._mock_chat_create()
        with patch.object(type(self.provider_or), "client", mock_client):
            self.provider_or.openai_chat(
                messages=self.env["mail.message"],
                model=self.model_or,
                stream=False,
            )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("reasoning_effort", call_kwargs)
        self.assertEqual(
            call_kwargs.get("extra_body"),
            {"reasoning": {"effort": "low"}},
        )

    def test_per_call_kwarg_overrides_model_field(self):
        """D2: per-call ``reasoning_effort`` kwarg > model.reasoning_effort."""
        self.model_scw.sudo().write({"reasoning_effort": "low"})
        mock_client = self._mock_chat_create()
        with patch.object(type(self.provider_scw), "client", mock_client):
            self.provider_scw.openai_chat(
                messages=self.env["mail.message"],
                model=self.model_scw,
                stream=False,
                reasoning_effort="none",
            )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs.get("reasoning_effort"), "none")

    def test_empty_effort_sends_no_param(self):
        """Empty effort = provider default (no param sent — byte-for-byte
        current behavior)."""
        mock_client = self._mock_chat_create()
        with patch.object(type(self.provider_scw), "client", mock_client):
            self.provider_scw.openai_chat(
                messages=self.env["mail.message"],
                model=self.model_scw,
                stream=False,
            )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertNotIn("reasoning_effort", call_kwargs)
        self.assertNotIn("extra_body", call_kwargs)

    def test_simple_completion_forwards_effort_and_max_tokens(self):
        """P1-1.2: ``openai_simple_completion`` forwards effort + max_tokens."""
        mock_client = self._mock_chat_create()
        with patch.object(type(self.provider_scw), "client", mock_client):
            self.provider_scw.openai_simple_completion(
                prompt="Generate a title",
                model=self.model_scw,
                reasoning_effort="none",
                max_tokens=50,
            )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs.get("reasoning_effort"), "none")
        self.assertEqual(call_kwargs.get("max_tokens"), 50)

    def test_simple_completion_model_field_effort(self):
        """P1-1.2: ``simple_completion`` falls back to model.reasoning_effort."""
        self.model_scw.sudo().write({"reasoning_effort": "high"})
        mock_client = self._mock_chat_create()
        with patch.object(type(self.provider_scw), "client", mock_client):
            self.provider_scw.openai_simple_completion(
                prompt="Generate a title",
                model=self.model_scw,
            )
        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs.get("reasoning_effort"), "high")
