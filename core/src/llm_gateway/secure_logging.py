"""Defense-in-depth redaction for gateway logs.

Do not log entire requests or headers.  This formatter provides a fallback for
unexpected secrets in exception messages and structured log fields.
"""
from __future__ import annotations

import logging
import re


_SECRET_PAIR = re.compile(
    r"""(?i)(["']?(?:access_token|refresh_token|id_token|api[_-]?key|
    x-api-key|client_secret|authorization|password|secret|code_verifier|
    github_token)["']?\s*[:=]\s*)(["']?)([^\s"',&}]+)""",
    re.VERBOSE,
)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+")
_URL_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|refresh_token|api_key|key|"
    r"client_secret|code|signature)=)[^&#\s]+"
)
_KNOWN_TOKEN = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"sk-(?:ant-)?[A-Za-z0-9_-]{12,})\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def redact_secrets(message: str) -> str:
    value = _BEARER.sub(r"\1[REDACTED]", message)
    value = _URL_SECRET.sub(r"\1[REDACTED]", value)
    value = _SECRET_PAIR.sub(r"\1[REDACTED]", value)
    value = _KNOWN_TOKEN.sub("[REDACTED]", value)
    return _JWT.sub("[REDACTED]", value)


class RedactingFormatter(logging.Formatter):
    def __init__(self, base: logging.Formatter) -> None:
        super().__init__()
        self.base = base

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(self.base.format(record))


def install_log_redaction() -> None:
    """Apply redaction to all default service stderr handlers."""
    root = logging.getLogger()
    for handler in root.handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(
                RedactingFormatter(handler.formatter or logging.Formatter())
            )
