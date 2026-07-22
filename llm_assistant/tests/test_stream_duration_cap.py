"""PERF-09: tests for the total-duration streaming cap.

Verifies:
1. A stream exceeding the cap raises ``TransientLLMError`` at ~cap (kill
   path: stream closed, sink finalized with duration + error annotated).
2. A healthy stream under the cap is unaffected (no kill).
3. ``max_stream_duration_s <= 0`` disables the cap.
4. The ICP fallback (``llm_assistant.max_stream_duration_s``) applies when
   no per-call kwarg is passed.
5. ``GenerationCancelled`` keeps precedence over the cap (never swallowed).
6. Partial content is KEPT on a mid-content kill (message_created in sink).

Drive the handler directly with sleepy synthetic streams (tiny real sleeps
+ tiny caps — no live LLM, no 180 s waits). One test drives
``_generate_assistant_response`` to assert the retry-loop integration.
"""

import time
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.llm_assistant.models.llm_thread import (
    GenerationCancelled,
    TransientLLMError,
)


def _sleepy_stream(chunks, gap_s):
    """Yield chunk dicts with a real sleep BEFORE each chunk (except the first)."""
    for i, chunk in enumerate(chunks):
        if i:
            time.sleep(gap_s)
        yield chunk


@tagged("post_install", "-at_install")
class TestStreamDurationCap(TransactionCase):
    """PERF-09 acceptance: kill at ~cap, trace finalized, partial kept."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Duration Cap Test Provider",
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
                    "name": "duration-cap-test-model",
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
                    "name": "Duration cap test thread",
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

    def _run_handler(self, stream, sink, **kwargs):
        gen = self.thread._handle_streaming_response(
            stream,
            trace_sink=sink,
            **kwargs,
        )
        return self._drain(gen)

    def test_kill_at_cap_reasoning_explosion(self):
        """Reasoning-only stream exceeding the cap → TransientLLMError at ~cap.

        This is the pathological 2026-07-22 signature (200–1200 reasoning
        chunks, no content). No message is ever created → message_created
        False; the sink still gets finish_ts + duration_ms (trace finalized).
        """
        sink = {}
        stream = _sleepy_stream(
            [{"reasoning": "thinking..."} for _ in range(50)], gap_s=0.05
        )
        started = time.monotonic()
        _result, _events, exc = self._run_handler(
            stream, sink, max_stream_duration_s=0.2
        )
        elapsed = time.monotonic() - started
        self.assertIsInstance(exc, TransientLLMError, "cap must kill the stream")
        self.assertIn("total-duration cap", str(exc))
        self.assertLess(elapsed, 5.0, "kill happens at ~cap, not at stream end")
        self.assertIn("finish_ts", sink, "trace finalized on the kill path")
        self.assertGreaterEqual(
            sink.get("duration_ms", 0), 150, "duration recorded (~cap)"
        )
        self.assertFalse(
            sink.get("message_created"), "no partial message on reasoning-only kill"
        )

    def test_healthy_stream_under_cap_unaffected(self):
        """A normal fast stream completes with its message intact."""
        sink = {}
        stream = iter([{"content": "Hello there."}, {"finish_reason": "stop"}])
        result, events, exc = self._run_handler(stream, sink, max_stream_duration_s=30)
        self.assertIsNone(exc)
        self.assertTrue(result, "assistant message returned")
        self.assertTrue(sink.get("message_created"))
        self.assertTrue(any(e.get("type") == "message_create" for e in events))

    def test_cap_disabled_with_zero(self):
        """max_stream_duration_s=0 disables the cap (slow stream completes)."""
        sink = {}
        stream = _sleepy_stream(
            [{"reasoning": "slow"}, {"content": "done"}, {"finish_reason": "stop"}],
            gap_s=0.1,
        )
        result, _events, exc = self._run_handler(stream, sink, max_stream_duration_s=0)
        self.assertIsNone(exc)
        self.assertTrue(result)

    def test_icp_fallback_applies_without_kwarg(self):
        """No kwarg → the ICP ``llm_assistant.max_stream_duration_s`` rules."""
        self.env["ir.config_parameter"].sudo().set_param(
            "llm_assistant.max_stream_duration_s", "0.2"
        )
        sink = {}
        stream = _sleepy_stream([{"reasoning": "x"} for _ in range(50)], gap_s=0.05)
        _result, _events, exc = self._run_handler(stream, sink)
        self.assertIsInstance(
            exc, TransientLLMError, "ICP cap must kill when no kwarg passed"
        )

    def test_kwarg_overrides_icp(self):
        """The per-call kwarg wins over the ICP (EFF-02 synthesis seam)."""
        self.env["ir.config_parameter"].sudo().set_param(
            "llm_assistant.max_stream_duration_s", "0.05"
        )
        sink = {}
        stream = _sleepy_stream(
            [{"reasoning": "x"}, {"content": "ok"}, {"finish_reason": "stop"}],
            gap_s=0.05,
        )
        result, _events, exc = self._run_handler(stream, sink, max_stream_duration_s=30)
        self.assertIsNone(exc, "kwarg cap (30s) must override the tiny ICP cap")
        self.assertTrue(result)

    def test_cancel_takes_precedence_over_cap(self):
        """GenerationCancelled is never swallowed by the cap path."""
        sink = {}
        stream = _sleepy_stream([{"reasoning": "x"} for _ in range(50)], gap_s=0.05)

        def cancel_hook(tool_call=None):
            return {"cancel": True, "mode": "immediate"}

        gen = self.thread._handle_streaming_response(
            stream,
            loop_control_check=cancel_hook,
            trace_sink=sink,
            max_stream_duration_s=0.2,
        )
        _result, _events, exc = self._drain(gen)
        self.assertIsInstance(exc, GenerationCancelled, "cancel must win over the cap")

    def test_partial_content_kept_on_kill(self):
        """Mid-content kill: the partial message is kept (message_created True)."""
        sink = {}
        stream = _sleepy_stream(
            [{"content": "partial answer"}] + [{"content": " more"} for _ in range(50)],
            gap_s=0.05,
        )
        _result, events, exc = self._run_handler(
            stream, sink, max_stream_duration_s=0.2
        )
        self.assertIsInstance(exc, TransientLLMError)
        self.assertTrue(sink.get("message_created"), "partial message must be kept")
        self.assertTrue(sink.get("message_id"), "partial message linked in the sink")
        # The partial message persists in the thread (never deleted).
        partial = self.env["mail.message"].sudo().browse(sink["message_id"])
        self.assertTrue(partial.exists(), "partial message row must survive the kill")
        self.assertIn("partial answer", partial.body or "")
        self.assertTrue(any(e.get("type") == "message_create" for e in events))

    def test_retry_loop_integration(self):
        """Via _generate_assistant_response: each killed attempt is traced,
        the retry loop re-calls chat, and the error propagates after
        max_retries (classified transient)."""
        calls = []

        def fake_chat(self_model, **kwargs):
            calls.append(1)
            return _sleepy_stream([{"reasoning": "x"} for _ in range(50)], gap_s=0.05)

        LLMModel = type(self.env["llm.model"])
        # NOTE: do NOT patch llm_thread.time.sleep here — it is the shared
        # stdlib time module; patching it would neutralize the sleepy
        # stream's gaps and the cap would never fire. The retry backoff is
        # 0.01s/0.02s via the backoff_base ICP — fast enough unpatched.
        with patch.object(LLMModel, "chat", autospec=True, side_effect=fake_chat):
            gen = self.thread._generate_assistant_response(max_stream_duration_s=0.2)
            _result, _events, exc = self._drain(gen)
        self.assertIsInstance(
            exc, TransientLLMError, "propagates after retries exhausted"
        )
        self.assertEqual(len(calls), 3, "max_retries attempts, each killed by the cap")
        traces = (
            self.env["koenig.ai.llm.trace"]
            .sudo()
            .search([("thread_id", "=", self.thread.id)], order="id asc")
        )
        self.assertEqual(len(traces), 3, "one trace per killed attempt")
        self.assertTrue(all(t.status == "error" for t in traces))
        self.assertTrue(all(t.error_class == "TransientLLMError" for t in traces))
        self.assertTrue(
            all("total-duration cap" in (t.error_message or "") for t in traces),
            f"kill annotated on every trace: {[(t.attempt, t.error_message) for t in traces]}",
        )
