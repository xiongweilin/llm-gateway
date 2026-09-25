import asyncio
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[2]
ENTRY_PATH = ROOT / "tools" / "agent-gateway.py"
SPEC = importlib.util.spec_from_file_location("agent_gateway", ENTRY_PATH)
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


def test_agent_entry_routes_each_role_to_the_configured_service() -> None:
    core = "http://127.0.0.1:port-core"
    responses = "http://127.0.0.1:port-responses"
    chat = "http://127.0.0.1:port-chat"

    assert gateway.select_backend("/v1/chat/completions", core, responses, chat) == chat
    assert gateway.select_backend("/v1/models", core, responses, chat) == core
    assert gateway.select_backend("/health/liveliness", core, responses, chat) == core
    assert gateway.select_backend("/v1/responses", core, responses, chat) == responses
    assert gateway.select_backend("/v1/alpha/search", core, responses, chat) == responses
    assert gateway.select_backend("/v1/alpha2/search", core, responses, chat) is None
    assert gateway.select_backend("/unsupported", core, responses, chat) is None


def test_agent_entry_rejects_responses_websocket_upgrade_for_http_fallback() -> None:
    from aiohttp.test_utils import make_mocked_request

    request = make_mocked_request(
        "GET",
        "/v1/responses",
        headers={
            "Connection": "Upgrade",
            "Upgrade": "websocket",
        },
    )
    response = asyncio.run(gateway.handle(request))
    assert response.status == 426
    assert response.headers["Upgrade"] == "websocket"

    assert not gateway.is_responses_websocket_upgrade(
        "POST",
        "/v1/responses",
        {"Upgrade": "websocket"},
    )
    assert not gateway.is_responses_websocket_upgrade(
        "GET",
        "/v1/chat/completions",
        {"Upgrade": "websocket"},
    )


def test_gateway_configuration_assigns_requested_ports() -> None:
    config = json.loads((ROOT / "config" / "gateway.json").read_text(encoding="utf-8"))

    assert config["ports"] == {
        "core": 4100,
        "agent": 4101,
        "responses": 4102,
        "chat": 4103,
    }


def test_agent_entry_preserves_forwarded_headers_and_query() -> None:
    headers = gateway.forward_headers(
        {
            "Authorization": "Bearer client-token",
            "X-OpEnCoDe-SeSsIoN": "session-1",
            "Content-Encoding": "zstd",
            "Content-Length": "123",
            "Connection": "keep-alive",
        }
    )
    assert headers["Authorization"] == "Bearer client-token"
    assert headers["X-OpEnCoDe-SeSsIoN"] == "session-1"
    assert "Content-Encoding" not in headers
    assert "Content-Length" not in headers
    assert "Connection" not in headers
    assert headers["Accept-Encoding"] == "identity"
    assert (
        gateway.build_url("http://127.0.0.1:4100/", "/v1/models", "limit=2")
        == "http://127.0.0.1:4100/v1/models?limit=2"
    )
