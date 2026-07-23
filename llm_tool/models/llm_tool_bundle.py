import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class LLMToolBundle(models.Model):
    """A named capability pack of :class:`llm.tool` records.

    CAP-02 tool registry. Instead of linking each tool to each assistant by hand
    (``assistant_attach.xml`` + self-heal hooks — linear-in-assistants rot), an
    assistant **subscribes to bundles** (``llm.assistant.bundle_ids``, defined in
    ``llm_assistant``) and tools **join bundles** (``tool_ids``). Adding a new
    tool to a bundle then reaches every subscribing assistant at once — no
    per-assistant edit, no migration.

    The assistant resolves its live tool set as
    ``tool_ids | bundle_ids.tool_ids`` (see ``llm.assistant._resolved_tools``);
    the thread then gates that set per turn in ``llm.thread._effective_tools``.
    This is the Odoo-native registry idiom (a selection registry the point of use
    reads live), the same class as ``ir.actions`` groupings — never a snapshot.

    Defined in ``llm_tool`` so tool-owning addons can seed bundles + membership
    without depending on ``llm_assistant``. The reverse ``assistant_ids`` inverse
    is added by ``llm_assistant`` (which owns the subscription M2M).
    """

    _name = "llm.tool.bundle"
    _description = "LLM Tool Bundle"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        required=True,
        help="Stable technical key for the bundle (e.g. 'web_research'). "
        "Used by data seeds and migrations to reference the bundle.",
    )
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    description = fields.Text(
        help="What capability this pack provides (for operators)."
    )

    tool_ids = fields.Many2many(
        "llm.tool",
        relation="llm_tool_bundle_tool_rel",
        column1="bundle_id",
        column2="tool_id",
        string="Tools",
        help="Tools included in this bundle. Any assistant subscribing to the "
        "bundle offers all of these tools (subject to the per-turn resolver).",
    )
    tool_count = fields.Integer(compute="_compute_tool_count")

    _sql_constraints = [
        ("unique_code", "UNIQUE(code)", "A tool bundle with this code already exists!"),
    ]

    @api.depends("tool_ids")
    def _compute_tool_count(self):
        for bundle in self:
            bundle.tool_count = len(bundle.tool_ids)
