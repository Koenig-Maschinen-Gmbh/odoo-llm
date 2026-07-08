"""Tags for LLM chat threads (P-UX).

User-manageable colored tags, modelled on ``project.tags``: a shared global
namespace with a ``name_uniq`` constraint, a ``color`` index (0-11), and a
``name_create`` override that deduplicates case-insensitively so the
``many2many_tags`` quick-create never produces silent duplicates.
"""

from random import randint

from odoo import api, fields, models


class LLMThreadTag(models.Model):
    _name = "llm.thread.tag"
    _description = "LLM Thread Tag"
    _order = "name"

    def _get_default_color(self):
        return randint(1, 11)

    name = fields.Char(string="Name", required=True, translate=True)
    color = fields.Integer(
        string="Color",
        default=lambda self: self._get_default_color(),
        help="Transparent tags (color 0) are hidden in the sidebar.",
    )
    # No inverse ``thread_ids`` field: ``llm.thread.tag_ids`` uses an AUTO
    # relation (required for compatibility with the ``llm.thread.mock``
    # prototype-inheriting transient in ``llm_assistant`` — see the comment
    # on ``llm.thread.tag_ids``). An inverse would need the same explicit
    # relation and would collide. The sidebar + form view read
    # ``thread.tag_ids`` directly; nothing needs ``tag.thread_ids``.

    _sql_constraints = [
        ("name_uniq", "unique (name)", "A tag with the same name already exists."),
    ]

    @api.model
    def name_create(self, name):
        """Quick-create dedup: return an existing tag on a case-insensitive match.

        Mirrors ``project.tags.name_create`` — the ``many2many_tags`` widget
        quick-creates on type-ahead, so without dedup the same label would
        spawn duplicate rows.
        """
        existing = self.search([("name", "=ilike", name.strip())], limit=1)
        if existing:
            return existing.id, existing.display_name
        return super().name_create(name)
