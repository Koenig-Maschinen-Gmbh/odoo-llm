"""Extends ``llm.tool`` with the inverse assistant usage list."""

from odoo import fields, models


class LlmTool(models.Model):
    """Add ``assistant_ids`` — the inverse of ``llm.assistant.tool_ids``.

    Tools are a shared capability pool: any number of assistants (and expert
    profiles) may whitelist the same tool. The inverse is declared here (not
    in ``llm_tool``) to keep the dependency direction
    ``llm_assistant -> llm_tool``. It reuses the same auto-generated relation
    table (``llm_assistant_llm_tool_rel``) — read/write through either side
    updates the same rows.
    """

    _inherit = "llm.tool"

    assistant_ids = fields.Many2many(
        "llm.assistant",
        relation="llm_assistant_llm_tool_rel",
        column1="llm_tool_id",
        column2="llm_assistant_id",
        string="Assistants",
        help="Assistants that whitelist this tool. Tools are a shared pool — "
        "one tool can be used by any number of assistants and expert profiles.",
    )
