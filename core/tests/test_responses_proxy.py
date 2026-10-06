import asyncio
import importlib.util
import json
from pathlib import Path

import aiohttp
import aiohttp.web
from aiohttp.test_utils import TestClient, TestServer

PROXY_PATH = Path(__file__).parents[2] / "tools" / "responses-proxy.py"
SPEC = importlib.util.spec_from_file_location("responses_proxy", PROXY_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


def test_collaboration_request_uses_plaintext_alias() -> None:
    request = {
        "model": "gpt-6-luna",
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


def test_collaboration_request_repairs_flattened_call_name() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {
                "type": "function_call",
                "name": "local_collaboration.spawn_agent",
                "arguments": "{}",
                "call_id": "call_flat",
            }
        ],
    }

    adapted = json.loads(proxy.adapt_collaboration_request(json.dumps(request).encode()))
    call = adapted["input"][0]
    assert call["namespace"] == "local_collaboration"
    assert call["name"] == "spawn_agent"


def test_collaboration_request_repairs_unqualified_call_name() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {
                "type": "function_call",
                "name": "spawn_agent",
                "arguments": "{}",
                "call_id": "call_unqualified",
            }
        ],
    }

    adapted = json.loads(proxy.adapt_collaboration_request(json.dumps(request).encode()))
    call = adapted["input"][0]
    assert call["namespace"] == "local_collaboration"
    assert call["name"] == "spawn_agent"



def test_agent_message_conversion_is_scoped_to_opencode_plaintext() -> None:
    plain = {
        "model": "opencode-go/deepseek-flash",
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
    chatgpt["model"] = "gpt-6-luna"
    assert json.loads(proxy.normalize_agent_messages(json.dumps(chatgpt).encode())) == chatgpt


def test_compaction_trigger_normalization_remains_for_opencode() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {"type": "message", "role": "user", "content": []},
            {"type": "compaction_trigger", "id": "compact_1"},
            {"type": "function_call", "name": "exec", "arguments": "{}"},
        ],
    }

    normalized = json.loads(
        proxy.normalize_opencode_compaction_triggers(json.dumps(request).encode())
    )
    assert [item["type"] for item in normalized["input"]] == [
        "message",
        "function_call",
    ]

    request["model"] = "gpt-6-luna"
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert proxy.normalize_opencode_compaction_triggers(raw) == raw

def test_deepseek_compaction_item_is_restored_before_provider_request() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {"type": "message", "role": "user", "content": []},
            {
                "type": "compaction",
                "id": "cmp_1",
                "encrypted_content": "gateway-muse-test-token",
            },
        ],
    }
    old_tokens = proxy._MUSE_COMPACTION_TOKENS.copy()
    try:
        proxy._MUSE_COMPACTION_TOKENS.clear()
        proxy._MUSE_COMPACTION_TOKENS["gateway-muse-test-token"] = "Keep the active task."
        normalized = json.loads(
            proxy.normalize_muse_compaction_items(json.dumps(request).encode())
        )
        assert [item["type"] for item in normalized["input"]] == [
            "message",
            "message",
        ]
        assert "Gateway-generated historical checkpoint" in normalized["input"][1]["content"][0]["text"]

        request["model"] = "gpt-6-luna"
        raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        assert proxy.normalize_muse_compaction_items(raw) is raw
    finally:
        proxy._MUSE_COMPACTION_TOKENS.clear()
        proxy._MUSE_COMPACTION_TOKENS.update(old_tokens)


def test_deepseek_flash_reuses_muse_agent_loop_and_checkpoint_compatibility() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {"type": "message", "role": "user", "content": []},
            {
                "type": "compaction",
                "id": "cmp_deepseek",
                "encrypted_content": "gateway-deepseek-test-token",
            },
        ],
    }
    old_tokens = proxy._MUSE_COMPACTION_TOKENS.copy()
    try:
        proxy._MUSE_COMPACTION_TOKENS.clear()
        proxy._MUSE_COMPACTION_TOKENS["gateway-deepseek-test-token"] = (
            "Keep the DeepSeek Flash task context."
        )
        normalized = json.loads(
            proxy.normalize_muse_compaction_items(json.dumps(request).encode())
        )
        assert [item["type"] for item in normalized["input"]] == [
            "message",
            "message",
        ]
        assert "Gateway-generated historical checkpoint" in normalized["input"][1]["content"][0]["text"]

        instructions = json.loads(
            proxy.ensure_muse_autonomous_instructions(
                json.dumps({"model": request["model"], "input": []}).encode()
            )
        )["instructions"]
        assert "Continue the user's task across tool calls" in instructions
    finally:
        proxy._MUSE_COMPACTION_TOKENS.clear()
        proxy._MUSE_COMPACTION_TOKENS.update(old_tokens)


