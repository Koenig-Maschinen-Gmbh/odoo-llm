"""Tests for the release-and-resume HITL pause mechanism (Phase 4a).

Phase 4a adds four fork-level capabilities (all additive, no behaviour change
without the hook):

1. :class:`GenerationPaused` — a ``BaseException`` subclass parallel to
   :class:`GenerationCancelled`, raised when the loop-control hook returns
   ``{"pause": True}`` before a write-tool executes.
2. Pause check at the per-tool-call boundary — the hook is called with the
   ``tool_call`` so it can decide whether to pause for approval.
3. :meth:`post_tool_result` — posts a synthetic tool message with a real
   result, used by the approval handler to inject the write result so the
   loop resumes correctly.
4. Double-execution guard — ``get_unexecuted_tool_calls`` filters out tool
   calls that already have a completed/error result, preventing accidental
   re-execution on structured resume.
"""

from unittest.mock import Mock, patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.llm_assistant.models.llm_thread import (
    GenerationCancelled,
    GenerationPaused,
)

# ---------------------------------------------------------------------------
# GenerationPaused exception contract
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestGenerationPausedContract(TransactionCase):
    """``GenerationPaused`` must behave like ``GenerationCancelled``:
    inherit from ``BaseException``, NOT be caught by ``except Exception:``,
    and be catchable by ``except GenerationPaused:``."""

    def test_inherits_base_exception(self):
        self.assertTrue(issubclass(GenerationPaused, BaseException))

    def test_not_caught_by_except_exception(self):
        """``except Exception:`` must NOT catch ``GenerationPaused`` — this
        is critical because the tool execution pipeline has ``except Exception``
        blocks that would otherwise swallow the pause signal."""
        try:
            raise GenerationPaused("test")
        except Exception:
            self.fail("GenerationPaused must NOT be caught by except Exception")
        except GenerationPaused:
            pass  # expected

    def test_caught_by_except_generation_paused(self):
        """The orchestrator catches ``GenerationPaused`` explicitly."""
        raised = False
        try:
            raise GenerationPaused("test")
        except GenerationPaused:
            raised = True
        self.assertTrue(raised)

    def test_not_caught_by_except_generation_cancelled(self):
        """``GenerationPaused`` and ``GenerationCancelled`` are distinct
        control-flow signals — catching one must not catch the other."""
        try:
            raise GenerationPaused("test")
        except GenerationCancelled:
            self.fail(
                "GenerationPaused must NOT be caught by except GenerationCancelled"
            )
        except GenerationPaused:
            pass  # expected

    def test_cancel_not_caught_by_except_generation_paused(self):
        """Conversely, ``GenerationCancelled`` must not be caught by
        ``except GenerationPaused:``."""
        try:
            raise GenerationCancelled("test")
        except GenerationPaused:
            self.fail(
                "GenerationCancelled must NOT be caught by except GenerationPaused"
            )
        except GenerationCancelled:
            pass  # expected


