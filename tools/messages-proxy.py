"""Anthropic Messages protocol forwarder to the Gateway Core."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Mapping
from pathlib import Path

import aiohttp
from aiohttp import web

try:
    from protocol_models import load_protocol_models
except ModuleNotFoundError:  # pragma: no cover - direct file loading in tests
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from protocol_models import load_protocol_models


MESSAGES_PATH = "/v1/messages"
COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
MODELS_PATH = "/v1/models"
HEALTH_PATH = "/health/liveliness"
HOP_BY_HOP_HEADERS = {"host", "content-length", "content-encoding", "connection", "transfer-encoding"}

log = logging.getLogger("anthropic-messages-proxy")
CORE_URL_KEY = web.AppKey("core_url", str)
MESSAGES_MODELS_KEY = web.AppKey("messages_models", set[str])
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)


def is_allowed_path(path: str, method: str) -> bool:
    if path in {MODELS_PATH, HEALTH_PATH}:
        return method == "GET"
    if path in {MESSAGES_PATH, COUNT_TOKENS_PATH}:
        return method == "POST"
    return False


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


def _forward_headers(request_headers: Mapping[str, str], body_length: int) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request_headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    headers["Accept-Encoding"] = "identity"
    headers["Content-Length"] = str(body_length)
    return headers


async def handle(request: web.Request):
    if not is_allowed_path(request.path, request.method):
        return web.json_response(
            {"error": {"type": "invalid_request_error", "message": "path is not served by the Anthropic Messages service"}},
            status=404,
        )
    if request.method == "GET" and request.path == HEALTH_PATH:
        return web.json_response({"status": "ok", "service": "anthropic-messages"})

    body = await request.read()
    if request.method == "POST":
        try:
            request_obj = json.loads(body)
            model = request_obj.get("model") if isinstance(request_obj, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            model = None
        messages_models = request.app[MESSAGES_MODELS_KEY]
        if messages_models and model is not None and model not in messages_models:
            return web.json_response(
                {"error": {"type": "invalid_request_error", "message": "model is not available on the Anthropic Messages service"}},
                status=404,
            )

    url = f"{request.app[CORE_URL_KEY].rstrip('/')}{request.path}"
    if request.query_string:
        url += f"?{request.query_string}"
    try:
        async with request.app[SESSION_KEY].request(
            request.method,
            url,
            data=body,
            headers=_forward_headers(request.headers, len(body)),
            compress=False,
            timeout=aiohttp.ClientTimeout(total=4200),
        ) as upstream:
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
            }
            if "text/event-stream" in upstream.headers.get("Content-Type", "").lower():
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    if chunk:
                        await response.write(chunk)
                await response.write_eof()
                return response

            response_body = await upstream.read()
            if (
                request.method == "GET"
                and request.path == MODELS_PATH
                and 200 <= upstream.status < 300
            ):
                response_body = filter_models_response(
                    response_body,
                    request.app[MESSAGES_MODELS_KEY],
                )
            return web.Response(status=upstream.status, body=response_body, headers=response_headers)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Anthropic Messages upstream failed: %s", type(exc).__name__)
        return web.json_response(
            {"error": {"type": "upstream_error", "message": "Anthropic Messages upstream unavailable"}},
            status=502,
        )


async def _start_session(app: web.Application) -> None:
    app[SESSION_KEY] = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=64),
        trust_env=True,
    )


async def _close_session(app: web.Application) -> None:
    await app[SESSION_KEY].close()


def create_app(core_url: str, messages_models: set[str] | None = None) -> web.Application:
    app = web.Application(client_max_size=128 * 1024 * 1024)
    app[CORE_URL_KEY] = core_url
    app[MESSAGES_MODELS_KEY] = messages_models or set()
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_startup.append(_start_session)
    app.on_cleanup.append(_close_session)
    return app


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Anthropic Messages forwarding service.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--models-config", required=True)
    args = parser.parse_args()
    _, _, messages_models, _ = load_protocol_models(args.models_config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = create_app(args.core_url, messages_models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("Anthropic Messages service listening on %s:%s -> %s", args.host, args.port, args.core_url)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
