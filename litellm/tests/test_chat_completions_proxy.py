import importlib.util
import json
from pathlib import Path


PROXY_PATH = Path(__file__).parents[2] / "tools" / "chat-completions-proxy.py"
SPEC = importlib.util.spec_from_file_location("chat_completions_proxy", PROXY_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


def test_chat_proxy_preserves_explicit_headers_without_body_rewrite() -> None:
    headers = {"X-OpEnCoDe-SeSsIoN": "explicit-session"}
    forwarded = proxy._forward_headers(headers, 12)

    assert forwarded["X-OpEnCoDe-SeSsIoN"] == "explicit-session"
    assert forwarded["Content-Length"] == "12"


def test_chat_proxy_enforces_protocol_boundary_and_filters_models() -> None:
    assert proxy.is_allowed_path("/v1/chat/completions", "POST")
    assert proxy.is_allowed_path("/v1/models", "GET")
    assert proxy.is_allowed_path("/health/liveliness", "GET")
    assert not proxy.is_allowed_path("/v1/responses", "POST")
    assert not proxy.is_allowed_path("/v1/alpha/search", "GET")

    upstream = json.dumps(
        {
            "object": "list",
            "data": [
                {"id": "responses-model-a", "object": "model"},
                {"id": "chat-model-a", "object": "model"},
            ],
        }
    ).encode()
    filtered = json.loads(proxy.filter_models_response(upstream, {"chat-model-a"}))
    assert [item["id"] for item in filtered["data"]] == ["chat-model-a"]


def test_chat_proxy_preserves_query_string() -> None:
    assert (
        proxy.build_upstream_url(
            "http://127.0.0.1:4101/",
            "/v1/chat/completions",
            "foo=bar",
        )
        == "http://127.0.0.1:4101/v1/chat/completions?foo=bar"
    )
