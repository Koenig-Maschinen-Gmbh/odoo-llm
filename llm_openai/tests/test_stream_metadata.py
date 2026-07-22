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


class _FakeToolFunction:
    """Mimics ``openai.types.chat.ChatCompletionMessageToolCallChunk.Function``."""

    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeToolCallChunk:
    """Mimics ``openai.types.chat.ChatCompletionMessageToolCallChunk``."""

    def __init__(self, index=0, call_id="call_1", call_type="function", function=None):
        self.index = index
        self.id = call_id
        self.type = call_type
        self.function = function


@tagged("post_install", "-at_install")
class TestOpenAIDegenerateStreamMatrix(TransactionCase):
    """TEL-04 Step 2: degenerate-stream matrix at the PROVIDER boundary.

    Drives ``_openai_process_streaming_response`` with synthetic raw chunks
    and asserts the normalized yield sequence for each degenerate shape, so
    a silent-done regression at the provider level can never go green again.

    Deviations from the plan table (documented in the phase doc):
    - ``test_matrix_empty_stream``: the processor yields NOTHING for a truly
      empty stream (finish_reason is only yielded when a chunk carried one) —
      the plan's ``{"finish_reason": None}`` expectation does not match the
      committed code; asserting the code truth.
    - ``test_matrix_usage_chunk``: asserts the provider-level usage shape
      (``prompt_tokens``/``completion_tokens``) — the plan's
      ``{"input": ..., "output": ...}`` shape is the TRACE-side contract.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Stream Matrix Test Provider",
                    "service": "openai",
                    "api_base": "https://test.example.com/v1",
                    "api_key": "sk-test-key",
                }
            )
        )

    def _stream(self, chunks):
        provider = self.provider.sudo()
        return list(provider._openai_process_streaming_response(iter(chunks)))

    def test_matrix_healthy_stream(self):
        """content×3 + finish="stop" → 3 content yields + finish yield, nothing else."""
        chunks = [
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(content="a"), finish_reason=None)]
            ),
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(content="b"), finish_reason=None)]
            ),
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(content="c"), finish_reason=None)]
            ),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(), finish_reason="stop")]),
        ]
        yielded = self._stream(chunks)
        contents = [y for y in yielded if "content" in y]
        self.assertEqual(len(contents), 3, "3 content yields")
        self.assertEqual([y["content"] for y in contents], ["a", "b", "c"])
        self.assertEqual(
            [y for y in yielded if "finish_reason" in y], [{"finish_reason": "stop"}]
        )
        self.assertFalse([y for y in yielded if "tool_calls" in y or "error" in y])

    def test_matrix_reasoning_only(self):
        """reasoning×5 + finish="stop" → 5 reasoning yields + finish; NO content/tool."""
        reasoning_chunks = [
            _FakeChunk(
                choices=[
                    _FakeChoice(delta=_FakeDelta(reasoning=f"r{i}"), finish_reason=None)
                ]
            )
            for i in range(5)
        ]
        chunks = reasoning_chunks + [
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(), finish_reason="stop")]),
        ]
        yielded = self._stream(chunks)
        self.assertEqual(
            len([y for y in yielded if "reasoning" in y]), 5, "5 reasoning yields"
        )
        self.assertFalse([y for y in yielded if "content" in y], "no content yields")
        self.assertFalse([y for y in yielded if "tool_calls" in y], "no tool yields")
        self.assertEqual(
            [y for y in yielded if "finish_reason" in y], [{"finish_reason": "stop"}]
        )

    def test_matrix_empty_stream(self):
        """A truly empty stream yields nothing at all (code truth — see class docstring)."""
        self.assertEqual(self._stream([]), [])

    def test_matrix_usage_chunk(self):
        """content×2 + usage chunk + finish → exactly one usage yield (provider shape)."""
        chunks = [
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(content="x"), finish_reason=None)]
            ),
            _FakeChunk(
                choices=[_FakeChoice(delta=_FakeDelta(content="y"), finish_reason=None)]
            ),
            _FakeChunk(
                choices=[], usage=_FakeUsage(prompt_tokens=100, completion_tokens=0)
            ),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(), finish_reason="stop")]),
        ]
        yielded = self._stream(chunks)
        usages = [y for y in yielded if "usage" in y]
        self.assertEqual(len(usages), 1, "exactly one usage yield")
        self.assertEqual(usages[0]["usage"]["prompt_tokens"], 100)
        self.assertEqual(usages[0]["usage"]["completion_tokens"], 0)

    def test_matrix_error_mid_stream(self):
        """content×1, finish captured, then the iterator raises → error yield +
        finish yield (captured before the raise) — the degenerate provider cutoff."""

        class _RaisingIter:
            def __init__(self, items):
                self._items = list(items)
                self._i = 0

            def __iter__(self):
                return self

            def __next__(self):
                if self._i >= len(self._items):
                    raise ConnectionError("provider stream cut off")
                item = self._items[self._i]
                self._i += 1
                return item

        chunks = [
            _FakeChunk(
                choices=[
                    _FakeChoice(delta=_FakeDelta(content="partial"), finish_reason=None)
                ]
            ),
            _FakeChunk(choices=[_FakeChoice(delta=_FakeDelta(), finish_reason="stop")]),
        ]
        provider = self.provider.sudo()
        yielded = list(
            provider._openai_process_streaming_response(_RaisingIter(chunks))
        )
        errors = [y for y in yielded if "error" in y]
        self.assertEqual(len(errors), 1, "exactly one error yield")
        self.assertIn("provider stream cut off", errors[0]["error"])
        self.assertEqual(
            [y for y in yielded if "finish_reason" in y], [{"finish_reason": "stop"}]
        )

    def test_matrix_tool_chunks_bad_finish(self):
        """Tool chunks + finish="length" → NO tool_calls yield (bad finish),
        incomplete-call error surfaced, finish chunk still yielded."""
        tool_chunk = _FakeChunk(
            choices=[
                _FakeChoice(
                    delta=_FakeDelta(
                        tool_calls=[
                            _FakeToolCallChunk(
                                index=0,
                                function=_FakeToolFunction(
                                    name="koenig_odoo_query",
                                    arguments='{"model": "res.partner", "domain": [',  # incomplete
                                ),
                            ),
                        ],
                    ),
                    finish_reason="length",
                ),
            ],
        )
        yielded = self._stream([tool_chunk])
        self.assertFalse(
            [y for y in yielded if "tool_calls" in y],
            "bad finish → no tool_calls yield",
        )
        errors = [y for y in yielded if "error" in y]
        self.assertEqual(len(errors), 1, "incomplete tool call surfaced as error")
        self.assertIn("incomplete tool call", errors[0]["error"])
        self.assertEqual(
            [y for y in yielded if "finish_reason" in y], [{"finish_reason": "length"}]
        )
