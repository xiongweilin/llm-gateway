"""Chat Completions protocol forwarder.

The listener on 127.0.0.1:4102 keeps the Chat Completions transport separate
from the Responses listener on 127.0.0.1:4100.  It forwards requests to the
LiteLLM listener on 127.0.0.1:4101 without changing the protocol or buffering
streaming responses.

The upstream OpenCode Go service requires an opaque session header for the
chat-only deployment.  When that header is absent, this proxy adds it through
LiteLLM's ``extra_headers`` request field and the outgoing HTTP header.  Other
models pass through unchanged.

No request or response body is logged, and the listener binds to loopback only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import uuid
from collections.abc import Mapping

import aiohttp
import aiohttp.web


DEFAULT_BACKEND = "http://127.0.0.1:4101"
SESSION_HEADER = "x-opencode-session"
CHAT_COMPLETIONS_PATH = "/chat/completions"
_PROCESS_SESSION = f"chat-{uuid.uuid4().hex}"


log = logging.getLogger("chat-completions-proxy")


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected and isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _find_stable_session_value(value) -> str | None:
    stable_keys = {
        "threadid",
        "thread_id",
        "conversationid",
        "conversation_id",
        "sessionid",
        "session_id",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in stable_keys and isinstance(child, str) and child.strip():
                return child.strip()
        for child in value.values():
            found = _find_stable_session_value(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_stable_session_value(child)
            if found:
                return found
    return None


def _opaque_session(source: str) -> str:
    digest = hashlib.sha256(f"chat-session:{source}".encode()).hexdigest()
    return f"chat-{digest[:32]}"


def _model_requires_session(model: object) -> bool:
    """Return whether the configured chat deployment needs a session header."""
    return isinstance(model, str) and (
        model == "omen-alpha" or model.endswith("/omen-alpha")
    )


def resolve_session(body: bytes, headers: Mapping[str, str]) -> str | None:
    """Resolve the upstream session without exposing its value in logs."""
    try:
        obj = json.loads(body)
    except Exception:
        return None
    if not isinstance(obj, dict) or not _model_requires_session(obj.get("model")):
        return None

    extra_headers = obj.get("extra_headers")
    if isinstance(extra_headers, dict):
        explicit = _header_value(extra_headers, SESSION_HEADER)
        if explicit:
            return explicit

    explicit = _header_value(headers, SESSION_HEADER)
    if explicit:
        return explicit

    for key in (
        "thread_id",
        "threadId",
        "conversation_id",
        "conversationId",
        "session_id",
        "sessionId",
    ):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return _opaque_session(value.strip())

    native_id = _find_stable_session_value(obj.get("metadata"))
    if native_id:
        return _opaque_session(native_id)

    return _PROCESS_SESSION


def ensure_session(
    body: bytes,
    headers: Mapping[str, str],
) -> tuple[bytes, str | None]:
    """Add the provider session field only for the affected chat deployment."""
    session = resolve_session(body, headers)
    if session is None:
        return body, None

    try:
        obj = json.loads(body)
    except Exception:
        return body, None
    extra_headers = obj.get("extra_headers")
    if not isinstance(extra_headers, dict):
        extra_headers = {}
    extra_headers[SESSION_HEADER] = session
    obj["extra_headers"] = extra_headers
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), session


def build_upstream_url(backend: str, path: str, query_string: str = "") -> str:
    url = f"{backend.rstrip('/')}{path}"
    if query_string:
        url += f"?{query_string}"
    return url


def _forward_headers(request_headers: Mapping[str, str], body_length: int) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in request_headers.items():
        if key.lower() in {
            "host",
            "content-length",
            "content-encoding",
            "connection",
            "transfer-encoding",
        }:
            continue
        headers[key] = value
    headers["Accept-Encoding"] = "identity"
    headers["Content-Length"] = str(body_length)
    return headers


async def handle(
    request: aiohttp.web.Request,
    backend: str,
    session: aiohttp.ClientSession,
):
    body = await request.read()
    provider_session: str | None = None
    if request.method == "POST" and CHAT_COMPLETIONS_PATH in request.path:
        body, provider_session = ensure_session(body, request.headers)

    headers = _forward_headers(request.headers, len(body))
    if provider_session:
        headers[SESSION_HEADER] = provider_session

    url = build_upstream_url(backend, request.path, request.query_string)

    try:
        async with session.request(
            request.method,
            url,
            data=body,
            headers=headers,
            compress=False,
            timeout=aiohttp.ClientTimeout(total=4200),
        ) as upstream:
            content_type = upstream.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type.lower():
                response_body = await upstream.read()
                response_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower()
                    not in {"content-length", "transfer-encoding", "connection"}
                }
                return aiohttp.web.Response(
                    status=upstream.status,
                    body=response_body,
                    headers=response_headers,
                )

            response = aiohttp.web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() in {"content-length", "transfer-encoding", "connection"}:
                    continue
                response.headers[key] = value
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                if chunk:
                    await response.write(chunk)
            await response.write_eof()
            return response
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("backend request failed: %s", exc)
        return aiohttp.web.Response(
            status=502,
            text="chat-completions-proxy: backend unreachable",
        )


async def main() -> None:
    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else 4102
    backend = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BACKEND
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = aiohttp.web.Application(client_max_size=128 * 1024 * 1024)
    connector = aiohttp.TCPConnector(limit=64)
    session = aiohttp.ClientSession(connector=connector, trust_env=True)
    app.router.add_route(
        "*",
        "/{tail:.*}",
        lambda request: handle(request, backend, session),
    )
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", listen_port)
    await site.start()
    log.info("Chat Completions proxy listening on 127.0.0.1:%s -> %s", listen_port, backend)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
