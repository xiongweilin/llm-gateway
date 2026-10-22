from __future__ import annotations

import io
import logging

from llm_gateway.secure_logging import (
    RedactingFormatter,
    install_log_redaction,
    redact_secrets,
)


def test_scrubs_common_oauth_and_provider_secret_formats():
    sample = (
        'Authorization: Bearer bearer_value_123 '
        '"refresh_token": "refresh_value_123", '
        'access_token=access_value_123 '
        'https://example.invalid/oauth?code=auth_code_123&api_key=api_key_value_123 '
        'x-api-key: provider_value_123 '
    )
    sanitized = redact_secrets(sample)
    for secret in (
        "bearer_value_123",
        "refresh_value_123",
        "access_value_123",
        "auth_code_123",
        "api_key_value_123",
        "provider_value_123",
    ):
        assert secret not in sanitized
    assert "example.invalid" in sanitized


def test_redacts_known_token_prefixes_and_jwt():
    text = (
        "ghp_AAAAAAAAAAAAAAAAAAAAAA "
        "sk-abcdefghijklmnopqrstuvwxyz "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEFGHIJK012345"
    )
    sanitized = redact_secrets(text)
    assert "ghp_" not in sanitized
    assert "sk-abc" not in sanitized
    assert "eyJhbG" not in sanitized


def test_formatter_redacts_exception_text_and_preserves_safe_details():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(logging.Formatter("%(levelname)s %(message)s")))
    logger = logging.Logger("redaction-test", level=logging.INFO)
    logger.addHandler(handler)
    logger.error("backend raised: %s", ValueError("Authorization: Bearer danger_secret"))
    output = stream.getvalue()
    assert "danger_secret" not in output
    assert "backend raised" in output


def test_install_is_idempotent_and_preserves_original_handler():
    root = logging.getLogger()
    original = [(handler, handler.formatter) for handler in root.handlers]
    try:
        install_log_redaction()
        install_log_redaction()
        assert all(isinstance(handler.formatter, RedactingFormatter) for handler in root.handlers)
        assert all(not isinstance(handler.formatter.base, RedactingFormatter) for handler in root.handlers)
    finally:
        for handler, formatter in original:
            handler.setFormatter(formatter)
