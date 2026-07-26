from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestNotifyThreadBus(TransactionCase):
    """V9 (2026-07-26): the ``_notify_thread`` bus broadcast must never break
    message posting — and its failure must be LOUD (warning) after being
    invisible at debug level.

    Background: the fork's ``_notify_thread`` wraps the live-UI broadcast
    (``_bus_send_store`` + the ``llm.thread/new_message`` event) in a
    try/except so a Store serialization edge cannot break ``message_post``.
    The swallow used to log at debug level — with ``log_level = info`` the
    exception never reached the log (the V9 silent-failure report). The
    level is now WARNING so any recurrence shows up with a full traceback.
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
                    "name": "V9 Broadcast Test",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                }
            )
        )

    def test_normal_post_broadcasts(self):
        """A normal assistant post broadcasts via _bus_send_store."""
        calls = []
        orig = type(self.thread)._bus_send_store

        def spy(self, message, *args, **kwargs):
            calls.append(message.id)
            return orig(self, message, *args, **kwargs)

        with patch.object(type(self.thread), "_bus_send_store", spy):
            msg = self.thread.message_post(body="<p>answer</p>", llm_role="assistant")
        self.assertIn(msg.id, calls)

    def test_post_succeeds_when_broadcast_raises(self):
        """Defense-in-depth: a broadcast failure must not break message_post —
        the message is returned and the failure is logged at WARNING level
        (the V9 tripwire)."""

        def boom(self, message, *args, **kwargs):
            raise RuntimeError("V9 simulated serialization edge")

        with (
            patch.object(type(self.thread), "_bus_send_store", boom),
            self.assertLogs(
                "odoo.addons.llm_thread.models.llm_thread", level="WARNING"
            ) as logs,
        ):
            msg = self.thread.message_post(body="<p>answer</p>", llm_role="assistant")
        self.assertTrue(msg.exists())
        self.assertTrue(
            any("Failed to broadcast" in out for out in logs.output),
            f"expected the V9 warning in logs, got: {logs.output}",
        )

    def test_streaming_placeholder_skips_broadcast(self):
        """FIX-4b: streaming placeholders never hit the bus (their stale body
        would overwrite the streamed answer at the end-of-run flush)."""
        calls = []
        orig = type(self.thread)._bus_send_store

        def spy(self, message, *args, **kwargs):
            calls.append(message.id)
            return orig(self, message, *args, **kwargs)

        with patch.object(type(self.thread), "_bus_send_store", spy):
            msg = self.thread.with_context(llm_streaming_placeholder=True).message_post(
                body="<p>Thinking...</p>",
                llm_role="assistant",
            )
        self.assertNotIn(msg.id, calls)
