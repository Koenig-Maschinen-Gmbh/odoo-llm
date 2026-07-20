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

from unittest.mock import MagicMock, patch

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

    Uses streaming mode (the default — ``supports_streaming`` defaults to
    ``True`` when not a field on the model).  The retry logic is identical
    for streaming and non-streaming; it wraps ``model_id.chat()`` the same
    way.
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


# ---------------------------------------------------------------------------
# Empty-response retry — reasoning-only / empty streams and non-streaming
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestEmptyResponseRetry(TransactionCase):
    """Verify empty LLM responses (no content, no tool_calls) are retried.

    Covers three root-cause scenarios that previously produced silent failures:

    1. **Reasoning-only stream** — the stream completes but every chunk lacks a
       ``content`` key (e.g. reasoning-only deltas).  Previously
       ``_handle_streaming_response`` returned ``None`` silently; now it raises
       ``TransientLLMError`` so the retry loop re-calls ``chat()``.
    2. **Non-streaming empty dict** — ``chat(stream=False)`` returns
       ``{"content": "", "tool_calls": []}``.  Previously
       ``_handle_non_streaming_response`` fabricated a fake ``"No response from
       model"`` assistant message (false success); now it raises
       ``TransientLLMError``.
    3. **Retries exhausted** — when every attempt is empty, the
       ``TransientLLMError`` propagates after ``max_retries`` attempts instead
       of silently returning ``None`` or a fake message.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Empty Response Test Provider",
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
                    "name": "empty-response-test-model",
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
                    "name": "Empty response test thread",
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

    def _run_generate(self, chat_side_effects, *, streaming=True):
        """Run ``_generate_assistant_response`` with mocked ``model_id.chat``.

        ``chat_side_effects`` entries:
            - ``BaseException`` instance → raised by ``chat()``.
            - ``str`` → wrapped as ``{"content": <str>}`` (content chunk/dict).
            - ``dict`` → used as-is (a stream chunk in streaming mode, or the
              full response dict in non-streaming mode).
            - ``list`` → used as the full iterable of stream chunks (streaming
              mode only).

        ``streaming=False`` patches ``supports_streaming`` to ``False`` so the
        non-streaming handler path is exercised; ``fake_chat`` then returns a
        single dict instead of an iterable.
        """

        def fake_chat(self_model, **kwargs):
            item = chat_side_effects.pop(0)
            if isinstance(item, BaseException):
                raise item
            stream = kwargs.get("stream", True)
            if stream:
                if isinstance(item, list):
                    return iter(item)
                if isinstance(item, str):
                    return iter([{"content": item}])
                return iter([item])
            if isinstance(item, str):
                return {"content": item}
            if isinstance(item, list):
                return item[0] if item else {}
            return item

        LLMModel = type(self.env["llm.model"])
        with (
            patch.object(LLMModel, "chat", autospec=True, side_effect=fake_chat),
            patch(
                "odoo.addons.llm_assistant.models.llm_thread.time.sleep"
            ) as mock_sleep,
            patch.object(LLMModel, "supports_streaming", streaming, create=True),
        ):
            gen = self.thread._generate_assistant_response()
            result, events, exc = self._drain_generator(gen)
            return result, events, exc, mock_sleep

    # -- Streaming: reasoning-only then success --------------------------------

    def test_reasoning_only_stream_then_success(self):
        """Reasoning-only chunks (no content) on attempt 1, content on attempt 2.

        The first stream carries only a ``reasoning`` key — no ``content`` and
        no ``tool_calls``.  Previously this returned ``None`` silently; now it
        raises ``TransientLLMError``, the retry loop re-calls ``chat()``, and
        the second attempt (with real content) succeeds.
        """
        result, events, exc, mock_sleep = self._run_generate(
            [
                [{"reasoning": "thinking..."}, {"reasoning": "more thinking..."}],
                [{"content": "Hello back!"}],
            ]
        )
        self.assertIsNone(exc, "Expected success after retry, got exception")
        self.assertIsNotNone(result, "Expected an assistant message after retry")
        mock_sleep.assert_called_once()
        self.assertTrue(
            any(e.get("type") == "message_create" for e in events),
            "Expected at least one message_create event from the successful attempt",
        )

    def test_reasoning_only_stream_retries_exhausted(self):
        """Reasoning-only chunks on every attempt → TransientLLMError propagates."""
        result, events, exc, mock_sleep = self._run_generate(
            [
                [{"reasoning": "thinking..."}],
                [{"reasoning": "thinking..."}],
                [{"reasoning": "thinking..."}],
            ]
        )
        self.assertIsNotNone(exc, "Expected exception after retries exhausted")
        self.assertIsInstance(exc, TransientLLMError)
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertIsNone(result)
        self.assertFalse(
            any(e.get("type") == "message_create" for e in events),
            "No message_create event should be yielded for empty responses",
        )

    def test_truly_empty_stream_retries_exhausted(self):
        """An empty stream (zero chunks) on every attempt → error propagates."""
        result, events, exc, mock_sleep = self._run_generate([[], [], []])
        self.assertIsNotNone(exc, "Expected exception after retries exhausted")
        self.assertIsInstance(exc, TransientLLMError)
        self.assertEqual(mock_sleep.call_count, 2)

    # -- Non-streaming: empty dict then success --------------------------------

    def test_non_streaming_empty_then_success(self):
        """Empty non-streaming dict on attempt 1, content on attempt 2 → success.

        The first response is ``{"content": "", "tool_calls": []}``.  Previously
        this fabricated a fake ``"No response from model"`` message; now it
        raises ``TransientLLMError`` and the retry succeeds.
        """
        result, events, exc, mock_sleep = self._run_generate(
            [
                {"content": "", "tool_calls": []},
                {"content": "Hello back!"},
            ],
            streaming=False,
        )
        self.assertIsNone(exc, "Expected success after retry, got exception")
        self.assertIsNotNone(result, "Expected an assistant message after retry")
        mock_sleep.assert_called_once()
        self.assertNotEqual(result.body, "No response from model")

    def test_non_streaming_empty_retries_exhausted(self):
        """Empty non-streaming dict on every attempt → TransientLLMError."""
        result, events, exc, mock_sleep = self._run_generate(
            [
                {"content": "", "tool_calls": []},
                {"content": "", "tool_calls": []},
                {"content": "", "tool_calls": []},
            ],
            streaming=False,
        )
        self.assertIsNotNone(exc, "Expected exception after retries exhausted")
        self.assertIsInstance(exc, TransientLLMError)
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertIsNone(result)

    def test_non_streaming_no_content_key_then_success(self):
        """A non-streaming dict with no content/tool_calls keys → retry → success."""
        result, events, exc, mock_sleep = self._run_generate(
            [
                {},
                {"content": "Hello back!"},
            ],
            streaming=False,
        )
        self.assertIsNone(exc)
        self.assertIsNotNone(result)
        mock_sleep.assert_called_once()

    # -- Regression: normal content unchanged ----------------------------------

    def test_normal_streaming_content_unchanged(self):
        """A normal content stream on the first attempt → success, no retry."""
        result, events, exc, mock_sleep = self._run_generate(
            [[{"content": "Hello back!"}]]
        )
        self.assertIsNone(exc)
        self.assertIsNotNone(result)
        mock_sleep.assert_not_called()

    def test_normal_non_streaming_content_unchanged(self):
        """A normal non-streaming content response → success, no retry."""
        result, events, exc, mock_sleep = self._run_generate(
            [{"content": "Hello back!"}],
            streaming=False,
        )
        self.assertIsNone(exc)
        self.assertIsNotNone(result)
        mock_sleep.assert_not_called()

    def test_normal_tool_call_stream_unchanged(self):
        """A tool-call stream on the first attempt → success, no retry.

        The product tool-call path calls ``self.env.cr.commit()`` before tool
        execution (in ``_handle_streaming_response``) to persist the assistant
        message with tool_calls.  ``TransactionCase`` class-patches
        ``cr.commit`` with a ``forbidden`` function that raises
        ``AssertionError``, so we override it with a no-op mock for this test
        only — the test transaction rolls back at the end regardless.
        """
        tool_calls = [{"id": "call_1", "name": "echo", "arguments": "{}"}]
        mock_commit = MagicMock()
        with patch.object(self.env.cr, "commit", mock_commit):
            result, events, exc, mock_sleep = self._run_generate(
                [[{"tool_calls": tool_calls}]]
            )
        self.assertIsNone(exc)
        self.assertIsNotNone(result)
        mock_sleep.assert_not_called()
        self.assertTrue(result.has_tool_calls())
        # The product path commits the assistant message before tool execution.
        self.assertTrue(
            mock_commit.called,
            "cr.commit() must be called by the tool-call stream path",
        )

    # -- Cancellation semantics preserved --------------------------------------

    def test_generation_cancelled_not_caught_by_empty_response_retry(self):
        """``GenerationCancelled`` (BaseException) is NOT caught by the
        empty-response retry ``except TransientLLMError`` block — it propagates.
        """
        result, events, exc, mock_sleep = self._run_generate(
            [GenerationCancelled("user cancelled")]
        )
        self.assertIsNotNone(exc, "Expected GenerationCancelled to propagate")
        self.assertIsInstance(exc, GenerationCancelled)
        mock_sleep.assert_not_called()

    # -- Streaming: error chunk before any content (no partial state) -----------

    def test_error_chunk_before_content_then_success(self):
        """An error chunk on attempt 1 (no content yet), content on attempt 2.

        When an error chunk arrives before any assistant message was posted,
        there is no partial state — safe to retry.  Previously this returned
        ``None`` silently; now it raises ``TransientLLMError`` and the retry
        succeeds.
        """
        result, events, exc, mock_sleep = self._run_generate(
            [
                [{"error": "server error mid-stream"}],
                [{"content": "Hello back!"}],
            ]
        )
        self.assertIsNone(exc, "Expected success after retry, got exception")
        self.assertIsNotNone(result, "Expected an assistant message after retry")
        mock_sleep.assert_called_once()
        # The error event from attempt 1 was yielded
        self.assertTrue(
            any(e.get("type") == "error" for e in events),
            "Expected an error event from the failed attempt",
        )
        # The successful attempt produced a message
        self.assertTrue(
            any(e.get("type") == "message_create" for e in events),
            "Expected at least one message_create event from the successful attempt",
        )

    def test_error_chunk_before_content_retries_exhausted(self):
        """Error chunks on every attempt (no content) → TransientLLMError."""
        result, events, exc, mock_sleep = self._run_generate(
            [
                [{"error": "server error"}],
                [{"error": "server error"}],
                [{"error": "server error"}],
            ]
        )
        self.assertIsNotNone(exc, "Expected exception after retries exhausted")
        self.assertIsInstance(exc, TransientLLMError)
        self.assertEqual(mock_sleep.call_count, 2)
        self.assertIsNone(result)

    # -- Streaming: error chunk after partial content (existing semantics) ------

    def test_error_chunk_after_partial_content_returns_partial(self):
        """An error chunk after partial content returns the partial message.

        When some content was already posted as an assistant message, a
        subsequent error chunk returns the partial message — it cannot be
        un-posted.  This is NOT retried (existing semantics preserved).
        """
        result, events, exc, mock_sleep = self._run_generate(
            [
                [{"content": "Hello"}, {"error": "connection lost"}],
            ]
        )
        # No exception propagated — partial message is returned
        self.assertIsNone(exc, "Expected partial message, not exception")
        self.assertIsNotNone(result, "Expected a partial assistant message")
        # No retry attempted
        mock_sleep.assert_not_called()
        # Content was partially streamed
        self.assertIn("Hello", result.body)
        # An error event was yielded
        self.assertTrue(
            any(e.get("type") == "error" for e in events),
            "Expected an error event",
        )
