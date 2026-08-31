18.0.1.1.0 (2026-08-31)
------------------------

* [KOENIG][SEC] Disabled the raw ``knowledge_retriever`` tool implementation.
  It searched ``llm.knowledge.chunk`` unfiltered as the calling user and
  returned chunk content WITHOUT gating each chunk through its source record's
  ``check_access('read')`` — leaking any indexed data to any caller.
  ``knowledge_retriever_execute`` now returns a structured error dict
  (``{"error": True, "message": ...}``) pointing at the ACL-safe source tools
  (wiki/attachment/chatter/code search) plus a warning log, instead of running
  a vector search. The now-dead ``_group_chunks_by_resource`` /
  ``_get_top_resources`` / ``_process_search_results`` helpers were removed.
  New test module ``tests/test_knowledge_retriever_disabled.py``.

18.0.1.0.1 (2025-10-23)
------------------------

* [MIG] Migrated to Odoo 18.0
* [FIX] Fixed tool schema generation to safely handle missing description keys
* [IMP] Updated view_mode references from 'tree' to 'list' for Odoo 18.0 compatibility

16.0.1.0.1 (2025-04-04)
------------------------

* [ADD] Added Knowledge Bot assistant that uses knowledge_retriever tool
* [IMP] Integrated with OpenAI GPT-4o model for enhanced knowledge retrieval capabilities

16.0.1.0.0 (2025-03-28)
------------------------

* Initial release of the LLM Tool RAG module
* Added Knowledge Retriever tool for semantic document search
* Implemented document search mixin for reusable search functionality
* Added integration with core RAG module for document chunk access
* Implemented security model with read-only access for regular users and full access for LLM managers
