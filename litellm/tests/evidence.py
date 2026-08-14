# -*- coding: utf-8 -*-
"""收集 conformance 证据（合成 canary 内容，无真实凭据/正文），会话结束时导出 JSON。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EVIDENCE: dict[str, Any] = {}


def record(item: str, status: str, detail: Any) -> None:
    EVIDENCE[item] = {"status": status, "detail": detail}


def dump_evidence(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8")
