from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLlmMessageStore(TransactionCase):
    """mail.message store serialization of LLM fields (P-CHAT M2).

    The frontend hierarchy (steps drawer, error prominence) reads
    ``llm_role`` / ``body_json`` / ``is_error`` from the mail store.
    ``_extras_to_store`` must serialize them — in particular ``is_error``,
    which was missing before P-CHAT M2 and silently broke error prominence
    (the frontend ``isLLMError`` classification was always false).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Test Provider",
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
                    "name": "test-model",
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
                    "name": "Store Test Thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def test_error_message_serializes_is_error(self):
        """An error message carries is_error=True to the frontend."""
        msg = self.thread.message_post(
            body="<p>boom</p>",
            llm_role="assistant",
            author_id=False,
            is_error=True,
        )
        data = msg.to_store_format()
        self.assertTrue(data.get("is_error"))
        self.assertEqual(data.get("llm_role"), "assistant")

    def test_normal_message_does_not_carry_is_error(self):
        """is_error is only sent when True (absent on the client ⇒ falsy)."""
        msg = self.thread.message_post(
            body="hello",
            llm_role="user",
            author_id=self.env.user.partner_id.id,
        )
        data = msg.to_store_format()
        self.assertNotIn("is_error", data)

    def test_tool_message_serializes_body_json(self):
        """A tool message carries body_json to the frontend."""
        msg = self.thread.message_post(
            body="",
            llm_role="tool",
            author_id=False,
            body_json={
                "type": "tool_execution",
                "tool_name": "koenig_sap_query",
                "status": "completed",
            },
        )
        data = msg.to_store_format()
        self.assertEqual(data.get("llm_role"), "tool")
        self.assertEqual(data["body_json"]["type"], "tool_execution")

    # ------------------------------------------------------------------
    # P-CHAT M6 — notification suppression (AI threads never notify)
    # ------------------------------------------------------------------

    def _assert_zero_notifications(self, message, scenario):
        notifs = self.env["mail.notification"].search(
            [("mail_message_id", "=", message.id)]
        )
        self.assertFalse(notifs, f"[{scenario}] notifications created")
        mails = self.env["mail.mail"].search([("mail_message_id", "=", message.id)])
        self.assertFalse(mails, f"[{scenario}] outgoing emails created")

    def test_ai_assistant_message_creates_no_notifications(self):
        """An AI assistant message creates zero notifications + zero mails."""
        msg = self.thread.message_post(
            body="hello",
            llm_role="assistant",
            author_id=False,
        )
        self._assert_zero_notifications(msg, "assistant message")

    def test_ai_user_message_creates_no_notifications(self):
        """An AI user message also creates zero notifications."""
        msg = self.thread.message_post(
            body="hi",
            llm_role="user",
            author_id=self.env.user.partner_id.id,
        )
        self._assert_zero_notifications(msg, "user message")

    def test_non_llm_thread_still_notifies(self):
        """The suppression is llm.thread-specific — a normal mail.thread
        model (res.partner) still notifies its followers. (Subscribe a
        partner OTHER than the author — the author is never notified of
        their own message.)"""
        partner = self.env["res.partner"].create({"name": "Notify Test"})
        follower = self.env["res.partner"].create({"name": "Other Follower"})
        partner.message_subscribe(partner_ids=[follower.id])
        msg = partner.message_post(
            body="normal message", subtype_xmlid="mail.mt_comment"
        )
        notifs = self.env["mail.notification"].search(
            [("mail_message_id", "=", msg.id)]
        )
        self.assertTrue(
            notifs, "non-llm.thread message_post should still notify followers"
        )
