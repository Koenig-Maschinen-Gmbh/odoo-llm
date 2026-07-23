"""Tests for CAP-01 live tool resolution — ``llm.thread._effective_tools()``.

The thread used to carry a per-thread ``tool_ids`` SNAPSHOT copied from
``assistant.tool_ids`` at assistant-selection. Execution read that snapshot, so a
tool added to an assistant after a thread was created never reached the thread
(97% of live threads were stale). CAP-01 resolves the tool set LIVE from the
bound assistant:

    effective = (assistant.tool_ids | tool_ids_extra) - tool_ids_disabled

These tests assert the real post-condition of that resolution — not a proxy:

1. A thread whose raw ``tool_ids`` snapshot is EMPTY still resolves its
   assistant's current tools (the staleness fix).
2. A tool added to the assistant AFTER thread creation reaches the thread with
   no re-selection (live).
3. ``tool_ids_disabled`` / ``tool_ids_extra`` deviate the effective set.
4. A thread with no assistant falls back to its raw ``tool_ids`` column.
5. ``_prepare_chat_kwargs`` offers the model the EFFECTIVE set, not the snapshot.
6. The ``mail.message`` tool-availability validation reads the effective set, so
   a tool present on the assistant but absent from a stale snapshot is accepted,
   and an unknown tool is refused.
"""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEffectiveTools(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Tool = cls.env["llm.tool"]

        def mk_tool(name):
            return Tool.create(
                {"name": name, "implementation": "function", "description": name}
            )

        cls.t1 = mk_tool("cap01_tool_1")
        cls.t2 = mk_tool("cap01_tool_2")
        cls.t3 = mk_tool("cap01_tool_3")
        cls.provider = cls.env["llm.provider"].create(
            {"name": "CAP01 Provider", "service": "openai"}
        )
        cls.model = cls.env["llm.model"].create(
            {"name": "cap01-model", "provider_id": cls.provider.id}
        )
        cls.prompt = cls.env["llm.prompt"].create(
            {"name": "CAP01 Prompt", "template": "You are a test assistant."}
        )
        cls.assistant = cls.env["llm.assistant"].create(
            {
                "name": "CAP01 Assistant",
                "provider_id": cls.provider.id,
                "model_id": cls.model.id,
                "prompt_id": cls.prompt.id,
                "tool_ids": [(6, 0, [cls.t1.id, cls.t2.id])],
            }
        )
        # Plain create with assistant_id: the ORM onchange does NOT fire, so the
        # raw tool_ids snapshot stays EMPTY — exactly the state that used to make
        # the model see no tools.
        cls.thread = cls.env["llm.thread"].create(
            {
                "name": "CAP01 Thread",
                "provider_id": cls.provider.id,
                "model_id": cls.model.id,
                "assistant_id": cls.assistant.id,
            }
        )

    def test_effective_tools_derives_live_from_assistant(self):
        """Empty raw snapshot, yet the effective set is the assistant's tools."""
        self.assertFalse(
            self.thread.tool_ids,
            "Precondition: plain create leaves the raw tool_ids snapshot empty.",
        )
        self.assertEqual(
            set(self.thread._effective_tools().ids),
            {self.t1.id, self.t2.id},
            "Effective tools must derive live from the assistant, not the "
            "empty raw snapshot.",
        )

    def test_effective_tools_reflects_assistant_change_live(self):
        """A tool added to the assistant reaches the thread with no re-select."""
        self.assistant.write({"tool_ids": [(4, self.t3.id)]})
        self.assertEqual(
            set(self.thread._effective_tools().ids),
            {self.t1.id, self.t2.id, self.t3.id},
            "Adding a tool to the assistant must reach existing threads live.",
        )

    def test_tool_ids_disabled_removes_from_effective(self):
        self.thread.write({"tool_ids_disabled": [(6, 0, [self.t2.id])]})
        self.assertEqual(
            set(self.thread._effective_tools().ids),
            {self.t1.id},
            "tool_ids_disabled must remove a tool from the effective set.",
        )

    def test_tool_ids_extra_adds_to_effective(self):
        self.thread.write({"tool_ids_extra": [(6, 0, [self.t3.id])]})
        self.assertEqual(
            set(self.thread._effective_tools().ids),
            {self.t1.id, self.t2.id, self.t3.id},
            "tool_ids_extra must add a tool on top of the assistant's set.",
        )

    def test_disabled_and_extra_combined(self):
        self.thread.write(
            {
                "tool_ids_disabled": [(6, 0, [self.t1.id])],
                "tool_ids_extra": [(6, 0, [self.t3.id])],
            }
        )
        self.assertEqual(
            set(self.thread._effective_tools().ids),
            {self.t2.id, self.t3.id},
            "effective = (assistant | extra) - disabled.",
        )

    def test_no_assistant_falls_back_to_raw_tool_ids(self):
        """Without an assistant, effective == the raw tool_ids column (base)."""
        thread = self.env["llm.thread"].create(
            {
                "name": "CAP01 No-Assistant Thread",
                "provider_id": self.provider.id,
                "model_id": self.model.id,
                "tool_ids": [(6, 0, [self.t1.id])],
            }
        )
        # Downstream modules (e.g. the koenig orchestrator) default a new thread
        # to an assistant on create; force the no-assistant state explicitly so
        # this fork-level assertion is environment-agnostic.
        thread.write({"assistant_id": False, "tool_ids": [(6, 0, [self.t1.id])]})
        self.assertFalse(thread.assistant_id)
        self.assertEqual(
            set(thread._effective_tools().ids),
            {self.t1.id},
            "No-assistant threads must fall back to their raw tool_ids column.",
        )

    def test_prepare_chat_kwargs_offers_effective_tools(self):
        """The model is offered the effective set, not the stale snapshot."""
        kwargs = self.thread._prepare_chat_kwargs([], use_streaming=False)
        self.assertEqual(
            set(kwargs["tools"].ids),
            {self.t1.id, self.t2.id},
            "_prepare_chat_kwargs must offer _effective_tools(), not tool_ids.",
        )
        # And it must equal the seam exactly.
        self.assertEqual(
            set(kwargs["tools"].ids), set(self.thread._effective_tools().ids)
        )

    def test_tool_validation_reads_effective_set(self):
        """mail.message tool-availability validation uses the effective set.

        A tool present on the assistant but absent from the (empty) raw snapshot
        must be ACCEPTED; a tool absent from the effective set must be REFUSED.
        """
        Message = self.env["mail.message"]
        good_call = {
            "id": "call_good",
            "type": "function",
            "function": {"name": self.t1.name, "arguments": "{}"},
        }
        # Must not raise "Tool ... not found in thread" — t1 is on the assistant
        # even though the raw snapshot is empty.
        Message.post_tool_call(good_call, thread_model=self.thread)

        bad_call = {
            "id": "call_bad",
            "type": "function",
            "function": {"name": "cap01_unknown_tool", "arguments": "{}"},
        }
        with self.assertRaises(UserError):
            Message.post_tool_call(bad_call, thread_model=self.thread)
