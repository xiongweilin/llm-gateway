from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway.config import ModelRoute
from llm_gateway.core_server import create_app as create_core_app


ROOT = Path(__file__).parents[2]
TOOLS = ROOT / "tools"
for directory in (TOOLS, ROOT / "core" / "src"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def _load_messages_proxy():
    spec = importlib.util.spec_from_file_location(
        "anthropic_messages_proxy_test_module",
        TOOLS / "messages-proxy.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


messages_proxy = _load_messages_proxy()


def test_messages_service_forwards_native_requests_and_streams(monkeypatch) -> None:
    monkeypatch.setenv("KITOOL_TEST_API_KEY", "synthetic-provider-token")
    records: list[dict[str, object]] = []
    provider_app = web.Application()

    async def provider_handler(request: web.Request):
        body = await request.json()
        records.append(
            {
                "path": request.path,
                "model": body.get("model"),
                "api_key": request.headers.get("x-api-key"),
                "authorization": request.headers.get("authorization"),
                "version": request.headers.get("anthropic-version"),
            }
        )
        if body.get("stream"):
            return web.Response(
                body=b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\"}\n\n",
                content_type="text/event-stream",
            )
        return web.json_response(
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": body.get("model"),
                "content": [{"type": "text", "text": "accepted"}],
                "stop_reason": "end_turn",
            }
        )

    provider_app.router.add_route("*", "/{tail:.*}", provider_handler)

    async def exercise() -> None:
        async with TestClient(TestServer(provider_app)) as provider_client:
            api_base = str(provider_client.make_url("/")).rstrip("/")
            messages_route = ModelRoute(
                id="sonnet-5.5",
                mode="messages",
                upstream_model="upstream-sonnet-5.5",
                api_base=api_base,
                api_key_env="KITOOL_TEST_API_KEY",
            )
            other_route = ModelRoute(
                id="responses-model",
                mode="responses",
                upstream_model="upstream-responses-model",
                api_base=api_base,
            )
            routes = {messages_route.id: messages_route, other_route.id: other_route}
            async with TestClient(TestServer(create_core_app(routes))) as core_client:
                core_url = str(core_client.make_url("/")).rstrip("/")
                async with TestClient(
                    TestServer(messages_proxy.create_app(core_url, {messages_route.id}))
                ) as client:
                    health = await client.get("/health/liveliness")
                    assert health.status == 200

                    catalog = await client.get("/v1/models")
                    assert {
                        item["id"] for item in (await catalog.json())["data"]
                    } == {messages_route.id}

                    unavailable = await client.post(
                        "/v1/messages",
                        json={"model": "responses-model", "messages": []},
                    )
                    assert unavailable.status == 404

                    headers = {
                        "x-api-key": "client-supplied-key",
                        "Authorization": "Bearer client-token",
                        "anthropic-version": "2023-06-01",
                    }
                    non_streaming = await client.post(
                        "/v1/messages",
                        headers=headers,
                        json={
                            "model": messages_route.id,
                            "max_tokens": 32,
                            "messages": [{"role": "user", "content": "hello"}],
                        },
                    )
                    assert non_streaming.status == 200
                    assert (await non_streaming.json())["model"] == "upstream-sonnet-5.5"

                    streaming = await client.post(
                        "/v1/messages",
                        headers=headers,
                        json={
                            "model": messages_route.id,
                            "max_tokens": 32,
                            "stream": True,
                            "messages": [{"role": "user", "content": "hello"}],
                        },
                    )
                    assert streaming.status == 200
                    assert "text/event-stream" in streaming.headers["Content-Type"]
                    assert "content_block_delta" in await streaming.text()

                    token_count = await client.post(
                        "/v1/messages/count_tokens",
                        headers=headers,
                        json={"model": messages_route.id, "messages": []},
                    )
                    assert token_count.status == 200

        assert [record["path"] for record in records] == [
            "/v1/messages",
            "/v1/messages",
            "/v1/messages/count_tokens",
        ]
        assert all(record["api_key"] == "synthetic-provider-token" for record in records)
        assert all(record["authorization"] is None for record in records)
        assert all(record["version"] == "2023-06-01" for record in records)
        assert records[0]["model"] == "upstream-sonnet-5.5"

    asyncio.run(exercise())
