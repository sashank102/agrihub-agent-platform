"""Plan 06 checks that do not need PostgreSQL."""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent_platform.core.settings import Settings
from agent_platform.services.api_key_crypto import (
    generate_api_key,
    hash_api_key_secret,
    parse_api_key,
    secrets_match,
)
from agent_platform.services.graph_registry import GraphRegistry
from agent_platform.services.redaction import redact_text, sanitize
from agent_platform.services.run_manager import RunManager


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
            "prompt": "private prompt",
            "note": "ok",
        }
    )
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["prompt"] == "[redacted]"
    assert cleaned["note"] == "ok"


def test_production_settings_reject_disabled_auth():
    with pytest.raises(ValidationError, match="AUTH_MODE=disabled"):
        Settings(
            ENVIRONMENT="production",
            DATABASE_URI="postgresql://agent_platform:agent_platform@localhost:5432/agent_platform",
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
