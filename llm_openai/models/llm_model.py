from odoo import fields, models


class LLMModel(models.Model):
    _inherit = "llm.model"

    reasoning_effort = fields.Selection(
        [
            ("none", "None (fastest)"),
            ("minimal", "Minimal"),
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
        ],
        string="Reasoning Effort",
        help="For reasoning-capable models (via OpenRouter's unified `reasoning` "
        "parameter): how much internal reasoning to allocate. 'None' disables "
        "reasoning for the fastest, cheapest responses — recommended for RAG / "
        "tool-using chat, where extended reasoning rarely improves answer quality "
        "but multiplies latency. Leave empty for the provider default.",
    )