def test_deepseek_compaction_response_contains_one_protocol_item() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "objective"}],
            },
            {"type": "message", "role": "assistant", "content": []},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "continue"}],
            },
        ],
    }
    state = proxy.MuseCompactionState(
        summary="Objective: continue the active task.",
        compacted_prefix_count=1,
        compacted_prefix_hash="prefix-hash",
        head_hash="head-hash",
        provider_session="provider-session",
    )
    old_tokens = proxy._MUSE_COMPACTION_TOKENS.copy()
    try:
        proxy._MUSE_COMPACTION_TOKENS.clear()
        response = proxy._build_muse_compaction_response(
            json.dumps(request, separators=(",", ":")).encode(),
            state,
            "codex-session",
        )
        assert response["object"] == "response.compaction"
        assert response["output"][-1]["type"] == "compaction"
        assert sum(item["type"] == "compaction" for item in response["output"]) == 1
        assert all(item["role"] == "user" for item in response["output"][:-1])
        assert response["usage"]["total_tokens"] == (
            response["usage"]["input_tokens"] + response["usage"]["output_tokens"]
        )

        compacted_request = {
            "model": request["model"],
            "input": [request["input"][0], request["input"][2]],
        }
        compacted_response = proxy._build_muse_compaction_response(
            json.dumps(request, separators=(",", ":")).encode(),
            state,
            "codex-session",
            usage_body=json.dumps(compacted_request, separators=(",", ":")).encode(),
        )
        assert compacted_response["usage"]["input_tokens"] == proxy._json_tokens(
            compacted_request
        )

        wire = proxy._muse_compaction_response_to_sse(response)
        events = [
            json.loads(line[6:])
            for line in wire.splitlines()
            if line.startswith(b"data: ")
        ]
        completed = next(event for event in events if event["type"] == "response.completed")
        assert sum(
            item["type"] == "compaction"
            for item in completed["response"]["output"]
        ) == 1
    finally:
        proxy._MUSE_COMPACTION_TOKENS.clear()
        proxy._MUSE_COMPACTION_TOKENS.update(old_tokens)


def test_deepseek_compaction_trigger_detection_is_stable() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {"type": "message", "role": "user", "content": []},
            {"type": "message", "role": "assistant", "content": []},
            {"type": "message", "role": "user", "content": []},
        ],
    }
    assert not proxy._has_compaction_trigger(request)
    request["input"].insert(1, {"type": "compaction_trigger", "id": "c1"})
    assert proxy._has_compaction_trigger(request)


def test_deepseek_compaction_checkpoint_rewrites_history_and_reuses_state() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {"type": "message", "role": "user", "content": "objective"},
            *[
                {
                    "type": "message",
                    "role": "assistant" if index % 2 else "user",
                    "content": f"turn-{index}-" + ("x" * 20_000),
                }
                for index in range(6)
            ],
        ],
    }
    raw = json.dumps(request, separators=(",", ":")).encode()
    old_budget = proxy.MUSE_COMPACTION_TOKEN_BUDGET
    old_keep = proxy.MUSE_COMPACTION_KEEP_TOKEN_BUDGET
    old_states = proxy._MUSE_COMPACTION_STATES.copy()
    old_locks = proxy._MUSE_COMPACTION_LOCKS.copy()
    calls = []

    async def fake_checkpoint(*args, **kwargs):
        calls.append((args, kwargs))
        return "## Objective\nKeep working on the requested task."

    original_generator = proxy._generate_muse_checkpoint
    try:
        proxy.MUSE_COMPACTION_TOKEN_BUDGET = 30_000
        proxy.MUSE_COMPACTION_KEEP_TOKEN_BUDGET = 4_000
        proxy._MUSE_COMPACTION_STATES.clear()
        proxy._MUSE_COMPACTION_LOCKS.clear()
        proxy._generate_muse_checkpoint = fake_checkpoint

        compacted, changed, compacted_session = asyncio.run(
            proxy.create_muse_compaction_checkpoint(
                raw,
                "codex-test-session",
                None,
                "http://127.0.0.1:4100",
                {},
            )
        )
        compacted_obj = json.loads(compacted)
        assert changed
        assert len(calls) == 1
        assert compacted_session != "codex-test-session"
        assert "Gateway-generated historical checkpoint" in compacted_obj["input"][1]["content"][0]["text"]
        assert compacted_obj["input"][0] == request["input"][0]
        assert compacted_obj["input"][-1] == request["input"][-1]

        compacted_again, changed_again, compacted_session_again = asyncio.run(
            proxy.create_muse_compaction_checkpoint(
                raw,
                "codex-test-session",
                None,
                "http://127.0.0.1:4100",
                {},
            )
        )
        assert changed_again
        assert compacted_again == compacted
        assert compacted_session_again == compacted_session
        assert len(calls) == 1
    finally:
        proxy._generate_muse_checkpoint = original_generator
        proxy.MUSE_COMPACTION_TOKEN_BUDGET = old_budget
        proxy.MUSE_COMPACTION_KEEP_TOKEN_BUDGET = old_keep
        proxy._MUSE_COMPACTION_STATES.clear()
        proxy._MUSE_COMPACTION_STATES.update(old_states)
        proxy._MUSE_COMPACTION_LOCKS.clear()
        proxy._MUSE_COMPACTION_LOCKS.update(old_locks)


