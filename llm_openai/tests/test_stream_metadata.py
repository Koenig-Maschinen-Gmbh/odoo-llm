"""TEL-01: tests for terminal metadata chunks in ``_openai_process_streaming_response``.

Verifies the fork yields ``{'usage': ...}`` on the terminal usage-only chunk
(choices empty + chunk.usage present) and ``{'finish_reason': ...}`` at both
normal stream end and after a terminal error yield. Existing yield shapes
(content, reasoning, tool_calls, error) are untouched.

Pure unit test — no DB, no provider call. Feeds synthetic chunk objects to
the stream processor and asserts the yielded dict sequence.
"""

from odoo.tests import TransactionCase, tagged


class _FakeDelta:
    """Mimics ``openai.types.chat.ChatCompletionChunk.choices[0].delta``."""

    def __init__(self, content=None, reasoning=None, tool_calls=None):
        self.content = content
        self.reasoning = reasoning
        self.tool_calls = tool_calls


class _FakeChoice:
    """Mimics ``openai.types.chat.ChatCompletionChunk.choices[0]``."""

    def __init__(self, delta=None, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _FakeChunk:
    """Mimics ``openai.types.chat.ChatCompletionChunk``."""

    def __init__(self, choices=None, usage=None):
        self.choices = choices if choices is not None else []
        self.usage = usage


class _FakeUsage:
    """Mimics ``openai.types.CompletionUsage``."""

    def __init__(self, prompt_tokens=0, completion_tokens=0, total_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


@tagged("post_install", "-at_install")
class TestOpenAIStreamMetadata(TransactionCase):
    """Verify terminal metadata chunks are yielded correctly (TEL-01 Step 2)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Stream Metadata Test Provider",
                    "service": "openai",
                    "api_base": "https://test.example.com/v1",
                    "api_key": "sk-test-key",
                }
            )
        )

    def _stream(self, chunks):
        """Run ``_openai_process_streaming_response`` and collect yielded dicts."""
        provider = self.provider.sudo()
        return list(provider._openai_process_streaming_response(iter(chunks)))

    def test_usage_chunk_yielded_when_choices_empty(self):
        """A terminal usage-only chunk (choices empty + usage present) is yielded."""
        chunks = [
            _FakeChunk(
                choices=[
                    _FakeChoice(delta=_FakeDelta(content="Hello"), finish_reason=None)
                ],
            ),
            _FakeChunk(
                choices=[],
                usage=_FakeUsage(
                    prompt_tokens=10, completion_tokens=5, total_tokens=15
                ),
            ),
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(), finish_reason="stop")],
            ),
        ]
        yielded = self._stream(chunks)
        usage_yields = [y for y in yielded if "usage" in y]
        self.assertEqual(len(usage_yields), 1, "Expected exactly one usage yield")
        self.assertEqual(usage_yields[0]["usage"]["prompt_tokens"], 10)
        self.assertEqual(usage_yields[0]["usage"]["completion_tokens"], 5)
        self.assertEqual(usage_yields[0]["usage"]["total_tokens"], 15)

    def test_finish_reason_yielded_at_normal_end(self):
        """finish_reason is yielded once at normal stream end."""
        chunks = [
            _FakeChunk(
                choices=[
                    _FakeChoice(delta=_FakeDelta(content="Hi"), finish_reason=None)
                ],
            ),
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(), finish_reason="stop")],
            ),
        ]
        yielded = self._stream(chunks)
        finish_yields = [y for y in yielded if "finish_reason" in y]
        self.assertEqual(
            len(finish_yields), 1, "Expected one finish_reason yield at end"
        )
        self.assertEqual(finish_yields[0]["finish_reason"], "stop")

    def test_finish_reason_yielded_after_error(self):
        """finish_reason is yielded after a terminal error yield too."""

        class _RaisingIter:
            """Yields the good chunk, then raises on the next __next__."""

            def __init__(self, items):
                self._items = list(items)
                self._i = 0

            def __iter__(self):
                return self

            def __next__(self):
                if self._i >= len(self._items):
                    raise RuntimeError("stream boom")
                item = self._items[self._i]
                self._i += 1
                return item

        # One chunk with finish_reason="stop", then the iterator raises.
        chunks = [
            _FakeChunk(
                choices=[
                    _FakeChoice(delta=_FakeDelta(content="Hi"), finish_reason="stop")
                ],
            ),
        ]
        provider = self.provider.sudo()
        yielded = list(
            provider._openai_process_streaming_response(_RaisingIter(chunks))
        )
        error_yields = [y for y in yielded if "error" in y]
        self.assertTrue(len(error_yields) >= 1, "Expected an error yield")
        finish_yields = [y for y in yielded if "finish_reason" in y]
        self.assertEqual(
            len(finish_yields),
            1,
            "Expected finish_reason yield after error (captured before the raise)",
        )
        self.assertEqual(finish_yields[0]["finish_reason"], "stop")

    def test_existing_content_yields_untouched(self):
        """Content chunks are still yielded normally alongside metadata."""
        chunks = [
            _FakeChunk(
                choices=[
                    _FakeChoice(delta=_FakeDelta(content="Hello "), finish_reason=None)
                ],
            ),
            _FakeChunk(
                choices=[
                    _FakeChoice(delta=_FakeDelta(content="world"), finish_reason=None)
                ],
            ),
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(), finish_reason="stop")],
            ),
        ]
        yielded = self._stream(chunks)
        content_yields = [y for y in yielded if "content" in y]
        self.assertEqual(len(content_yields), 2, "Expected two content yields")
        self.assertEqual(content_yields[0]["content"], "Hello ")
        self.assertEqual(content_yields[1]["content"], "world")

    def test_reasoning_chunks_yielded(self):
        """Reasoning chunks are yielded (D-REASONING, unchanged)."""
        chunks = [
            _FakeChunk(
                choices=[
                    _FakeChoice(
                        delta=_FakeDelta(reasoning="thinking..."), finish_reason=None
                    )
                ],
            ),
            _FakeChunk(
                choices=[
                    _FakeChoice(
                        delta=_FakeDelta(content="answer"), finish_reason="stop"
                    )
                ],
            ),
        ]
        yielded = self._stream(chunks)
        reasoning_yields = [y for y in yielded if "reasoning" in y]
        self.assertEqual(len(reasoning_yields), 1)
        self.assertEqual(reasoning_yields[0]["reasoning"], "thinking...")
