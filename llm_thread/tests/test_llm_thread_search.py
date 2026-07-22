from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLlmThreadSearch(TransactionCase):
    """P-UX: ``search_threads`` (name + content, owner-scoped, archived
    findable) and the archived-thread loading change in ``_init_messaging``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "P",
                    "service": "openai",
                    "api_base": "https://x/v1",
                    "api_key": "k",
                }
            )
        )
        cls.model = (
            cls.env["llm.model"]
            .sudo()
            .create({"name": "m", "provider_id": cls.provider.id, "model_use": "chat"})
        )
        # Regular users (base.group_user only — NOT llm.manager) so the
        # owner-only record rule ``llm_thread_rule_personal`` is the active
        # rule and owner scoping is testable.
        cls.user_a = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "User A",
                    "login": "llm_ux_user_a",
                    "email": "user_a@example.com",
                    "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
                }
            )
        )
        cls.user_b = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "User B",
                    "login": "llm_ux_user_b",
                    "email": "user_b@example.com",
                    "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
                }
            )
        )

    def _make_thread(self, user, name, body=None, active=True):
        thread = (
            self.env["llm.thread"]
            .with_user(user)
            .create(
                {
                    "name": name,
                    "provider_id": self.provider.id,
                    "model_id": self.model.id,
                }
            )
        )
        if body:
            thread.with_user(user).message_post(
                body=body, llm_role="user", author_id=user.partner_id.id
            )
        if not active:
            thread.with_user(user).action_archive()
        return thread

    # ------------------------------------------------------------------
    # search_threads
    # ------------------------------------------------------------------

    def test_short_term_returns_empty(self):
        """Terms shorter than 2 characters are ignored (no noisy matches)."""
        self.assertEqual(self.env["llm.thread"].search_threads("a"), [])

    def test_name_match(self):
        thread = self._make_thread(self.user_a, "Quarterly Report")
        results = self.env["llm.thread"].with_user(self.user_a).search_threads("report")
        ids = [r["id"] for r in results]
        self.assertIn(thread.id, ids)

    def test_content_match(self):
        """A thread whose message body contains the term is found by content."""
        thread = self._make_thread(
            self.user_a,
            "Generic Name",
            body="Please review the <b>zaphod42</b> proposal",
        )
        results = (
            self.env["llm.thread"].with_user(self.user_a).search_threads("zaphod42")
        )
        ids = [r["id"] for r in results]
        self.assertIn(thread.id, ids, "content match should find the thread")

    def test_archived_thread_findable_by_name(self):
        """Archived threads are findable (search is the main way back to an
        archived thread — ``active_test=False`` in the content/name path)."""
        thread = self._make_thread(self.user_a, "Archived Project X", active=False)
        self.assertFalse(thread.active)
        results = (
            self.env["llm.thread"]
            .with_user(self.user_a)
            .search_threads("Archived Project")
        )
        ids = [r["id"] for r in results]
        self.assertIn(thread.id, ids)

    def test_owner_scoped(self):
        """User B's search never returns user A's threads (explicit ``user_id``
        filter + the owner-only record rule both scope to owner)."""
        self._make_thread(self.user_a, "Secret A Only Report")
        results = (
            self.env["llm.thread"]
            .with_user(self.user_b)
            .search_threads("Secret A Only")
        )
        ids = [r["id"] for r in results]
        self.assertEqual(ids, [], "user B must not see user A's threads")

    def test_result_shape_has_active_and_tags(self):
        """``search_threads`` returns the same dict shape as ``_thread_to_store``
        — incl. the P-UX ``active`` + ``tag_ids`` keys (unconditional)."""
        thread = self._make_thread(self.user_a, "Shape Check Report")
        tag = (
            self.env["llm.thread.tag"]
            .with_user(self.user_a)
            .create([{"name": "UX-Tag"}])
        )
        thread.with_user(self.user_a).tag_ids = [(4, tag.id)]
        results = (
            self.env["llm.thread"].with_user(self.user_a).search_threads("Shape Check")
        )
        match = next(r for r in results if r["id"] == thread.id)
        self.assertIn("active", match)
        self.assertTrue(match["active"])
        self.assertIn("tag_ids", match)
        self.assertEqual(match["tag_ids"][0]["name"], "UX-Tag")

    def test_store_payload_has_is_expert_subthread(self):
        """UI-11: ``_thread_store_dict`` ships ``is_expert_subthread``
        unconditionally (same store-merge rationale as ``active``) so the
        sidebar can filter expert sub-threads client-side."""
        thread = self._make_thread(self.user_a, "Regular Thread Report")
        regular = thread._thread_store_dict(thread)
        self.assertIn("is_expert_subthread", regular)
        self.assertFalse(regular["is_expert_subthread"])

        expert_thread = (
            self.env["llm.thread"]
            .with_user(self.user_a)
            .create(
                {
                    "name": "Expert: sap_sale_order — probe",
                    "provider_id": self.provider.id,
                    "model_id": self.model.id,
                    "is_expert_subthread": True,
                }
            )
        )
        payload = expert_thread._thread_store_dict(expert_thread)
        self.assertTrue(payload["is_expert_subthread"])

    # ------------------------------------------------------------------
    # _init_messaging loads archived threads
    # ------------------------------------------------------------------

    def test_init_messaging_loads_archived_threads(self):
        """``_init_messaging`` now uses ``active_test=False`` so the sidebar's
        "Show archived" toggle has the data upfront (no RPC)."""
        from odoo.addons.mail.tools.discuss import Store

        active = self._make_thread(self.user_a, "Active Thread")
        archived = self._make_thread(self.user_a, "Ghost Thread", active=False)
        self.assertTrue(active.active)
        self.assertFalse(archived.active)

        store = Store()
        # Run as user_a so the owner-only record rule scopes the thread
        # search to user_a's own threads (mirrors the real init_messaging call).
        self.user_a.with_user(self.user_a)._init_messaging(store)
        # get_result()["mail.thread"] is a LIST of per-record dicts.
        result = store.get_result().get("mail.thread", [])
        ids = {r["id"] for r in result}
        self.assertIn(active.id, ids, "active thread must be in init_messaging")
        self.assertIn(
            archived.id, ids, "archived thread must ALSO be in init_messaging"
        )

    # ------------------------------------------------------------------
    # ACL: users can unlink own thread (GDPR), not others' (record rule)
    # ------------------------------------------------------------------

    def test_user_can_unlink_own_thread(self):
        """The P-UX ACL change grants users unlink on ``llm.thread`` so bulk
        delete + the GDPR 'delete my threads' right work. The owner-only
        record rule scopes it to own threads."""
        thread = self._make_thread(self.user_a, "Mine To Delete")
        thread.with_user(self.user_a).unlink()
        self.assertFalse(thread.exists())

    def test_user_cannot_unlink_other_users_thread(self):
        """The owner-only record rule blocks cross-user unlink even though the
        ACL now grants users unlink."""
        thread_b = self._make_thread(self.user_b, "Belongs To B")
        with self.assertRaises(AccessError):
            thread_b.with_user(self.user_a).unlink()
        self.assertTrue(thread_b.exists())

    # ------------------------------------------------------------------
    # Tag ACL: users read+create, only managers unlink/rename
    # ------------------------------------------------------------------

    def test_user_can_create_tag(self):
        tag = (
            self.env["llm.thread.tag"]
            .with_user(self.user_a)
            .create([{"name": "User Tag"}])
        )
        self.assertTrue(tag.id)

    def test_user_cannot_unlink_tag(self):
        """Tags are a shared global namespace (``name_uniq``): users can create
        + read + use them, but only managers curate (rename/delete)."""
        tag = self.env["llm.thread.tag"].sudo().create([{"name": "Curated"}])
        with self.assertRaises(AccessError):
            tag.with_user(self.user_a).unlink()
