from __future__ import annotations

import asyncio
import base64
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway.chatgpt_auth import get_chatgpt_credentials


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_chatgpt_auth_reads_codex_subscription_login(tmp_path) -> None:
    auth_file = tmp_path / "auth.json"
    access_token = _jwt(
        {
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "account-from-access-token"
            },
        }
    )
    auth_file.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": "client-api-key-must-not-be-used",
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": "refresh-token",
                    "id_token": "id-token",
                    "account_id": "subscription-account-id",
                },
            }
        ),
        encoding="utf-8",
    )

    async def exercise() -> None:
        async with web.ClientSession() as session:
            credentials = await get_chatgpt_credentials(
                session,
                auth_paths=[auth_file],
            )
        assert credentials.access_token == access_token
        assert credentials.account_id == "subscription-account-id"

    asyncio.run(exercise())


def test_chatgpt_auth_refreshes_expired_codex_login(tmp_path) -> None:
    auth_file = tmp_path / "auth.json"
    expired = _jwt({"exp": int(time.time()) - 60})
    refreshed = _jwt(
        {
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "refreshed-account-id"
            },
        }
    )
    auth_file.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": expired,
                    "refresh_token": "old-refresh-token",
                    "id_token": expired,
                },
            }
        ),
        encoding="utf-8",
    )

    async def token_handler(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-refresh-token"
        return web.json_response(
            {
                "access_token": refreshed,
                "refresh_token": "new-refresh-token",
                "id_token": refreshed,
            }
        )

    async def exercise() -> None:
        app = web.Application()
        app.router.add_post("/oauth/token", token_handler)
        async with TestClient(TestServer(app)) as auth_server:
            async with web.ClientSession() as session:
                credentials = await get_chatgpt_credentials(
                    session,
                    auth_paths=[auth_file],
                    oauth_token_url=str(auth_server.make_url("/oauth/token")),
                )
        assert credentials.access_token == refreshed
        assert credentials.account_id == "refreshed-account-id"

    asyncio.run(exercise())
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["tokens"]["refresh_token"] == "new-refresh-token"
    assert saved["tokens"]["access_token"] == refreshed
    assert "last_refresh" in saved
