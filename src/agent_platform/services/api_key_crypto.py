"""Generate and verify high-entropy API keys. Plaintext is never stored.

The platform prefix comes from ``API_KEY_PREFIX``. It is not read from the
request. Keys issued with the default prefix ``aghub`` keep working. Changing
the prefix does not rewrite stored hashes; those keys must be reissued.
"""

import hashlib
import hmac
import math
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

_SECRET_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_PREFIX_RE = re.compile(r"^[a-z]{2,32}$")


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


def validate_platform_prefix(prefix: str) -> str:
    """Return a lowercase letter prefix suitable for key generation and parsing."""
    if not isinstance(prefix, str) or _PREFIX_RE.fullmatch(prefix) is None:
        raise ValueError("API_KEY_PREFIX must be 2-32 lowercase letters")
    return prefix


def platform_key_label(prefix: str) -> str:
    """Return the ``prefix_`` label prepended to every issued key."""
    return f"{validate_platform_prefix(prefix)}_"


def generate_api_key(
    *,
    pepper: str,
    lookup_prefix_length: int = 8,
    secret_bytes: int = 32,
    platform_prefix: str = "aghub",
) -> GeneratedApiKey:
    """Build a prefixed key with at least 256 bits of random secret material."""
    if secret_bytes < 32:
        raise ValueError("API key secrets must contain at least 256 bits")
    if lookup_prefix_length < 4:
        raise ValueError("API key lookup prefixes must be at least 4 characters")
    label = platform_key_label(platform_prefix)
    lookup = _token(lookup_prefix_length)
    secret = _token_bytes(secret_bytes)
    plaintext = f"{label}{lookup}{secret}"
    return GeneratedApiKey(
        plaintext=plaintext,
        key_prefix=lookup,
        secret_hash=hash_api_key_secret(secret, pepper),
    )


def parse_api_key(
    token: str,
    *,
    lookup_prefix_length: int,
    platform_prefix: str = "aghub",
) -> ParsedApiKey | None:
    """Split a presented key. Malformed values produce no secret material.

    ``platform_prefix`` is the server setting. Callers must not pass a prefix
    taken from the request.
    """
    try:
        label = platform_key_label(platform_prefix)
    except ValueError:
        return None
    if not token.startswith(label):
        return None
    body = token[len(label) :]
    if len(body) <= lookup_prefix_length:
        return None
    prefix = body[:lookup_prefix_length]
    secret = body[lookup_prefix_length:]
    if not prefix or not secret:
        return None
    if not _is_token_text(prefix) or not _is_token_text(secret):
        return None
    return ParsedApiKey(key_prefix=prefix, secret=secret)


def replacement_expiry(
    *,
    current: datetime | None,
    now: datetime,
    explicit: datetime | None,
    explicit_set: bool,
) -> datetime | None:
    """Choose the expiry stored on a rotated key.

    An explicit replacement expiry always wins. An expiry that is already in
    the past is never copied. When the key being rotated is expired and the
    caller does not supply a new expiry, the replacement does not expire.
    """
    if explicit_set:
        return explicit
    if current is None:
        return None
    current_utc = current if current.tzinfo is not None else current.replace(tzinfo=UTC)
    if current_utc <= now:
        return None
    return current


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
    """Return ``length`` characters from the secret alphabet."""
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(length))


def _token_bytes(nbytes: int) -> str:
    """Encode ``nbytes`` of entropy without mapping distinct symbols together.

    Each character is drawn from a 62-symbol alphabet, so the string is long
    enough that its entropy is at least ``nbytes * 8`` bits. Characters are
    never produced by replacing ``-`` or ``_`` with another alphabet symbol.
    """
    bits = nbytes * 8
    length = math.ceil(bits / math.log2(len(_SECRET_ALPHABET)))
    return _token(length)


def _is_token_text(value: str) -> bool:
    return bool(value) and all(character in _SECRET_ALPHABET for character in value)
