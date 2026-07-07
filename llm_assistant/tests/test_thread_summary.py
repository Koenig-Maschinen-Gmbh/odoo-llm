"""D8 (P-MEM) — Rolling thread summarization tests for the fork.

Tests the fork's ``llm.thread`` summary mechanics:
- Domain exclusion (folded ids absent from ``get_llm_messages``)
- Prepend block presence when summary is set
- Fold with mocked ``chat`` (anchor merge + watermark advance)
- Fail-soft on chat exception (anchor + watermark unchanged)
- ``keep_last`` respected
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestThreadSummary(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Thread = cls.env["llm.thread"]

        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Test Provider",
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
                    "name": "test-model",
                    "provider_id": cls.provider.id,
                    "model_use": "chat",
                },
            )
        )
        cls.thread = cls.Thread.create(
            {
                "name": "Summary Test Thread",
                "provider_id": cls.provider.id,
                "model_id": cls.model_rec.id,
            },
        )

    def _post_messages(self, count, start_role="user"):
        """Post ``count`` LLM messages on the thread, alternating roles."""
        for i in range(count):
            role = "user" if (i % 2 == 0) == (start_role == "user") else "assistant"
            self.thread.with_context(mail_create_nosubscribe=True).message_post(
                body=f"<p>Message {i}</p>",
                llm_role=role,
                author_id=False,
                email_from="Test <test@example.com>",
            )

    def test_folded_messages_excluded_from_get_llm_messages(self):
        """When llm_summary_upto_message_id is set, folded messages are excluded."""
        self._post_messages(5)
        all_msgs = self.thread.get_llm_messages(limit=0)
        self.assertEqual(len(all_msgs), 5)

        # Set watermark to the 3rd message
        watermark = all_msgs[2]
        self.thread.write({"llm_summary_upto_message_id": watermark.id})

        remaining = self.thread.get_llm_messages(limit=0)
        self.assertEqual(
            len(remaining), 2, "Only messages after the watermark should remain"
        )
        self.assertTrue(all(m.id > watermark.id for m in remaining))

    def test_summary_block_in_prepend_messages(self):
        """When llm_summary is set, a system block is appended."""
        self.thread.write({"llm_summary": "The user wanted help with SAP orders."})
        messages = self.thread.get_prepend_messages()
        summary_blocks = [
            m
            for m in messages
            if m.get("role") == "system"
            and "Summary of the earlier conversation" in m.get("content", "")
        ]
        self.assertEqual(len(summary_blocks), 1)
        self.assertIn("SAP orders", summary_blocks[0]["content"])

    def test_no_summary_block_when_unset(self):
        """When llm_summary is not set, no summary block is appended."""
        messages = self.thread.get_prepend_messages()
        summary_blocks = [
            m
            for m in messages
            if m.get("role") == "system"
            and "Summary of the earlier conversation" in m.get("content", "")
        ]
        self.assertEqual(len(summary_blocks), 0)

    def test_fold_with_mocked_chat(self):
        """Fold: chat is called, summary + watermark are written."""
        self._post_messages(15)
        all_msgs = self.thread.get_llm_messages(limit=0)

        captured = {}

        def fake_chat(self_model, *args, **kwargs):
            captured["called"] = True
            return {"content": "Merged summary of the conversation."}

        with patch.object(type(self.env["llm.model"]), "chat", fake_chat):
            self.thread._llm_fold_into_summary(self.model_rec, keep_last=5)

        self.assertTrue(captured.get("called"))
        self.thread.invalidate_recordset()
        self.assertEqual(self.thread.llm_summary, "Merged summary of the conversation.")
        self.assertTrue(self.thread.llm_summary_upto_message_id)
        # Watermark should be the 10th message (15 - 5 = 10 folded)
        expected_watermark = all_msgs[9]
        self.assertEqual(
            self.thread.llm_summary_upto_message_id.id, expected_watermark.id
        )

    def test_fold_fail_soft_on_chat_exception(self):
        """When chat raises, the old anchor and watermark are unchanged."""
        self.thread.write(
            {"llm_summary": "Old anchor", "llm_summary_upto_message_id": False}
        )
        self._post_messages(15)

        with patch.object(
            type(self.env["llm.model"]), "chat", side_effect=Exception("API down")
        ):
            self.thread._llm_fold_into_summary(self.model_rec, keep_last=5)

        self.thread.invalidate_recordset()
        self.assertEqual(
            self.thread.llm_summary,
            "Old anchor",
            "Old anchor must be preserved on failure",
        )
        self.assertFalse(
            self.thread.llm_summary_upto_message_id,
            "Watermark must not advance on failure",
        )

    def test_keep_last_respected(self):
        """Foldable span = all_after[:-keep_last]; keep_last messages stay raw."""
        self._post_messages(20)
        all_msgs = self.thread.get_llm_messages(limit=0)

        with patch.object(
            type(self.env["llm.model"]), "chat", return_value={"content": "Summary"}
        ):
            self.thread._llm_fold_into_summary(self.model_rec, keep_last=10)

        self.thread.invalidate_recordset()
        # Watermark = 10th message (20 - 10 = 10 folded)
        expected_watermark = all_msgs[9]
        self.assertEqual(
            self.thread.llm_summary_upto_message_id.id, expected_watermark.id
        )

        # Remaining raw messages = 10
        remaining = self.thread.get_llm_messages(limit=0)
        self.assertEqual(len(remaining), 10)

    def test_no_fold_when_not_enough_messages(self):
        """When total messages <= keep_last, no fold happens."""
        self._post_messages(5)

        captured = {}

        def fake_chat(self_model, *args, **kwargs):
            captured["called"] = True
            return {"content": "Summary"}

        with patch.object(type(self.env["llm.model"]), "chat", fake_chat):
            self.thread._llm_fold_into_summary(self.model_rec, keep_last=10)

        self.assertFalse(
            captured.get("called"), "Chat should not be called when not enough messages"
        )
        self.assertFalse(self.thread.llm_summary)
