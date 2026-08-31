import logging
from typing import Any

from odoo import _, api, models

_logger = logging.getLogger(__name__)


class LLMToolKnowledgeRetriever(models.Model):
    _inherit = "llm.tool"

    @api.model
    def _get_available_implementations(self):
        implementations = super()._get_available_implementations()
        return implementations + [("knowledge_retriever", "Knowledge Retriever")]

    @api.model
    def _get_available_collections(self):
        """Retrieve a list of available resource collections.

        Returns:
            list: List of tuples with collection_id and name
        """
        Collection = self.env["llm.knowledge.collection"].sudo()
        collections = Collection.search([("active", "=", True)])
        return [(str(collection.id), collection.name) for collection in collections]

    def get_input_schema(self):
        schema = super().get_input_schema()
        if self.implementation == "knowledge_retriever":
            available_collections = self._get_available_collections()
            collections_description = ", ".join(
                [
                    f"'{name}' (ID: {collection_id})"
                    for collection_id, name in available_collections
                ]
            )
            # Add or append to collection_id description
            if "properties" in schema and "collection_id" in schema["properties"]:
                existing_desc = schema["properties"]["collection_id"].get(
                    "description", ""
                )
                if existing_desc:
                    schema["properties"]["collection_id"]["description"] = (
                        f"{existing_desc}. Available collections: {collections_description}"
                    )
                else:
                    schema["properties"]["collection_id"]["description"] = (
                        f"The ID of the knowledge collection to search. Available collections: {collections_description}"
                    )
        return schema

    def knowledge_retriever_execute(
        self,
        query: str,
        collection_id: int,
        top_k: int = 5,
        top_n: int = 3,
        similarity_cutoff: float = 0.5,
    ) -> dict[str, Any]:
        """
        Retrieve relevant knowledge from the resource database using semantic search.

        .. warning::
            DISABLED at König: this implementation searches ``llm.knowledge.chunk``
            unfiltered as the calling user and returns content WITHOUT checking
            whether the user may read the *source record* each chunk came from.
            It leaks any indexed data to any caller. It returns a structured error
            (not an exception) so the tool-calling model can relay the reason to
            the user instead of producing exception noise.

        Use this tool when you need to:
        - Answer questions that require specific information from the knowledge base
        - Find relevant resources or content based on semantic similarity
        - Access information that may not be in your training data

        The tool returns chunks of text from resources ranked by relevance to your query.

        Parameters:
            query: REQUIRED The search query text used to find relevant information. Be specific and focused in your query to get the most relevant results.
            collection_id: REQUIRED The ID (as a string) of the 'llm.knowledge.collection' record to search within.
            top_k: Maximum number of chunks to retrieve per resource. Higher values return more context from each resource but may include less relevant passages.
            top_n: Maximum number of distinct resources to retrieve results from. Increase this value to get information from more diverse sources.
            similarity_cutoff: Minimum semantic similarity threshold (0.0-1.0) for including results. Higher values (e.g., 0.7) return only highly relevant results.
        """
        _logger.warning(
            "knowledge_retriever invoked but is disabled (bypasses Odoo record "
            "access control); query=%r collection_id=%s",
            query,
            collection_id,
        )
        return {
            "error": True,
            "message": _(
                "knowledge_retriever is disabled on this system: it bypasses "
                "Odoo record access control. Use the ACL-safe source tools "
                "(wiki / attachment / chatter / code search) instead."
            ),
        }
