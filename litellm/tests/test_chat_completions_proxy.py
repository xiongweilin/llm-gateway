import importlib.util
import json
from pathlib import Path


PROXY_PATH = Path(__file__).parents[2] / "tools" / "chat-completions-proxy.py"
SPEC = importlib.util.spec_from_file_location("chat_completions_proxy", PROXY_PATH)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


def test_chat_proxy_adds_session_only_for_chat_deployment() -> None:
    omen = {"model": "omen-alpha", "messages": [{"role": "user", "content": "hi"}]}
    raw_omen = json.dumps(omen).encode()

    normalized, session = proxy.ensure_session(raw_omen, {})

    assert session
    assert json.loads(normalized)["extra_headers"]["x-opencode-session"] == session

    gpt = {"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "hi"}]}
    raw_gpt = json.dumps(gpt).encode()
    unchanged, no_session = proxy.ensure_session(raw_gpt, {})

    assert unchanged == raw_gpt
    assert no_session is None


def test_chat_proxy_preserves_explicit_session_header() -> None:
    request = {"model": "omen-alpha", "messages": []}
    raw = json.dumps(request).encode()

    normalized, session = proxy.ensure_session(
        raw,
        {"X-OpEnCoDe-SeSsIoN": "explicit-session"},
    )

    assert session == "explicit-session"
    assert json.loads(normalized)["extra_headers"]["x-opencode-session"] == "explicit-session"


def test_chat_proxy_does_not_treat_responses_model_as_chat() -> None:
    request = {
        "model": "muse-spark-1.3-contributor",
        "input": "hello",
    }
    raw = json.dumps(request).encode()

    normalized, session = proxy.ensure_session(raw, {})

    assert normalized == raw
    assert session is None


def test_chat_proxy_preserves_query_string() -> None:
    assert (
        proxy.build_upstream_url(
            "http://127.0.0.1:4101/",
            "/v1/chat/completions",
            "foo=bar",
        )
        == "http://127.0.0.1:4101/v1/chat/completions?foo=bar"
    )
