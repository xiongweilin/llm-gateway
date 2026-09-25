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

    assert gateway.select_backend("/v1/chat/completions", core, responses) == core
    assert gateway.select_backend("/v1/models", core, responses) == core
    assert gateway.select_backend("/health/liveliness", core, responses) == core
    assert gateway.select_backend("/v1/responses", core, responses) == responses
    assert gateway.select_backend("/v1/alpha/search", core, responses) == responses
    assert gateway.select_backend("/v1/alpha2/search", core, responses) is None
    assert gateway.select_backend("/unsupported", core, responses) is None


def test_gateway_configuration_assigns_requested_ports() -> None:
    config = json.loads((ROOT / "config" / "gateway.json").read_text(encoding="utf-8"))

    assert config["ports"] == {"core": 4100, "agent": 4101, "responses": 4102}


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
