"""Generate and verify high-entropy API keys. Plaintext is never stored."""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

KEY_PLATFORM_PREFIX = "aghub_"


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    """One new key. ``plaintext`` is shown a single time by the caller."""

    plaintext: str
    key_prefix: str
    secret_hash: str


@dataclass(frozen=True, slots=True)
class ParsedApiKey:
    """The visible lookup prefix and the secret half of a presented key."""

    key_prefix: str
    secret: str


def generate_api_key(
    *,
    pepper: str,
    lookup_prefix_length: int = 8,
    secret_bytes: int = 32,
) -> GeneratedApiKey:
    """Build a prefixed key with at least 256 bits of random secret material."""
    if secret_bytes < 32:
        raise ValueError("API key secrets must contain at least 256 bits")
    if lookup_prefix_length < 4:
        raise ValueError("API key lookup prefixes must be at least 4 characters")
    lookup = _token(lookup_prefix_length)
    secret = _token_bytes(secret_bytes)
    plaintext = f"{KEY_PLATFORM_PREFIX}{lookup}{secret}"
    return GeneratedApiKey(
        plaintext=plaintext,
        key_prefix=lookup,
        secret_hash=hash_api_key_secret(secret, pepper),
    )


def parse_api_key(token: str, *, lookup_prefix_length: int) -> ParsedApiKey | None:
    """Split a presented key. Malformed values produce no secret material."""
    if not token.startswith(KEY_PLATFORM_PREFIX):
        return None
    body = token[len(KEY_PLATFORM_PREFIX) :]
    if len(body) <= lookup_prefix_length:
        return None
    prefix = body[:lookup_prefix_length]
    secret = body[lookup_prefix_length:]
    if not prefix or not secret:
        return None
    if not _is_token_text(prefix) or not _is_token_text(secret):
        return None
    return ParsedApiKey(key_prefix=prefix, secret=secret)


def hash_api_key_secret(secret: str, pepper: str) -> str:
    """HMAC-SHA256 the secret with the server pepper. This is not reversible."""
    return hmac.new(
        pepper.encode("utf-8"),
        secret.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def secrets_match(stored_hash: str, presented_secret: str, pepper: str) -> bool:
    """Compare a stored HMAC with a presented secret in constant time."""
    candidate = hash_api_key_secret(presented_secret, pepper)
    if len(stored_hash) != len(candidate):
        return hmac.compare_digest(candidate, candidate) and False
    return hmac.compare_digest(stored_hash, candidate)


def key_is_usable(
    *,
    revoked_at: datetime | None,
    expires_at: datetime | None,
    now: datetime,
) -> bool:
    """Return whether a matched key is still active at ``now``."""
    if revoked_at is not None:
        return False
    if expires_at is not None and expires_at <= now:
        return False
    return True


def new_key_id() -> uuid.UUID:
    """Return a new primary key for an API-key row."""
    return uuid.uuid4()


def _token(length: int) -> str:
    """Return ``length`` characters from a URL-safe alphabet without separators."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _token_bytes(nbytes: int) -> str:
    """Return random text containing at least ``nbytes`` of entropy."""
    # token_urlsafe uses 6 bits per character after padding is stripped.
    raw = secrets.token_urlsafe(nbytes)
    return raw.replace("-", "A").replace("_", "B")


def _is_token_text(value: str) -> bool:
    return value.isalnum()
