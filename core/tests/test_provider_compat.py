from llm_gateway.core_server import (
    normalize_opencode_custom_tools,
    normalize_opencode_tool_schemas,
)


def test_custom_tools_are_normalized_for_responses_compatible_provider() -> None:
    payload = {
        "tools": [
            {"type": "custom", "name": "shell", "description": "Run shell"}
        ]
    }

    assert normalize_opencode_custom_tools(payload) == 1
    assert payload["tools"][0]["type"] == "function"
    assert payload["tools"][0]["name"] == "shell"
    assert payload["tools"][0]["parameters"]["required"] == ["input"]


def test_tool_schema_required_fields_include_all_properties() -> None:
    payload = {
        "tools": [
            {
                "type": "function",
                "name": "query",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
            }
        ]
    }

    assert normalize_opencode_tool_schemas(payload) == 1
    assert payload["tools"][0]["parameters"]["required"] == ["query", "limit"]
