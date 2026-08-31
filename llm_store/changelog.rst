18.0.1.1.0 (2026-08-31)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][SEC] Access-control hardening: removed the ``base.group_user`` read
  ACL from ``llm.store`` — now ``llm.group_llm_manager``-only. Store config was
  only needed by the (now disabled) raw ``knowledge_retriever`` tool; regular
  users reach retrieval through the sudo'd König source-mixin choke-point.

18.0.1.0.1 (2026-07-16)
~~~~~~~~~~~~~~~~~~~~~~~~

* [KOENIG][IMP] Menu restructure: menus consolidated under 'König Intelligence'
  (was 'LLM'). Items re-parented and re-sequenced for clarity.

18.0.1.0.0 (2025-10-23)
~~~~~~~~~~~~~~~~~~~~~~~

* [MIGRATION] Migrated to Odoo 18.0
* [IMP] Updated views and dependencies for compatibility

16.0.1.0.0
~~~~~~~~~~

* [INIT] Initial release
* [ADD] Vector store abstraction layer
* [ADD] Collection management
* [ADD] Support for ChromaDB, pgvector, and Qdrant
