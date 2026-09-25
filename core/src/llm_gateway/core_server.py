from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import aiohttp
from aiohttp import web

from llm_gateway.chatgpt_auth import ChatGPTCredentials, get_chatgpt_credentials
from llm_gateway.config import ModelRoute, load_model_routes


log = logging.getLogger("llm-gateway-core")
ROUTES_KEY = web.AppKey("routes", dict[str, ModelRoute])
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
HEALTH_PATH = "/health/liveliness"
MODELS_PATH = "/v1/models"
PROTOCOL_PATHS = {
    "responses": "/v1/responses",
    "chat": "/v1/chat/completions",
}
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "content-encoding",
    "host",
    "transfer-encoding",
}
LEGACY_CODEX_UPSTREAM_HEADERS = {
    "accept",
    "authorization",
    "chatgpt-account-id",
    "content-type",
    "originator",
    "session_id",
    "user-agent",
}
CHATGPT_RESPONSES_ALLOWED_KEYS = {
    "model",
    "input",
    "instructions",
    "stream",
    "store",
    "include",
    "tools",
    "tool_choice",
    "reasoning",
    "previous_response_id",
    "truncation",
}
DEFAULT_CHATGPT_ORIGINATOR = "codex_cli_rs"
DEFAULT_CHATGPT_USER_AGENT = "codex_cli_rs/0.0.0 (Unknown 0; unknown) unknown"


def normalize_opencode_tool_schemas(data: dict[str, Any]) -> int:
    fixed = 0

    def visit(value: Any) -> None:
        nonlocal fixed
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        properties = value.get("properties")
        if isinstance(properties, dict):
            required = value.get("required")
            normalized = [name for name in required if isinstance(name, str)] if isinstance(required, list) else []
            for name in properties:
                if name not in normalized:
                    normalized.append(name)
            if normalized != required:
                value["required"] = normalized
                fixed += 1
        for child in value.values():
            visit(child)

    visit(data)
    return fixed


def normalize_opencode_custom_tools(data: dict[str, Any]) -> int:
    converted = 0

    def visit(value: Any) -> None:
        nonlocal converted
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return
        for key, child in list(value.items()):
            if key == "tools" and isinstance(child, list):
                normalized = []
                for tool in child:
                    if isinstance(tool, dict) and tool.get("type") == "custom":
                        name = tool.get("name")
                        if not isinstance(name, str) or not name:
                            normalized.append(tool)
                            continue
                        description = tool.get("description")
                        if not isinstance(description, str) or not description.strip():
                            description = f"{name} tool"
                        normalized.append(
                            {
                                "type": "function",
                                "name": name,
                                "description": description,
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "input": {
                                            "type": "string",
                                            "description": "Raw input for this tool.",
                                        }
                                    },
                                    "required": ["input"],
                                    "additionalProperties": False,
                                },
                            }
                        )
                        converted += 1
                    else:
                        normalized.append(tool)
                        visit(tool)
                value[key] = normalized
            else:
                visit(child)

    visit(data)
    return converted


def build_upstream_url(api_base: str, request_path: str, query: str = "") -> str:
    suffix = request_path.removeprefix("/v1")
    url = f"{api_base.rstrip('/')}{suffix}"
    return f"{url}?{query}" if query else url


def normalize_chatgpt_responses_payload(data: dict[str, Any]) -> None:
    data["store"] = False
    data["stream"] = True
    raw_include = data.get("include")
    include = list(raw_include) if isinstance(raw_include, list) else []
    if "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")
    data["include"] = include

    for key in tuple(data):
        if key not in CHATGPT_RESPONSES_ALLOWED_KEYS:
            del data[key]


def _setdefault_header(headers: dict[str, str], name: str, value: str) -> None:
    if not any(key.lower() == name.lower() for key in headers):
        headers[name] = value


def build_upstream_headers(
    request_headers: Mapping[str, str],
    route: ModelRoute,
    environ: Mapping[str, str] | None = None,
    chatgpt_credentials: ChatGPTCredentials | None = None,
) -> dict[str, str]:
    env = os.environ if environ is None else environ
    allowed_headers = LEGACY_CODEX_UPSTREAM_HEADERS
    if route.compatibility == "opencode-go":
        allowed_headers = allowed_headers | {"x-opencode-session"}
    headers = {
        key: value
        for key, value in request_headers.items()
        if key.lower() in allowed_headers
    }
    headers["Accept-Encoding"] = "identity"
    if route.api_key_env:
        api_key = env.get(route.api_key_env)
        if not api_key:
            raise RuntimeError(f"provider credential is not configured: {route.api_key_env}")
        for key in tuple(headers):
            if key.lower() == "authorization":
                del headers[key]
        headers["Authorization"] = f"Bearer {api_key}"
    elif route.authorization == "chatgpt":
        if chatgpt_credentials is None:
            raise RuntimeError("ChatGPT subscription credential was not resolved")
        for key in tuple(headers):
            if key.lower() in {"authorization", "chatgpt-account-id"}:
                del headers[key]
        headers["Authorization"] = f"Bearer {chatgpt_credentials.access_token}"
        if chatgpt_credentials.account_id:
            headers["ChatGPT-Account-Id"] = chatgpt_credentials.account_id

        _setdefault_header(headers, "Content-Type", "application/json")
        _setdefault_header(headers, "Accept", "text/event-stream")
        _setdefault_header(
            headers,
            "Originator",
            env.get("CHATGPT_ORIGINATOR") or DEFAULT_CHATGPT_ORIGINATOR,
        )
        _setdefault_header(
            headers,
            "User-Agent",
            env.get("CHATGPT_USER_AGENT") or DEFAULT_CHATGPT_USER_AGENT,
        )
        _setdefault_header(headers, "session_id", str(uuid4()))
    elif route.authorization == "none":
        for key in tuple(headers):
            if key.lower() == "authorization":
                del headers[key]
    return headers


