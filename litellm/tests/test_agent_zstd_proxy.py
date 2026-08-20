import importlib.util
import json
from pathlib import Path


PROXY_PATH = Path(__file__).parents[2] / "tools" / "agent-zstd-proxy.py"
SPEC = importlib.util.spec_from_file_location("agent_zstd_proxy", PROXY_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


def test_collaboration_request_uses_plaintext_alias() -> None:
    request = {
        "model": "gpt-5.6-luna",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "collaboration",
                        "tools": [
                            {
                                "type": "function",
                                "name": "spawn_agent",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "message": {"type": "string", "encrypted": True},
                                    },
                                },
                            },
                            {
                                "type": "function",
                                "name": "wait_agent",
                                "parameters": {"type": "object"},
                            },
                        ],
                    }
                ],
            },
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "arguments": "{}",
                "encrypted_function_args": [],
                "call_id": "call_old",
            },
        ],
    }

    adapted = json.loads(proxy.adapt_collaboration_request(json.dumps(request).encode()))
    namespace = adapted["input"][0]["tools"][0]
    assert namespace["name"] == "local_collaboration"
    assert "encrypted" not in namespace["tools"][0]["parameters"]["properties"]["message"]
    assert adapted["input"][1]["namespace"] == "local_collaboration"
    assert "encrypted_function_args" not in adapted["input"][1]


def test_agent_message_conversion_is_scoped_to_opencode_plaintext() -> None:
    plain = {
        "model": "opencode-go/deepseek-v4-flash",
        "input": [
            {
                "type": "agent_message",
                "content": [{"type": "input_text", "text": "task"}],
            }
        ],
    }
    converted = json.loads(proxy.normalize_agent_messages(json.dumps(plain).encode()))
    assert converted["input"][0] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "task"}],
    }

    encrypted = json.loads(json.dumps(plain))
    encrypted["input"][0]["content"].append(
        {"type": "encrypted_content", "encrypted_content": "opaque"}
    )
    assert json.loads(proxy.normalize_agent_messages(json.dumps(encrypted).encode())) == encrypted

    chatgpt = json.loads(json.dumps(plain))
    chatgpt["model"] = "gpt-5.6-luna"
    assert json.loads(proxy.normalize_agent_messages(json.dumps(chatgpt).encode())) == chatgpt


def test_sse_rewrites_all_collaboration_calls() -> None:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "namespace": "local_collaboration",
            "name": "wait_agent",
            "arguments": "{}",
            "call_id": "call_1",
        },
    }
    wire = b"data: " + json.dumps(event).encode() + b"\n\n"

    rewritten, pending, count = proxy.rewrite_sse_collaboration_calls(wire)
    assert pending == b""
    assert count == 1
    payload = next(line[6:] for line in rewritten.splitlines() if line.startswith(b"data: "))
    item = json.loads(payload)["item"]
    assert item["namespace"] == "collaboration"
    assert "encrypted_function_args" not in item


def test_nonstream_aggregation_recovers_output_items() -> None:
    item_event = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "OK"}],
        },
    }
    completed_event = {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        },
    }
    wire = (
        b"data: "
        + json.dumps(item_event).encode()
        + b"\n\n"
        + b"data: "
        + json.dumps(completed_event).encode()
        + b"\n\n"
    )

    aggregated, count = proxy.aggregate_responses_sse(wire)
    assert count == 0
    response = json.loads(aggregated)
    assert response["output"] == [item_event["item"]]
    assert response["usage"]["total_tokens"] == 12


def test_request_summary_never_contains_prompt_text() -> None:
    body = json.dumps(
        {
            "model": "test",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "PRIVATE_CANARY"}],
                }
            ],
        }
    ).encode()
    summary = proxy.request_summary(body)
    assert "PRIVATE_CANARY" not in json.dumps(summary)


def test_tool_schema_normalization_fixes_null_type() -> None:
    request = {
        "model": "opencode-go/deepseek-v4-flash",
        "input": [{"type": "message", "role": "user", "content": []}],
        "tools": [
            {
                "type": "function",
                "name": "automation_update",
                "parameters": {
                    "type": None,
                    "properties": {"limit": {"type": "integer"}},
                },
            },
            {
                "type": "function",
                "name": "shell",
                "input_schema": {"properties": {"cmd": {"type": "string"}}},
            },
            {
                "type": "function",
                "name": "valid",
                "parameters": {"type": "object", "properties": {}},
            },
        ],
    }

    normalized = json.loads(proxy.normalize_tool_schemas(json.dumps(request).encode()))
    tools = {tool["name"]: tool for tool in normalized["tools"]}
    assert tools["automation_update"]["parameters"]["type"] == "object"
    assert tools["automation_update"]["parameters"]["required"] == ["limit"]
    assert tools["shell"]["input_schema"]["type"] == "object"
    assert tools["valid"]["parameters"]["type"] == "object"


