import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))
from tools.protocol_models import load_protocol_models


def test_load_protocol_models_reads_mode_sets(tmp_path: Path) -> None:
    config = tmp_path / "models.yaml"
    config.write_text(
        """
models:
  - id: responses-model-a
    mode: responses
    upstream_model: upstream-responses-a
    api_base: https://provider.example/v1
  - id: chat-model-a
    mode: chat
    upstream_model: upstream-chat-a
    api_base: https://provider.example/v1
  - id: messages-model-a
    mode: messages
    upstream_model: upstream-messages-a
    api_base: https://provider.example
    codex_responses: true
""".lstrip(),
        encoding="utf-8",
    )

    responses, chat, messages, codex_responses = load_protocol_models(str(config))

    assert responses == {"responses-model-a"}
    assert chat == {"chat-model-a"}
    assert messages == {"messages-model-a"}
    assert codex_responses == {"responses-model-a", "messages-model-a"}


def test_load_protocol_models_rejects_overlap(tmp_path: Path) -> None:
    config = tmp_path / "models.yaml"
    config.write_text(
        """
models:
  - id: shared
    mode: responses
    upstream_model: upstream-a
    api_base: https://provider.example/v1
  - id: shared
    mode: chat
    upstream_model: upstream-a
    api_base: https://provider.example/v1
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate model route"):
        load_protocol_models(str(config))
