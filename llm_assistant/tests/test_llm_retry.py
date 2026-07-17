"""Tests for the LLM API transient-error retry layer in ``_generate_assistant_response``.

The retry loop is a SECOND layer on top of the OpenAI SDK's own retry
(``max_retries=3`` in ``openai_get_client``).  These tests verify:

1. **Classification** — ``_is_transient_llm_error`` correctly classifies
   transient (5xx, timeout, connection, 429) vs non-transient (400, 401, 403)
   errors.
2. **Retry succeeds** — a transient error on the first attempt followed by
   success on the second attempt produces a valid assistant message.
3. **Retry exhausted** — a persistent transient error is retried ``max_retries``
   times, then propagates.
4. **Non-transient propagates immediately** — a 400 error is NOT retried.
5. **``GenerationCancelled`` propagates** — being ``BaseException``, it is
   never caught by ``except Exception`` in the retry loop.
6. **``TransientLLMError`` is always transient** — explicit classification.

Test isolation: ``time.sleep`` is mocked so the backoff delay is zero —
tests run in milliseconds, not seconds.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.llm_assistant.models.llm_thread import (
    GenerationCancelled,
    TransientLLMError,
)

# ---------------------------------------------------------------------------
# Helpers — synthetic exception classes mimicking OpenAI SDK exceptions
# ---------------------------------------------------------------------------


class FakeAPIStatusError(Exception):
    """Mimics ``openai.APIStatusError`` — has a ``status_code`` attribute."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class FakeAPITimeoutError(Exception):
    """Mimics ``openai.APITimeoutError`` — class name contains 'Timeout'."""


class FakeAPIConnectionError(Exception):
    """Mimics ``openai.APIConnectionError`` — class name contains 'Connection'."""


