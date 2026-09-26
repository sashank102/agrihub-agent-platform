"""Remove secrets and raw file bodies from logs, errors, and audit metadata.

Field names are matched exactly. Telemetry such as ``content_type``,
``input_tokens``, ``output_tokens``, and cache token counts is preserved.
"""

import re
from typing import Any

_EXACT_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "proxy_authorization",
        "password",
        "passwd",
        "pepper",
        "secret",
        "client_secret",
        "api_secret",
        "provider_secret",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer_token",
        "session_token",
        "file_data",
        "file_body",
        "raw_file",
        "image_data",
        "pdf_data",
        "data_base64",
    }
)
_DEFAULT_KEY_MATERIAL = (
    r"aghub_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{8,}|(?i:bearer\s+[A-Za-z0-9\-_\.]+)"
)
_KEY_MATERIAL = re.compile(_DEFAULT_KEY_MATERIAL)
_DATA_URL = re.compile(
    r"data:(?:image|application)/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{16,}"
)


def configure_redaction(platform_prefix: str) -> None:
    """Match issued keys for the configured prefix in log text."""
    global _KEY_MATERIAL
    escaped = re.escape(platform_prefix)
    _KEY_MATERIAL = re.compile(
        rf"{escaped}_[A-Za-z0-9]{{8,}}|sk-[A-Za-z0-9]{{8,}}|"
        r"(?i:bearer\s+[A-Za-z0-9\-_\.]+)"
    )


def is_sensitive_key(key: str) -> bool:
    """Return whether a mapping key is an exact secret or file-body field."""
    normalized = key.lower().replace("-", "_")
    if normalized in _EXACT_SECRET_KEYS:
        return True
    return normalized.endswith("_secret") or normalized.endswith("_password")


def redact_text(value: str) -> str:
    """Replace recognizable secrets and inline file bodies inside a string."""
    redacted = _KEY_MATERIAL.sub("[redacted]", value)
    return _DATA_URL.sub("[redacted]", redacted)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe copy with secrets and raw file bodies removed."""
    if depth > 6:
        return "[redacted]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key)
            if is_sensitive_key(text):
                cleaned[text] = "[redacted]"
            else:
                cleaned[text] = sanitize(item, depth=depth + 1)
        return cleaned
    if isinstance(value, list | tuple):
        return [sanitize(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, int | float | bool):
        return value
    return redact_text(str(value))


def strip_client_secrets(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop client-supplied credentials before they reach graph configuration."""
    cleaned: dict[str, Any] = {}
    for key, item in mapping.items():
        if is_sensitive_key(str(key)):
            continue
        if isinstance(item, dict):
            cleaned[key] = strip_client_secrets(item)
        else:
            cleaned[key] = item
    return cleaned
