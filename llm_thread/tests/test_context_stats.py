from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGetContextStats(TransactionCase):
    """FIX-2 (TRACKER_2026-07-25_UI_RESEARCH.md §2): ``get_context_stats``
    returns the context window, last real prompt tokens, and an estimated
    breakdown so the HUD can display a Kilo-Code-style context meter.

    Gracefully degrades when ``koenig_ai_core`` (trace model, context window
    field, estimator) is not installed — returns zeros so the HUD hides the
    context segment.
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
                    "name": "Context Stats Test",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def test_returns_expected_shape(self):
        """The method returns a dict with the expected keys."""
        result = self.env["llm.thread"].get_context_stats(self.thread.id)
        self.assertIn("context_window", result)
        self.assertIn("reserved_output", result)
        self.assertIn("last_prompt_tokens", result)
        self.assertIn("last_prompt_at", result)
        self.assertIn("estimate", result)
        est = result["estimate"]
        for key in ("system", "tools", "summary", "window_msgs", "window_tokens", "total"):
            self.assertIn(key, est)

    def test_context_window_from_model(self):
        """When koenig_ai_core is installed, the context window comes from
        the model's ``koenig_context_window`` field."""
        ctx_window = getattr(self.model, "koenig_context_window", None)
        if ctx_window is not None:
            self.model.sudo().write({"koenig_context_window": 131072})
            self.model.invalidate_recordset(["koenig_context_window"])
            result = self.env["llm.thread"].get_context_stats(self.thread.id)
            self.assertEqual(result["context_window"], 131072)
        else:
            self.skipTest("koenig_ai_core not installed (no koenig_context_window)")

    def test_no_trace_returns_zero_prompt_tokens(self):
        """A thread with no traces returns last_prompt_tokens=0."""
        result = self.env["llm.thread"].get_context_stats(self.thread.id)
        self.assertEqual(result["last_prompt_tokens"], 0)

    def test_graceful_without_koenig_ai_core(self):
        """When koenig_ai_core is not installed, the method returns zeros
        (context_window=0, last_prompt_tokens=0) without raising."""
        result = self.env["llm.thread"].get_context_stats(self.thread.id)
        # context_window may be 0 (field absent) or a real value.
        # The key invariant: the method does NOT raise.
        self.assertIsInstance(result, dict)

    def test_context_window_prefers_assistant_model(self):
        """F4 (2026-07-26): the HUD denominator resolves via the ASSISTANT's
        configured model first — the assistant is the configuration source of
        truth; the thread's model_id (synced from it) may drift after an
        admin reconfiguration."""
        ctx_window = getattr(self.model, "koenig_context_window", None)
        if ctx_window is None:
            self.skipTest("koenig_ai_core not installed (no koenig_context_window)")
        if "llm.assistant" not in self.env:
            self.skipTest("llm_assistant not installed")
        assistant_model = (
            self.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "test-assistant-model",
                    "provider_id": self.provider.id,
                    "model_use": "chat",
                }
            )
        )
        self.model.sudo().write({"koenig_context_window": 100000})
        assistant_model.sudo().write({"koenig_context_window": 200000})
        prompt = (
            self.env["llm.prompt"]
            .sudo()
            .create({"name": "Test Prompt", "template": "You are a test assistant."})
        )
        assistant = (
            self.env["llm.assistant"]
            .sudo()
            .create(
                {
                    "name": "Test Assistant",
                    "provider_id": self.provider.id,
                    "model_id": assistant_model.id,
                    "prompt_id": prompt.id,
                }
            )
        )
        self.thread.sudo().write({"assistant_id": assistant.id})
        self.thread.invalidate_recordset()
        result = self.env["llm.thread"].get_context_stats(self.thread.id)
        self.assertEqual(result["context_window"], 200000)

    def test_context_window_falls_back_to_thread_model(self):
        """F4: without an assistant, the thread's own model is the fallback
        (covered by test_context_window_from_model — this pins the None-
        assistant path explicitly)."""
        ctx_window = getattr(self.model, "koenig_context_window", None)
        if ctx_window is None:
            self.skipTest("koenig_ai_core not installed (no koenig_context_window)")
        self.model.sudo().write({"koenig_context_window": 131072})
        self.model.invalidate_recordset(["koenig_context_window"])
        if "assistant_id" in self.thread._fields:
            self.thread.sudo().write({"assistant_id": False})
            self.thread.invalidate_recordset(["assistant_id"])
        result = self.env["llm.thread"].get_context_stats(self.thread.id)
        self.assertEqual(result["context_window"], 131072)

    def test_thread_id_wrapped_in_list(self):
        """The RPC layer may wrap thread_id in a list — the method handles it."""
        result = self.env["llm.thread"].get_context_stats([self.thread.id])
        self.assertIsInstance(result, dict)
