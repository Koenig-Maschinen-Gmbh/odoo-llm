"""KOENIG fork: the raw ``knowledge_retriever`` tool is disabled.

It searched ``llm.knowledge.chunk`` unfiltered as the calling user and returned
content WITHOUT gating each chunk through its source record's ``check_access``
— leaking any indexed data to any caller. The implementation now returns a
structured error pointing at the ACL-safe source tools instead of executing a
vector search. This test locks that behaviour (and guards against someone
re-enabling the leaky path by accident).
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestKnowledgeRetrieverDisabled(TransactionCase):
    def test_retriever_returns_structured_error(self):
        tool = self.env["llm.tool"].new()  # transient instance to reach the method
        result = tool.knowledge_retriever_execute(query="anything", collection_id=1)
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("error"), "must flag an error, not search")
        self.assertIn("disabled", str(result.get("message", "")).lower())
        # Must NOT return any chunk content / results payload.
        self.assertNotIn("results", result)
        self.assertNotIn("total_chunks", result)

    def test_retriever_does_not_touch_chunk_model(self):
        """The disabled path must not even browse the (now manager-only) chunks."""
        # A plain internal user calling the tool must still get the structured
        # error — NOT an AccessError from the manager-only chunk ACL.
        staff = self.env["res.users"].create(
            {
                "name": "Retriever Probe Staff",
                "login": "retriever_probe_staff",
                "email": "retriever_probe_staff@example.com",
                "groups_id": [(4, self.env.ref("base.group_user").id)],
            }
        )
        tool = self.env["llm.tool"].with_user(staff).new()
        result = tool.knowledge_retriever_execute(query="x", collection_id=1)
        self.assertTrue(result.get("error"))
