"""Headless Hoot driver for the llm_thread fork.

Wraps the JS unit tests in ``static/tests/*.test.js`` so they run in the
standard ``--test-enable --test-tags /llm_thread`` pipeline. See the
``odoo-javascript-testing`` cursor rule for the rationale.
"""

import odoo.tests

from odoo.addons.web.tests.test_js import unit_test_error_checker


@odoo.tests.tagged("post_install", "-at_install")
class TestLlmThreadHoot(odoo.tests.HttpCase):
    """Run the Hoot bundle for ``@llm_thread`` headlessly."""

    @odoo.tests.no_retry
    def test_hoot_unit(self):
        self.browser_js(
            "/web/tests"
            "?headless"
            "&loglevel=2"
            "&preset=desktop"
            "&timeout=15000"
            "&filter=%40llm_thread",
            "",
            "",
            login="admin",
            timeout=180,
            success_signal="[HOOT] Test suite succeeded",
            error_checker=unit_test_error_checker,
        )
