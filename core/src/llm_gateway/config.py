from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelRoute:
    id: str
    mode: str
    upstream_model: str
    api_base: str
    api_key_env: str | None = None
    authorization: str = "client"
    compatibility: str | None = None


def load_model_routes(path: str | Path) -> dict[str, ModelRoute]:
    """Load and validate gateway-owned routes without resolving secret values."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("models")
    if not isinstance(entries, list):
        raise ValueError("model configuration must contain a models list")

    routes: dict[str, ModelRoute] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each model route must be an object")
        model_id = entry.get("id")
        mode = entry.get("mode")
        upstream_model = entry.get("upstream_model")
        api_base = _configured_value(entry, "api_base", "api_base_env")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("each model route requires a non-empty id")
        if model_id in routes:
            raise ValueError(f"duplicate model route: {model_id}")
        if mode not in {"responses", "chat"}:
            raise ValueError(f"unsupported protocol mode for {model_id}")
        if not isinstance(upstream_model, str) or not upstream_model.strip():
            raise ValueError(f"model route {model_id} requires upstream_model")
        if not isinstance(api_base, str) or not api_base.startswith(("http://", "https://")):
            raise ValueError(f"model route {model_id} requires an HTTP api_base")

        api_key_env = entry.get("api_key_env")
        authorization = entry.get("authorization", "client")
        compatibility = entry.get("compatibility")
        if api_key_env is not None and (not isinstance(api_key_env, str) or not api_key_env):
            raise ValueError(f"invalid api_key_env for {model_id}")
        if authorization not in {"client", "none"}:
            raise ValueError(f"unsupported authorization source for {model_id}")
        if compatibility not in {None, "opencode-go"}:
            raise ValueError(f"unsupported compatibility profile for {model_id}")

        routes[model_id] = ModelRoute(
            id=model_id,
            mode=mode,
            upstream_model=upstream_model,
            api_base=api_base.rstrip("/"),
            api_key_env=api_key_env,
            authorization=authorization,
            compatibility=compatibility,
        )
    return routes


def _configured_value(entry: dict[str, Any], value_key: str, env_key: str) -> Any:
    env_name = entry.get(env_key)
    if isinstance(env_name, str) and env_name:
        value = os.environ.get(env_name)
        if value:
            return value
    return entry.get(value_key)


def protocol_models(routes: dict[str, ModelRoute]) -> tuple[set[str], set[str]]:
    responses = {route.id for route in routes.values() if route.mode == "responses"}
    chat = {route.id for route in routes.values() if route.mode == "chat"}
    return responses, chat
