from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import aiohttp


CHATGPT_AUTH_BASE = "https://auth.openai.com"
CHATGPT_OAUTH_TOKEN_URL = f"{CHATGPT_AUTH_BASE}/oauth/token"
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_EXPIRY_SKEW_SECONDS = 60


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


def _token_record(auth_data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    nested = auth_data.get("tokens")
    if isinstance(nested, dict):
        return nested, True
    return auth_data, False


def _candidate_auth_files(
    environ: Mapping[str, str],
    auth_paths: Iterable[str | Path] | None,
) -> list[Path]:
    if auth_paths is not None:
        return [Path(path).expanduser() for path in auth_paths]

    token_dir = Path(
        environ.get("CHATGPT_TOKEN_DIR")
        or (Path.home() / ".config" / "litellm" / "chatgpt")
    ).expanduser()
    token_name = environ.get("CHATGPT_AUTH_FILE") or "auth.json"
    litellm_auth = token_dir / token_name

    codex_home = Path(
        environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser()
    codex_auth = codex_home / "auth.json"

    result: list[Path] = []
    # The active Codex login is the authoritative subscription identity.
    # Keep the historical LiteLLM token store only as a migration fallback.
    for path in (codex_auth, litellm_auth):
        resolved = path.resolve(strict=False)
        if resolved not in result:
            result.append(resolved)
    return result


def _read_auth_file(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_auth_file(path: Path, auth_data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(auth_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


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
    result = {"access_token": access_token}
    if isinstance(id_token, str) and id_token:
        result["id_token"] = id_token
    new_refresh = value.get("refresh_token") if isinstance(value, dict) else None
    result["refresh_token"] = (
        new_refresh if isinstance(new_refresh, str) and new_refresh else refresh_token
    )
    return result


def _store_refreshed_tokens(
    path: Path,
    auth_data: dict[str, Any],
    token_record: dict[str, Any],
    nested: bool,
    refreshed: Mapping[str, str],
) -> ChatGPTCredentials:
    token_record.update(refreshed)
    access_token = refreshed["access_token"]
    expires_at = _expires_at(token_record, access_token)
    account_id = _account_id(token_record)

    if nested:
        auth_data["tokens"] = token_record
        auth_data["last_refresh"] = datetime.now(timezone.utc).isoformat()
    else:
        if expires_at is not None:
            token_record["expires_at"] = expires_at
        if account_id:
            token_record["account_id"] = account_id

    _write_auth_file(path, auth_data)
    return ChatGPTCredentials(access_token=access_token, account_id=account_id)


async def get_chatgpt_credentials(
    session: aiohttp.ClientSession,
    environ: Mapping[str, str] | None = None,
    auth_paths: Iterable[str | Path] | None = None,
    oauth_token_url: str = CHATGPT_OAUTH_TOKEN_URL,
) -> ChatGPTCredentials:
    env = os.environ if environ is None else environ
    for path in _candidate_auth_files(env, auth_paths):
        auth_data = _read_auth_file(path)
        if auth_data is None:
            continue
        token_record, nested = _token_record(auth_data)
        access_token = token_record.get("access_token")
        if isinstance(access_token, str) and access_token:
            expires_at = _expires_at(token_record, access_token)
            if expires_at is not None and time.time() < expires_at - TOKEN_EXPIRY_SKEW_SECONDS:
                return ChatGPTCredentials(
                    access_token=access_token,
                    account_id=_account_id(token_record),
                )

        refresh_token = token_record.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            continue
        try:
            refreshed = await _refresh(session, refresh_token, oauth_token_url)
        except RuntimeError:
            continue
        return _store_refreshed_tokens(
            path,
            auth_data,
            token_record,
            nested,
            refreshed,
        )

    raise RuntimeError(
        "ChatGPT subscription credential is unavailable; sign in with Codex "
        "or restore the ChatGPT OAuth token store"
    )
