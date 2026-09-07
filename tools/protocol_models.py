"""Load protocol model sets from the generated runtime configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def load_protocol_models(config_path: str) -> tuple[set[str], set[str]]:
    """Return ``(responses_models, chat_models)`` from a runtime YAML file."""
    path = Path(config_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("model_list"), list):
        raise ValueError(f"runtime config has no model_list: {path}")

    responses: set[str] = set()
    chat: set[str] = set()
    for entry in data["model_list"]:
        if not isinstance(entry, dict):
            raise ValueError(f"runtime model entry is not an object: {path}")
        model_name = entry.get("model_name")
        model_info = entry.get("model_info")
        mode = model_info.get("mode") if isinstance(model_info, dict) else None
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError(f"runtime model entry has no model_name: {path}")
        if mode == "responses":
            responses.add(model_name)
        elif mode == "chat":
            chat.add(model_name)
        else:
            raise ValueError(f"runtime model has unsupported mode {mode!r}: {model_name}")

    if responses & chat:
        overlap = ", ".join(sorted(responses & chat))
        raise ValueError(f"runtime model appears in both protocol sets: {overlap}")
    return responses, chat


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: protocol_models.py RUNTIME_CONFIG")
    responses, chat = load_protocol_models(sys.argv[1])
    print(json.dumps({"responses": sorted(responses), "chat": sorted(chat)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
