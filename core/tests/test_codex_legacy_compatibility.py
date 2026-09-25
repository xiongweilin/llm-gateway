from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

TOOLS = Path(__file__).parents[2] / "tools"
CORE_SRC = Path(__file__).parents[1] / "src"
for directory in (TOOLS, CORE_SRC):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from llm_gateway.chatgpt_auth import ChatGPTCredentials
from llm_gateway.config import ModelRoute
import llm_gateway.core_server as core_server
from llm_gateway.core_server import create_app as create_core_app


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEGACY_CLIENT_HEADERS = {
    "accept",
    "authorization",
    "chatgpt-account-id",
    "content-type",
    "originator",
    "session_id",
    "user-agent",
}
EVENT_TYPES = [
    "response.created",
    "response.reasoning_summary_text.delta",
    "response.output_text.delta",
]
EVENT_GAP_SECONDS = 0.6

agent_gateway = _load_script_module("codex_agent_gateway", "agent-gateway.py")
responses_service = _load_script_module("codex_responses_service", "responses-proxy.py")


async def _collect_events(response: web.Response, started_at: float) -> tuple[list[str], float]:
    event_types: list[str] = []
    first_event_latency: float | None = None
    while line := await response.content.readline():
        if line.startswith(b"event: "):
            if first_event_latency is None:
                first_event_latency = time.monotonic() - started_at
            event_types.append(line.removeprefix(b"event: ").strip().decode())
    assert first_event_latency is not None
    return event_types, first_event_latency


def test_codex_legacy_and_new_chains_match_upstream_headers_and_first_sse_event(monkeypatch) -> None:
    provider_observations: list[dict] = []

    async def provider(request: web.Request) -> web.StreamResponse:
        received = {key.lower(): value for key, value in request.headers.items()}
        observation = {
            "headers": {
                key: value
                for key, value in received.items()
                if key not in {"host", "content-length", "connection"}
            },
            "codex_internal_headers": sorted(
                key for key in received if key.startswith("x-openai-internal-codex-")
            ),
            "event_write_times": [],
        }
        provider_observations.append(observation)
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)
        for index, event_type in enumerate(EVENT_TYPES):
            if index:
                await asyncio.sleep(EVENT_GAP_SECONDS)
            observation["event_write_times"].append(time.monotonic())
            data = json.dumps({"type": event_type, "delta": "x"})
            await response.write(f"event: {event_type}\ndata: {data}\n\n".encode())
        await response.write_eof()
        return response

    async def exercise() -> None:
        provider_app = web.Application()
        provider_app.router.add_post("/v1/responses", provider)
        async with TestClient(TestServer(provider_app)) as provider_client:
            provider_url = str(provider_client.make_url("/")).rstrip("/")
            routes = {
                "codex-model": ModelRoute(
                    id="codex-model",
                    mode="responses",
                    upstream_model="codex-model",
                    api_base=f"{provider_url}/v1",
                    authorization="chatgpt",
                )
            }

            async def fake_chatgpt_credentials(_session):
                return ChatGPTCredentials(
                    access_token="subscription-oauth-token",
                    account_id="subscription-account-id",
                )

            monkeypatch.setattr(
                core_server,
                "get_chatgpt_credentials",
                fake_chatgpt_credentials,
            )
            async with TestClient(TestServer(create_core_app(routes))) as core_client:
                core_url = str(core_client.make_url("/")).rstrip("/")
                responses_session = ClientSession(trust_env=False)
                responses_app = web.Application(client_max_size=128 * 1024 * 1024)

                async def handle_responses(request: web.Request):
                    return await responses_service.handle(
                        request,
                        core_url,
                        responses_session,
                        None,
                        {"codex-model"},
                    )

                responses_app.router.add_route("*", "/{tail:.*}", handle_responses)
                try:
                    async with TestClient(TestServer(responses_app)) as responses_client:
                        responses_url = str(responses_client.make_url("/")).rstrip("/")
                        agent_app = agent_gateway.create_app(
                            core_url,
                            responses_url,
                            "http://127.0.0.1:9",
                        )
                        async with TestClient(TestServer(agent_app)) as agent_client:
                            request_headers = {
                                "Authorization": "Bearer synthetic-session",
                                "Content-Type": "application/json",
                                "Accept": "text/event-stream",
                                "Originator": "codex_exec_cli_rs",
                                "User-Agent": "codex-compatibility-test/1.0",
                                "session_id": "synthetic-session-id",
                                "ChatGPT-Account-Id": "synthetic-account-id",
                                "x-openai-internal-codex-responses-lite": "synthetic-private-value",
                                "x-codex-test-header": "not-forwarded",
                            }
                            request_body = {
                                "model": "codex-model",
                                "input": [{"role": "user", "content": "same request"}],
                                "stream": True,
                            }
                            legacy_headers = {
                                key: value
                                for key, value in request_headers.items()
                                if key.lower() in LEGACY_CLIENT_HEADERS
                                and key.lower() not in {"authorization", "chatgpt-account-id"}
                            }
                            legacy_headers["Authorization"] = "Bearer subscription-oauth-token"
                            legacy_headers["ChatGPT-Account-Id"] = "subscription-account-id"
                            legacy_headers["Accept-Encoding"] = "identity"

                            async with ClientSession(trust_env=False) as legacy_session:
                                legacy_started = time.monotonic()
                                async with legacy_session.post(
                                    f"{provider_url}/v1/responses",
                                    json=request_body,
                                    headers=legacy_headers,
                                ) as legacy_response:
                                    assert legacy_response.status == 200
                                    legacy_events, legacy_first_latency = await _collect_events(
                                        legacy_response,
                                        legacy_started,
                                    )

                            new_started = time.monotonic()
                            async with agent_client.post(
                                "/v1/responses",
                                json=request_body,
                                headers=request_headers,
                            ) as new_response:
                                assert new_response.status == 200
                                new_events, new_first_latency = await _collect_events(
                                    new_response,
                                    new_started,
                                )
                finally:
                    await responses_session.close()

        assert len(provider_observations) == 2
        expected_headers = {
            key.lower(): value
            for key, value in request_headers.items()
            if key.lower() in LEGACY_CLIENT_HEADERS
            and key.lower() not in {"authorization", "chatgpt-account-id"}
        }
        expected_headers["authorization"] = "Bearer subscription-oauth-token"
        expected_headers["chatgpt-account-id"] = "subscription-account-id"
        expected_headers["accept-encoding"] = "identity"
        for observation in provider_observations:
            assert observation["headers"] == expected_headers
            assert observation["codex_internal_headers"] == []
            assert len(observation["event_write_times"]) == len(EVENT_TYPES)

        assert legacy_events == EVENT_TYPES
        assert new_events == legacy_events
        assert new_events[0] == "response.created"
        assert new_first_latency < EVENT_GAP_SECONDS
        assert abs(new_first_latency - legacy_first_latency) < 0.25

    asyncio.run(exercise())