def test_deepseek_compaction_waiter_unwraps_body() -> None:
    async def run_waiter():
        task = asyncio.create_task(asyncio.sleep(0, result=(b"compacted", True, "session-2")))
        return await proxy._await_muse_compaction(task, None, False)

    body, prepared_response, provider_session = asyncio.run(run_waiter())
    assert body == b"compacted"
    assert prepared_response is None
    assert provider_session == "session-2"


def test_additional_tools_are_lifted_for_opencode() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "local_collaboration",
                        "tools": [
                            {
                                "type": "function",
                                "name": "wait_agent",
                                "parameters": {"type": "object"},
                            }
                        ],
                    }
                ],
            },
            {"type": "message", "role": "user", "content": []},
        ],
        "tools": [
            {"type": "function", "name": "existing", "parameters": {"type": "object"}}
        ],
    }

    normalized = json.loads(
        proxy.normalize_opencode_additional_tools(json.dumps(request).encode())
    )

    assert [item["type"] for item in normalized["input"]] == ["message"]
    assert [tool["name"] for tool in normalized["tools"]] == [
        "existing",
        "local_collaboration",
    ]


def test_additional_tools_are_unchanged_for_chatgpt() -> None:
    request = {
        "model": "gpt-6-luna",
        "input": [{"type": "additional_tools", "tools": []}],
    }
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()

    assert proxy.normalize_opencode_additional_tools(raw) is raw


def test_search_content_types_are_kept_only_for_preview_tools() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "tools": [
            {
                "type": "web_search",
                "search_content_types": ["text", "image"],
            },
            {
                "type": "web_search_preview",
                "search_content_types": ["text"],
            },
            {
                "type": "namespace",
                "name": "demo",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "search_content_types": ["text"],
                    },
                    {
                        "type": "web_search_preview",
                        "search_content_types": ["image"],
                    },
                ],
            },
        ],
        "input": [
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "web_search",
                        "search_content_types": ["text"],
                    }
                ],
            }
        ],
    }

    normalized = json.loads(
        proxy.normalize_opencode_search_tool_fields(json.dumps(request).encode())
    )

    assert "search_content_types" not in normalized["tools"][0]
    assert normalized["tools"][1]["search_content_types"] == ["text"]
    assert "search_content_types" not in normalized["tools"][2]["tools"][0]
    assert normalized["tools"][2]["tools"][1]["search_content_types"] == ["image"]
    assert "search_content_types" not in normalized["input"][0]["tools"][0]


def test_search_content_types_are_unchanged_for_chatgpt() -> None:
    request = {
        "model": "gpt-6-luna",
        "tools": [
            {"type": "web_search", "search_content_types": ["text"]},
        ],
    }
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()

    assert proxy.normalize_opencode_search_tool_fields(raw) is raw


