"""Remove secrets and payloads from logs, errors, and audit metadata."""

import re
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "pepper",
    "secret",
    "token",
    "prompt",
    "messages",
    "file",
    "payload",
    "content",
    "input",
)
_KEY_MATERIAL = re.compile(
    r"aghub_[A-Za-z0-9\-_]{8,}|sk-[A-Za-z0-9]{8,}|(?i:bearer\s+[A-Za-z0-9\-_\.]+)"
)


def is_sensitive_key(key: str) -> bool:
    """Return whether a mapping key should be removed from recorded metadata."""
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact_text(value: str) -> str:
    """Replace recognizable secrets inside a log or error string."""
    return _KEY_MATERIAL.sub("[redacted]", value)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe copy with secrets and bulky payloads removed."""
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
    """Drop client-supplied credentials before they reach graph state."""
    cleaned: dict[str, Any] = {}
    for key, item in mapping.items():
        if is_sensitive_key(str(key)):
            continue
        if isinstance(item, dict):
            cleaned[key] = strip_client_secrets(item)
        else:
            cleaned[key] = item
    return cleaned
