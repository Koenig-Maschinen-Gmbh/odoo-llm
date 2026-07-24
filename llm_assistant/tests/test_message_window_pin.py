"""Phase-3 (2026-07-24): the latest user message is pinned in the
``get_llm_messages(limit=N)`` window.

In long tool-call runs the N-message window can scroll the user's question out
of context; the model then silently drifts from the original intent (observed
on intranettest 2026-07-24: "list ALL Swiss customers" narrowed to "Top 10 by
volume" once the question had scrolled out of the 25-message window). The pin
guarantees the current question is always in context — at the cost of at most
one extra message. Folded (summarized) user messages must NOT be re-pinned:
their content lives in the rolling summary (D8).
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMessageWindowPin(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Pin Test Provider",
                    "service": "openai",
                    "api_base": "https://test.example.com/v1",
                    "api_key": "sk-test-key",
                },
            )
        )
        cls.model_rec = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "pin-test-model",
                    "provider_id": cls.provider.id,
                    "model_use": "chat",
                },
            )
        )
        cls.thread = cls.env["llm.thread"].create(
            {
                "name": "Pin Test Thread",
                "provider_id": cls.provider.id,
                "model_id": cls.model_rec.id,
            },
        )

    def _post(self, body, role):
        self.thread.with_context(mail_create_nosubscribe=True).message_post(
            body=body,
            llm_role=role,
            author_id=False,
            email_from="Test <test@example.com>",
        )

    def test_latest_user_message_pinned_in_window(self):
        """1 user question + 30 assistant messages (> 25 window): the question
        must still be in the returned context (pinned), first chronologically."""
        self._post("<p>THE QUESTION</p>", "user")
        question = self.thread.get_llm_messages(limit=0)[0]
        for i in range(30):
            self._post(f"<p>assistant {i}</p>", "assistant")

        msgs = self.thread.get_llm_messages()
        self.assertIn(
            question,
            msgs,
            "the user's question must be pinned even when it scrolled out "
            "of the 25-message window",
        )
        self.assertEqual(
            len(msgs),
            26,
            "25-message window + 1 pinned question",
        )
        self.assertEqual(
            msgs[0],
            question,
            "chronological order preserved (question is the oldest message)",
        )

    def test_pin_not_needed_when_question_in_window(self):
        """Short history (question inside the window): no duplicate pin."""
        self._post("<p>q</p>", "user")
        for i in range(3):
            self._post(f"<p>a {i}</p>", "assistant")
        msgs = self.thread.get_llm_messages()
        self.assertEqual(len(msgs), 4, "no pin added when the question fits")

    def test_folded_user_message_not_repinned(self):
        """A user message folded into the D8 summary must NOT be re-pinned —
        the summary block already covers it (no double context)."""
        self._post("<p>folded question</p>", "user")
        folded = self.thread.get_llm_messages(limit=0)[0]
        for i in range(30):
            self._post(f"<p>assistant {i}</p>", "assistant")
        # Fold everything up to the 20th assistant message.
        watermark = self.thread.get_llm_messages(limit=0)[20]
        self.thread.write({"llm_summary_upto_message_id": watermark.id})

        msgs = self.thread.get_llm_messages()
        self.assertNotIn(
            folded,
            msgs,
            "folded user messages stay folded (covered by the summary block)",
        )
        self.assertEqual(
            len(msgs),
            len(self.thread.get_llm_messages(limit=0)),
            "window smaller than remaining history; no folded re-pin",
        )

    def test_pin_latest_user_message_not_an_older_one(self):
        """With several questions, the LATEST one is pinned (the question the
        model is currently answering), not an older one."""
        self._post("<p>old question</p>", "user")
        old_q = self.thread.get_llm_messages(limit=0)[0]
        for i in range(30):
            self._post(f"<p>assistant {i}</p>", "assistant")
        self._post("<p>new question</p>", "user")
        new_q = self.thread.get_llm_messages(limit=0)[-1]

        msgs = self.thread.get_llm_messages()
        self.assertIn(new_q, msgs, "the current question is pinned")
        self.assertNotIn(
            old_q,
            msgs,
            "an older question that scrolled out stays out (window intact)",
        )