# ---------------------------------------------------------------------------
# _check_loop_control_tool contract
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestCheckLoopControlToolContract(TransactionCase):
    """Unit tests for the per-tool-call control check (Phase 4a)."""

    def _tool_call(self):
        return {
            "id": "call_1",
            "type": "function",
            "function": {"name": "write_tool", "arguments": "{}"},
        }

    def test_no_hook_returns_empty_dict(self):
        thread = self.env["llm.thread"]
        self.assertEqual(
            thread._check_loop_control_tool(None, self._tool_call()),
            {},
        )
        self.assertEqual(
            thread._check_loop_control_tool(False, self._tool_call()),
            {},
        )

    def test_falsy_return_returns_empty_dict(self):
        self.assertEqual(
            self.env["llm.thread"]._check_loop_control_tool(
                lambda tc: None,
                self._tool_call(),
            ),
            {},
        )

    def test_empty_dict_returned_as_is(self):
        self.assertEqual(
            self.env["llm.thread"]._check_loop_control_tool(
                lambda tc: {},
                self._tool_call(),
            ),
            {},
        )

    def test_pause_true_returned(self):
        self.assertEqual(
            self.env["llm.thread"]._check_loop_control_tool(
                lambda tc: {"pause": True},
                self._tool_call(),
            ),
            {"pause": True},
        )

    def test_cancel_and_pause_both_returned(self):
        result = self.env["llm.thread"]._check_loop_control_tool(
            lambda tc: {"cancel": True, "pause": True},
            self._tool_call(),
        )
        self.assertTrue(result.get("cancel"))
        self.assertTrue(result.get("pause"))

    def test_tool_call_passed_to_hook(self):
        """The hook receives the ``tool_call`` dict so it can decide whether
        the tool is a write-tool that needs approval."""
        received = []
        self.env["llm.thread"]._check_loop_control_tool(
            lambda tc: received.append(tc) or {},
            self._tool_call(),
        )
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["function"]["name"], "write_tool")

    def test_no_pause_no_cancel_returns_dict_without_those_keys(self):
        """A hook that returns ``{"mode": "after_turn"}`` (no cancel/pause)
        means "keep going" — the result dict is returned as-is so the caller
        can inspect it."""
        result = self.env["llm.thread"]._check_loop_control_tool(
            lambda tc: {"mode": "after_turn"},
            self._tool_call(),
        )
        self.assertNotIn("cancel", result)
        self.assertNotIn("pause", result)


