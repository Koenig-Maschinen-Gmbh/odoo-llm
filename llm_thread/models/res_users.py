from odoo import models


class ResUsers(models.Model):
    _inherit = "res.users"

    # pylint: disable=missing-return  # void store hook: mutates `store`, no return value
    def _init_messaging(self, store):
        """Extend init_messaging to include LLM threads following Odoo patterns."""
        super()._init_messaging(store)

        # P-UX: load BOTH active and archived threads so the sidebar's
        # "Show archived" toggle is instant (no RPC). The owner-only record
        # rule (``llm_thread_rule_personal``) still scopes the search to the
        # current user's own threads — no sudo needed. ``active_test=False``
        # is the only way to include archived rows in an ``active``-field
        # model search.
        llm_threads = (
            self.env["llm.thread"]
            .with_context(active_test=False)
            .search([("user_id", "=", self.id)], order="write_date DESC")
        )

        # Use inherited _thread_to_store method from mail.thread
        if llm_threads:
            llm_threads._thread_to_store(store)
