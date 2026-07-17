{
    "name": "LLM Assistant",
    "summary": """
        LLM/AI Assistant module with prompt templates for Odoo
    """,
    "description": "Configure AI assistants with specific roles, goals, and tools to enhance AI interactions, plus reusable prompt templates (text, YAML, JSON) with dynamic arguments, multi-step workflows, categories/tags, and context-simulation testing.",
    "category": "Productivity, Discuss",
    "version": "18.0.1.9.0",
    "depends": [
        "base",
        "mail",
        "web",
        "llm",
        "llm_thread",
        "llm_tool",
        "web_json_editor",
    ],
    "external_dependencies": {
        "python": ["jinja2", "pyyaml", "jsonschema"],
    },
    "author": "Apexive Solutions LLC",
    "website": "https://github.com/apexive/odoo-llm",
    "data": [
        "security/ir.model.access.csv",
        "data/llm_prompt_tag_data.xml",
        "data/llm_prompt_category_data.xml",
        "data/llm_prompt_export_data.xml",
        "data/llm_prompt_data.xml",
        "data/llm_assistant_data.xml",
        "views/llm_prompt_views.xml",
        "views/llm_prompt_tag_views.xml",
        "views/llm_prompt_category_views.xml",
        "views/llm_assistant_views.xml",
        "views/llm_thread_views.xml",
        "views/llm_menu_views.xml",
        "wizards/llm_prompt_test_views.xml",
    ],
    "images": [
        "static/description/banner.jpeg",
    ],
    "assets": {
        "web.assets_backend": [
            # Service patches
            "llm_assistant/static/src/services/llm_store_service_patch.js",
            # Component patches
            "llm_assistant/static/src/patches/llm_thread_header_patch.js",
            "llm_assistant/static/src/patches/llm_thread_header_patch.xml",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
    "auto_install": False,
}