def test_opencode_tool_descriptions_are_nonempty_and_scoped() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "tools": [
            {
                "type": "namespace",
                "name": "demo",
                "description": " ",
                "tools": [
                    {"type": "function", "name": "missing", "description": ""},
                ],
            },
            {"type": "function", "name": "omitted"},
            {"type": "function", "name": "kept", "description": "Keep this"},
        ],
    }

    normalized = json.loads(
        proxy.normalize_opencode_tool_descriptions(json.dumps(request).encode())
    )

    assert normalized["tools"][0]["description"] == "demo tool"
    assert normalized["tools"][0]["tools"][0]["description"] == "missing tool"
    assert normalized["tools"][1]["description"] == "omitted tool"
    assert normalized["tools"][2]["description"] == "Keep this"

    request["model"] = "gpt-6-luna"
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert proxy.normalize_opencode_tool_descriptions(raw) is raw


def test_scalar_responses_input_is_normalized_for_chatgpt_backend() -> None:
    request = {"model": "gpt-6-luna", "input": "diagnose this alert"}

    normalized = json.loads(proxy.normalize_scalar_responses_input(json.dumps(request).encode()))

    assert normalized["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "diagnose this alert"}],
        }
    ]


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


def test_sse_rewrites_flattened_collaboration_call_name() -> None:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "local_collaboration.spawn_agent",
            "arguments": "{}",
            "call_id": "call_flat",
        },
    }
    wire = b"data: " + json.dumps(event).encode() + b"\n\n"

    rewritten, pending, count = proxy.rewrite_sse_collaboration_calls(wire)
    assert pending == b""
    assert count == 1
    payload = next(line[6:] for line in rewritten.splitlines() if line.startswith(b"data: "))
    item = json.loads(payload)["item"]
    assert item["namespace"] == "collaboration"
    assert item["name"] == "spawn_agent"
    assert item["encrypted_function_args"] == []


def test_sse_rewrites_unqualified_collaboration_call_name() -> None:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "spawn_agent",
            "arguments": "{}",
            "call_id": "call_unqualified",
        },
    }
    wire = b"data: " + json.dumps(event).encode() + b"\n\n"

    rewritten, pending, count = proxy.rewrite_sse_collaboration_calls(wire)
    assert pending == b""
    assert count == 1
    payload = next(line[6:] for line in rewritten.splitlines() if line.startswith(b"data: "))
    item = json.loads(payload)["item"]
    assert item["namespace"] == "collaboration"
    assert item["name"] == "spawn_agent"
    assert item["encrypted_function_args"] == []


def test_opencode_function_call_arguments_coerce_integral_floats() -> None:
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"cmd":"Get-Date","yield_time_ms":1000.0,"max_output_tokens":10000.0}',
            "call_id": "call_1",
        },
    }
    wire = b"data: " + json.dumps(event).encode() + b"\n\n"

    rewritten, pending, count = proxy.rewrite_sse_collaboration_calls(
        wire,
        normalize_function_args=True,
    )
    assert pending == b""
    assert count == 1
    payload = next(line[6:] for line in rewritten.splitlines() if line.startswith(b"data: "))
    item = json.loads(payload)["item"]
    assert json.loads(item["arguments"]) == {
        "cmd": "Get-Date",
        "yield_time_ms": 1000,
        "max_output_tokens": 10000,
    }


def test_function_call_argument_normalization_preserves_fractional_values() -> None:
    value = {
        "type": "function_call",
        "name": "tool",
        "arguments": '{"ratio":1.5,"count":2.0}',
    }

    assert proxy._normalize_function_call_arguments(value) == 1
    assert json.loads(value["arguments"]) == {"ratio": 1.5, "count": 2}


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


def test_request_summary_records_tool_shapes_without_arguments() -> None:
    body = json.dumps(
        {
            "model": "opencode-go/deepseek-flash",
            "input": [
                {"type": "function_call", "name": "exec_command", "arguments": "SECRET"},
                {"type": "tool_search_call", "arguments": {"query": "SECRET"}},
            ],
            "tools": [
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                {
                    "type": "namespace",
                    "name": "mcp__demo",
                    "tools": [{"type": "function", "name": "read_file"}],
                },
            ],
        }
    ).encode()

    summary = proxy.request_summary(body)
    assert summary["input_call_names"] == {
        "function_call:exec_command": 1,
        "tool_search_call:<none>": 1,
    }
    assert summary["tool_declaration_types"] == {"function": 2, "namespace": 1}
    assert summary["tool_declaration_names"] == {
        "function:exec_command": 1,
        "function:read_file": 1,
        "namespace:mcp__demo": 1,
    }
    assert "SECRET" not in json.dumps(summary)


