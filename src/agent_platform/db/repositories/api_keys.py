"""Persistence for hashed API keys."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import ApiKey


class ApiKeyRepository:
    """Store key prefixes and hashes. Callers never pass a full key to queries."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        key_prefix: str,
        secret_hash: str,
        label: str | None = None,
        expires_at: datetime | None = None,
        key_id: uuid.UUID | None = None,
    ) -> ApiKey:
        """Insert one hashed key."""
        row = ApiKey(
            id=key_id or uuid.uuid4(),
            user_id=user_id,
            key_prefix=key_prefix,
            secret_hash=secret_hash,
            label=label,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, key_id: uuid.UUID) -> ApiKey | None:
        """Return one key row by id."""
        return await self.session.get(ApiKey, key_id)

    async def list_for_user(self, user_id: uuid.UUID) -> list[ApiKey]:
        """Return key metadata for one user, newest first."""
        statement = (
            select(ApiKey)
            .where(ApiKey.user_id == user_id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id)
        )
        return list((await self.session.scalars(statement)).all())

    async def list_by_prefix(self, key_prefix: str) -> list[ApiKey]:
        """Return every row with this lookup prefix so collisions can be compared."""
        statement = select(ApiKey).where(ApiKey.key_prefix == key_prefix)
        return list((await self.session.scalars(statement)).all())

    async def revoke(self, key: ApiKey, *, revoked_at: datetime) -> ApiKey:
        """Record revocation without deleting the hash."""
        key.revoked_at = revoked_at
        await self.session.flush()
        return key

    async def touch_last_used(self, key: ApiKey, *, used_at: datetime) -> ApiKey:
        """Update the throttled last-used timestamp."""
        key.last_used_at = used_at
        await self.session.flush()
        return key
