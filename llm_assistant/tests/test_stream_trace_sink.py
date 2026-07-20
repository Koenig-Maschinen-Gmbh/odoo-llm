"""TEL-01: tests for the ``trace_sink`` filling + ``_record_llm_call_trace`` hook.

Verifies:
1. A reasoning-only stream fills the sink (reasoning>0, content=0, finish_ts
   set, message_created False) and raises ``TransientLLMError`` (the
   committed retry semantics — unchanged).
2. A healthy stream fills the sink (message_created True, content_length > 0).
3. The ``_record_llm_call_trace`` hook fires per attempt with the right
   attempt numbers and statuses (error, error, ok) when chat raises-empty
   twice then succeeds.

Drive via ``_generate_assistant_response`` with mocked ``chat`` — do NOT
re-test the retry logic itself (that's ``test_llm_retry.py``); only assert
the trace calls.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.llm_assistant.models.llm_thread import TransientLLMError


@tagged("post_install", "-at_install")
class TestStreamTraceSink(TransactionCase):
    """Verify trace_sink filling + hook firing (TEL-01 Step 3)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Trace Sink Test Provider",
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
                    "name": "trace-sink-test-model",
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
                    "name": "Trace sink test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )
        cls.thread.with_context(mail_create_nosubscribe=True).message_post(
            body="Hello, AI!",
            llm_role="user",
            author_id=cls.env.user.partner_id.id,
        )
        cls.env["ir.config_parameter"].sudo().set_param("llm_assistant.max_retries", 3)
        cls.env["ir.config_parameter"].sudo().set_param(
            "llm_assistant.backoff_base", 0.01
        )

    def _drain(self, gen):
        """Drain a generator, returning (result, events, exception)."""
        events = []
        exc = None
        result = None
        try:
            while True:
                events.append(next(gen))
        except StopIteration as e:
            result = e.value
        except BaseException as e:  # noqa: BLE001
            exc = e
        return result, events, exc

    def _run_with_chat(self, chat_side_effects):
        """Run ``_generate_assistant_response`` with mocked chat.

        Each side effect is either an Exception (raised by chat) or a list
        of chunk dicts (returned as an iterable stream).
        """

        def fake_chat(self_model, **kwargs):
            item = chat_side_effects.pop(0)
            if isinstance(item, BaseException):
                raise item
            return iter(item)

        LLMModel = type(self.env["llm.model"])
        with (
            patch.object(LLMModel, "chat", autospec=True, side_effect=fake_chat),
            patch("odoo.addons.llm_assistant.models.llm_thread.time.sleep"),
        ):
            gen = self.thread._generate_assistant_response()
            return self._drain(gen)

    def test_reasoning_only_stream_fills_sink_and_raises(self):
        """A reasoning-only stream fills the sink + raises TransientLLMError.

        The sink must show reasoning>0, content=0, finish_ts set,
        message_created False. After 3 retries the error propagates.
        """
        reasoning_stream = [
            {"reasoning": "thinking hard..."},
            {"reasoning": "still thinking..."},
            {"finish_reason": "stop"},
        ]
        result, events, exc = self._run_with_chat(
            [reasoning_stream, reasoning_stream, reasoning_stream]
        )
        # After 3 attempts exhausted, TransientLLMError propagates.
        self.assertIsNotNone(exc, "Expected TransientLLMError after retries exhausted")
        self.assertIsInstance(exc, TransientLLMError)
        # The hook fired 3 times (one per attempt) — all with status "error".
        # Verify via the trace rows created by the koenig_ai_core override.
        traces = (
            self.env["koenig.ai.llm.trace"]
            .sudo()
            .search([("thread_id", "=", self.thread.id)])
        )
        self.assertEqual(
            len(traces), 3, "Expected 3 trace rows (one per retry attempt)"
        )
        for trace in traces:
            self.assertEqual(trace.status, "error")
            self.assertGreater(trace.chunks_reasoning, 0, "Reasoning chunks counted")
            self.assertEqual(trace.chunks_content, 0, "No content chunks")
            self.assertEqual(trace.chunks_tool, 0, "No tool chunks")
            self.assertFalse(trace.message_created, "No message created")
            self.assertTrue(trace.finish_ts, "finish_ts set")
            self.assertEqual(trace.error_class, "TransientLLMError")
        # Attempt numbers 1, 2, 3.
        self.assertEqual(sorted(traces.mapped("attempt")), [1, 2, 3])

    def test_healthy_stream_fills_sink(self):
        """A healthy stream fills the sink with message_created True."""
        healthy_stream = [
            {"content": "Hello "},
            {"content": "back!"},
            {"finish_reason": "stop"},
        ]
        result, events, exc = self._run_with_chat([healthy_stream])
        self.assertIsNone(exc, "Expected success")
        self.assertIsNotNone(result, "Expected an assistant message")
        traces = (
            self.env["koenig.ai.llm.trace"]
            .sudo()
            .search([("thread_id", "=", self.thread.id)])
        )
        self.assertEqual(len(traces), 1, "Expected 1 trace row")
        trace = traces[0]
        self.assertEqual(trace.status, "ok")
        self.assertTrue(trace.message_created, "Message created")
        self.assertEqual(trace.message_id.id, result.id)
        self.assertGreater(trace.chunks_content, 0, "Content chunks counted")
        self.assertGreater(trace.content_length, 0, "Content length > 0")
        self.assertEqual(trace.finish_reason, "stop")
        self.assertFalse(trace.error_class, "No error class on success")

    def test_hook_fires_per_attempt_with_right_statuses(self):
        """chat raises-empty twice then succeeds → 3 hook calls: error, error, ok."""
        empty_stream = [{"finish_reason": "stop"}]  # no content, no tool_calls
        healthy_stream = [{"content": "Finally!"}, {"finish_reason": "stop"}]
        result, events, exc = self._run_with_chat(
            [empty_stream, empty_stream, healthy_stream]
        )
        self.assertIsNone(exc, "Expected success on 3rd attempt")
        traces = (
            self.env["koenig.ai.llm.trace"]
            .sudo()
            .search([("thread_id", "=", self.thread.id)])
        )
        self.assertEqual(len(traces), 3, "Expected 3 trace rows")
        statuses = [t.status for t in traces]
        self.assertEqual(statuses, ["error", "error", "ok"])
        attempts = [t.attempt for t in traces]
        self.assertEqual(attempts, [1, 2, 3])
