from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import aiohttp


CHATGPT_AUTH_BASE = "https://auth.openai.com"
CHATGPT_DEVICE_CODE_URL = f"{CHATGPT_AUTH_BASE}/api/accounts/deviceauth/usercode"
CHATGPT_DEVICE_TOKEN_URL = f"{CHATGPT_AUTH_BASE}/api/accounts/deviceauth/token"
CHATGPT_DEVICE_VERIFY_URL = f"{CHATGPT_AUTH_BASE}/codex/device"
CHATGPT_OAUTH_TOKEN_URL = f"{CHATGPT_AUTH_BASE}/oauth/token"
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_EXPIRY_SKEW_SECONDS = 60
DEVICE_CODE_TIMEOUT_SECONDS = 15 * 60
DEVICE_CODE_POLL_SLEEP_SECONDS = 5
_AUTH_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class ChatGPTCredentials:
    access_token: str
    account_id: str | None


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def _expires_at(token_record: Mapping[str, Any], access_token: str) -> float | None:
    configured = token_record.get("expires_at")
    if isinstance(configured, (int, float)):
        return float(configured)
    if isinstance(configured, str):
        try:
            return float(configured)
        except ValueError:
            pass
    exp = _decode_jwt_claims(access_token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def _account_id(token_record: Mapping[str, Any]) -> str | None:
    configured = token_record.get("account_id")
    if isinstance(configured, str) and configured:
        return configured
    for token_name in ("id_token", "access_token"):
        token = token_record.get(token_name)
        if not isinstance(token, str) or not token:
            continue
        auth_claims = _decode_jwt_claims(token).get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            account_id = auth_claims.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
    return None


def _candidate_auth_files(
    environ: Mapping[str, str],
    auth_paths: Iterable[str | Path] | None,
) -> list[Path]:
    if auth_paths is not None:
        return [Path(path).expanduser() for path in auth_paths]
    token_dir = Path(
        environ.get("CHATGPT_TOKEN_DIR")
        or (Path.home() / ".config" / "llm" / "chatgpt")
    ).expanduser()
    token_name = environ.get("CHATGPT_AUTH_FILE") or "auth.json"
    return [(token_dir / token_name).resolve(strict=False)]


def _read_auth_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_auth_file(path: Path, auth_data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(auth_data), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _build_auth_record(tokens: Mapping[str, str]) -> dict[str, Any]:
    access_token = tokens.get("access_token")
    record: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token"),
    }
    if access_token:
        record["expires_at"] = _expires_at({}, access_token)
    record["account_id"] = _account_id(record)
    return record


async def _refresh(
    session: aiohttp.ClientSession,
    refresh_token: str,
    oauth_token_url: str,
) -> dict[str, str]:
    try:
        async with session.post(
            oauth_token_url,
            json={
                "client_id": CHATGPT_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "openid profile email",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"ChatGPT OAuth refresh failed with HTTP {response.status}")
            value = await response.json()
    except asyncio.CancelledError:
        raise
    except RuntimeError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise RuntimeError("ChatGPT OAuth refresh failed") from exc

    access_token = value.get("access_token") if isinstance(value, dict) else None
    id_token = value.get("id_token") if isinstance(value, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("ChatGPT OAuth refresh returned no access token")
    result = {
        "access_token": access_token,
        "refresh_token": (
            value.get("refresh_token")
            if isinstance(value.get("refresh_token"), str) and value.get("refresh_token")
            else refresh_token
        ),
    }
    if isinstance(id_token, str) and id_token:
        result["id_token"] = id_token
    return result


async def _request_device_code(
    session: aiohttp.ClientSession,
    device_code_url: str,
) -> dict[str, str]:
    try:
        async with session.post(
            device_code_url,
            json={"client_id": CHATGPT_CLIENT_ID},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"ChatGPT device-code request failed with HTTP {response.status}")
            value = await response.json()
    except asyncio.CancelledError:
        raise
    except RuntimeError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise RuntimeError("ChatGPT device-code request failed") from exc

    device_auth_id = value.get("device_auth_id") if isinstance(value, dict) else None
    user_code = None
    interval = None
    if isinstance(value, dict):
        user_code = value.get("user_code") or value.get("usercode")
        interval = value.get("interval")
    if not isinstance(device_auth_id, str) or not device_auth_id:
        raise RuntimeError("ChatGPT device-code response omitted device_auth_id")
    if not isinstance(user_code, str) or not user_code:
        raise RuntimeError("ChatGPT device-code response omitted user_code")
    return {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "interval": str(interval or "5"),
    }


async def _poll_for_authorization_code(
    session: aiohttp.ClientSession,
    device_code: Mapping[str, str],
    device_token_url: str,
) -> dict[str, str]:
    interval = max(int(device_code.get("interval", "5")), DEVICE_CODE_POLL_SLEEP_SECONDS)
    deadline = time.monotonic() + DEVICE_CODE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            async with session.post(
                device_token_url,
                json={
                    "device_auth_id": device_code["device_auth_id"],
                    "user_code": device_code["user_code"],
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    value = await response.json()
                    required = ("authorization_code", "code_challenge", "code_verifier")
                    if isinstance(value, dict) and all(
                        isinstance(value.get(key), str) and value.get(key)
                        for key in required
                    ):
                        return {key: value[key] for key in required}
                elif response.status not in (403, 404):
                    raise RuntimeError(
                        f"ChatGPT device authorization polling failed with HTTP {response.status}"
                    )
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise RuntimeError("ChatGPT device authorization polling failed") from exc
        await asyncio.sleep(interval)
    raise RuntimeError("Timed out waiting for ChatGPT device authorization")


async def _exchange_code_for_tokens(
    session: aiohttp.ClientSession,
    code_data: Mapping[str, str],
    oauth_token_url: str,
) -> dict[str, str]:
    redirect_uri = f"{CHATGPT_AUTH_BASE}/deviceauth/callback"
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code_data["authorization_code"],
            "redirect_uri": redirect_uri,
            "client_id": CHATGPT_CLIENT_ID,
            "code_verifier": code_data["code_verifier"],
        }
    )
    try:
        async with session.post(
            oauth_token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"ChatGPT token exchange failed with HTTP {response.status}")
            value = await response.json()
    except asyncio.CancelledError:
        raise
    except RuntimeError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise RuntimeError("ChatGPT token exchange failed") from exc

    required = ("access_token", "refresh_token", "id_token")
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), str) and value.get(key) for key in required
    ):
        raise RuntimeError("ChatGPT token exchange response omitted required tokens")
    return {key: value[key] for key in required}


