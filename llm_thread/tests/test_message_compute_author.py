from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestMessageComputeAuthor(TransactionCase):
    """llm.thread._message_compute_author allows authorless notifications.

    Progress messages (``message_type='notification'``,
    ``subtype_xmlid='mail.mt_note'``) are posted with ``author_id=False``
    and no ``email_from`` — they have no human sender.  The base
    ``mail.thread._message_compute_author`` raises ``UserError`` when
    ``email_from`` is empty and ``raise_on_email=True`` (the default),
    *unless* ``self.env.su`` is True — superuser mode (or uid ==
    SUPERUSER_ID) skips the check entirely (``mail_thread.py:2911``).
    ``llm.thread`` overrides this to pass ``raise_on_email=False`` to
    ``super()``, following the OCB ``discuss.channel`` precedent
    (ref: ``discuss_channel.py:696-697``).

    This is safe because ``_notify_thread`` on ``llm.thread`` is a no-op
    for the email pipeline — no ``mail.mail`` / ``mail.notification``
    records are created.  The bus broadcast (the real delivery path) does
    not use ``email_from``.

    NOTE: ``TransactionCase.setUpClass`` creates the class environment
    with ``uid=odoo.SUPERUSER_ID``, which makes ``env.su=True`` by
    default (``api.py:572-573``).  To exercise the OCB sender-email
    guard, tests must switch to a non-superuser internal user via
    ``with_user`` so that ``env.su`` is False.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Non-superuser internal user for exercising the sender-email guard.
        # base.user_admin (uid 2) is an internal user in group_system
        # (which implies llm.group_llm_manager) with full read/write ACL
        # on llm.thread and read access on llm.provider / llm.model.
        cls.admin_user = cls.env.ref("base.user_admin")
        # Create provider and model with sudo (ACL bypass for setup).
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
        # Create thread with sudo (ACL bypass for creation), but set
        # user_id explicitly to the admin user so the personal record
        # rule (``user_id = user.id``) grants non-sudo read/write access.
        thread_sudo = (
            cls.env["llm.thread"]
            .sudo()
            .create(
                {
                    "name": "Author Test Thread",
                    "provider_id": cls.provider.id,
                    "model_id": cls.model.id,
                    "user_id": cls.admin_user.id,
                }
            )
        )
        # Store the thread ID; tests re-browse with a non-su user so
        # that env.su is False and the OCB sender-email guard is active.
        cls.thread_id = thread_sudo.id

    def _thread_as_admin(self):
        """Return the test thread browsed as a non-superuser internal user.

        TransactionCase runs with uid=SUPERUSER_ID (su=True), which
        bypasses the OCB ``_message_compute_author`` sender-email check
        (``mail_thread.py:2911: not self.env.su``).  Re-browsing with a
        non-superuser user ensures ``env.su=False`` so the guard fires —
        making the regression test meaningful.
        """
        return (
            self.env["llm.thread"].with_user(self.admin_user.id).browse(self.thread_id)
        )

    def test_authorless_notification_posts_non_su(self):
        """An authorless notification message (no email_from) posts
        successfully from a non-sudo recordset.

        This is the exact call pattern used by orchestration progress
        messages: ``author_id=False``, ``message_type='notification'``,
        ``subtype_xmlid='mail.mt_note'``, no ``email_from``.

        Without the ``_message_compute_author`` override, OCB would raise
        ``UserError("Unable to send message, please configure the sender's
        email address.")`` because ``self.env.su`` is False and
        ``raise_on_email`` defaults to True (mail_thread.py:2911).
        """
        thread = self._thread_as_admin()
        msg = thread.with_context(
            mail_create_nosubscribe=True,
        ).message_post(
            body="<p>Analyzing your request...</p>",
            message_type="notification",
            subtype_xmlid="mail.mt_note",
            author_id=False,
        )
        self.assertTrue(msg.id, "message_post should return a valid message")
        self.assertEqual(msg.message_type, "notification")
        self.assertFalse(
            msg.author_id, "author_id should be False for authorless notification"
        )

    def test_message_with_author_still_works(self):
        """Messages that specify an author_id still resolve normally."""
        thread = self._thread_as_admin()
        msg = thread.message_post(
            body="<p>Normal message</p>",
            llm_role="user",
            author_id=self.admin_user.partner_id.id,
        )
        self.assertTrue(msg.id)
        self.assertEqual(msg.author_id, self.admin_user.partner_id)

    def test_base_mail_thread_still_raises_on_missing_email(self):
        """The override is llm.thread-specific — a plain mail.thread model
        (res.partner) still raises UserError for authorless messages
        without email_from (i.e. the base behavior is preserved).

        Must use a non-superuser env so ``env.su`` is False — otherwise
        the OCB guard is bypassed (mail_thread.py:2911)."""
        partner = (
            self.env["res.partner"]
            .with_user(self.admin_user.id)
            .create({"name": "Author Test Partner"})
        )
        with self.assertRaises(UserError):
            partner.message_post(
                body="<p>test</p>",
                message_type="comment",
                author_id=False,
            )
