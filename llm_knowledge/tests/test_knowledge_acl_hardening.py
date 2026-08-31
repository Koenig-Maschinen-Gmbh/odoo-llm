"""KOENIG fork: knowledge models are LLM-manager-only at the ACL level.

The vector store's tables hold derived copies of potentially sensitive source
records (wiki pages, attachments, chatter, code). The apexive base granted
``base.group_user`` read on all of them, which let ANY internal user dump full
chunk content via RPC/UI — bypassing the per-source-record ``check_access``
gate enforced at the retrieval choke-point (``koenig_ai_core`` source mixin).
The fork now scopes all five models to ``llm.group_llm_manager``; regular users
reach content only through the sudo'd choke-point, which applies the live
per-record check. These tests lock that posture.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestKnowledgeAclHardening(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain internal user: member of base.group_user, NOT llm manager.
        cls.staff = cls.env["res.users"].create(
            {
                "name": "ACL Probe Staff",
                "login": "acl_probe_staff",
                "email": "acl_probe_staff@example.com",
                "groups_id": [(4, cls.env.ref("base.group_user").id)],
            }
        )
        cls.manager_group = cls.env.ref("llm.group_llm_manager")
        cls.knowledge_models = [
            "llm.knowledge.chunk",
            "llm.knowledge.collection",
            "llm.knowledge.domain",
            "llm.resource",
        ]

    def test_regular_user_cannot_read_knowledge_models(self):
        """Every internal user had read before; now they must get AccessError."""
        for model_name in self.knowledge_models:
            with self.subTest(model=model_name):
                model = self.env[model_name].with_user(self.staff)
                with self.assertRaises(AccessError):
                    model.search([], limit=1).read()

    def test_regular_user_cannot_create_or_write_chunk(self):
        """Write/create/unlink were never granted to base.group_user; keep it so."""
        chunk_model = self.env["llm.knowledge.chunk"].with_user(self.staff)
        with self.assertRaises(AccessError):
            chunk_model.create({"resource_id": False, "sequence": 1, "content": "x"})

    def test_manager_can_search_knowledge_models(self):
        """LLM managers retain read access (search must not raise)."""
        manager = self.env["res.users"].create(
            {
                "name": "ACL Probe Manager",
                "login": "acl_probe_manager",
                "email": "acl_probe_manager@example.com",
                "groups_id": [
                    (4, self.env.ref("base.group_user").id),
                    (4, self.manager_group.id),
                ],
            }
        )
        for model_name in self.knowledge_models:
            with self.subTest(model=model_name):
                # search() applies ir.model.access; must not raise for manager.
                self.env[model_name].with_user(manager).search([], limit=1)

    def test_embedding_table_is_manager_only(self):
        """The pgvector embedding table (raw vectors) must not be user-readable."""
        embedding = self.env["llm.knowledge.chunk.embedding"].with_user(self.staff)
        with self.assertRaises(AccessError):
            embedding.search([], limit=1).read()
