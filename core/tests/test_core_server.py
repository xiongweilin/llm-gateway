from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from llm_gateway.config import ModelRoute, load_model_routes, protocol_models
from llm_gateway.core_server import build_upstream_headers, create_app
from fake_provider import FakeProviderServer



def test_production_codex_routes_use_client_auth_and_pinned_chatgpt_backend(monkeypatch) -> None:
    monkeypatch.setenv("CHATGPT_API_BASE", "https://api.openai.com/v1")
    routes = load_model_routes(Path(__file__).parents[1] / "models.yaml")

    for model_id in ("gpt-6-sol", "gpt-6-luna"):
        route = routes[model_id]
        assert route.authorization == "client"
        assert route.api_base == "https://chatgpt.com/backend-api/codex"


def test_core_forwards_responses_to_the_configured_provider(monkeypatch) -> None:
    provider = FakeProviderServer().start()
    monkeypatch.setenv("FAKE_PROVIDER_API_KEY", "synthetic-test-token")
    routes = {
        "public-model": ModelRoute(
            id="public-model",
            mode="responses",
            upstream_model="provider-model",
            api_base=provider.api_base,
            api_key_env="FAKE_PROVIDER_API_KEY",
        )
    }

    async def exercise() -> None:
        async with TestClient(TestServer(create_app(routes))) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": "public-model", "input": "hello"},
                headers={"Authorization": "Bearer client-token"},
            )
            assert response.status == 200
            value = await response.json()
            assert value["model"] == "provider-model"
            assert value["status"] == "completed"

            catalog = await client.get("/v1/models")
            assert [item["id"] for item in (await catalog.json())["data"]] == [
                "public-model"
            ]

    try:
        asyncio.run(exercise())
        records = provider.records()
        assert len(records) == 1
        assert records[0]["path"] == "/v1/responses"
        assert records[0]["body"]["model"] == "provider-model"
        assert records[0]["auth_header_present"] is True
        assert records[0]["auth_scheme"] == "Bearer"
    finally:
        provider.stop()


def test_codex_route_preserves_client_subscription_auth() -> None:
    route = ModelRoute(
        id="codex-model",
        mode="responses",
        upstream_model="provider-model",
        api_base="https://chatgpt.com/backend-api/codex",
        authorization="client",
    )
    request_headers = {
        "Authorization": "Bearer subscription-oauth-token",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Originator": "codex_exec_cli_rs",
        "User-Agent": "codex-test/1.0",
        "session_id": "synthetic-session-id",
        "ChatGPT-Account-Id": "subscription-account-id",
        "x-openai-internal-codex-responses-lite": "synthetic-internal-value",
        "x-codex-test-header": "not-forwarded",
        "x-forwarded-for": "192.0.2.1",
    }

    actual = build_upstream_headers(request_headers, route)

    assert actual == {
        "Authorization": "Bearer subscription-oauth-token",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Originator": "codex_exec_cli_rs",
        "User-Agent": "codex-test/1.0",
        "session_id": "synthetic-session-id",
        "ChatGPT-Account-Id": "subscription-account-id",
        "Accept-Encoding": "identity",
    }


def test_opencode_route_preserves_its_gateway_session_header_only() -> None:
    route = ModelRoute(
        id="opencode-model",
        mode="responses",
        upstream_model="provider-model",
        api_base="https://provider.example/v1",
        compatibility="opencode-go",
    )

    actual = build_upstream_headers(
        {
            "x-opencode-session": "synthetic-session",
            "x-openai-internal-codex-responses-lite": "synthetic-private-value",
        },
        route,
    )

    assert actual == {
        "x-opencode-session": "synthetic-session",
        "Accept-Encoding": "identity",
    }


def test_core_rejects_a_model_on_the_wrong_protocol() -> None:
    routes = {
        "responses-only": ModelRoute(
            id="responses-only",
            mode="responses",
            upstream_model="provider-model",
            api_base="https://provider.example/v1",
        )
    }

    async def exercise() -> None:
        async with TestClient(TestServer(create_app(routes))) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "responses-only", "messages": []},
            )
            assert response.status == 404
            assert (await response.json())["error"]["message"] == (
                "model is unavailable for this protocol"
            )

    asyncio.run(exercise())


def test_core_reports_missing_provider_credential_without_exposing_it() -> None:
    route = ModelRoute(
        id="secured-model",
        mode="responses",
        upstream_model="provider-model",
        api_base="https://provider.example/v1",
        api_key_env="MISSING_GATEWAY_TEST_KEY",
    )
    routes = {route.id: route}

    async def exercise() -> None:
        async with TestClient(TestServer(create_app(routes))) as client:
            response = await client.post(
                "/v1/responses",
                json={"model": route.id, "input": "hello"},
            )
            assert response.status == 503
            body = await response.text()
            assert "MISSING_GATEWAY_TEST_KEY" in body
            assert "provider-token" not in body

    asyncio.run(exercise())


def test_chatgpt_authorization_rejects_openai_api_host(tmp_path) -> None:
    config = tmp_path / "models.yaml"
    config.write_text(
        """
models:
  - id: codex-model
    mode: responses
    upstream_model: codex-model
    api_base: https://api.openai.com/v1
    authorization: chatgpt
""".lstrip(),
        encoding="utf-8",
    )

    try:
        load_model_routes(config)
    except ValueError as exc:
        assert "must target chatgpt.com" in str(exc)
    else:
        raise AssertionError("ChatGPT subscription route accepted api.openai.com")


def test_model_configuration_loads_protocol_roles_and_env_overrides(tmp_path, monkeypatch) -> None:
    config = tmp_path / "models.yaml"
    config.write_text(
        """
models:
  - id: response-model
    mode: responses
    upstream_model: remote-response-model
    api_base: https://default.example/v1
    api_base_env: TEST_PROVIDER_BASE
  - id: chat-model
    mode: chat
    upstream_model: remote-chat-model
    api_base: https://chat.example/v1
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_PROVIDER_BASE", "https://override.example/v1")

    routes = load_model_routes(config)
    responses, chat = protocol_models(routes)
    assert responses == {"response-model"}
    assert chat == {"chat-model"}
    assert routes["response-model"].api_base == "https://override.example/v1"
    assert routes["response-model"].authorization == "client"