def test_tool_schema_normalization_fixes_null_type() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
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
        "model": "opencode-go/deepseek-flash",
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
        "model": "gpt-6-luna",
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
        "model": "opencode-go/deepseek-flash",
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

    request["model"] = "gpt-6-luna"
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert proxy.normalize_tool_schemas(raw) is raw


def test_custom_tool_declarations_are_bridged_for_opencode() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "tools": [
            {"type": "custom", "name": "exec", "description": "Run JavaScript."},
            {"type": "function", "name": "read_file", "parameters": {"properties": {}}},
        ],
        "input": [
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call_exec",
                "input": "await tools.exec_command({cmd: 'Get-Location'})",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_exec",
                "output": "ok",
            },
        ],
    }
    normalized = json.loads(proxy.normalize_opencode_custom_tools(json.dumps(request).encode()))
    assert [tool["type"] for tool in normalized["tools"]] == ["function", "function"]
    assert normalized["tools"][0]["parameters"]["required"] == ["input"]
    assert normalized["input"][0]["type"] == "function_call"
    assert json.loads(normalized["input"][0]["arguments"]) == {
        "input": "await tools.exec_command({cmd: 'Get-Location'})"
    }
    assert normalized["input"][1]["type"] == "function_call_output"

    request["model"] = "gpt-6-luna"
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    assert proxy.normalize_opencode_custom_tools(raw) is raw



def test_custom_exec_json_input_is_restored_to_javascript() -> None:
    assert proxy._custom_input_from_arguments(
        '{"input":"{\\"cmd\\":\\"Get-Location\\"}"}',
        "exec",
    ) == 'return await tools.exec_command({"cmd":"Get-Location"});'
    assert proxy._custom_input_from_arguments(
        '{"input":"{\\"code\\":\\"return 1;\\"}"}',
        "exec",
    ) == "return 1;"
    assert proxy._custom_input_from_arguments(
        '{"input":"const result = 1;"}',
        "exec",
    ) == "const result = 1;"
    assert proxy._custom_input_from_arguments(
        '{"code":"const result = 1;"}',
        "exec",
    ) == "const result = 1;"


def test_namespaced_calls_are_repaired_for_opencode() -> None:
    request = {
        "model": "opencode-go/deepseek-flash",
        "tools": [
            {
                "type": "namespace",
                "name": "mcp__cua_repl",
                "tools": [
                    {
                        "type": "function",
                        "name": "js",
                        "description": "Run JavaScript.",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ],
        "input": [
            {
                "type": "function_call",
                "name": "mcp__cua_repl.js",
                "arguments": "{}",
                "call_id": "call_js",
            }
        ],
    }
    normalized = json.loads(
        proxy.normalize_opencode_namespaced_calls(json.dumps(request).encode())
    )
    assert normalized["input"][0]["namespace"] == "mcp__cua_repl"
    assert normalized["input"][0]["name"] == "js"

    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "name": "mcp__cua_repl.js",
            "arguments": "{}",
            "call_id": "call_js_2",
        },
    }
    wire = b"data: " + json.dumps(event).encode() + b"\n\n"
    rewritten, pending, count = proxy.rewrite_sse_collaboration_calls(
        wire,
        namespaced_tools=proxy.collect_opencode_namespaced_tools(
            json.dumps(request).encode()
        ),
    )
    assert pending == b""
    assert count == 1
    payload = next(line[6:] for line in rewritten.splitlines() if line.startswith(b"data: "))
    item = json.loads(payload)["item"]
    assert item["namespace"] == "mcp__cua_repl"
    assert item["name"] == "js"


def test_custom_function_stream_is_restored_to_codex_custom_call() -> None:
    events = [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_exec",
                "name": "exec",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_exec",
            "delta": "const result = 1;",
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_exec",
            "arguments": '{"input":"const result = 1;"}',
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_exec",
                "name": "exec",
                "arguments": '{"input":"const result = 1;"}',
            },
        },
    ]
    wire = b"".join(
        b"data: " + json.dumps(event).encode() + b"\n\n"
        for event in events
    )

    rewritten, pending, count = proxy.rewrite_sse_collaboration_calls(
        wire,
        custom_tool_names={"exec"},
        custom_call_item_ids={},
    )
    assert pending == b""
    assert count == 4
    payloads = [
        json.loads(line[6:])
        for line in rewritten.splitlines()
        if line.startswith(b"data: ")
    ]
    assert payloads[0]["item"]["type"] == "custom_tool_call"
    assert payloads[1]["type"] == "response.custom_tool_call_input.delta"
    assert payloads[2]["type"] == "response.custom_tool_call_input.done"
    assert payloads[2]["input"] == "const result = 1;"
    assert payloads[3]["item"]["type"] == "custom_tool_call"
    assert payloads[3]["item"]["input"] == "const result = 1;"


