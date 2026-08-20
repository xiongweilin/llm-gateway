import sys
from types import SimpleNamespace
from pathlib import Path


LITELLM_DIR = Path(__file__).parents[1]
if str(LITELLM_DIR) not in sys.path:
    sys.path.insert(0, str(LITELLM_DIR))

from run_server import normalize_opencode_tool_schemas
from litellm.llms.openai.responses.transformation import OpenAIResponsesAPIConfig


def test_normalize_opencode_tool_schemas_adds_missing_properties() -> None:
    body = {
        "tools": [
            {
                "type": "function",
                "name": "shell",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["cmd"],
                },
            }
        ]
    }

    assert normalize_opencode_tool_schemas(body) == 1
    assert body["tools"][0]["parameters"]["required"] == ["cmd", "limit"]


def test_normalize_opencode_tool_schemas_handles_nested_input_tools() -> None:
    body = {
        "input": [
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "tools": [
                            {
                                "type": "function",
                                "parameters": {
                                    "properties": {"limit": {"type": "integer"}}
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }

    assert normalize_opencode_tool_schemas(body) == 1
    schema = body["input"][0]["tools"][0]["tools"][0]["parameters"]
    assert schema["required"] == ["limit"]


def test_openai_responses_transform_applies_outbound_compatibility() -> None:
    config = OpenAIResponsesAPIConfig()
    data = config.transform_responses_api_request(
        model="muse-spark-1.2-contributor",
        input=[],
        response_api_optional_request_params={
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "parameters": {
                        "type": "object",
                        "properties": {"limit": {"type": "integer"}},
                    },
                }
            ]
        },
        litellm_params=SimpleNamespace(api_base="https://opencode.ai/zen/go/v1"),
        headers={},
    )

    assert data["tools"][0]["parameters"]["required"] == ["limit"]