def _log_route_summary(routes: dict[str, ModelRoute]) -> None:
    for route in sorted(routes.values(), key=lambda item: item.id):
        host = urlparse(route.api_base).netloc or "<invalid>"
        log.info(
            "route model=%s mode=%s upstream_host=%s authorization=%s",
            route.id,
            route.mode,
            host,
            route.authorization,
        )


def model_catalog(routes: dict[str, ModelRoute], modes: set[str] | None = None) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": route.id, "object": "model"}
            for route in sorted(routes.values(), key=lambda item: item.id)
            if modes is None or route.mode in modes
        ],
    }


async def _proxy_response(
    request: web.Request,
    route: ModelRoute,
    session: aiohttp.ClientSession,
    payload: bytes,
):
    chatgpt_credentials = None
    if route.authorization == "chatgpt":
        try:
            chatgpt_credentials = await get_chatgpt_credentials(session)
        except RuntimeError as exc:
            return web.json_response(
                {"error": {"type": "configuration_error", "message": str(exc)}},
                status=503,
            )

    try:
        headers = build_upstream_headers(
            request.headers,
            route,
            chatgpt_credentials=chatgpt_credentials,
        )
    except RuntimeError as exc:
        return web.json_response(
            {"error": {"type": "configuration_error", "message": str(exc)}},
            status=503,
        )

    try:
        for attempt in range(5):
            should_retry = False
            delay = 0.0
            async with session.request(
                request.method,
                build_upstream_url(route.api_base, request.path, request.query_string),
                data=payload,
                headers=headers,
                compress=False,
                timeout=aiohttp.ClientTimeout(total=4200),
            ) as upstream:
                if upstream.status == 429 and attempt < 4:
                    delay = _retry_delay(upstream.headers.get("Retry-After"), attempt)
                    should_retry = True
                else:
                    return await _copy_upstream_response(request, upstream)
            if should_retry:
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("provider request failed for model %s: %s", route.id, type(exc).__name__)
        return web.json_response(
            {"error": {"type": "upstream_error", "message": "provider request failed"}},
            status=502,
        )


def _retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            seconds = float(value)
        except ValueError:
            seconds = 0
        if 0 < seconds <= 10:
            return seconds
    return float(min(2**attempt, 8))


async def _copy_upstream_response(request: web.Request, upstream: aiohttp.ClientResponse):
    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    if "text/event-stream" not in upstream.headers.get("Content-Type", "").lower():
        return web.Response(
            status=upstream.status,
            body=await upstream.read(),
            headers=response_headers,
        )

    response = web.StreamResponse(status=upstream.status, headers=response_headers)
    await response.prepare(request)
    async for chunk in upstream.content.iter_any():
        if chunk:
            await response.write(chunk)
    await response.write_eof()
    return response


async def handle(request: web.Request):
    routes = request.app[ROUTES_KEY]
    if request.method == "GET" and request.path == HEALTH_PATH:
        return web.json_response({"status": "ok", "service": "core"})
    if request.method == "GET" and request.path == MODELS_PATH:
        return web.json_response(model_catalog(routes))
    if request.method != "POST" or request.path not in {
        PROTOCOL_PATHS["responses"],
        PROTOCOL_PATHS["chat"],
    }:
        return web.json_response(
            {"error": {"type": "not_found", "message": "path is not served by the core"}},
            status=404,
        )

    expected_mode = "responses" if request.path == PROTOCOL_PATHS["responses"] else "chat"
    try:
        # aiohttp decodes supported request content encodings before handlers run.
        body = await request.read()
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return web.json_response(
            {"error": {"type": "invalid_request_error", "message": "request body must be JSON"}},
            status=400,
        )
    if not isinstance(payload, dict):
        return web.json_response(
            {"error": {"type": "invalid_request_error", "message": "request body must be an object"}},
            status=400,
        )
    model_id = payload.get("model")
    route = routes.get(model_id) if isinstance(model_id, str) else None
    if route is None:
        return web.json_response(
            {"error": {"type": "invalid_request_error", "message": "model is not configured"}},
            status=404,
        )
    if route.mode != expected_mode:
        return web.json_response(
            {"error": {"type": "invalid_request_error", "message": "model is unavailable for this protocol"}},
            status=404,
        )

    payload["model"] = route.upstream_model
    if route.authorization == "chatgpt" and expected_mode == "responses":
        normalize_chatgpt_responses_payload(payload)
    if route.compatibility == "opencode-go" and expected_mode == "responses":
        normalize_opencode_custom_tools(payload)
        normalize_opencode_tool_schemas(payload)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return await _proxy_response(request, route, request.app[SESSION_KEY], serialized)


async def _start_session(app: web.Application) -> None:
    app[SESSION_KEY] = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=64),
        trust_env=True,
    )


async def _close_session(app: web.Application) -> None:
    await app[SESSION_KEY].close()


def create_app(routes: dict[str, ModelRoute]) -> web.Application:
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app[ROUTES_KEY] = routes
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_startup.append(_start_session)
    app.on_cleanup.append(_close_session)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the self-owned model routing core.")
    parser.add_argument("--config", required=True, help="gateway model route YAML")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    routes = load_model_routes(args.config)
    _log_route_summary(routes)
    web.run_app(create_app(routes), host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
