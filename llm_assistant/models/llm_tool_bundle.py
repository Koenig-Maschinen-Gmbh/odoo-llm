"""Extends ``llm.tool.bundle`` with the inverse assistant-subscription list."""

from odoo import api, fields, models


class LLMToolBundle(models.Model):
    """Add ``assistant_ids`` — the inverse of ``llm.assistant.bundle_ids``.

    Declared here (not in ``llm_tool``) to keep the dependency direction
    ``llm_assistant -> llm_tool``: the subscription M2M table
    (``llm_assistant_tool_bundle_rel``) is owned by ``llm_assistant``, so its
    inverse must be too. Read-only display on the bundle form.
    """

    _inherit = "llm.tool.bundle"

    assistant_ids = fields.Many2many(
        "llm.assistant",
        relation="llm_assistant_tool_bundle_rel",
        column1="bundle_id",
        column2="assistant_id",
        string="Subscribing Assistants",
        help="Assistants that subscribe to this bundle.",
    )
    assistant_count = fields.Integer(compute="_compute_assistant_count")

    @api.depends("assistant_ids")
    def _compute_assistant_count(self):
        for bundle in self:
            bundle.assistant_count = len(bundle.assistant_ids)