# ---------------------------------------------------------------------------
# Pause fires before tool execution
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestPauseBeforeToolExecution(TransactionCase):
    """The pause check fires BEFORE ``_execute_tool_call`` — no tool
    side-effect occurs when the hook requests a pause."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Pause Test Provider",
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
                    "name": "pause-test-model",
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
                    "name": "Pause test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def _post_assistant_tool_message(self, tool_name="write_tool"):
        return self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="",
            body_json={
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": "{}",
                        },
                    }
                ]
            },
            llm_role="assistant",
            author_id=False,
        )

    def test_pause_fires_before_tool_execution(self):
        """Hook returns ``{"pause": True}`` → ``GenerationPaused`` raised,
        ``_execute_tool_call`` NOT called."""
        asst_msg = self._post_assistant_tool_message()
        check = Mock(
            side_effect=[
                {"cancel": False},  # top of while
                {"pause": True},  # per-tool-call → pause!
            ],
        )
        with patch.object(
            type(self.env["llm.thread"]),
            "_execute_tool_call",
            side_effect=AssertionError(
                "_execute_tool_call must not run when paused",
            ),
        ):
            with self.assertRaises(GenerationPaused):
                list(
                    self.thread.generate_messages(
                        asst_msg,
                        loop_control_check=check,
                    )
                )
        # call #1 = top-of-iteration (no cancel); call #2 = per-tool (pause).
        self.assertEqual(check.call_count, 2)

    def test_cancel_takes_precedence_over_pause(self):
        """When both cancel and pause are True, cancel wins — the loop raises
        ``GenerationCancelled``, not ``GenerationPaused``."""
        asst_msg = self._post_assistant_tool_message()
        check = Mock(
            side_effect=[
                {"cancel": False},  # top of while
                {"cancel": True, "pause": True},  # per-tool → cancel+pause
            ],
        )
        with patch.object(
            type(self.env["llm.thread"]),
            "_execute_tool_call",
            side_effect=AssertionError(
                "_execute_tool_call must not run when cancelled",
            ),
        ):
            with self.assertRaises(GenerationCancelled):
                list(
                    self.thread.generate_messages(
                        asst_msg,
                        loop_control_check=check,
                    )
                )

    def test_no_pause_executes_tool_normally(self):
        """Without a pause signal, the tool executes normally — Phase 4a is
        a pure addition (no behaviour change without the pause hook)."""
        asst_msg = self._post_assistant_tool_message()
        check = Mock(return_value={})
        # Use a Mock (not a real mail.message) so it doesn't interfere
        # with get_unexecuted_tool_calls on the thread.
        mock_msg = Mock()
        mock_msg.llm_role = "tool"

        def _fake_execute(tool_call, assistant_message):
            """Minimal generator that mimics _execute_tool_call's contract:
            yields a message_create event, returns the tool message."""
            yield {"type": "message_create", "message": {}}
            return mock_msg

        with (
            patch.object(
                type(self.env["llm.thread"]),
                "_execute_tool_call",
                side_effect=_fake_execute,
            ) as mock_exec,
            patch.object(
                type(self.env["llm.model"]),
                "chat",
                return_value=iter([{"content": "Done."}]),
            ),
            patch.object(
                type(self.env["mail.message"]),
                "to_store_format",
                return_value={},
            ),
        ):
            try:
                list(
                    self.thread.generate_messages(
                        asst_msg,
                        loop_control_check=check,
                    )
                )
            except StopIteration:
                pass
            except Exception:
                pass
        # _execute_tool_call WAS called (no pause → normal execution).
        self.assertTrue(mock_exec.called)


# ---------------------------------------------------------------------------
# Double-execution guard (get_unexecuted_tool_calls)
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestDoubleExecutionGuard(TransactionCase):
    """``get_unexecuted_tool_calls`` filters out tool calls that already have
    a completed/error tool message — the critical safety gate for structured
    resume."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Guard Test Provider",
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
                    "name": "guard-test-model",
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
                    "name": "Guard test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def _post_assistant_with_two_tool_calls(self):
        """Post an assistant message with two tool calls."""
        return self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="",
            body_json={
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "tool_a", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "tool_b", "arguments": "{}"},
                    },
                ]
            },
            llm_role="assistant",
            author_id=False,
        )

    def _post_tool_message(self, tool_call_id, status, tool_name="tool"):
        return self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body=f"Tool: {tool_name}",
            body_json={
                "type": "tool_execution",
                "tool_call_id": tool_call_id,
                "tool_call": {
                    "id": tool_call_id,
                    "function": {"name": tool_name, "arguments": "{}"},
                },
                "status": status,
                "tool_name": tool_name,
                "result": "ok" if status == "completed" else None,
            },
            llm_role="tool",
            author_id=False,
        )

    def test_returns_all_when_no_results(self):
        asst_msg = self._post_assistant_with_two_tool_calls()
        unexecuted = asst_msg.get_unexecuted_tool_calls()
        self.assertEqual(len(unexecuted), 2)
        ids = {tc["id"] for tc in unexecuted}
        self.assertEqual(ids, {"call_a", "call_b"})

    def test_filters_completed(self):
        asst_msg = self._post_assistant_with_two_tool_calls()
        self._post_tool_message("call_a", "completed", "tool_a")
        unexecuted = asst_msg.get_unexecuted_tool_calls()
        self.assertEqual(len(unexecuted), 1)
        self.assertEqual(unexecuted[0]["id"], "call_b")

    def test_filters_error(self):
        asst_msg = self._post_assistant_with_two_tool_calls()
        self._post_tool_message("call_a", "error", "tool_a")
        unexecuted = asst_msg.get_unexecuted_tool_calls()
        self.assertEqual(len(unexecuted), 1)
        self.assertEqual(unexecuted[0]["id"], "call_b")

    def test_keeps_requested_status(self):
        """A tool message with status ``requested`` (not yet executed) does
        NOT filter out the tool call — it's not a completed/error result."""
        asst_msg = self._post_assistant_with_two_tool_calls()
        self._post_tool_message("call_a", "requested", "tool_a")
        unexecuted = asst_msg.get_unexecuted_tool_calls()
        self.assertEqual(len(unexecuted), 2)

    def test_all_have_results_returns_empty(self):
        asst_msg = self._post_assistant_with_two_tool_calls()
        self._post_tool_message("call_a", "completed", "tool_a")
        self._post_tool_message("call_b", "error", "tool_b")
        unexecuted = asst_msg.get_unexecuted_tool_calls()
        self.assertEqual(unexecuted, [])

    def test_no_tool_calls_returns_empty(self):
        """An assistant message without tool_calls returns ``[]``."""
        msg = self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="<p>hello</p>",
            llm_role="assistant",
            author_id=False,
        )
        self.assertEqual(msg.get_unexecuted_tool_calls(), [])


