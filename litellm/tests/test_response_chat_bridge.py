import importlib.util
import asyncio
import json
from pathlib import Path

from aiohttp import ClientSession, web


BRIDGE_PATH = Path(__file__).parents[2] / "tools" / "response_chat_bridge.py"
SPEC = importlib.util.spec_from_file_location("response_chat_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)

PROXY_PATH = Path(__file__).parents[2] / "tools" / "responses-proxy.py"
PROXY_SPEC = importlib.util.spec_from_file_location("responses_proxy_bridge_test", PROXY_PATH)
assert PROXY_SPEC and PROXY_SPEC.loader
proxy = importlib.util.module_from_spec(PROXY_SPEC)
PROXY_SPEC.loader.exec_module(proxy)


def _events(wire: bytes) -> list[dict]:
    events = []
    for frame in wire.decode().split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                events.append(json.loads(line[6:]))
    return events


def test_responses_request_is_converted_to_chat_with_tools_and_history() -> None:
    request = {
        "model": "opencode-go/omen-alpha",
        "instructions": "Use the available tools.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "check"}]},
            {
                "type": "function_call",
                "namespace": "shell",
                "name": "run",
                "call_id": "call_1",
                "arguments": {"command": "dir"},
            },
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        ],
        "tools": [
            {
                "type": "namespace",
                "name": "shell",
                "tools": [
                    {
                        "type": "function",
                        "name": "run",
                        "description": "Run a command.",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ],
        "tool_choice": "required",
        "max_output_tokens": 123,
        "stream": False,
    }

    converted, name_map = bridge.responses_to_chat_request(request)

    assert converted["model"] == request["model"]
    assert converted["messages"][0] == {
        "role": "system",
        "content": "Use the available tools.",
    }
    assert converted["messages"][1]["role"] == "user"
    assert converted["messages"][2]["tool_calls"][0]["function"] == {
        "name": "shell__run",
        "arguments": '{"command":"dir"}',
    }
    assert converted["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "ok",
    }
    assert converted["tools"][0]["function"]["name"] == "shell__run"
    assert converted["tool_choice"] == "auto"
    assert converted["max_tokens"] == 123
    assert converted["stream"] is False
    assert name_map == {"shell__run": ("shell", "run")}


def test_union_reasoning_effort_is_converted_to_anthropic_thinking() -> None:
    converted, _ = bridge.responses_to_chat_request(
        {
            "model": "opencode-go/union-alpha-free",
            "input": "check",
            "reasoning": {"effort": "high"},
        }
    )

    assert converted["thinking"] == {"type": "enabled", "budget_tokens": 4096}

    omen_converted, _ = bridge.responses_to_chat_request(
        {
            "model": "opencode-go/omen-alpha",
            "input": "check",
            "reasoning": {"effort": "high"},
        }
    )
    assert "thinking" not in omen_converted


def test_chat_response_is_converted_to_responses_function_call() -> None:
    response = {
        "id": "chatcmpl_123",
        "created": 1700000000,
        "model": "opencode-go/omen-alpha",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "shell__run",
                                "arguments": '{"command":"dir"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }

    converted = bridge.chat_response_to_responses(
        response,
        response_model="opencode-go/omen-alpha",
        tool_name_map={"shell__run": ("shell", "run")},
    )

    assert converted["id"] == "resp_chatcmpl_123"
    assert converted["model"] == "opencode-go/omen-alpha"
    assert converted["output"] == [
        {
            "id": "call_2",
            "type": "function_call",
            "call_id": "call_2",
            "name": "run",
            "arguments": '{"command":"dir"}',
            "namespace": "shell",
        }
    ]
    assert converted["usage"]["input_tokens"] == 10
    assert converted["usage"]["output_tokens"] == 4


def test_chat_response_without_usage_emits_valid_responses_usage() -> None:
    converted = bridge.chat_response_to_responses(
        {
            "id": "chatcmpl_no_usage",
            "model": "opencode-go/omen-alpha",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "done"},
                }
            ],
        },
        response_model="opencode-go/omen-alpha",
    )

    assert converted["usage"] == {
        "input_tokens": 0,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 0,
    }


def test_chat_stream_bridge_preserves_text_and_tool_call_events() -> None:
    bridge_instance = bridge.ChatStreamBridge(
        response_model="opencode-go/omen-alpha",
        tool_name_map={"shell__run": ("shell", "run")},
    )
    stream = b"".join(
        [
            b'data: {"id":"chatcmpl_stream","created":1700000001,"model":"omen-alpha","choices":[{"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n',
            b'data: {"id":"chatcmpl_stream","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_3","function":{"name":"shell__run","arguments":"{\\"command\\":\\"dir\\"}"}}]},"finish_reason":null}]}\n\n',
            b'data: [DONE]\n\n',
        ]
    )

    wire = bridge_instance.feed(stream, final=True)
    events = _events(wire)
    event_types = [event["type"] for event in events]

    assert "response.output_text.delta" in event_types
    assert "response.function_call_arguments.delta" in event_types
    completed = next(event for event in events if event["type"] == "response.completed")
    assert completed["response"]["usage"]["input_tokens"] == 0
    assert completed["response"]["usage"]["output_tokens"] == 0
    assert completed["response"]["usage"]["total_tokens"] == 0
    output = completed["response"]["output"]
    assert output[0]["type"] == "message"
    assert output[0]["content"][0]["text"] == "hi"
    assert output[1]["type"] == "function_call"
    assert output[1]["namespace"] == "shell"
    assert output[1]["name"] == "run"


def test_unified_ingress_routes_responses_and_chat_to_chat_hop() -> None:
    async def run() -> None:
        upstream_requests: list[tuple[str, dict, str | None]] = []

        async def upstream_handler(request: web.Request) -> web.Response:
            body = await request.json()
            upstream_requests.append((request.path, body, request.headers.get("x-opencode-session")))
            if request.path == "/v1/chat/completions":
                return web.json_response(
                    {
                        "id": "chatcmpl_route",
                        "created": 1700000002,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "routed"},
                            }
                        ],
                    }
                )
            return web.json_response({"object": "list", "data": []})

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]
        upstream_url = f"http://127.0.0.1:{upstream_port}"

        proxy_session = ClientSession()
        proxy_app = web.Application()
        proxy_app.router.add_route(
            "*",
            "/{tail:.*}",
            lambda request: proxy.handle(
                request,
                upstream_url,
                proxy_session,
                None,
                {"responses-model"},
                {"chat-model", "opencode-go/union-alpha-free"},
                upstream_url,
            ),
        )
        proxy_runner = web.AppRunner(proxy_app)
        await proxy_runner.setup()
        proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
        await proxy_site.start()
        proxy_port = proxy_site._server.sockets[0].getsockname()[1]

        try:
            async with ClientSession() as client:
                response_request = {
                    "model": "chat-model",
                    "input": [{"role": "user", "content": "hello"}],
                    "stream": False,
                }
                response = await client.post(
                    f"http://127.0.0.1:{proxy_port}/v1/responses",
                    json=response_request,
                )
                assert response.status == 200
                response_body = await response.json()
                assert response_body["output"][0]["content"][0]["text"] == "routed"

                direct_chat = await client.post(
                    f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                    json={
                        "model": "chat-model",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )
                assert direct_chat.status == 200
                assert (await direct_chat.json())["choices"][0]["message"]["content"] == "routed"

                union_response = await client.post(
                    f"http://127.0.0.1:{proxy_port}/v1/responses",
                    headers={
                        "x-codex-turn-metadata": json.dumps(
                            {"threadId": "union-integration-thread", "turnId": "turn-1"}
                        )
                    },
                    json={
                        "model": "opencode-go/union-alpha-free",
                        "input": "hello",
                        "reasoning": {"effort": "high"},
                        "stream": False,
                    },
                )
                assert union_response.status == 200
                assert (await union_response.json())["output"][0]["content"][0]["text"] == "routed"

            assert [path for path, _, _ in upstream_requests] == [
                "/v1/chat/completions",
                "/v1/chat/completions",
                "/v1/chat/completions",
            ]
            assert upstream_requests[0][1]["messages"] == [
                {"role": "user", "content": "hello"}
            ]
            assert upstream_requests[1][1]["messages"] == [
                {"role": "user", "content": "hello"}
            ]
            assert upstream_requests[2][1]["thinking"] == {
                "type": "enabled",
                "budget_tokens": 4096,
            }
            assert "reasoning_effort" not in upstream_requests[2][1]
            assert upstream_requests[2][2].startswith("chat-")
        finally:
            await proxy_session.close()
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_unified_ingress_sanitizes_tools_lifted_from_additional_tools() -> None:
    async def run() -> None:
        upstream_requests: list[dict] = []

        async def upstream_handler(request: web.Request) -> web.Response:
            upstream_requests.append(await request.json())
            return web.json_response(
                {
                    "id": "resp_tool_description",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            )

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream_handler)
        upstream_runner = web.AppRunner(upstream_app)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        upstream_port = upstream_site._server.sockets[0].getsockname()[1]
        upstream_url = f"http://127.0.0.1:{upstream_port}"

        proxy_session = ClientSession()
        proxy_app = web.Application()
        proxy_app.router.add_route(
            "*",
            "/{tail:.*}",
            lambda request: proxy.handle(
                request,
                upstream_url,
                proxy_session,
                None,
                {"opencode-go/muse-spark-1.3-contributor"},
                set(),
                upstream_url,
            ),
        )
        proxy_runner = web.AppRunner(proxy_app)
        await proxy_runner.setup()
        proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
        await proxy_site.start()
        proxy_port = proxy_site._server.sockets[0].getsockname()[1]

        try:
            async with ClientSession() as client:
                response = await client.post(
                    f"http://127.0.0.1:{proxy_port}/v1/responses",
                    json={
                        "model": "opencode-go/muse-spark-1.3-contributor",
                        "input": [
                            {
                                "type": "additional_tools",
                                "role": "developer",
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "empty_description",
                                        "description": "",
                                        "parameters": {"type": "object", "properties": {}},
                                    }
                                ],
                            }
                        ],
                    },
                )
                assert response.status == 200
                await response.read()

            assert len(upstream_requests) == 1
            forwarded = upstream_requests[0]
            assert forwarded["input"] == []
            assert forwarded["tools"][0]["description"] == "empty_description tool"
        finally:
            await proxy_session.close()
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())
