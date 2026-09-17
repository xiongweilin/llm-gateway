import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from aiohttp import ClientSession, web


TOOLS_PATH = Path(__file__).parents[2] / "tools"
if str(TOOLS_PATH) not in sys.path:
    sys.path.insert(0, str(TOOLS_PATH))

BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "union_anthropic_bridge_test", TOOLS_PATH / "union_anthropic_bridge.py"
)
assert BRIDGE_SPEC and BRIDGE_SPEC.loader
union = importlib.util.module_from_spec(BRIDGE_SPEC)
sys.modules[BRIDGE_SPEC.name] = union
BRIDGE_SPEC.loader.exec_module(union)

RESPONSE_PROXY_SPEC = importlib.util.spec_from_file_location(
    "responses_proxy_union_test", TOOLS_PATH / "responses-proxy.py"
)
assert RESPONSE_PROXY_SPEC and RESPONSE_PROXY_SPEC.loader
proxy = importlib.util.module_from_spec(RESPONSE_PROXY_SPEC)
RESPONSE_PROXY_SPEC.loader.exec_module(proxy)


def _events(wire: bytes) -> list[dict]:
    events = []
    for frame in wire.decode().split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _anthropic_stream() -> bytes:
    frames = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_union_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "union-alpha",
                    "content": [],
                    "usage": {"input_tokens": 7, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "reason"},
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "done"},
            },
        ),
        (
            "content_block_stop",
            {"type": "content_block_stop", "index": 1},
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
        for event, data in frames
    )


def test_native_messages_request_preserves_union_thinking_and_tools() -> None:
    chat_request, tool_name_map = proxy.responses_to_chat_request(
        {
            "model": "opencode-go/union-alpha-free",
            "instructions": "Use tools.",
            "input": [
                {
                    "type": "function_call",
                    "namespace": "functions",
                    "name": "exec",
                    "call_id": "call.exec:1",
                    "arguments": {"code": "return 1;"},
                },
                {
                    "type": "function_call_output",
                    "call_id": "call.exec:1",
                    "output": "ok",
                },
            ],
            "tools": [
                {
                    "type": "namespace",
                    "name": "functions",
                    "tools": [
                        {
                            "type": "function",
                            "name": "exec",
                            "description": "Run code.",
                            "parameters": {
                                "type": "object",
                                "properties": {"code": {"type": "string"}},
                            },
                        }
                    ],
                }
            ],
            "reasoning": {"effort": "max"},
            "stream": True,
        }
    )
    native, tool_ids = union.build_anthropic_request(
        chat_request,
        union.UnionAnthropicRoute(
            api_base="https://example.invalid/v1/messages",
            model="union-alpha",
            api_key_env="TEST_UNION_KEY",
        ),
    )

    assert native["model"] == "union-alpha"
    assert native["system"] == [{"type": "text", "text": "Use tools."}]
    assert native["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert native["max_tokens"] == 32768
    assert native["tools"][0]["name"] == "functions_run_code"
    assert native["tools"][0]["input_schema"]["properties"]["code"]
    assert native["messages"][0]["role"] == "assistant"
    assert native["messages"][0]["content"][0]["type"] == "tool_use"
    safe_id = native["messages"][0]["content"][0]["id"]
    assert safe_id == tool_ids["call.exec:1"]
    assert native["messages"][1]["content"][0]["type"] == "tool_result"
    assert native["messages"][1]["content"][0]["tool_use_id"] == safe_id
    assert tool_name_map == {"functions_run_code": ("functions", "exec")}


def test_anthropic_stream_bridge_emits_responses_lifecycle_and_output() -> None:
    bridge = union.AnthropicStreamBridge(
        "opencode-go/union-alpha-free",
        {"functions_run_code": ("functions", "exec")},
    )
    source = _anthropic_stream()
    wire = b"".join(
        bridge.feed(source[index : index + 7])
        for index in range(0, len(source), 7)
    ) + bridge.feed(b"", final=True)
    events = _events(wire)
    event_types = [event["type"] for event in events]

    assert event_types[0:2] == ["response.created", "response.in_progress"]
    assert "response.reasoning_summary_text.delta" in event_types
    assert "response.output_text.delta" in event_types
    assert event_types[-1] == "response.completed"
    completed = events[-1]["response"]
    assert completed["output"][0]["type"] == "reasoning"
    assert completed["output"][0]["summary"][0]["text"] == "reason"
    assert completed["output"][1]["content"][0]["text"] == "done"
    assert completed["usage"]["input_tokens"] == 7
    assert completed["usage"]["output_tokens"] == 3


def test_union_failure_sse_starts_lifecycle_before_failed() -> None:
    events = _events(
        union._failure_sse(
            "opencode-go/union-alpha-free",
            "Union upstream request failed",
            response_id="resp_union_failed",
        )
    )
    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.failed",
    ]
    assert events[0]["response"]["status"] == "in_progress"
    assert events[-1]["response"]["status"] == "failed"


def test_union_responses_use_native_messages_and_retry_transient_provider_errors(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_UNION_KEY", "synthetic-union-key")

    async def run() -> None:
        upstream_attempts: list[dict] = []

        async def upstream_handler(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            upstream_attempts.append(
                {
                    "path": request.path,
                    "body": body,
                    "api_key": request.headers.get("x-api-key"),
                    "session": request.headers.get("x-opencode-session"),
                }
            )
            if len(upstream_attempts) < 3:
                return web.json_response({"error": {"type": "unavailable"}}, status=503)
            return web.Response(
                status=200,
                body=_anthropic_stream(),
                content_type="text/event-stream",
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
        route = union.UnionAnthropicRoute(
            api_base=f"{upstream_url}/v1/messages",
            model="union-alpha",
            api_key_env="TEST_UNION_KEY",
        )
        proxy_app.router.add_route(
            "*",
            "/{tail:.*}",
            lambda request: proxy.handle(
                request,
                upstream_url,
                proxy_session,
                None,
                set(),
                {"opencode-go/union-alpha-free"},
                upstream_url,
                route,
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
                    headers={
                        "x-codex-turn-metadata": json.dumps(
                            {"threadId": "native-union-test", "turnId": "turn-1"}
                        )
                    },
                    json={
                        "model": "opencode-go/union-alpha-free",
                        "input": "hello",
                        "reasoning": {"effort": "high"},
                        "stream": True,
                    },
                )
                assert response.status == 200
                events = _events(await response.read())
        finally:
            await proxy_session.close()
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

        assert len(upstream_attempts) == 3
        assert {item["path"] for item in upstream_attempts} == {"/v1/messages"}
        assert {item["api_key"] for item in upstream_attempts} == {"synthetic-union-key"}
        assert upstream_attempts[-1]["body"]["model"] == "union-alpha"
        assert upstream_attempts[-1]["body"]["thinking"] == {
            "type": "enabled",
            "budget_tokens": 4096,
        }
        assert upstream_attempts[-1]["body"]["stream"] is True
        event_types = [event["type"] for event in events]
        assert "response.output_text.delta" in event_types
        assert event_types[-1] == "response.completed"

    asyncio.run(run())
