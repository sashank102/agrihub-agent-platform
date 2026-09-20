"""Async persistence operations for users."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import User


class UserRepository:
    """Persist users without owning the surrounding transaction."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def create(
        self,
        *,
        display_name: str,
        email: str | None = None,
        status: str = "active",
    ) -> User:
        """Add and flush a user."""
        user = User(display_name=display_name, email=email, status=status)
        self.session.add(user)
        await self.session.flush()
        return user

    async def get(self, user_id: uuid.UUID) -> User | None:
        """Return a user by primary key."""
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Return a user by exact email."""
        return await self.session.scalar(select(User).where(User.email == email))

    async def update_profile(
        self,
        user: User,
        *,
        display_name: str | None = None,
        email: str | None = None,
    ) -> User:
        """Update mutable profile fields and flush."""
        if display_name is not None:
            user.display_name = display_name
        if email is not None:
            user.email = email
        await self.session.flush()
        return user

    async def disable(self, user: User) -> User:
        """Soft-disable a user while preserving owned data."""
        user.status = "disabled"
        user.disabled_at = datetime.now(UTC)
        await self.session.flush()
        return user

    async def soft_delete(self, user: User) -> User:
        """Mark a user deleted; hard deletion is an explicit retention action."""
        user.status = "deleted"
        user.deleted_at = datetime.now(UTC)
        await self.session.flush()
        return user
