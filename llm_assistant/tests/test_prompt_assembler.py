"""Tests for CAP-03 deterministic prompt-fragment assembler.

``llm.thread._build_system_messages()`` replaces the old
``get_prepend_messages()`` override chain with ONE assembler that:

1. Collects fragments from ``_system_prompt_fragments()`` (super() chain).
2. Collects per-tool fragments from ``tool._system_prompt_fragment(thread)``
   for each tool in ``_effective_tools()`` (CAP-02 fix — NOT
   ``assistant.tool_ids``).
3. Adds a consent fragment for ``requires_user_consent`` tools.
4. Sorts by ``sequence`` (deterministic order, not MRO-accidental).
5. Applies a token budget (drops droppable fragments in reverse sequence).

These tests assert real post-conditions (not proxies):
- The exact fragment order for base fragments (persona before summary).
- Per-tool fragments key off the EFFECTIVE set (hidden tools contribute
  nothing).
- Token budget drops droppable fragments, never guardrails/persona/consent.
- Consent is expressed once, from the effective set.
- Backward compat: ``get_prepend_messages()`` delegates to the assembler.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.llm_assistant.models.llm_thread import SystemPromptFragment


@tagged("post_install", "-at_install")
class TestPromptAssembler(TransactionCase):
    """CAP-03 assembler tests (fork-only — no koenig contributors)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Tool = cls.env["llm.tool"]

        def mk_tool(name, **kw):
            vals = {"name": name, "implementation": "function", "description": name}
            vals.update(kw)
            return Tool.create(vals)

        cls.tool_plain = mk_tool("cap03_plain_tool")
        cls.tool_consent = mk_tool("cap03_consent_tool", requires_user_consent=True)
        cls.provider = cls.env["llm.provider"].create(
            {"name": "CAP03 Provider", "service": "openai"}
        )
        cls.model = cls.env["llm.model"].create(
            {"name": "cap03-model", "provider_id": cls.provider.id}
        )
        cls.prompt = cls.env["llm.prompt"].create(
            {"name": "CAP03 Prompt", "template": "You are a test assistant."}
        )
        cls.assistant = cls.env["llm.assistant"].create(
            {
                "name": "CAP03 Assistant",
                "provider_id": cls.provider.id,
                "model_id": cls.model.id,
                "prompt_id": cls.prompt.id,
                "tool_ids": [(6, 0, [cls.tool_plain.id])],
            }
        )
        cls.thread = cls.env["llm.thread"].create(
            {
                "name": "CAP03 Thread",
                "provider_id": cls.provider.id,
                "model_id": cls.model.id,
                "assistant_id": cls.assistant.id,
                "prompt_id": cls.prompt.id,
            }
        )

    # ------------------------------------------------------------------
    # 1. Backward compatibility — persona + summary unchanged
    # ------------------------------------------------------------------
    def test_backward_compatible_persona_present(self):
        """Fork-only (no koenig): the assembler produces the persona fragment."""
        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        self.assertTrue(
            any("test assistant" in c.lower() for c in system_contents),
            "Persona must be present in the assembled messages.",
        )

    def test_backward_compatible_no_summary_when_empty(self):
        """No llm_summary → no summary fragment. Persona is still present.
        (Other contributors like guardrails may be present when koenig is
        installed — we check for summary ABSENCE, not exact count.)"""
        self.assertFalse(self.thread.llm_summary)
        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        # Persona present, summary absent.
        self.assertTrue(
            any("test assistant" in c.lower() for c in system_contents),
            "Persona must be present.",
        )
        self.assertFalse(
            any("Summary of the earlier conversation" in c for c in system_contents),
            "No summary fragment when llm_summary is empty.",
        )

    def test_backward_compatible_summary_appended(self):
        """llm_summary → summary fragment present AFTER persona.
        (Other contributors may also be present — we check relative order.)"""
        self.thread.llm_summary = "Earlier we discussed testing."
        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        # Both persona and summary present, persona before summary.
        persona_idx = next(
            (i for i, c in enumerate(system_contents) if "test assistant" in c.lower()),
            None,
        )
        summary_idx = next(
            (
                i
                for i, c in enumerate(system_contents)
                if "Earlier we discussed testing." in c
            ),
            None,
        )
        self.assertIsNotNone(persona_idx, "Persona must be present.")
        self.assertIsNotNone(summary_idx, "Summary must be present.")
        self.assertLess(persona_idx, summary_idx, "Persona must come before summary.")

    # ------------------------------------------------------------------
    # 2. Deterministic order (base fragments)
    # ------------------------------------------------------------------
    def test_prompt_order_persona_before_summary(self):
        """Persona (seq=30) comes before summary (seq=90)."""
        self.thread.llm_summary = "Summary text."
        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        self.assertLess(
            system_contents.index(
                next(c for c in system_contents if "test assistant" in c.lower())
            ),
            system_contents.index(
                next(c for c in system_contents if "Summary text" in c)
            ),
            "Persona must come before summary.",
        )

    # ------------------------------------------------------------------
    # 3. Per-tool fragment keys off _effective_tools() (CAP-02 fix)
    # ------------------------------------------------------------------
    def test_per_tool_fragment_effective_only(self):
        """A tool's _system_prompt_fragment is called only when the tool is
        in the EFFECTIVE set — not when hidden by the resolver."""

        fragment_text = "TOOL GUIDANCE: use this tool wisely."
        call_count = {"n": 0}

        def fake_fragment(self_tool, thread):
            call_count["n"] += 1
            return SystemPromptFragment(
                sequence=60,
                role="system",
                content=fragment_text,
                droppable=True,
                source="test_tool_guidance",
            )

        with patch.object(
            type(self.env["llm.tool"]),
            "_system_prompt_fragment",
            fake_fragment,
        ):
            # Tool is in the effective set (active, no consent gate) → fragment present.
            messages = self.thread._build_system_messages()
            system_contents = [
                m["content"] for m in messages if m.get("role") == "system"
            ]
            self.assertTrue(
                any(fragment_text in c for c in system_contents),
                "Tool guidance must be present when the tool is effective.",
            )
            self.assertGreater(call_count["n"], 0, "Fragment method was called.")

    def test_per_tool_fragment_absent_when_hidden(self):
        """When a tool is hidden (disabled via tool_ids_disabled), its
        _system_prompt_fragment is NOT called — no guidance for a hidden tool."""

        fragment_text = "SHOULD NOT APPEAR"
        call_count = {"n": 0}

        def fake_fragment(self_tool, thread):
            call_count["n"] += 1
            return SystemPromptFragment(
                sequence=60,
                role="system",
                content=fragment_text,
                droppable=True,
                source="hidden_tool_guidance",
            )

        # Hide the tool via tool_ids_disabled.
        self.thread.tool_ids_disabled = [(4, self.tool_plain.id)]
        with patch.object(
            type(self.env["llm.tool"]),
            "_system_prompt_fragment",
            fake_fragment,
        ):
            messages = self.thread._build_system_messages()
            system_contents = [
                m["content"] for m in messages if m.get("role") == "system"
            ]
            self.assertFalse(
                any(fragment_text in c for c in system_contents),
                "Tool guidance must be ABSENT when the tool is hidden.",
            )

    # ------------------------------------------------------------------
    # 4. Token budget — droppable fragments dropped in reverse sequence
    # ------------------------------------------------------------------
    def test_token_budget_drops_droppable(self):
        """Over-cap prompt drops droppable fragments; non-droppable survive."""
        self.thread.llm_summary = "X" * 5000  # large summary (droppable, seq=90)

        # Set a very low budget so the summary exceeds it.
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("llm_assistant.prompt_token_budget", "100")  # 400 chars

        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]

        # Persona (non-droppable) must survive; summary (droppable) must be dropped.
        self.assertTrue(
            any("test assistant" in c.lower() for c in system_contents),
            "Persona (non-droppable) must survive the budget cut.",
        )
        self.assertFalse(
            any("XXXX" in c for c in system_contents),
            "Summary (droppable) must be dropped when over budget.",
        )

    def test_token_budget_keeps_all_when_under_cap(self):
        """Under-cap prompt keeps all fragments (persona + summary present)."""
        self.thread.llm_summary = "Short summary."
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("llm_assistant.prompt_token_budget", "8000")  # default

        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        # Both persona and summary must be present (other contributors may be too).
        self.assertTrue(
            any("test assistant" in c.lower() for c in system_contents),
            "Persona must be kept under cap.",
        )
        self.assertTrue(
            any("Short summary." in c for c in system_contents),
            "Summary must be kept under cap.",
        )

    # ------------------------------------------------------------------
    # 5. Consent — single source from effective tools
    # ------------------------------------------------------------------
    def test_consent_fragment_from_effective_tools(self):
        """Consent fragment present when a requires_user_consent tool is
        in the effective set."""
        # Add a consent-requiring tool to the assistant.
        self.assistant.tool_ids = [(4, self.tool_consent.id)]
        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        # The consent fragment should be present (from llm.tool.consent.config).
        # We check for the tool name in the consent text.
        consent_msgs = [c for c in system_contents if "cap03_consent_tool" in c]
        self.assertEqual(
            len(consent_msgs),
            1,
            "Exactly one consent fragment, sourced from the effective tool set.",
        )

    def test_consent_absent_when_no_consent_tools(self):
        """No consent fragment when no requires_user_consent tool is effective."""
        # Only tool_plain (no consent) is on the assistant.
        messages = self.thread._build_system_messages()
        system_contents = [m["content"] for m in messages if m.get("role") == "system"]
        consent_msgs = [c for c in system_contents if "consent" in c.lower()]
        self.assertEqual(
            len(consent_msgs),
            0,
            "No consent fragment when no consent-requiring tool is effective.",
        )

    # ------------------------------------------------------------------
    # 6. get_prepend_messages backward-compat shim
    # ------------------------------------------------------------------
    def test_get_prepend_messages_delegates_to_assembler(self):
        """get_prepend_messages() delegates to _build_system_messages()."""
        assembled = self.thread._build_system_messages()
        via_hook = self.thread.get_prepend_messages()
        self.assertEqual(assembled, via_hook)

    # ------------------------------------------------------------------
    # 7. _prepare_chat_kwargs uses the assembler
    # ------------------------------------------------------------------
    def test_prepare_chat_kwargs_uses_assembler(self):
        """_prepare_chat_kwargs calls _build_system_messages for prepend_messages."""
        history = self.env["mail.message"]
        kwargs = self.thread._prepare_chat_kwargs(history, use_streaming=False)
        assembled = self.thread._build_system_messages()
        self.assertEqual(kwargs["prepend_messages"], assembled)

    # ------------------------------------------------------------------
    # 8. Fragment deduplication by source
    # ------------------------------------------------------------------
    def test_fragment_dedup_by_source(self):
        """Two tools contributing the same source → only one fragment kept."""
        # Add a second tool to the assistant.
        tool2 = self.env["llm.tool"].create(
            {"name": "cap03_tool_2", "implementation": "function", "description": "t2"}
        )
        self.assistant.tool_ids = [(4, tool2.id)]

        shared_text = "SHARED TOOL GUIDANCE."
        call_count = {"n": 0}

        def fake_fragment(self_tool, thread):
            call_count["n"] += 1
            return SystemPromptFragment(
                sequence=60,
                role="system",
                content=shared_text,
                droppable=True,
                source="shared_guidance",
            )

        with patch.object(
            type(self.env["llm.tool"]),
            "_system_prompt_fragment",
            fake_fragment,
        ):
            messages = self.thread._build_system_messages()
            system_contents = [
                m["content"] for m in messages if m.get("role") == "system"
            ]
            count = sum(1 for c in system_contents if shared_text in c)
            self.assertEqual(
                count,
                1,
                "Two tools with the same source → one fragment (dedup).",
            )
            # Both tools were called (the dedup happens in the assembler).
            self.assertGreaterEqual(call_count["n"], 2)
