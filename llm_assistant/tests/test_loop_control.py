"""Tests for the cooperative loop-control hook (cancel guarantee Phase 0).

``generate_messages`` accepts an optional ``loop_control_check`` callable
returning ``{"cancel": bool}``. The loop consults it at three sites — before
each ``while`` iteration, before each tool-call execution, and between stream
chunks — and raises :class:`GenerationCancelled` as soon as it reports a
cancel. With no hook (or a hook that never cancels) the loop is unchanged.
"""

from unittest.mock import Mock, patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.llm_assistant.models.llm_thread import GenerationCancelled


@tagged("post_install", "-at_install")
class TestCheckLoopControlContract(TransactionCase):
    """Unit tests for the ``_check_loop_control`` helper (the hook contract)."""

    def test_no_hook_returns_false(self):
        thread = self.env["llm.thread"]
        self.assertFalse(thread._check_loop_control(None))
        self.assertFalse(thread._check_loop_control(False))

    def test_falsy_return_returns_false(self):
        self.assertFalse(self.env["llm.thread"]._check_loop_control(lambda: None))

    def test_dict_without_cancel_returns_false(self):
        self.assertFalse(self.env["llm.thread"]._check_loop_control(lambda: {}))

    def test_cancel_false_returns_false(self):
        self.assertFalse(
            self.env["llm.thread"]._check_loop_control(lambda: {"cancel": False})
        )

    def test_cancel_true_returns_true(self):
        self.assertTrue(
            self.env["llm.thread"]._check_loop_control(lambda: {"cancel": True})
        )

    def test_truthy_cancel_returns_true(self):
        self.assertTrue(
            self.env["llm.thread"]._check_loop_control(lambda: {"cancel": 1})
        )


@tagged("post_install", "-at_install")
class TestGenerateMessagesCancelHook(TransactionCase):
    """The hook aborts ``generate_messages`` cooperatively at each check site."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Loop-control Test Provider",
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
                    "name": "loop-control-test-model",
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
                    "name": "Loop-control test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def _post_user_message(self):
        return self.thread.with_context(mail_create_nosubscribe=True).message_post(
            body="<p>hello</p>",
            llm_role="user",
            author_id=self.env.user.partner_id.id,
        )

    def _post_assistant_tool_message(self):
        return self.thread.with_context(mail_create_nosubscribe=True).message_post(
            body="",
            body_json={
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "noop", "arguments": "{}"},
                    }
                ]
            },
            llm_role="assistant",
            author_id=False,
        )

    def test_cancel_at_iteration_top_raises_before_any_llm_call(self):
        """A hook that cancels on the first check aborts the loop at the top of
        the first iteration — before any LLM/chat call is made."""
        user_msg = self._post_user_message()
        check = Mock(side_effect=[{"cancel": True}])
        with patch.object(
            type(self.env["llm.model"]),
            "chat",
            side_effect=AssertionError(
                "chat() must not be called when cancelled at iteration top",
            ),
        ):
            with self.assertRaises(GenerationCancelled):
                list(self.thread.generate_messages(user_msg, loop_control_check=check))
        self.assertEqual(check.call_count, 1)

    def test_cancel_before_tool_call_does_not_execute_tool(self):
        """With an assistant tool-call message, the per-tool-call check fires
        before ``_execute_tool_call`` — the tool is NOT executed."""
        asst_msg = self._post_assistant_tool_message()
        check = Mock(side_effect=[{"cancel": False}, {"cancel": True}])
        with patch.object(
            type(self.env["llm.thread"]),
            "_execute_tool_call",
            side_effect=AssertionError(
                "_execute_tool_call must not run when cancelled before the tool call",
            ),
        ):
            with self.assertRaises(GenerationCancelled):
                list(self.thread.generate_messages(asst_msg, loop_control_check=check))
        # call #1 = top-of-iteration (no cancel); call #2 = per-tool-call (cancel).
        self.assertEqual(check.call_count, 2)

    def test_cancel_between_stream_chunks(self):
        """The between-chunks check aborts ``_handle_streaming_response`` after
        the current chunk — only the first chunk is processed, the second is not."""
        check = Mock(side_effect=[{"cancel": False}, {"cancel": True}])
        stream = [{"content": "Hello"}, {"content": " world"}]
        # to_store_format is irrelevant to the cancel behavior; stub it so the
        # test does not depend on the full store serialization pipeline.
        with patch.object(
            type(self.env["mail.message"]),
            "to_store_format",
            return_value={},
        ):
            gen = self.thread._handle_streaming_response(
                stream,
                loop_control_check=check,
            )
            # Use try/except (not assertRaises): the first chunk's message_post
            # + body write happen BEFORE the cancel raises, and assertRaises
            # wraps its body in a savepoint that is rolled back when the
            # expected exception fires — erasing those writes. try/except keeps
            # them so we can verify "only chunk 1 was processed" afterwards.
            cancelled = False
            try:
                list(gen)
            except GenerationCancelled:
                cancelled = True
        self.assertTrue(
            cancelled,
            "the between-chunks check must raise GenerationCancelled",
        )
        self.assertEqual(check.call_count, 2)
        # The thread's creation log ("LLM Chat Thread created") is also on
        # res_id; filter to the assistant message whose body holds the first
        # chunk and confirm the second chunk was never appended.
        msgs = (
            self.env["mail.message"]
            .sudo()
            .search(
                [("model", "=", "llm.thread"), ("res_id", "=", self.thread.id)],
            )
        )
        asst = msgs.filtered(lambda m: "Hello" in (m.body or ""))
        self.assertTrue(
            asst,
            "the streaming path must have created an assistant message with the first chunk",
        )
        self.assertNotIn("world", asst.body or "")

    def test_no_hook_is_a_pure_addition(self):
        """Without a hook the loop is unchanged: a terminal assistant message
        (no tool calls) makes ``_should_continue`` False, so the loop body
        never runs and ``generate_messages`` returns the message unchanged.
        Pins that the ``loop_control_check`` kwarg (default None) is a pure
        addition — no GenerationCancelled, no behaviour change."""
        asst_msg = self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="<p>final answer</p>",
            llm_role="assistant",
            author_id=False,
        )
        gen = self.thread.generate_messages(asst_msg)
        returned = None
        try:
            while True:
                next(gen)
        except StopIteration as exc:
            returned = exc.value
        self.assertEqual(returned, asst_msg)
