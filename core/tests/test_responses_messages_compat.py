from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


ROOT = Path(__file__).parents[2]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _load_agent_gateway():
    spec = importlib.util.spec_from_file_location("agent_gateway_bridge_test", TOOLS / "agent-gateway.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_gateway = _load_agent_gateway()
from responses_messages_compat import responses_request_to_messages  # noqa: E402


def test_responses_request_conversion_preserves_replayed_tool_turns() -> None:
    result = responses_request_to_messages(
        {
            "model": "sonnet-5.5",
            "instructions": "Use tools when appropriate.",
            "max_output_tokens": 80,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Run pwd."}]},
                {"type": "function_call", "call_id": "call-1", "name": "exec", "arguments": "{\"command\":\"pwd\"}"},
                {"type": "function_call_output", "call_id": "call-1", "output": "C:/work"},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec",
                    "description": "Run a command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                }
            ],
            "tool_choice": "auto",
        }
    )

    assert result["model"] == "sonnet-5.5"
    assert result["stream"] is True
    assert result["max_tokens"] == 80
    assert result["system"] == "Use tools when appropriate."
    assert result["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Run pwd."}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "call-1", "name": "exec", "input": {"command": "pwd"}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "C:/work"}]},
    ]
    assert result["tools"][0]["input_schema"]["properties"]["command"]["type"] == "string"
    assert result["tool_choice"] == {"type": "auto"}


def test_agent_entry_routes_all_protocols_and_converts_codex_responses() -> None:
    received: list[dict[str, object]] = []
    app = web.Application()

    async def endpoint(request: web.Request):
        if request.method == "GET" and request.path == "/v1/models":
            return web.json_response(
                {"object": "list", "data": [{"id": "sonnet-5.5"}, {"id": "chat-model"}]}
            )
        if request.method == "GET" and request.path == "/health/liveliness":
            return web.json_response({"status": "ok"})
        body = await request.json()
        received.append({"path": request.path, "body": body})
        if request.path != "/v1/messages":
            return web.json_response({"error": {"type": "not_found", "message": "unexpected path"}}, status=404)
        if body.get("tools"):
            frames = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 8}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_test", "name": "exec", "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"command\":\"pwd\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            frames = [
                ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        payload = b"".join(
            f"event: {name}\ndata: {json.dumps(value)}\n\n".encode("utf-8")
            for name, value in frames
        )
        return web.Response(body=payload, content_type="text/event-stream")

    app.router.add_route("*", "/{tail:.*}", endpoint)

    async def exercise() -> None:
        async with TestClient(TestServer(app)) as backend_client:
            backend_url = str(backend_client.make_url("/")).rstrip("/")
            agent_app = agent_gateway.create_app(
                backend_url,
                "http://unused-responses",
                "http://unused-chat",
                {"chat-model"},
                {"sonnet-5.5", "chat-model"},
                backend_url,
                {"sonnet-5.5"},
            )
            async with TestClient(TestServer(agent_app)) as client:
                catalog = await client.get("/v1/models")
                assert {item["id"] for item in (await catalog.json())["data"]} == {
                    "sonnet-5.5",
                    "chat-model",
                }

                streamed = await client.post(
                    "/v1/responses",
                    json={
                        "model": "sonnet-5.5",
                        "instructions": "Answer briefly.",
                        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                        "max_output_tokens": 16,
                        "stream": True,
                    },
                )
                assert streamed.status == 200
                assert "text/event-stream" in streamed.headers["Content-Type"]
                stream_body = await streamed.text()
                assert "event: response.output_text.delta" in stream_body
                assert "event: response.completed" in stream_body

                tool = await client.post(
                    "/v1/responses",
                    json={
                        "model": "sonnet-5.5",
                        "input": "Run pwd.",
                        "max_output_tokens": 16,
                        "stream": True,
                        "tools": [
                            {
                                "type": "function",
                                "name": "exec",
                                "description": "Run a command",
                                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                            }
                        ],
                    },
                )
                assert tool.status == 200
                tool_body = await tool.text()
                assert "event: response.function_call_arguments.delta" in tool_body
                assert '"type":"function_call"' in tool_body
                assert "event: response.completed" in tool_body

                direct_messages = await client.post(
                    "/v1/messages",
                    json={"model": "sonnet-5.5", "max_tokens": 16, "messages": [{"role": "user", "content": "hello"}]},
                )
                assert direct_messages.status == 200
                assert "event: message_stop" in await direct_messages.text()

        assert [item["path"] for item in received] == [
            "/v1/messages",
            "/v1/messages",
            "/v1/messages",
        ]
        first_body = received[0]["body"]
        assert isinstance(first_body, dict)
        assert first_body["model"] == "sonnet-5.5"
        assert first_body["stream"] is True
        assert first_body["system"] == "Answer briefly."
        second_body = received[1]["body"]
        assert isinstance(second_body, dict)
        assert second_body["tools"][0]["name"] == "exec"

    asyncio.run(exercise())
