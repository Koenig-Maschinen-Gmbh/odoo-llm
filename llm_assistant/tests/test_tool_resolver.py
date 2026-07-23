"""Tests for CAP-02 tool resolver — bundles + capability gate.

CAP-01 made ``llm.thread._effective_tools()`` resolve the tool set live from the
bound assistant. CAP-02 widens that single seam into the resolver:

    effective = candidate ∩ per-turn availability gates

where ``candidate`` = ``assistant._resolved_tools()`` (explicit ``tool_ids`` ∪
subscribed ``bundle_ids``) ± the per-thread deviation, and the availability gate
(``llm.tool._ai_is_available_for``) drops inactive tools and tools whose
``requires_capability`` does not match the thread model's ``model_use``.

These tests assert the real post-condition of the resolver — the exact effective
set — not a proxy.
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestToolResolver(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Tool = cls.env["llm.tool"]

        def mk_tool(name, **kw):
            return Tool.create(
                {"name": name, "implementation": "function", "description": name, **kw}
            )

        cls.t_explicit = mk_tool("cap02_explicit")
        cls.t_bundle = mk_tool("cap02_bundle")
        cls.t_bundle2 = mk_tool("cap02_bundle2")
        cls.t_multimodal = mk_tool("cap02_multimodal", requires_capability="multimodal")
        cls.t_inactive = mk_tool("cap02_inactive")

        cls.bundle = cls.env["llm.tool.bundle"].create(
            {
                "name": "CAP02 Bundle",
                "code": "cap02_bundle",
                "tool_ids": [(6, 0, [cls.t_bundle.id])],
            }
        )

        cls.provider = cls.env["llm.provider"].create(
            {"name": "CAP02 Provider", "service": "openai"}
        )
        cls.chat_model = cls.env["llm.model"].create(
            {"name": "cap02-chat", "provider_id": cls.provider.id, "model_use": "chat"}
        )
        cls.mm_model = cls.env["llm.model"].create(
            {
                "name": "cap02-mm",
                "provider_id": cls.provider.id,
                "model_use": "multimodal",
            }
        )
        cls.prompt = cls.env["llm.prompt"].create(
            {"name": "CAP02 Prompt", "template": "You are a test assistant."}
        )
        cls.assistant = cls.env["llm.assistant"].create(
            {
                "name": "CAP02 Assistant",
                "provider_id": cls.provider.id,
                "model_id": cls.chat_model.id,
                "prompt_id": cls.prompt.id,
                "tool_ids": [(6, 0, [cls.t_explicit.id])],
                "bundle_ids": [(6, 0, [cls.bundle.id])],
            }
        )

    def _mk_thread(self, model=None):
        return self.env["llm.thread"].create(
            {
                "name": "CAP02 Thread",
                "provider_id": self.provider.id,
                "model_id": (model or self.chat_model).id,
                "assistant_id": self.assistant.id,
            }
        )

    def test_bundle_and_explicit_union(self):
        """Effective set = explicit tool_ids ∪ subscribed bundle tools."""
        thread = self._mk_thread()
        self.assertEqual(
            set(thread._effective_tools().ids),
            {self.t_explicit.id, self.t_bundle.id},
            "Resolver must union explicit tool_ids with subscribed bundle tools.",
        )

    def test_new_tool_in_bundle_reaches_thread_live(self):
        """A tool added to a subscribed bundle reaches existing threads live."""
        thread = self._mk_thread()
        self.bundle.write({"tool_ids": [(4, self.t_bundle2.id)]})
        self.assertIn(
            self.t_bundle2.id,
            thread._effective_tools().ids,
            "Adding a tool to a subscribed bundle must reach the thread with no "
            "re-selection and no migration.",
        )

    def test_inactive_bundle_excluded(self):
        """Tools of a deactivated bundle are not offered."""
        thread = self._mk_thread()
        self.bundle.write({"active": False})
        self.assertNotIn(
            self.t_bundle.id,
            thread._effective_tools().ids,
            "A deactivated bundle must not contribute its tools.",
        )

    def test_inactive_tool_excluded(self):
        """A deactivated tool is never offered, even when linked."""
        self.assistant.write({"tool_ids": [(4, self.t_inactive.id)]})
        thread = self._mk_thread()
        self.assertIn(
            self.t_inactive.id,
            thread._effective_tools().ids,
            "Precondition: the active linked tool is offered.",
        )
        self.t_inactive.write({"active": False})
        self.assertNotIn(
            self.t_inactive.id,
            thread._effective_tools().ids,
            "A deactivated tool must be dropped by the registry gate.",
        )

    def test_capability_gate_hides_on_wrong_model(self):
        """requires_capability='multimodal' hidden on a chat model."""
        self.assistant.write({"tool_ids": [(4, self.t_multimodal.id)]})
        chat_thread = self._mk_thread(self.chat_model)
        self.assertNotIn(
            self.t_multimodal.id,
            chat_thread._effective_tools().ids,
            "A multimodal-requiring tool must be hidden on a chat model.",
        )

    def test_capability_gate_shows_on_matching_model(self):
        """requires_capability='multimodal' offered on a multimodal model."""
        self.assistant.write({"tool_ids": [(4, self.t_multimodal.id)]})
        mm_thread = self._mk_thread(self.mm_model)
        self.assertIn(
            self.t_multimodal.id,
            mm_thread._effective_tools().ids,
            "A multimodal-requiring tool must be offered on a multimodal model.",
        )

    def test_deviation_combines_with_bundles(self):
        """tool_ids_extra / tool_ids_disabled still deviate the resolved set."""
        thread = self._mk_thread()
        thread.write(
            {
                "tool_ids_disabled": [(6, 0, [self.t_bundle.id])],
                "tool_ids_extra": [(6, 0, [self.t_bundle2.id])],
            }
        )
        self.assertEqual(
            set(thread._effective_tools().ids),
            {self.t_explicit.id, self.t_bundle2.id},
            "Deviation must apply on top of the (explicit ∪ bundle) candidate.",
        )

    def test_resolved_tools_active_only(self):
        """assistant._resolved_tools() filters to active tools."""
        self.t_bundle.write({"active": False})
        self.assertNotIn(
            self.t_bundle.id,
            self.assistant._resolved_tools().ids,
            "_resolved_tools must exclude inactive tools.",
        )

    def test_no_assistant_thread_capability_gated(self):
        """A no-assistant thread's raw tool_ids is still capability-gated."""
        thread = self.env["llm.thread"].create(
            {
                "name": "CAP02 No-Assistant",
                "provider_id": self.provider.id,
                "model_id": self.chat_model.id,
                "tool_ids": [(6, 0, [self.t_explicit.id, self.t_multimodal.id])],
            }
        )
        thread.write(
            {
                "assistant_id": False,
                "tool_ids": [(6, 0, [self.t_explicit.id, self.t_multimodal.id])],
            }
        )
        self.assertEqual(
            set(thread._effective_tools().ids),
            {self.t_explicit.id},
            "Environment gates (capability) apply to no-assistant threads too.",
        )
