from __future__ import annotations

import asyncio
import base64
import json
import time

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from llm_gateway.chatgpt_auth import _candidate_auth_files, get_chatgpt_credentials


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
        async with ClientSession() as session:
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
            async with ClientSession() as session:
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


def test_default_auth_store_matches_litellm_contract(tmp_path) -> None:
    env = {"CHATGPT_TOKEN_DIR": str(tmp_path)}
    assert _candidate_auth_files(env, None) == [
        (tmp_path / "auth.json").resolve(strict=False)
    ]


def test_chatgpt_auth_falls_back_to_device_code_login(tmp_path) -> None:
    auth_file = tmp_path / "auth.json"
    access_token = _jwt(
        {
            "exp": int(time.time()) + 3600,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "device-account"
            },
        }
    )

    async def exercise() -> None:
        app = web.Application()

        async def device_code(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "device_auth_id": "device-auth-id",
                    "user_code": "ABCD-EFGH",
                    "interval": 0,
                }
            )

        async def device_token(_request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "authorization_code": "authorization-code",
                    "code_challenge": "challenge",
                    "code_verifier": "verifier",
                }
            )

        async def oauth_token(request: web.Request) -> web.Response:
            body = await request.post()
            assert body["grant_type"] == "authorization_code"
            return web.json_response(
                {
                    "access_token": access_token,
                    "refresh_token": "refresh-token",
                    "id_token": access_token,
                }
            )

        app.router.add_post("/device-code", device_code)
        app.router.add_post("/device-token", device_token)
        app.router.add_post("/oauth/token", oauth_token)
        async with TestClient(TestServer(app)) as auth_server:
            async with ClientSession() as session:
                credentials = await get_chatgpt_credentials(
                    session,
                    auth_paths=[auth_file],
                    device_code_url=str(auth_server.make_url("/device-code")),
                    device_token_url=str(auth_server.make_url("/device-token")),
                    oauth_token_url=str(auth_server.make_url("/oauth/token")),
                )
        assert credentials.access_token == access_token
        assert credentials.account_id == "device-account"

    asyncio.run(exercise())
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "refresh-token"
    assert saved["account_id"] == "device-account"
