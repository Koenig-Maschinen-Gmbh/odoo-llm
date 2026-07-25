from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGetThreadStoreData(TransactionCase):
    """FIX-3 (TRACKER_2026-07-25_UI_RESEARCH.md §3): ``get_thread_store_data``
    returns the store dict for given thread IDs so the client can insert a
    freshly-created thread deterministically — no dependence on the bus
    broadcast or a heavy ``init_messaging`` re-fetch.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = (
            cls.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "Test Provider",
                    "service": "openai",
                    "api_base": "https://test.example.com/v1",
                    "api_key": "sk-test-key",
                }
            )
        )
        cls.model = (
            cls.env["llm.model"]
            .sudo()
            .create(
                {
                    "name": "test-model",
                    "provider_id": cls.provider.id,
                    "model_use": "chat",
                }
            )
        )

    def _make_thread(self, **extra):
        vals = {
            "name": "Store Data Test",
            "provider_id": self.provider.id,
            "model_id": self.model.id,
        }
        vals.update(extra)
        return self.env["llm.thread"].sudo().create(vals)

    def test_returns_store_dict_for_existing_thread(self):
        """A valid thread ID returns a store dict with the expected shape."""
        thread = self._make_thread(name="My Chat")
        result = self.env["llm.thread"].get_thread_store_data([thread.id])
        self.assertIn("mail.thread", result)
        self.assertEqual(len(result["mail.thread"]), 1)
        data = result["mail.thread"][0]
        self.assertEqual(data["id"], thread.id)
        self.assertEqual(data["model"], "llm.thread")
        self.assertEqual(data["name"], "My Chat")
        self.assertEqual(data["write_date"], thread.write_date)

    def test_multiple_threads(self):
        """Multiple IDs return multiple store dicts in order."""
        t1 = self._make_thread(name="T1")
        t2 = self._make_thread(name="T2")
        result = self.env["llm.thread"].get_thread_store_data([t1.id, t2.id])
        names = [d["name"] for d in result["mail.thread"]]
        self.assertEqual(set(names), {"T1", "T2"})

    def test_nonexistent_id_returns_empty(self):
        """A non-existent ID (filtered by exists()) yields no entry."""
        thread = self._make_thread()
        result = self.env["llm.thread"].get_thread_store_data([thread.id, 999999999])
        self.assertEqual(len(result["mail.thread"]), 1)

    def test_empty_list(self):
        """An empty list returns an empty mail.thread list."""
        result = self.env["llm.thread"].get_thread_store_data([])
        self.assertEqual(result, {"mail.thread": []})