def test_custom_exec_json_stream_is_restored_to_executable_javascript() -> None:
    events = [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_exec_json",
                "name": "exec",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_exec_json",
            "delta": '{"input":"{\\"cmd\\":\\"pwd\\"}"}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_exec_json",
            "arguments": '{"input":"{\\"cmd\\":\\"pwd\\"}"}',
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_exec_json",
                "name": "exec",
                "arguments": '{"input":"{\\"cmd\\":\\"pwd\\"}"}',
            },
        },
    ]
    frames = [
        b"data: " + json.dumps(event).encode() + b"\n\n"
        for event in events
    ]

    rewritten_parts = []
    pending = b""
    count = 0
    item_ids = {}
    argument_buffers = {}
    for frame in frames:
        rewritten, pending, changed = proxy.rewrite_sse_collaboration_calls(
            pending + frame,
            custom_tool_names={"exec"},
            custom_call_item_ids=item_ids,
            custom_call_argument_buffers=argument_buffers,
        )
        rewritten_parts.append(rewritten)
        count += changed
    rewritten = b"".join(rewritten_parts)
    assert pending == b""
    assert count == 5
    payloads = [
        json.loads(line[6:])
        for line in rewritten.splitlines()
        if line.startswith(b"data: ")
    ]
    expected = 'return await tools.exec_command({"cmd":"pwd"});'
    assert payloads[1]["delta"] == ""
    assert payloads[2]["input"] == expected
    assert payloads[3]["item"]["input"] == expected