def test_tool_schema_normalization_reaches_nested_namespaces() -> None:
    request = {
        "model": "opencode-go/deepseek-v4-flash",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "collaboration",
                        "tools": [
                            {
                                "type": "function",
                                "name": "spawn_agent",
                                "parameters": {"properties": {}},
                            }
                        ],
                    }
                ],
            }
        ],
        "tools": [],
    }

    normalized = json.loads(proxy.normalize_tool_schemas(json.dumps(request).encode()))
    tool = normalized["input"][0]["tools"][0]["tools"][0]
    assert tool["parameters"]["type"] == "object"


def test_tool_schema_normalization_keeps_valid_request_unchanged() -> None:
    request = {
        "model": "gpt-5.6-luna",
        "input": [],
        "tools": [
            {
                "type": "function",
                "name": "valid",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert proxy.normalize_tool_schemas(raw) is raw


def test_tool_schema_required_normalization_is_scoped_to_opencode() -> None:
    request = {
        "model": "opencode-go/muse-spark-1.2-contributor",
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
        ],
    }
    normalized = json.loads(proxy.normalize_tool_schemas(json.dumps(request).encode()))
    assert normalized["tools"][0]["parameters"]["required"] == ["cmd", "limit"]

    request["model"] = "gpt-5.6-luna"
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert proxy.normalize_tool_schemas(raw) is raw


def test_custom_tool_declarations_are_removed_only_for_opencode() -> None:
    request = {
        "model": "opencode-go/muse-spark-1.2-contributor",
        "tools": [
            {"type": "custom", "name": "shell"},
            {"type": "function", "name": "read_file", "parameters": {"properties": {}}},
        ],
        "input": [{"type": "custom_tool_call", "name": "shell"}],
    }
    normalized = json.loads(proxy.drop_opencode_custom_tools(json.dumps(request).encode()))
    assert [tool["type"] for tool in normalized["tools"]] == ["function"]
    assert normalized["input"][0]["type"] == "custom_tool_call"

    request["model"] = "gpt-5.6-luna"
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert proxy.drop_opencode_custom_tools(raw) is raw


def test_truncate_input_never_leaves_orphaned_tool_outputs() -> None:
    # Regression: context truncation dropped a function_call but kept its
    # function_call_output, leaving an orphaned tool output behind a kept call.
    # Upstream (OpenCode Go) rejects that with "No tool call found for tool
    # output". The old boundary trim only removed outputs at the tail edge and
    # missed orphans sitting behind a kept call in interleaved tool rounds.
    request = {
        "model": "opencode-go/deepseek-v4-flash",
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sys"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "u1"}]},
            {"type": "function_call", "call_id": "call_A", "name": "tool_a", "arguments": "{}"},
            {"type": "function_call", "call_id": "call_B", "name": "tool_b", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_A", "output": "x" * 500},
            {"type": "function_call_output", "call_id": "call_B", "output": "y" * 200},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "u2"}]},
        ],
    }
    inp = request["input"]
    fixed = proxy._json_tokens({"model": "opencode-go/deepseek-v4-flash", "input": []})
    # Budget exactly fits head + items[3..]; drops [u1, call_A], keeps call_B
    # with orphaned output call_A still in the tail.
    budget = fixed + proxy._item_tokens(inp[0]) + sum(proxy._item_tokens(i) for i in inp[3:])
    previous = proxy.INPUT_TOKEN_BUDGET
    proxy.INPUT_TOKEN_BUDGET = budget
    try:
        truncated = json.loads(proxy.truncate_input(json.dumps(request).encode()))["input"]
    finally:
        proxy.INPUT_TOKEN_BUDGET = previous

    call_ids = {i["call_id"] for i in truncated if i.get("type") == "function_call"}
    outputs = [i for i in truncated if i.get("type") == "function_call_output"]
    assert call_ids == {"call_B"}
    assert not any(o["call_id"] not in call_ids for o in outputs)
    assert [o["call_id"] for o in outputs] == ["call_B"]
