"""Plan 06 checks that do not need PostgreSQL."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent_platform.core.settings import Settings
from agent_platform.services.api_key_crypto import (
    generate_api_key,
    hash_api_key_secret,
    parse_api_key,
    replacement_expiry,
    secrets_match,
)
from agent_platform.services.graph_registry import GraphRegistry
from agent_platform.services.redaction import (
    is_sensitive_key,
    redact_text,
    sanitize,
    strip_client_secrets,
)
from agent_platform.services.run_manager import RunManager, decide_orphan_outcome
from agent_platform.services.tenant_context import tenant_user_id
from agent_platform.services.tenant_store import TenantStore


def test_api_key_format_has_prefix_lookup_and_256_bits():
    pepper = "test-pepper-value"
    generated = [generate_api_key(pepper=pepper) for _ in range(8)]
    plaintexts = [item.plaintext for item in generated]
    assert len(set(plaintexts)) == len(plaintexts)
    for item in generated:
        assert item.plaintext.startswith("aghub_")
        parsed = parse_api_key(item.plaintext, lookup_prefix_length=8)
        assert parsed is not None
        assert parsed.key_prefix == item.key_prefix
        assert len(parsed.key_prefix) == 8
        assert len(parsed.secret) >= 43
        assert item.plaintext not in item.secret_hash
        assert item.secret_hash == hash_api_key_secret(parsed.secret, pepper)
        assert secrets_match(item.secret_hash, parsed.secret, pepper)
        assert secrets_match(item.secret_hash, parsed.secret + "x", pepper) is False


def test_hash_comparison_uses_compare_digest():
    pepper = "test-pepper-value"
    issued = generate_api_key(pepper=pepper)
    with patch("agent_platform.services.api_key_crypto.hmac.compare_digest", return_value=True) as compared:
        assert secrets_match(issued.secret_hash, "anything", pepper) is True
    compared.assert_called()


def test_redaction_removes_keys_and_payloads():
    raw = "Authorization: Bearer aghub_abcd1234SECRETVALUE"
    assert "SECRETVALUE" not in redact_text(raw)
    cleaned = sanitize(
        {
            "api_key": "aghub_abcd1234SECRETVALUE",
            "authorization": "Bearer aghub_abcd1234SECRETVALUE",
            "password": "hunter2",
            "content_type": "image/png",
            "input_tokens": 12,
            "output_tokens": 4,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 1,
            "note": "ok",
        }
    )
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["authorization"] == "[redacted]"
    assert cleaned["password"] == "[redacted]"
    assert cleaned["content_type"] == "image/png"
    assert cleaned["input_tokens"] == 12
    assert cleaned["output_tokens"] == 4
    assert cleaned["cache_read_input_tokens"] == 3
    assert cleaned["cache_creation_input_tokens"] == 1
    assert cleaned["note"] == "ok"


def test_redaction_allowlist_preserves_telemetry_and_denies_credentials():
    payload = {
        "openai_api_key": "sk-secretvalue1234",
        "anthropic_api_key": "sk-ant-secret",
        "groq_api_key": "gsk-secret",
        "aws_secret_access_key": "wJalrXUtnFEMI",
        "aws_access_key_id": "AKIASECRETKEY",
        "private_key": "private-key-material",
        "session_token": "session-secret",
        "refresh_token": "refresh-secret",
        "db_password": "hunter2",
        "client_secret": "client-secret",
        "cookie": "sid=abc12345",
        "authorization": "Bearer aghub_abcd1234SECRETVALUE",
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
        "cache_read_input_tokens": 1,
        "cache_creation_input_tokens": 2,
        "cache_write_tokens": 8,
        "content_type": "application/pdf",
        "request_id": "req-1",
        "thread_id": "thread-1",
    }
    cleaned = sanitize(payload)
    for key in (
        "openai_api_key",
        "anthropic_api_key",
        "groq_api_key",
        "aws_secret_access_key",
        "aws_access_key_id",
        "private_key",
        "session_token",
        "refresh_token",
        "db_password",
        "client_secret",
        "cookie",
        "authorization",
    ):
        assert cleaned[key] == "[redacted]"
        assert is_sensitive_key(key)
    assert cleaned["input_tokens"] == 3
    assert cleaned["output_tokens"] == 4
    assert cleaned["total_tokens"] == 7
    assert cleaned["cache_read_input_tokens"] == 1
    assert cleaned["cache_creation_input_tokens"] == 2
    assert cleaned["cache_write_tokens"] == 8
    assert cleaned["content_type"] == "application/pdf"
    assert cleaned["request_id"] == "req-1"
    assert cleaned["thread_id"] == "thread-1"
    assert not is_sensitive_key("input_tokens")
    assert not is_sensitive_key("content_type")
    stripped = strip_client_secrets(payload)
    assert "openai_api_key" not in stripped
    assert "cookie" not in stripped
    assert stripped["input_tokens"] == 3
    assert stripped["content_type"] == "application/pdf"
    redacted = redact_text("Cookie: sid=abc12345 Authorization: Bearer sk-secretvalue1234")
    assert "abc12345" not in redacted
    assert "secretvalue1234" not in redacted


def test_production_settings_reject_disabled_auth():
    with pytest.raises(ValidationError, match="AUTH_MODE=disabled"):
        Settings(
            ENVIRONMENT="production",
            DATABASE_URI="postgresql://agent_platform:production-password@localhost:5432/agent_platform",
            AUTH_MODE="disabled",
            _env_file=None,
        )


def test_thread_locks_are_reclaimed_and_waiters_keep_the_same_lock():
    async def scenario() -> None:
        manager = RunManager(
            session_factory=None,  # type: ignore[arg-type]
            registry=GraphRegistry(),
            max_concurrent_runs=2,
        )
        thread_ids = [uuid.uuid4() for _ in range(300)]

        async def use(thread_id: uuid.UUID) -> None:
            async with manager._thread_guard(thread_id):
                await asyncio.sleep(0)

        await asyncio.gather(*(use(thread_id) for thread_id in thread_ids))
        assert manager._thread_locks == {}

        shared = uuid.uuid4()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            async with manager._thread_guard(shared):
                entered.set()
                await release.wait()

        async def waiter() -> None:
            await entered.wait()
            async with manager._thread_guard(shared):
                return

        first = asyncio.create_task(hold())
        await entered.wait()
        second = asyncio.create_task(waiter())
        for _ in range(20):
            if shared in manager._thread_locks and manager._thread_locks[shared].refs == 2:
                break
            await asyncio.sleep(0)
        assert manager._thread_locks[shared].refs == 2
        release.set()
        await first
        await second
        assert shared not in manager._thread_locks

    asyncio.run(scenario())


def test_api_key_secrets_round_trip_without_symbol_collisions():
    pepper = "test-pepper-value"
    issued = [generate_api_key(pepper=pepper) for _ in range(40)]
    plaintexts = [item.plaintext for item in issued]
    assert len(set(plaintexts)) == len(plaintexts)
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
    for item in issued:
        parsed = parse_api_key(item.plaintext, lookup_prefix_length=8)
        assert parsed is not None
        assert f"aghub_{parsed.key_prefix}{parsed.secret}" == item.plaintext
        assert set(parsed.secret) <= alphabet
        assert "-" not in parsed.secret and "_" not in parsed.secret
        assert item.plaintext not in item.secret_hash
        again = parse_api_key(item.plaintext, lookup_prefix_length=8)
        assert again == parsed


def test_configured_prefix_controls_generation_and_parsing():
    pepper = "test-pepper-value"
    issued = generate_api_key(pepper=pepper, platform_prefix="crop")
    assert issued.plaintext.startswith("crop_")
    assert parse_api_key(issued.plaintext, lookup_prefix_length=8, platform_prefix="crop")
    assert (
        parse_api_key(issued.plaintext, lookup_prefix_length=8, platform_prefix="aghub")
        is None
    )
    with pytest.raises(ValidationError, match="API_KEY_PREFIX"):
        Settings(
            ENVIRONMENT="test",
            DATABASE_URI="postgresql://agent_platform:agent_platform@localhost:5432/agent_platform",
            API_KEY_PREFIX="AgHub",
            _env_file=None,
        )


def test_expired_rotation_does_not_copy_the_old_expiry():
    now = datetime(2026, 9, 25, tzinfo=UTC)
    expired = now - timedelta(days=2)
    future = now + timedelta(days=9)
    explicit = now + timedelta(days=3)
    assert replacement_expiry(
        current=expired, now=now, explicit=None, explicit_set=False
    ) is None
    assert (
        replacement_expiry(current=future, now=now, explicit=None, explicit_set=False)
        == future
    )
    assert (
        replacement_expiry(
            current=expired, now=now, explicit=explicit, explicit_set=True
        )
        == explicit
    )
    assert replacement_expiry(
        current=None, now=now, explicit=None, explicit_set=False
    ) is None


def test_restart_decision_keeps_a_completed_checkpoint():
    completed = decide_orphan_outcome(
        checkpoint="completed",
        intent={"intent": "interrupted", "reason": "process_restart"},
        fallback_reason="process_restart",
    )
    assert completed.status == "completed"
    assert completed.graph_succeeded is True
    unknown = decide_orphan_outcome(
        checkpoint="unknown",
        intent=None,
        fallback_reason="process_restart",
    )
    assert unknown.status == "interrupted"
    assert unknown.error_code == "process_restart"
    cancelled = decide_orphan_outcome(
        checkpoint="unknown",
        intent={
            "intent": "cancelled",
            "reason": "cancelled",
            "event_type": "end",
            "event_payload": {"status": "cancelled"},
        },
        fallback_reason="process_restart",
    )
    assert cancelled.status == "cancelled"
    paused = decide_orphan_outcome(
        checkpoint="interrupted",
        intent=None,
        fallback_reason="process_restart",
    )
    assert paused.status == "interrupted"
    assert paused.error_code == "graph_interrupt"


def test_tenant_store_rejects_a_foreign_user_namespace():
    class _Inner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def get(self, namespace, key, **kwargs):
            self.calls.append(tuple(namespace))
            return "ok"

    inner = _Inner()
    store = TenantStore(inner)
    owner = str(uuid.uuid4())
    foreign = str(uuid.uuid4())
    token = tenant_user_id.set(owner)
    try:
        assert store.get((owner, "notes"), "draft") == "ok"
        assert store.get(("notes",), "draft") == "ok"
        with pytest.raises(PermissionError, match="another tenant"):
            store.get((foreign, "notes"), "draft")
    finally:
        tenant_user_id.reset(token)
    assert inner.calls == [(owner, "notes"), (owner, "notes")]
