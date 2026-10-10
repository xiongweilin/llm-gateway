"""Unified loopback entry for agent clients.

Port assignment is supplied by the gateway configuration: Responses traffic
goes to the Responses service, Chat Completions to its protocol service, and
the model catalog to Core. Bodies are not logged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
import sys

import aiohttp
from aiohttp import web

CORE_SRC = Path(__file__).resolve().parents[1] / "core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from llm_gateway.config import load_model_routes, protocol_models
from responses_messages_compat import handle_responses_as_messages


log = logging.getLogger("agent-gateway")
CORE_URL_KEY = web.AppKey("core_url", str)
RESPONSES_URL_KEY = web.AppKey("responses_url", str)
CHAT_URL_KEY = web.AppKey("chat_url", str)
MESSAGES_URL_KEY = web.AppKey("messages_url", str | None)
CHAT_MODELS_KEY = web.AppKey("chat_models", set[str])
AGENT_MODELS_KEY = web.AppKey("agent_models", set[str] | None)
CODEX_MESSAGES_MODELS_KEY = web.AppKey("codex_messages_models", set[str])
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
RESPONSES_PATH = "/v1/responses"
CHAT_PATH = "/v1/chat/completions"
MESSAGES_PATH = "/v1/messages"
MESSAGES_COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
MODELS_PATH = "/v1/models"
HEALTH_PATH = "/health/liveliness"
CONTROL_PLANE_PREFIX = "/v1/alpha"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "content-encoding",
    "host",
    "transfer-encoding",
}


def is_control_plane_path(path: str) -> bool:
    return path == CONTROL_PLANE_PREFIX or path.startswith(f"{CONTROL_PLANE_PREFIX}/")


def select_backend(
    path: str,
    core_url: str,
    responses_url: str,
    chat_url: str,
    messages_url: str | None = None,
) -> str | None:
    if path == CHAT_PATH:
        return chat_url
    if path in {MESSAGES_PATH, MESSAGES_COUNT_TOKENS_PATH}:
        return messages_url
    if path == MODELS_PATH or path == HEALTH_PATH:
        return core_url
    if path == RESPONSES_PATH or is_control_plane_path(path):
        return responses_url
    return None


def build_url(backend: str, path: str, query: str = "") -> str:
    url = f"{backend.rstrip('/')}{path}"
    return f"{url}?{query}" if query else url


def forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result = {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    result["Accept-Encoding"] = "identity"
    return result


def filter_models_response(body: bytes, allowed_models: set[str]) -> bytes:
    try:
        value = json.loads(body)
    except Exception:
        return body
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        return body
    value["data"] = [
        item
        for item in value["data"]
        if isinstance(item, dict) and item.get("id") in allowed_models
    ]
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def is_responses_websocket_upgrade(
    method: str,
    path: str,
    headers: Mapping[str, str],
) -> bool:
    if method != "GET" or path != RESPONSES_PATH:
        return False
    return any(
        key.lower() == "upgrade" and value.strip().lower() == "websocket"
        for key, value in headers.items()
    )


async def handle(request: web.Request):
    if is_responses_websocket_upgrade(request.method, request.path, request.headers):
        return web.Response(
            status=426,
            headers={"Upgrade": "websocket"},
            text="Responses WebSocket transport is not supported; retry over HTTP.",
        )

    core_url = request.app[CORE_URL_KEY]
    responses_url = request.app[RESPONSES_URL_KEY]
    chat_url = request.app[CHAT_URL_KEY]
    messages_url = request.app[MESSAGES_URL_KEY]
    backend = select_backend(request.path, core_url, responses_url, chat_url, messages_url)
    if backend is None or request.method not in {"GET", "POST"}:
        return web.json_response(
            {"error": {"type": "not_found", "message": "path is not served by the agent entry"}},
            status=404,
        )

    if request.method == "POST" and request.path == RESPONSES_PATH:
        body = await request.read()
        try:
            body_obj = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            body_obj = None
        model = body_obj.get("model") if isinstance(body_obj, dict) else None
        codex_messages_models = request.app[CODEX_MESSAGES_MODELS_KEY]
        if isinstance(model, str) and model in codex_messages_models:
            if messages_url is None:
                return web.json_response(
                    {"error": {"type": "server_error", "message": "Anthropic Messages backend is not configured"}},
                    status=503,
                )
            return await handle_responses_as_messages(
                request,
                body,
                messages_url,
                request.app[SESSION_KEY],
            )
    elif request.method == "POST" and request.path == CHAT_PATH:
        try:
            body = await request.read()
            # aiohttp 会在进入此 handler 前解码受支持的 request encoding。
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response(
                {"error": {"type": "invalid_request_error", "message": "request body must be JSON"}},
                status=400,
            )
        model = value.get("model") if isinstance(value, dict) else None
        chat_models = request.app[CHAT_MODELS_KEY]
        if chat_models and model not in chat_models:
            return web.json_response(
                {"error": {"type": "invalid_request_error", "message": "model is unavailable for chat completions"}},
                status=404,
            )
    else:
        body = await request.read() if request.can_read_body else b""

    session = request.app[SESSION_KEY]
    try:
        async with session.request(
            request.method,
            build_url(backend, request.path, request.query_string),
            data=body,
            headers=forward_headers(request.headers),
            compress=False,
            timeout=aiohttp.ClientTimeout(total=4200),
        ) as upstream:
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
            }
            if "text/event-stream" not in upstream.headers.get("Content-Type", "").lower():
                response_body = await upstream.read()
                if (
                    request.method == "GET"
                    and request.path == MODELS_PATH
                    and 200 <= upstream.status < 300
                    and request.app[AGENT_MODELS_KEY] is not None
                ):
                    response_body = filter_models_response(
                        response_body,
                        request.app[AGENT_MODELS_KEY] or set(),
                    )
                return web.Response(
                    status=upstream.status,
                    body=response_body,
                    headers=response_headers,
                )
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                if chunk:
                    await response.write(chunk)
            await response.write_eof()
            return response
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("gateway target unavailable: %s", type(exc).__name__)
        return web.json_response(
            {"error": {"type": "upstream_error", "message": "gateway target is unavailable"}},
            status=502,
        )


async def _start_session(app: web.Application) -> None:
    app[SESSION_KEY] = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=64),
        trust_env=True,
    )


async def _close_session(app: web.Application) -> None:
    await app[SESSION_KEY].close()


def create_app(
    core_url: str,
    responses_url: str,
    chat_url: str,
    chat_models: set[str] | None = None,
    agent_models: set[str] | None = None,
    messages_url: str | None = None,
    codex_messages_models: set[str] | None = None,
):
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app[CORE_URL_KEY] = core_url
    app[RESPONSES_URL_KEY] = responses_url
    app[CHAT_URL_KEY] = chat_url
    app[MESSAGES_URL_KEY] = messages_url
    app[CHAT_MODELS_KEY] = chat_models or set()
    app[AGENT_MODELS_KEY] = agent_models
    app[CODEX_MESSAGES_MODELS_KEY] = codex_messages_models or set()
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_startup.append(_start_session)
    app.on_cleanup.append(_close_session)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the unified agent API entry.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--responses-url", required=True)
    parser.add_argument("--chat-url", required=True)
    parser.add_argument("--messages-url", required=True)
    parser.add_argument("--models-config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    routes = load_model_routes(args.models_config)
    _, chat_models, messages_models, codex_responses_models = protocol_models(routes)
    web.run_app(
        create_app(
            args.core_url,
            args.responses_url,
            args.chat_url,
            chat_models,
            codex_responses_models | chat_models,
            args.messages_url,
            messages_models & codex_responses_models,
        ),
        host=args.host,
        port=args.port,
        access_log=None,
    )


if __name__ == "__main__":
    main()
