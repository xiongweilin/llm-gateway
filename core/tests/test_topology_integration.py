from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
import zstandard

from fake_provider import FakeProviderServer
from llm_gateway.config import ModelRoute
from llm_gateway.core_server import create_app as create_core_app


ROOT = Path(__file__).parents[2]
TOOLS = ROOT / "tools"
CORE_SRC = ROOT / "core" / "src"
for directory in (TOOLS, CORE_SRC):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_gateway = _load_script_module("topology_agent_gateway", "agent-gateway.py")
responses_service = _load_script_module("topology_responses_service", "responses-proxy.py")
chat_service = _load_script_module("topology_chat_service", "chat-completions-proxy.py")


def test_agent_routes_responses_and_chat_through_their_protocol_services(
    monkeypatch,
) -> None:
    provider = FakeProviderServer().start()
    provider.reset()
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "synthetic-test-token")
    routes = {
        "responses-model": ModelRoute(
            id="responses-model",
            mode="responses",
            upstream_model="upstream-responses-model",
            api_base=provider.api_base,
            api_key_env="FAKE_PROVIDER_API_KEY",
        ),
        "chat-model": ModelRoute(
            id="chat-model",
            mode="chat",
            upstream_model="upstream-chat-model",
            api_base=provider.api_base,
            api_key_env="FAKE_PROVIDER_API_KEY",
        ),
        "messages-model": ModelRoute(
            id="messages-model",
            mode="messages",
            upstream_model="upstream-messages-model",
            api_base=provider.base_url,
            api_key_env="FAKE_PROVIDER_API_KEY",
        ),
    }

    async def exercise() -> None:
        async with TestClient(TestServer(create_core_app(routes))) as core_client:
            core_url = str(core_client.make_url("/")).rstrip("/")
            response_session = ClientSession(trust_env=False)
            chat_session = ClientSession(trust_env=False)
            response_app = web.Application(client_max_size=128 * 1024 * 1024)
            chat_app = web.Application(client_max_size=128 * 1024 * 1024)

            async def response_handler(request: web.Request):
                return await responses_service.handle(
                    request,
                    core_url,
                    response_session,
                    None,
                    {"responses-model"},
                )

            async def chat_handler(request: web.Request):
                return await chat_service.handle(
                    request,
                    core_url,
                    chat_session,
                    {"chat-model"},
                )

            response_app.router.add_route("*", "/{tail:.*}", response_handler)
            chat_app.router.add_route("*", "/{tail:.*}", chat_handler)
            try:
                async with TestClient(TestServer(response_app)) as response_client:
                    responses_url = str(response_client.make_url("/")).rstrip("/")
                    async with TestClient(TestServer(chat_app)) as chat_client:
                        chat_url = str(chat_client.make_url("/")).rstrip("/")
                        agent_app = agent_gateway.create_app(
                            core_url,
                            responses_url,
                            chat_url,
                            {"chat-model"},
                            {"responses-model", "chat-model"},
                        )
                        async with TestClient(TestServer(agent_app)) as agent_client:
                            native = await agent_client.post(
                                "/v1/responses",
                                json={
                                    "model": "responses-model",
                                    "input": [{"role": "user", "content": "native"}],
                                },
                            )
                            assert native.status == 200
                            assert (await native.json())["model"] == "upstream-responses-model"

                            compressed_request = zstandard.ZstdCompressor().compress(
                                json.dumps(
                                    {
                                        "model": "responses-model",
                                        "input": [{"role": "user", "content": "compressed"}],
                                    }
                                ).encode("utf-8")
                            )
                            compressed = await agent_client.post(
                                "/v1/responses",
                                data=compressed_request,
                                headers={
                                    "Content-Type": "application/json",
                                    "Content-Encoding": "zstd",
                                },
                            )
                            assert compressed.status == 200
                            assert (await compressed.json())["model"] == "upstream-responses-model"

                            response_chat = await agent_client.post(
                                "/v1/responses",
                                json={
                                    "model": "chat-model",
                                    "input": [{"role": "user", "content": "not bridged"}],
                                },
                            )
                            assert response_chat.status == 404

                            direct_chat = await agent_client.post(
                                "/v1/chat/completions",
                                json={
                                    "model": "chat-model",
                                    "messages": [{"role": "user", "content": "chat"}],
                                },
                            )
                            assert direct_chat.status == 200
                            chat_body = await direct_chat.json()
                            assert chat_body["choices"][0]["message"]["content"] == "echo: chat"

                            agent_models = await agent_client.get("/v1/models")
                            assert {item["id"] for item in (await agent_models.json())["data"]} == {
                                "responses-model",
                                "chat-model",
                            }
                            messages_on_agent_entry = await agent_client.post(
                                "/v1/messages",
                                json={"model": "messages-model", "messages": []},
                            )
                            assert messages_on_agent_entry.status == 404

                        response_models = await response_client.get("/v1/models")
                        assert {item["id"] for item in (await response_models.json())["data"]} == {
                            "responses-model",
                        }
            finally:
                await response_session.close()
                await chat_session.close()

    try:
        asyncio.run(exercise())
        assert [record["path"] for record in provider.records()] == [
            "/v1/responses",
            "/v1/responses",
            "/v1/chat/completions",
        ]
    finally:
        provider.stop()