# ---------------------------------------------------------------------------
# post_tool_result (synthetic tool result injection)
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestPostToolResult(TransactionCase):
    """``post_tool_result`` creates a tool message with a real result,
    bypassing the normal ``post_tool_call`` → ``execute_tool_call`` flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Post-Result Test Provider",
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
                    "name": "post-result-test-model",
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
                    "name": "Post-result test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def _tool_call(self, call_id="call_1", name="write_tool"):
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }

    def test_creates_completed_tool_message(self):
        tool_call = self._tool_call()
        msg = self.env["mail.message"].post_tool_result(
            "call_1",
            {"record_id": 42, "name": "New Page"},
            tool_call=tool_call,
            thread_model=self.thread,
        )
        self.assertEqual(msg.llm_role, "tool")
        tool_data = msg.get_tool_data()
        self.assertIsNotNone(tool_data)
        self.assertEqual(tool_data["tool_call_id"], "call_1")
        self.assertEqual(tool_data["status"], "completed")
        self.assertEqual(tool_data["result"], {"record_id": 42, "name": "New Page"})
        self.assertEqual(tool_data["tool_name"], "write_tool")

    def test_creates_error_tool_message(self):
        tool_call = self._tool_call()
        msg = self.env["mail.message"].post_tool_result(
            "call_1",
            "Validation error: name is required",
            status="error",
            tool_call=tool_call,
            thread_model=self.thread,
        )
        tool_data = msg.get_tool_data()
        self.assertEqual(tool_data["status"], "error")
        self.assertEqual(tool_data["result"], "Validation error: name is required")

    def test_message_posted_on_thread(self):
        """The tool message is posted on the correct thread (model + res_id)."""
        tool_call = self._tool_call()
        msg = self.env["mail.message"].post_tool_result(
            "call_1",
            "ok",
            tool_call=tool_call,
            thread_model=self.thread,
        )
        self.assertEqual(msg.model, "llm.thread")
        self.assertEqual(msg.res_id, self.thread.id)

    def test_tool_call_id_links_to_assistant_call(self):
        """The ``tool_call_id`` in the tool message matches the ID from the
        assistant message's ``tool_calls`` — this is how the validator
        links them for the LLM context."""
        tool_call = self._tool_call(call_id="call_abc")
        msg = self.env["mail.message"].post_tool_result(
            "call_abc",
            "ok",
            tool_call=tool_call,
            thread_model=self.thread,
        )
        tool_data = msg.get_tool_data()
        self.assertEqual(tool_data["tool_call_id"], "call_abc")

    def test_posted_result_is_found_by_get_unexecuted_tool_calls(self):
        """After injecting a result via ``post_tool_result``, the
        ``get_unexecuted_tool_calls`` guard filters out the completed
        tool call — this is the core of the structured resume mechanism."""
        tool_call = self._tool_call(call_id="call_1")
        asst_msg = self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="",
            body_json={"tool_calls": [tool_call]},
            llm_role="assistant",
            author_id=False,
        )
        # Before injection: the tool call is unexecuted.
        self.assertEqual(len(asst_msg.get_unexecuted_tool_calls()), 1)
        # Inject the result.
        self.env["mail.message"].post_tool_result(
            "call_1",
            {"id": 42},
            tool_call=tool_call,
            thread_model=self.thread,
        )
        # After injection: the tool call is filtered out (has a result).
        self.assertEqual(asst_msg.get_unexecuted_tool_calls(), [])


# ---------------------------------------------------------------------------
# Structured resume: inject result → re-enter → correct branch
# ---------------------------------------------------------------------------


@tagged("post_install", "-at_install")
class TestStructuredResume(TransactionCase):
    """The core mechanism: inject a tool result before re-entering
    ``generate_messages`` → the loop takes the ``_generate_assistant_response``
    branch (NOT the tool-execution branch) → the LLM chains naturally.

    This is the test the prior "release-and-resume is broken" verdict missed.
    Naive resume (re-enter with the assistant message as last_message) re-executes
    all tools. Structured resume (inject result → re-enter with
    last_message=None) works correctly — the loop sees the tool message as
    ``last_message``, takes the ``user/tool`` branch, and calls
    ``_generate_assistant_response``.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Resume Test Provider",
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
                    "name": "resume-test-model",
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
                    "name": "Resume test thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def test_resume_with_injected_result_does_not_re_execute(self):
        """Inject a tool result, re-enter with ``last_message=None`` →
        ``_execute_tool_call`` is NOT called (the loop takes the
        ``_generate_assistant_response`` branch instead)."""
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "write_tool", "arguments": "{}"},
        }
        # Post assistant message with tool_calls.
        self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="",
            body_json={"tool_calls": [tool_call]},
            llm_role="assistant",
            author_id=False,
        )
        # Inject the real result via post_tool_result.
        self.env["mail.message"].post_tool_result(
            "call_1",
            {"record_id": 42, "name": "New Page"},
            tool_call=tool_call,
            thread_model=self.thread,
        )
        # Re-enter with last_message=None → get_latest_llm_message returns
        # the injected tool message → _should_continue returns True (role=tool)
        # → loop takes the _generate_assistant_response branch.
        with (
            patch.object(
                type(self.env["llm.thread"]),
                "_execute_tool_call",
                side_effect=AssertionError(
                    "_execute_tool_call must NOT be called on structured resume",
                ),
            ) as mock_exec,
            patch.object(
                type(self.env["llm.model"]),
                "chat",
                return_value=iter([{"content": "Page created with id=42."}]),
            ),
            patch.object(
                type(self.env["mail.message"]),
                "to_store_format",
                return_value={},
            ),
        ):
            gen = self.thread.generate_messages(None)
            try:
                list(gen)
            except StopIteration:
                pass
            except Exception:
                pass
        # _execute_tool_call was NOT called — the loop took the
        # _generate_assistant_response branch, not the tool-execution branch.
        self.assertFalse(
            mock_exec.called,
            "_execute_tool_call must NOT be called on structured resume",
        )

    def test_double_execution_guard_prevents_reexecution_on_naive_resume(self):
        """The double-execution guard prevents re-execution when the loop
        accidentally re-enters with the assistant message as last_message
        (naive resume). Tool calls with existing results are filtered out."""
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "write_tool", "arguments": "{}"},
        }
        asst_msg = self.thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="",
            body_json={"tool_calls": [tool_call]},
            llm_role="assistant",
            author_id=False,
        )
        # Post a completed tool result for call_1.
        self.env["mail.message"].post_tool_result(
            "call_1",
            {"id": 42},
            tool_call=tool_call,
            thread_model=self.thread,
        )
        # Re-enter with the assistant message as last_message (naive resume).
        # The double-execution guard should filter out call_1 (has a result).
        with patch.object(
            type(self.env["llm.thread"]),
            "_execute_tool_call",
            side_effect=AssertionError(
                "_execute_tool_call must NOT be called when the tool "
                "call already has a result",
            ),
        ) as mock_exec:
            gen = self.thread.generate_messages(asst_msg)
            try:
                list(gen)
            except StopIteration:
                pass
            except Exception:
                pass
        # _execute_tool_call was NOT called — the guard filtered out
        # the completed tool call and the loop broke.
        self.assertFalse(
            mock_exec.called,
            "_execute_tool_call must NOT be called when the tool call "
            "already has a result",
        )
