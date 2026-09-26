"""Remove secrets and raw file bodies from logs, errors, and audit metadata.

An allowlist preserves telemetry such as ``content_type``, ``input_tokens``,
``output_tokens``, ``total_tokens``, and cache read/write token counts. Exact
provider key names, ``*_api_key``, credential-bearing ``*_token``,
``*_access_key``, ``*_secret``, ``*_password``, ``private_key``, and
authorization or cookie fields are redacted.
"""

import re
from typing import Any

_SAFE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "content_type",
        "mime_type",
        "request_id",
        "thread_id",
        "run_id",
        "user_id",
        "status",
        "model",
        "filename",
        "name",
    }
)
_EXACT_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "password",
        "passwd",
        "pepper",
        "secret",
        "client_secret",
        "api_secret",
        "provider_secret",
        "private_key",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer_token",
        "session_token",
        "token",
        "file_data",
        "file_body",
        "raw_file",
        "image_data",
        "pdf_data",
        "data_base64",
        "openai_api_key",
        "anthropic_api_key",
        "groq_api_key",
        "google_api_key",
        "tavily_api_key",
    }
)
_SECRET_SUFFIXES = (
    "_api_key",
    "_access_key",
    "_access_key_id",
    "_secret",
    "_password",
    "_private_key",
    "_token",
)
_DEFAULT_KEY_MATERIAL = (
    r"aghub_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{8,}|"
    r"(?i:bearer\s+[A-Za-z0-9\-_\.]+)|"
    r"(?i:(?:authorization|cookie|set-cookie)\s*[:=]\s*\S{8,})"
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
        r"(?i:bearer\s+[A-Za-z0-9\-_\.]+)|"
        r"(?i:(?:authorization|cookie|set-cookie)\s*[:=]\s*\S{8,})"
    )


def is_sensitive_key(key: str) -> bool:
    """Return whether a mapping key carries a credential or raw file body.

    Token-count telemetry and ``content_type`` stay visible. A key that only
    ends in ``_tokens`` is a metric, not a credential-bearing ``*_token``.
    """
    normalized = key.lower().replace("-", "_")
    if normalized in _SAFE_KEYS or normalized.endswith("_tokens"):
        return False
    if normalized in _EXACT_SECRET_KEYS:
        return True
    if any(normalized.endswith(suffix) for suffix in _SECRET_SUFFIXES):
        return True
    return "_secret_" in f"_{normalized}_"


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
