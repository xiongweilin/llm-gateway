"""Load protocol model sets from the gateway-owned route configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def load_protocol_models(config_path: str | Path) -> tuple[set[str], set[str]]:
    """Return ``(responses_models, chat_models)`` from the owned model source."""
    path = Path(config_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError(f"model config has no models list: {path}")

    responses: set[str] = set()
    chat: set[str] = set()
    seen: set[str] = set()
    for entry in data["models"]:
        if not isinstance(entry, dict):
            raise ValueError(f"model route is not an object: {path}")
        model_name = entry.get("id")
        mode = entry.get("mode")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError(f"model route has no id: {path}")
        if model_name in seen:
            raise ValueError(f"duplicate model route: {model_name}")
        seen.add(model_name)
        if mode == "responses":
            responses.add(model_name)
        elif mode == "chat":
            chat.add(model_name)
        else:
            raise ValueError(f"model route has unsupported mode {mode!r}: {model_name}")

    if responses & chat:
        overlap = ", ".join(sorted(responses & chat))
        raise ValueError(f"model appears in both protocol sets: {overlap}")
    return responses, chat


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: protocol_models.py MODEL_CONFIG")
    responses, chat = load_protocol_models(sys.argv[1])
    print(json.dumps({"responses": sorted(responses), "chat": sorted(chat)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
