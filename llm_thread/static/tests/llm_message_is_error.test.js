/** @odoo-module **/

import { describe, expect, test } from "@odoo/hoot";
import { defineMailModels, start } from "@mail/../tests/mail_test_helpers";
import { getService } from "@web/../tests/web_test_helpers";

/**
 * P-CHAT M2 — loop-closer for the is_error fix.
 *
 * The backend ``_extras_to_store`` serializes ``is_error``; the Message
 * model patch declares ``is_error`` as a Mail Store field (Record.attr in
 * setup) so the value is populated on the record. These tests prove the
 * frontend store actually receives it — the gap that hid the original bug
 * (isLLMError was always false because is_error never reached the client).
 */
defineMailModels();

describe("llm.message is_error field", () => {
    test("is_error is populated on the store Message", async () => {
        await start();
        const store = getService("mail.store");
        const message = store.Message.insert({
            id: 42,
            model: "llm.thread",
            res_id: 1,
            is_error: true,
            body: "<p>boom</p>",
        });
        expect(message.is_error).toBe(true);
    });

    test("is_error defaults to falsy when not sent", async () => {
        await start();
        const store = getService("mail.store");
        const message = store.Message.insert({
            id: 43,
            model: "llm.thread",
            res_id: 1,
            body: "<p>ok</p>",
        });
        expect(Boolean(message.is_error)).toBe(false);
    });

    test("is_error field is registered on the Message model", async () => {
        await start();
        const store = getService("mail.store");
        expect(store.Message._.fields.has("is_error")).toBe(true);
    });
});
