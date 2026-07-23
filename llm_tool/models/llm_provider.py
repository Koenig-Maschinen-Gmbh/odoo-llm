import json
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class LLMProvider(models.Model):
    _inherit = "llm.provider"

    @api.model
    def _is_tool_call_complete(self, function_data, expected_endings=("]", "}")):
        """Check if a tool call is complete (utility function for providers).

        Args:
            function_data: Dictionary with 'name' and 'arguments' keys
            expected_endings: Tuple of valid JSON ending characters

        Returns:
            bool: True if the tool call appears complete
        """
        tool_name = function_data.get("name")
        args_str = function_data.get("arguments", "").strip()

        if not tool_name or not args_str:
            return False

        try:
            json.loads(args_str)
            if args_str.endswith(expected_endings):
                return True
        except json.JSONDecodeError:
            pass

        return False

    # ------------------------------------------------------------------
    # CAP-03: the consent injection override of ``_prepare_prepend_messages``
    # is RETIRED. Consent is now a fragment in the thread assembler
    # (``llm.thread._consent_prompt_fragment``), sourced from the effective
    # tool set (``_effective_tools()``). The provider layer is a pure
    # pass-through (base ``_prepare_prepend_messages`` returns as-is).
    # ------------------------------------------------------------------
