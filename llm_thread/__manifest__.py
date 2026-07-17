{
    "name": "Easy AI Chat",
    "summary": "Simple AI Chat for Odoo",
    "description": """
Easy AI Chat for Odoo
=====================
A user-friendly module that brings AI-powered chat to your Odoo environment. Integrate with multiple AI providers, manage real-time conversations, and enhance workflows with multimodal support.

Key Features:
- Multiple AI Providers: OpenAI, Anthropic, Grok, Ollama, DeepSeek, and more
- Real-Time Chat: Instant AI conversations integrated with Odoo's mail system
- Multimodal Support: Go beyond text with advanced AI models
- Full Odoo Integration: Link chats to any Odoo record for context
- Tool Integration: Enable AI to execute custom tools and functions
- Function Calling: Select specific tools for each thread to enhance AI capabilities
- Optimized Performance: Efficient role-based message handling for better performance

Recent Updates:
- Refactored to use stored llm_role field for maximum efficiency
- Improved performance with direct field filtering and comparison
- Better integration with LLM base module's stored role field
- Enhanced database performance with proper indexing on llm_role field

Getting Started:
1. Install this module and the "LLM Integration Base" dependency
2. Configure your AI provider API keys
3. Fetch available models with one click
4. Start chatting from anywhere in Odoo

Use cases include customer support automation, data analysis, training assistance, custom AI workflows, and automated tool execution for your business.

Contact: support@apexive.com
    """,
    "category": "Productivity, Discuss",
    "version": "18.0.1.16.0",
    "depends": ["base", "mail", "web", "llm", "llm_tool"],
    "author": "Apexive Solutions LLC",
    "website": "https://github.com/apexive/odoo-llm",
    "external_dependencies": {"python": ["emoji", "markdown2"]},
    "data": [
        "security/llm_thread_security.xml",
        "security/ir.model.access.csv",
        "views/llm_thread_views.xml",
        "views/llm_thread_tag_views.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            # Services - LLM store service for integration with mail.store
            "llm_thread/static/src/services/llm_store_service.js",
            # Components - LLM Chat Container using existing mail components
            "llm_thread/static/src/components/llm_chat_container/llm_chat_container.js",
            "llm_thread/static/src/components/llm_chat_container/llm_chat_container.xml",
            "llm_thread/static/src/components/llm_chat_container/llm_chat_container.scss",
            # Thread Header component with provider/model/tool selections
            "llm_thread/static/src/components/llm_thread_header/llm_thread_header.js",
            "llm_thread/static/src/components/llm_thread_header/llm_thread_header.xml",
            "llm_thread/static/src/components/llm_thread_header/llm_thread_header.scss",
            # Related Record component for linking threads to Odoo records
            "llm_thread/static/src/components/llm_related_record/llm_related_record.js",
            "llm_thread/static/src/components/llm_related_record/llm_related_record.xml",
            "llm_thread/static/src/components/llm_related_record/llm_related_record.scss",
            "llm_thread/static/src/components/llm_related_record/llm_record_picker_dialog.js",
            "llm_thread/static/src/components/llm_related_record/llm_record_picker_dialog.xml",
            # Tool Message component for displaying tool results
            "llm_thread/static/src/components/llm_tool_message/llm_tool_message.js",
            "llm_thread/static/src/components/llm_tool_message/llm_tool_message.xml",
            "llm_thread/static/src/components/llm_tool_message/llm_tool_message.scss",
            # P-UX: Sidebar (thread & memory management UX — grouping/search/
            # tags/archive/bulk). Extracted from LLMChatContainer.
            "llm_thread/static/src/components/llm_sidebar/llm_sidebar.js",
            "llm_thread/static/src/components/llm_sidebar/llm_sidebar.xml",
            "llm_thread/static/src/components/llm_sidebar/llm_sidebar.scss",
            # P-HUD: subtle stats display under the composer
            "llm_thread/static/src/components/llm_thread_hud/llm_thread_hud.js",
            "llm_thread/static/src/components/llm_thread_hud/llm_thread_hud.xml",
            "llm_thread/static/src/components/llm_thread_hud/llm_thread_hud.scss",
            # P-UX: bulk-tag picker dialog
            "llm_thread/static/src/components/llm_bulk_tag_dialog/llm_bulk_tag_dialog.js",
            "llm_thread/static/src/components/llm_bulk_tag_dialog/llm_bulk_tag_dialog.xml",
            # Markdown body styling for assistant answers (P-CHAT M1)
            "llm_thread/static/src/components/llm_md_body/llm_md_body.scss",
            # Steps drawer + error prominence (P-CHAT M2)
            "llm_thread/static/src/components/llm_steps/llm_steps.scss",
            # Message classification helpers (P-CHAT M2) — used by message_patch.js
            "llm_thread/static/src/utils/llm_message_classify.js",
            # P-UX: date-bucket grouping helper — used by llm_sidebar.js
            "llm_thread/static/src/utils/llm_date_bucket.js",
            # Patches - Safe extensions of mail components with conditional LLM logic
            "llm_thread/static/src/patches/composer_patch.js",
            "llm_thread/static/src/patches/composer_patch.xml",
            "llm_thread/static/src/patches/thread_patch.js",
            "llm_thread/static/src/patches/thread_model_patch.js",
            "llm_thread/static/src/patches/chatter_patch.js",
            "llm_thread/static/src/patches/message_patch.js",
            "llm_thread/static/src/patches/message_patch.xml",
            # Templates - Extensions of existing mail templates
            "llm_thread/static/src/templates/chatter_ai_button.xml",
            "llm_thread/static/src/templates/chatter_ai.scss",
            "llm_thread/static/src/templates/llm_chat_client_action.xml",
            # Client Actions - Following Odoo 18.0 patterns
            "llm_thread/static/src/client_actions/llm_chat_client_action.js",
            "llm_thread/static/src/client_actions/open_chatter_action.js",
        ],
        "web.assets_unit_tests": [
            # P-CHAT M2 — Hoot unit tests for the message classification helpers.
            # The util is bundled here too (assets_unit_tests is isolated).
            "llm_thread/static/src/utils/llm_message_classify.js",
            "llm_thread/static/tests/llm_message_classify.test.js",
            # P-CHAT M2 — is_error store-field loop-closer.
            "llm_thread/static/tests/llm_message_is_error.test.js",
            # P-UX — date-bucket grouping helper (pure function) + its Hoot suite.
            "llm_thread/static/src/utils/llm_date_bucket.js",
            "llm_thread/static/tests/llm_date_bucket.test.js",
        ],
    },
    "images": [
        "static/description/banner.jpeg",
    ],
    "license": "LGPL-3",
    "installable": True,
    "application": True,
    "auto_install": False,
}
