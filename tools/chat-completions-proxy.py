"""Chat Completions protocol forwarder to the Gateway Core."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Mapping
from pathlib import Path

import aiohttp
import aiohttp.web

try:
    from protocol_models import load_protocol_models
except ModuleNotFoundError:  # pragma: no cover - direct file loading in tests
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from protocol_models import load_protocol_models


CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MODELS_PATH = "/v1/models"
HEALTH_PATH = "/health/liveliness"
CONTROL_PLANE_PATH_PREFIX = "/v1/alpha"
HOP_BY_HOP_HEADERS = {"host", "content-length", "content-encoding", "connection", "transfer-encoding"}

log = logging.getLogger("chat-completions-proxy")


def is_compatibility_extension_path(path: str) -> bool:
    return path == CONTROL_PLANE_PATH_PREFIX or path.startswith(f"{CONTROL_PLANE_PATH_PREFIX}/")


def is_allowed_path(path: str, method: str) -> bool:
    if is_compatibility_extension_path(path):
        return False
    if path == CHAT_COMPLETIONS_PATH:
        return method in {"GET", "POST"}
    if path in {MODELS_PATH, HEALTH_PATH}:
        return method == "GET"
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


async def handle(
    request: aiohttp.web.Request,
    core_url: str,
    session: aiohttp.ClientSession,
    chat_models: set[str] | None = None,
):
    if not is_allowed_path(request.path, request.method):
        return aiohttp.web.json_response(
            {"error": {"type": "invalid_request_error", "message": "path is not served by the Chat Completions service"}},
            status=404,
        )

    body = await request.read()
    model: object = None
    if request.method == "POST" and request.path == CHAT_COMPLETIONS_PATH:
        try:
            request_obj = json.loads(body)
            model = request_obj.get("model") if isinstance(request_obj, dict) else None
        except Exception:
            pass
        if chat_models is not None and model is not None and model not in chat_models:
            return aiohttp.web.json_response(
                {"error": {"type": "invalid_request_error", "message": "model is not available on the Chat Completions service"}},
                status=404,
            )

    url = f"{core_url.rstrip('/')}{request.path}"
    if request.query_string:
        url += f"?{request.query_string}"
    try:
        async with session.request(
            request.method,
            url,
            data=body,
            headers=_forward_headers(request.headers, len(body)),
            compress=False,
            timeout=aiohttp.ClientTimeout(total=4200),
        ) as upstream:
            content_type = upstream.headers.get("Content-Type", "")
            if "text/event-stream" not in content_type.lower():
                response_body = await upstream.read()
                if (
                    request.method == "GET"
                    and request.path == MODELS_PATH
                    and chat_models is not None
                    and 200 <= upstream.status < 300
                ):
                    response_body = filter_models_response(response_body, chat_models)
                response_headers = {
                    key: value
                    for key, value in upstream.headers.items()
                    if key.lower() not in HOP_BY_HOP_HEADERS
                }
                return aiohttp.web.Response(status=upstream.status, body=response_body, headers=response_headers)

            response = aiohttp.web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() in HOP_BY_HOP_HEADERS:
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
        log.warning("Chat Completions upstream failed: %s", type(exc).__name__)
        return aiohttp.web.json_response(
            {"error": {"type": "upstream_error", "message": "Chat Completions upstream unavailable"}},
            status=502,
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Chat Completions forwarding service.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--models-config", required=True)
    args = parser.parse_args()
    _, chat_models, _ = load_protocol_models(args.models_config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app = aiohttp.web.Application(client_max_size=128 * 1024 * 1024)
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=64),
        trust_env=True,
    )
    app.router.add_route(
        "*",
        "/{tail:.*}",
        lambda request: handle(request, args.core_url, session, chat_models),
    )
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, args.host, args.port)
    await site.start()
    log.info("Chat Completions service listening on %s:%s -> %s", args.host, args.port, args.core_url)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())
