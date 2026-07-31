/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { closeChatterWikiNotebook } from "@llm_thread/patches/chatter_patch";

/**
 * AI/Wiki switcher (2026-07-30 UX round 2) — only one chatter panel can be
 * open at a time: the AI chat OR the koenig_wiki_chatter notebook. These
 * tests pin the pure close-helper contract: it flips the wiki flag when
 * present and open, and silently no-ops when koenig_wiki_chatter is not
 * installed (the state field only exists with its patch).
 */
describe("llm_thread — AI/Wiki switcher", () => {
    test("closes an open wiki notebook", () => {
        const chatter = { state: { isWikiNotebookOpen: true } };
        closeChatterWikiNotebook(chatter);
        expect(chatter.state.isWikiNotebookOpen).toBe(false);
    });

    test("no-op when the notebook is already closed", () => {
        const chatter = { state: { isWikiNotebookOpen: false } };
        closeChatterWikiNotebook(chatter);
        expect(chatter.state.isWikiNotebookOpen).toBe(false);
    });

    test("no-op without koenig_wiki_chatter state (must not throw)", () => {
        const chatter = { state: {} };
        closeChatterWikiNotebook(chatter);
        expect("isWikiNotebookOpen" in chatter.state).toBe(false);
    });
});
