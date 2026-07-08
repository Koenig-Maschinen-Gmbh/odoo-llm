from psycopg2 import IntegrityError

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLlmThreadTag(TransactionCase):
    """P-UX: ``llm.thread.tag`` model — mirrors the ``project.tags`` pattern."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Tag = cls.env["llm.thread.tag"]

    def test_create_tag(self):
        tag = self.Tag.create([{"name": "Work", "color": 3}])
        self.assertEqual(tag.name, "Work")
        self.assertEqual(tag.color, 3)

    def test_default_color_in_range(self):
        """Default color is a valid color index (1-11)."""
        tag = self.Tag.create([{"name": "Inbox"}])
        self.assertIn(tag.color, range(1, 12))

    def test_name_uniq_constraint(self):
        """A duplicate name violates the DB unique constraint (``_sql_constraints``
        unique violations surface as a psycopg2 IntegrityError at the ORM level;
        the friendly message mapping is UI-side only)."""
        self.Tag.create([{"name": "Unique"}])
        with self.assertRaises(IntegrityError):
            self.Tag.create([{"name": "Unique"}])

    def test_name_create_dedup_case_insensitive(self):
        """Quick-create dedup: ``name_create`` returns the existing tag on a
        case-insensitive match instead of spawning a duplicate (``project.tags``
        convention — the ``many2many_tags`` widget quick-creates on type-ahead)."""
        existing = self.Tag.create([{"name": "Research"}])
        new_id, _name = self.Tag.name_create("research")
        self.assertEqual(
            new_id, existing.id, "name_create should reuse the existing tag"
        )
        # No duplicate was created.
        self.assertEqual(
            len(self.Tag.search([("name", "=ilike", "research")])),
            1,
        )

    def test_name_create_new_tag(self):
        """``name_create`` with a brand-new label creates the tag."""
        new_id, name = self.Tag.name_create("Brand New Tag")
        self.assertTrue(new_id)
        self.assertEqual(name, "Brand New Tag")

    def test_tag_thread_relation(self):
        """Tags relate to threads (``thread.tag_ids``). There is intentionally
        no inverse ``tag.thread_ids`` field — see the comment on
        ``llm.thread.tag_ids`` (auto relation, required for the
        ``llm.thread.mock`` prototype-inheriting transient)."""
        provider = (
            self.env["llm.provider"]
            .sudo()
            .create(
                {
                    "name": "P",
                    "service": "openai",
                    "api_base": "https://x/v1",
                    "api_key": "k",
                }
            )
        )
        model = (
            self.env["llm.model"]
            .sudo()
            .create({"name": "m", "provider_id": provider.id, "model_use": "chat"})
        )
        thread = (
            self.env["llm.thread"]
            .sudo()
            .create({"name": "T", "provider_id": provider.id, "model_id": model.id})
        )
        tag = self.Tag.create([{"name": "Linked"}])
        thread.tag_ids = [(4, tag.id)]
        self.assertIn(tag, thread.tag_ids)