def test_truncate_input_never_leaves_orphaned_tool_outputs() -> None:
    # 回归场景：context truncation 丢弃了 function_call，却保留了对应的
    # function_call_output，导致已保留 call 后面出现孤立的 tool output。
    # 上游（OpenCode Go）会以 "No tool call found for tool
    # output" 拒绝此输入。旧的 boundary trim 只移除尾部边界的 output，
    # 会漏掉 interleaved tool round 中位于已保留 call 后面的孤立 output。
    request = {
        "model": "opencode-go/deepseek-flash",
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
    fixed = proxy._json_tokens({"model": "opencode-go/deepseek-flash", "input": []})
    # Budget 恰好容纳 head + items[3..]；丢弃 [u1, call_A]，保留 call_B
    # 且尾部仍残留孤立的 call_A output。
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


def test_control_plane_paths_route_separately_from_model_requests() -> None:
    model_backend = "http://127.0.0.1:4100"
    control_plane_backend = "https://chatgpt.example/backend-api/codex"

    assert proxy.is_control_plane_path("/v1/alpha/search")
    assert proxy.is_control_plane_path("/v1/alpha")
    assert not proxy.is_control_plane_path("/v1/alpha2/search")

    assert (
        proxy.select_upstream_backend(
            "/v1/alpha/search",
            model_backend,
            control_plane_backend,
        )
        == control_plane_backend
    )
    assert (
        proxy.select_upstream_backend(
            "/v1/responses",
            model_backend,
            control_plane_backend,
        )
        == model_backend
    )
    assert (
        proxy.select_upstream_backend(
            "/v1/models",
            model_backend,
            control_plane_backend,
        )
        == model_backend
    )
    assert (
        proxy.build_upstream_url(
            control_plane_backend,
            "/v1/alpha/search",
            "q=1",
            strip_v1_prefix=True,
        )
        == "https://chatgpt.example/backend-api/codex/alpha/search?q=1"
    )
    assert (
        proxy.build_upstream_url(model_backend, "/v1/responses")
        == "http://127.0.0.1:4100/v1/responses"
    )


def test_responses_proxy_enforces_protocol_boundary_and_filters_models() -> None:
    assert proxy.is_allowed_path("/v1/responses", "POST")
    assert not proxy.is_allowed_path("/v1/chat/completions", "POST")
    assert proxy.is_allowed_path("/v1/models", "GET")
    assert proxy.is_allowed_path("/health/liveliness", "GET")
    assert proxy.is_allowed_path("/v1/alpha/search", "GET")

    upstream = json.dumps(
        {
            "object": "list",
            "data": [
                {"id": "responses-model-a", "object": "model"},
                {"id": "chat-model-a", "object": "model"},
            ],
        }
    ).encode()
    filtered = json.loads(
        proxy.filter_models_response(upstream, {"responses-model-a", "chat-model-a"})
    )
    assert [item["id"] for item in filtered["data"]] == [
        "responses-model-a",
        "chat-model-a",
    ]


def test_compatibility_features_are_shape_driven() -> None:
    plain = {"model": "responses-model-a", "input": [{"type": "message"}]}
    private = {
        "model": "responses-model-a",
        "input": [{"type": "agent_message", "content": []}],
    }
    collaboration = {
        "model": "responses-model-a",
        "tools": [{"type": "namespace", "name": "collaboration", "tools": []}],
    }
    additional = {
        "model": "responses-model-a",
        "input": [{"type": "additional_tools", "tools": []}],
    }

    assert not proxy.has_private_agent_items(plain)
    assert proxy.has_private_agent_items(private)
    assert proxy.has_collaboration_items(collaboration)
    assert proxy.has_additional_tools(additional)
    assert not proxy.has_additional_tools(plain)


def test_opencode_session_is_injected_without_touching_gpt_requests() -> None:
    muse = {
        "model": "opencode-go/deepseek-flash",
        "input": [],
        "extra_headers": {"x-client-header": "keep"},
    }
    body, session = proxy.ensure_opencode_session(
        json.dumps(muse).encode(),
        {"x-opencode-session": "provider-session-1"},
    )
    normalized = json.loads(body)
    assert session == "provider-session-1"
    assert normalized["extra_headers"] == {
        "x-client-header": "keep",
        "x-opencode-session": "provider-session-1",
    }

    codex_metadata = {"threadId": "thread-1", "turnId": "turn-1"}
    first_body, first_session = proxy.ensure_opencode_session(
        json.dumps({"model": "opencode-go/deepseek-flash"}).encode(),
        {"x-codex-turn-metadata": json.dumps(codex_metadata)},
    )
    codex_metadata["turnId"] = "turn-2"
    _, second_session = proxy.ensure_opencode_session(
        json.dumps({"model": "opencode-go/deepseek-flash"}).encode(),
        {"x-codex-turn-metadata": json.dumps(codex_metadata)},
    )
    assert json.loads(first_body)["extra_headers"]["x-opencode-session"] == first_session
    assert first_session == second_session

    gpt = {"model": "gpt-6-luna", "input": []}
    unchanged, gpt_session = proxy.ensure_opencode_session(
        json.dumps(gpt).encode(),
        {"x-opencode-session": "must-not-be-added"},
    )
    assert json.loads(unchanged) == gpt
    assert gpt_session is None



def test_set_opencode_session_replaces_embedded_epoch_without_touching_gpt() -> None:
    muse = {
        "model": "opencode-go/deepseek-flash",
        "extra_headers": {
            "X-OpenCode-Session": "old-session",
            "x-client-header": "keep",
        },
    }
    rotated = json.loads(proxy.set_opencode_session(json.dumps(muse).encode(), "new-session"))
    assert rotated["extra_headers"] == {
        "x-client-header": "keep",
        "x-opencode-session": "new-session",
    }

    gpt = {"model": "gpt-6-luna", "extra_headers": {"x-client": "keep"}}
    assert proxy.set_opencode_session(json.dumps(gpt).encode(), "must-not-apply") == json.dumps(
        gpt
    ).encode()


def test_replay_safe_retry_scope_is_narrow() -> None:
    assert proxy._replay_safe_responses_request(
        method="POST",
        path=proxy.RESPONSES_PATH,
        caller_stream=False,
        request_obj={"store": False},
        using_control_plane=False,
    )
    assert not proxy._replay_safe_responses_request(
        method="POST",
        path=proxy.RESPONSES_PATH,
        caller_stream=True,
        request_obj={"store": False},
        using_control_plane=False,
    )
    assert not proxy._replay_safe_responses_request(
        method="POST",
        path=proxy.RESPONSES_PATH,
        caller_stream=False,
        request_obj={"store": True},
        using_control_plane=False,
    )
    assert not proxy._replay_safe_responses_request(
        method="GET",
        path=proxy.MODELS_PATH,
        caller_stream=False,
        request_obj={"store": False},
        using_control_plane=False,
    )
    assert not proxy._replay_safe_responses_request(
        method="POST",
        path=proxy.RESPONSES_PATH,
        caller_stream=False,
        request_obj={"store": False},
        using_control_plane=True,
    )


def test_core_transient_upstream_error_requires_explicit_marker() -> None:
    transient = json.dumps(
        {"error": {"type": "upstream_error", "message": "provider request failed"}}
    ).encode()
    assert proxy._core_transient_upstream_error(502, transient)
    assert not proxy._core_transient_upstream_error(503, transient)
    assert not proxy._core_transient_upstream_error(
        502,
        json.dumps({"error": {"type": "configuration_error"}}).encode(),
    )
    assert not proxy._core_transient_upstream_error(502, b"bad gateway")


def test_nonstream_store_false_retries_explicit_core_upstream_error() -> None:
    attempts = 0

    async def exercise() -> None:
        nonlocal attempts

        async def backend_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return aiohttp.web.json_response(
                    {
                        "error": {
                            "type": "upstream_error",
                            "message": "provider request failed",
                        }
                    },
                    status=502,
                )
            completed = {
                "type": "response.completed",
                "response": {
                    "id": "resp_retry_test",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            }
            wire = b"data: " + json.dumps(completed).encode() + b"\n\n"
            return aiohttp.web.Response(
                status=200,
                body=wire,
                content_type="text/event-stream",
            )

        backend_app = aiohttp.web.Application()
        backend_app.router.add_route("*", "/{tail:.*}", backend_handler)
        backend_server = TestServer(backend_app)
        await backend_server.start_server()
        backend_url = str(backend_server.make_url("/")).rstrip("/")

        session = aiohttp.ClientSession()
        proxy_app = aiohttp.web.Application()

        async def proxy_handler(request: aiohttp.web.Request):
            return await proxy.handle(
                request,
                backend_url,
                session,
                None,
                {"gpt-6-luna"},
            )

        proxy_app.router.add_route("*", "/{tail:.*}", proxy_handler)
        client = TestClient(TestServer(proxy_app))
        await client.start_server()
        try:
            response = await client.post(
                proxy.RESPONSES_PATH,
                json={
                    "model": "gpt-6-luna",
                    "input": "hello",
                    "stream": False,
                    "store": False,
                },
            )
            assert response.status == 200
            body = await response.json()
            assert body["id"] == "resp_retry_test"
        finally:
            await client.close()
            await session.close()
            await backend_server.close()

    original_delay = proxy._backend_retry_delay
    proxy._backend_retry_delay = lambda attempt: 0.0
    try:
        asyncio.run(exercise())
    finally:
        proxy._backend_retry_delay = original_delay

    assert attempts == 2


def test_nonstream_store_true_does_not_retry_core_upstream_error() -> None:
    attempts = 0

    async def exercise() -> None:
        nonlocal attempts

        async def backend_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
            nonlocal attempts
            attempts += 1
            return aiohttp.web.json_response(
                {
                    "error": {
                        "type": "upstream_error",
                        "message": "provider request failed",
                    }
                },
                status=502,
            )

        backend_app = aiohttp.web.Application()
        backend_app.router.add_route("*", "/{tail:.*}", backend_handler)
        backend_server = TestServer(backend_app)
        await backend_server.start_server()
        backend_url = str(backend_server.make_url("/")).rstrip("/")

        session = aiohttp.ClientSession()
        proxy_app = aiohttp.web.Application()

        async def proxy_handler(request: aiohttp.web.Request):
            return await proxy.handle(
                request,
                backend_url,
                session,
                None,
                {"gpt-6-luna"},
            )

        proxy_app.router.add_route("*", "/{tail:.*}", proxy_handler)
        client = TestClient(TestServer(proxy_app))
        await client.start_server()
        try:
            response = await client.post(
                proxy.RESPONSES_PATH,
                json={
                    "model": "gpt-6-luna",
                    "input": "hello",
                    "stream": False,
                    "store": True,
                },
            )
            assert response.status == 502
        finally:
            await client.close()
            await session.close()
            await backend_server.close()

    asyncio.run(exercise())
    assert attempts == 1