# ---------------------------------------------------------------------------
# _is_transient_llm_error — pure classification tests
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestIsTransientLLMError(TransactionCase):
    """Verify ``_is_transient_llm_error`` classifies errors correctly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Retry Test Provider",
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
                    "name": "retry-test-model",
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
                    "name": "Retry test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def test_transient_explicit_transient_error(self):
        """``TransientLLMError`` is always transient."""
        self.assertTrue(self.thread._is_transient_llm_error(TransientLLMError("test")))

    def test_transient_500(self):
        """HTTP 500 is transient."""
        exc = FakeAPIStatusError("Internal Server Error", 500)
        self.assertTrue(self.thread._is_transient_llm_error(exc))

    def test_transient_502(self):
        """HTTP 502 is transient."""
        exc = FakeAPIStatusError("Bad Gateway", 502)
        self.assertTrue(self.thread._is_transient_llm_error(exc))

    def test_transient_503(self):
        """HTTP 503 is transient."""
        exc = FakeAPIStatusError("Service Unavailable", 503)
        self.assertTrue(self.thread._is_transient_llm_error(exc))

    def test_transient_429(self):
        """HTTP 429 (rate limit) is transient."""
        exc = FakeAPIStatusError("Rate Limited", 429)
        self.assertTrue(self.thread._is_transient_llm_error(exc))

    def test_transient_timeout(self):
        """``APITimeoutError`` (class name contains 'Timeout') is transient."""
        self.assertTrue(self.thread._is_transient_llm_error(FakeAPITimeoutError()))

    def test_transient_connection(self):
        """``APIConnectionError`` (class name contains 'Connection') is transient."""
        self.assertTrue(self.thread._is_transient_llm_error(FakeAPIConnectionError()))

    def test_not_transient_400(self):
        """HTTP 400 (bad request) is NOT transient."""
        exc = FakeAPIStatusError("Bad Request", 400)
        self.assertFalse(self.thread._is_transient_llm_error(exc))

    def test_not_transient_401(self):
        """HTTP 401 (auth) is NOT transient."""
        exc = FakeAPIStatusError("Unauthorized", 401)
        self.assertFalse(self.thread._is_transient_llm_error(exc))

    def test_not_transient_403(self):
        """HTTP 403 (forbidden) is NOT transient."""
        exc = FakeAPIStatusError("Forbidden", 403)
        self.assertFalse(self.thread._is_transient_llm_error(exc))

    def test_not_transient_404(self):
        """HTTP 404 (not found) is NOT transient."""
        exc = FakeAPIStatusError("Not Found", 404)
        self.assertFalse(self.thread._is_transient_llm_error(exc))

    def test_not_transient_generic_exception(self):
        """A generic Exception with no status_code is NOT transient."""

        class SomeRandomError(Exception):
            pass

        self.assertFalse(
            self.thread._is_transient_llm_error(SomeRandomError("unknown"))
        )

    def test_not_transient_user_error(self):
        """Odoo ``UserError`` is NOT transient (data issue, not infrastructure)."""
        from odoo.exceptions import UserError

        self.assertFalse(self.thread._is_transient_llm_error(UserError("bad data")))


# ---------------------------------------------------------------------------
# Retry loop in _generate_assistant_response
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestLLMRetryLoop(TransactionCase):
    """Verify the retry loop in ``_generate_assistant_response``.

    Uses non-streaming mode (``supports_streaming=False``) for simplicity —
    the retry logic is identical for streaming, it just wraps ``model_id.chat()``
    the same way.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Retry Loop Test Provider",
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
                    "name": "retry-loop-test-model",
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
                    "name": "Retry loop test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )
        # Post a user message so generate_messages has a last_message.
        cls.thread.with_context(mail_create_nosubscribe=True).message_post(
            body="Hello, AI!",
            llm_role="user",
            author_id=cls.env.user.partner_id.id,
        )
        # Fast backoff for tests — 10ms base instead of 1s.
        cls.env["ir.config_parameter"].sudo().set_param("llm_assistant.max_retries", 3)
        cls.env["ir.config_parameter"].sudo().set_param(
            "llm_assistant.backoff_base", 0.01
        )

    def _drain_generator(self, gen):
        """Drain a generator, returning (result, events, exception)."""
        events = []
        exc = None
        result = None
        try:
            while True:
                events.append(next(gen))
        except StopIteration as e:
            result = e.value
        except BaseException as e:  # noqa: BLE001 — catch GenerationCancelled (BaseException)
            exc = e
        return result, events, exc

    def _run_generate_assistant_response(self, chat_side_effects):
        """Run ``_generate_assistant_response`` with mocked ``model_id.chat``.

        ``chat_side_effects`` is a list — each entry is either an exception
        (to raise) or a string (returned as streaming content chunks).

        Returns ``(result, events, exc, mock_sleep)``.
        ``time.sleep`` is mocked to avoid real delays.

        Uses streaming mode (the default — ``supports_streaming`` defaults to
        ``True`` when not a field).  ``fake_chat`` returns a simple iterable
        yielding one content chunk so ``_handle_streaming_response`` processes
        it normally.
        """

        def fake_chat(self_model, **kwargs):
            raise_or_return = chat_side_effects.pop(0)
            if isinstance(raise_or_return, BaseException):
                raise raise_or_return
            # Return a stream that yields one chunk (dict or string).
            if isinstance(raise_or_return, str):
                return iter([{"content": raise_or_return}])
            return iter([raise_or_return])

        LLMModel = type(self.env["llm.model"])
        with (
            patch.object(LLMModel, "chat", autospec=True, side_effect=fake_chat),
            patch(
                "odoo.addons.llm_assistant.models.llm_thread.time.sleep"
            ) as mock_sleep,
        ):
            gen = self.thread._generate_assistant_response()
            result, events, exc = self._drain_generator(gen)
            return result, events, exc, mock_sleep

    def test_transient_error_then_success(self):
        """A transient 5xx on attempt 1, success on attempt 2 → assistant message returned."""
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [
                FakeAPIStatusError("Internal Server Error", 500),
                {"content": "Hello back!"},
            ]
        )
        # No exception propagated
        self.assertIsNone(exc, "Expected success after retry, got exception")
        # An assistant message was returned
        self.assertIsNotNone(result, "Expected an assistant message")
        # time.sleep was called once (1 retry)
        mock_sleep.assert_called_once()
        # At least one SSE event was yielded (message_create)
        self.assertTrue(
            any(e.get("type") == "message_create" for e in events),
            "Expected at least one message_create event",
        )

    def test_transient_timeout_then_success(self):
        """A timeout on attempt 1, success on attempt 2 → retries and succeeds."""
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [
                FakeAPITimeoutError(),
                {"content": "Hello back!"},
            ]
        )
        self.assertIsNone(exc, "Expected success after timeout retry")
        self.assertIsNotNone(result)
        mock_sleep.assert_called_once()

    def test_all_retries_exhausted(self):
        """A persistent 5xx → retried 3 times → exception propagates."""
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [
                FakeAPIStatusError("Internal Server Error", 500),
                FakeAPIStatusError("Internal Server Error", 500),
                FakeAPIStatusError("Internal Server Error", 500),
            ]
        )
        # Exception propagated (all 3 attempts exhausted)
        self.assertIsNotNone(exc, "Expected exception after retries exhausted")
        self.assertIsInstance(exc, FakeAPIStatusError)
        # time.sleep was called twice (between 3 attempts)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_non_transient_400_propagates_immediately(self):
        """HTTP 400 → NOT retried → propagates immediately (0 sleep calls)."""
        bad_request = FakeAPIStatusError("Bad Request", 400)
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [bad_request]
        )
        self.assertIsNotNone(exc, "Expected 400 to propagate immediately")
        self.assertIsInstance(exc, FakeAPIStatusError)
        self.assertEqual(exc.status_code, 400)
        # No retries → no sleep
        mock_sleep.assert_not_called()

    def test_non_transient_401_propagates_immediately(self):
        """HTTP 401 → NOT retried → propagates immediately."""
        unauthorized = FakeAPIStatusError("Unauthorized", 401)
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [unauthorized]
        )
        self.assertIsNotNone(exc)
        self.assertEqual(exc.status_code, 401)
        mock_sleep.assert_not_called()

    def test_generation_cancelled_not_caught(self):
        """``GenerationCancelled`` is BaseException → not caught by retry loop → propagates."""
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [GenerationCancelled("user cancelled")]
        )
        self.assertIsNotNone(exc, "Expected GenerationCancelled to propagate")
        self.assertIsInstance(exc, GenerationCancelled)
        # No retries for cancel
        mock_sleep.assert_not_called()

    def test_transient_llm_error_class_is_transient(self):
        """``TransientLLMError`` (explicitly classified) → retried → then succeeds."""
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [
                TransientLLMError("SDK retries exhausted"),
                {"content": "Hello back!"},
            ]
        )
        self.assertIsNone(exc, "Expected success after TransientLLMError retry")
        self.assertIsNotNone(result)
        mock_sleep.assert_called_once()

    def test_exponential_backoff_timing(self):
        """Verify the backoff follows exponential progression: base * 2^attempt."""
        # Set a known backoff base for verification
        self.env["ir.config_parameter"].sudo().set_param(
            "llm_assistant.backoff_base", 0.5
        )
        result, events, exc, mock_sleep = self._run_generate_assistant_response(
            [
                FakeAPIStatusError("Internal Server Error", 500),
                FakeAPIStatusError("Internal Server Error", 500),
                FakeAPIStatusError("Internal Server Error", 500),
            ]
        )
        # 3 attempts → 2 sleep calls with exponential backoff
        sleep_calls = [call.args[0] for call in mock_sleep.call_args_list]
        self.assertEqual(len(sleep_calls), 2)
        # Attempt 0 → backoff_base * 2^0 = 0.5
        # Attempt 1 → backoff_base * 2^1 = 1.0
        self.assertAlmostEqual(sleep_calls[0], 0.5, places=1)
        self.assertAlmostEqual(sleep_calls[1], 1.0, places=1)
        # Reset for other tests
        self.env["ir.config_parameter"].sudo().set_param(
            "llm_assistant.backoff_base", 0.01
        )
