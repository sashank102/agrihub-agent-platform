"""Create browser-test users and write their keys without printing secrets."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_platform.core.settings import Settings
from agent_platform.db.session import create_platform_engine, create_session_factory
from agent_platform.services.accounts import AccountService


async def main() -> None:
    settings = Settings(_env_file=None)
    if settings.DATABASE_URI is None:
        raise RuntimeError("DATABASE_URI is required")
    engine = create_platform_engine(settings.DATABASE_URI)
    try:
        accounts = AccountService(create_session_factory(engine), settings)
        user_a = await accounts.create_user(display_name="Browser A")
        user_b = await accounts.create_user(display_name="Browser B")
        deleted = await accounts.create_user(display_name="Browser Deleted")
        active = await accounts.issue_api_key(user_id=user_a.id, label="browser-a")
        ephemeral = await accounts.issue_api_key(user_id=user_a.id, label="ephemeral")
        other = await accounts.issue_api_key(user_id=user_b.id, label="browser-b")
        revoked = await accounts.issue_api_key(user_id=user_a.id, label="revoked")
        expired = await accounts.issue_api_key(
            user_id=user_a.id,
            label="expired",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        removed = await accounts.issue_api_key(user_id=deleted.id, label="deleted")
        await accounts.revoke_api_key(revoked.id)
        await accounts.delete_user(deleted.id)
        payload = {
            "user_a": active.plaintext,
            "user_b": other.plaintext,
            "ephemeral": ephemeral.plaintext,
            "ephemeral_id": str(ephemeral.id),
            "revoked": revoked.plaintext,
            "expired": expired.plaintext,
            "deleted": removed.plaintext,
        }
    finally:
        await engine.dispose()
    destination = Path(__file__).with_name(".keys.json")
    destination.write_text(json.dumps(payload))
    os.chmod(destination, 0o600)


if __name__ == "__main__":
    asyncio.run(main())