async def _device_login(
    session: aiohttp.ClientSession,
    auth_path: Path,
    device_code_url: str,
    device_token_url: str,
    oauth_token_url: str,
) -> ChatGPTCredentials:
    device_code = await _request_device_code(session, device_code_url)
    print(
        "ChatGPT subscription sign-in required for llm-gateway:\n"
        f"1) Visit {CHATGPT_DEVICE_VERIFY_URL}\n"
        f"2) Enter code: {device_code['user_code']}\n"
        "Do not share this device code.",
        file=sys.stderr,
        flush=True,
    )
    code_data = await _poll_for_authorization_code(
        session,
        device_code,
        device_token_url,
    )
    tokens = await _exchange_code_for_tokens(session, code_data, oauth_token_url)
    record = _build_auth_record(tokens)
    _write_auth_file(auth_path, record)
    return ChatGPTCredentials(
        access_token=tokens["access_token"],
        account_id=_account_id(record),
    )


async def get_chatgpt_credentials(
    session: aiohttp.ClientSession,
    environ: Mapping[str, str] | None = None,
    auth_paths: Iterable[str | Path] | None = None,
    oauth_token_url: str = CHATGPT_OAUTH_TOKEN_URL,
    device_code_url: str = CHATGPT_DEVICE_CODE_URL,
    device_token_url: str = CHATGPT_DEVICE_TOKEN_URL,
    allow_device_login: bool = True,
) -> ChatGPTCredentials:
    env = os.environ if environ is None else environ
    paths = _candidate_auth_files(env, auth_paths)

    async with _AUTH_LOCK:
        for path in paths:
            auth_data = _read_auth_file(path)
            if auth_data is None:
                continue
            access_token = auth_data.get("access_token")
            if isinstance(access_token, str) and access_token:
                expires_at = _expires_at(auth_data, access_token)
                if expires_at is not None and time.time() < expires_at - TOKEN_EXPIRY_SKEW_SECONDS:
                    return ChatGPTCredentials(
                        access_token=access_token,
                        account_id=_account_id(auth_data),
                    )

            refresh_token = auth_data.get("refresh_token")
            if isinstance(refresh_token, str) and refresh_token:
                try:
                    refreshed = await _refresh(session, refresh_token, oauth_token_url)
                except RuntimeError:
                    pass
                else:
                    record = _build_auth_record(refreshed)
                    _write_auth_file(path, record)
                    return ChatGPTCredentials(
                        access_token=refreshed["access_token"],
                        account_id=_account_id(record),
                    )

        if allow_device_login:
            return await _device_login(
                session,
                paths[0],
                device_code_url,
                device_token_url,
                oauth_token_url,
            )

    raise RuntimeError(
        "ChatGPT subscription credential is unavailable in ~/.config/llm/chatgpt/auth.json"
    )
