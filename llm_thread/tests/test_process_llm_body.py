from markupsafe import Markup

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestProcessLlmBody(TransactionCase):
    """Unit tests for ``llm.thread._process_llm_body`` (P-CHAT M1).

    The renderer runs at message-post time and its output is the stored HTML
    the user sees, so markdown fidelity (tables, task lists, emoji,
    code-friendliness, newline handling, Markup passthrough) is a real
    user-facing contract. These tests pin the extras aligned with the wiki
    content converter and guard against regressions of the 2026-07-07
    emoji/table fixes.
    """

    def _render(self, body):
        # _process_llm_body does not depend on thread instance state, so it
        # is safe to call on an empty recordset of the model.
        return self.env["llm.thread"]._process_llm_body(body)

    def test_markup_passthrough(self):
        """Pre-formatted HTML (Markup) is returned untouched."""
        html = Markup("<p>Already <strong>HTML</strong></p>")
        self.assertEqual(self._render(html), html)

    def test_empty_body(self):
        """None / empty short-circuit before markdown2."""
        self.assertIsNone(self._render(None))
        self.assertEqual(self._render(""), "")

    def test_pipe_table_renders_as_html_table(self):
        """Pipe tables render as a Bootstrap-classed <table> (F3 fix)."""
        body = "| Name | Count |\n| --- | --- |\n| Alpha | 1 |\n| Beta | 2 |\n\n"
        html = self._render(body)
        self.assertIn("<table", html)
        self.assertIn('class="table table-sm"', html)
        self.assertIn("Alpha", html)
        self.assertIn("Beta", html)

    def test_task_list_renders_checkboxes(self):
        """task_list extra: - [ ] / - [x] become checkboxes."""
        body = "- [ ] todo\n- [x] done\n"
        html = self._render(body)
        self.assertIn("checkbox", html)
        self.assertIn("todo", html)
        self.assertIn("done", html)

    def test_real_emoji_survives(self):
        """emojize (not demojize): real emoji stay emoji, not :shortcodes:.

        This is the 2026-07-07 F3 regression guard: ``demojize`` converted
        real emoji INTO ``:factory:``-style text markers; ``emojize`` leaves
        them untouched.
        """
        html = self._render("Done \u2705")
        self.assertIn("\u2705", html)
        self.assertNotIn(":white_check_mark:", html)

    def test_shortcode_converts_to_emoji(self):
        """emojize converts :shortcodes: to real emoji."""
        html = self._render(":white_check_mark:")
        self.assertNotIn(":white_check_mark:", html)

    def test_fenced_code_block(self):
        """fenced-code-blocks extra: ``` lang blocks render as <pre>.

        markdown2 Pygments-highlights fenced blocks (when Pygments is
        installed), so the code text is wrapped in syntax-highlight spans
        and HTML-escaped — assert the <pre> container and the token, not the
        raw source string.
        """
        body = "```python\nprint('hi')\n```\n"
        html = self._render(body)
        self.assertIn("<pre", html)
        self.assertIn("print", html)

    def test_code_friendly_disables_underscore_italic(self):
        """code-friendly extra: _text_ is NOT italicised (identifiers safe)."""
        html = self._render("this _should not be italic_ here")
        self.assertNotIn("<em>", html)

    def test_break_on_newline(self):
        """break-on-newline extra: single newline renders as <br>."""
        html = self._render("line one\nline two")
        self.assertIn("<br", html)

    def test_strikethrough(self):
        """strike extra: ~~text~~ renders as struck-through (<s>)."""
        html = self._render("~~old~~")
        self.assertIn("<s>", html)

    def test_record_marker_survives_markdown(self):
        """The [[model:id label]] marker survives markdown2 as literal text
        (P-CHAT M4 — the linkifier resolves it after render)."""
        html = self._render("see [[res.partner:42 Acme]] now")
        self.assertIn("[[res.partner:42 Acme]]", html)

    def test_record_marker_survives_in_table(self):
        """A marker inside a markdown table cell survives — the space
        separator (not "|") avoids the pipe-table conflict."""
        body = (
            "| Order | Amount |\n"
            "| --- | --- |\n"
            "| [[sap.sale.order:42 0000123456]] | 100 |\n\n"
        )
        html = self._render(body)
        self.assertIn("<table", html)
        self.assertIn("[[sap.sale.order:42 0000123456]]", html)
