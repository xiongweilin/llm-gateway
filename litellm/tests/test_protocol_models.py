import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))
from tools.protocol_models import load_protocol_models


def test_load_protocol_models_reads_mode_sets(tmp_path: Path) -> None:
    config = tmp_path / "runtime.yaml"
    config.write_text(
        """
model_list:
  - model_name: responses-model-a
    model_info:
      mode: responses
  - model_name: chat-model-a
    model_info:
      mode: chat
""".lstrip(),
        encoding="utf-8",
    )

    responses, chat = load_protocol_models(str(config))

    assert responses == {"responses-model-a"}
    assert chat == {"chat-model-a"}


def test_load_protocol_models_rejects_overlap(tmp_path: Path) -> None:
    config = tmp_path / "runtime.yaml"
    config.write_text(
        """
model_list:
  - model_name: shared
    model_info:
      mode: responses
  - model_name: shared
    model_info:
      mode: chat
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="both protocol sets"):
        load_protocol_models(str(config))
