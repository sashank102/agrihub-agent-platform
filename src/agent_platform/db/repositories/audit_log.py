"""Append-only audit records."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.api.request_context import current_request_id
from agent_platform.db.models import AuditLog
from agent_platform.services.redaction import sanitize


class AuditLogRepository:
    """Insert audit rows. Updates and deletes are not provided."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def append(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str,
        actor_user_id: uuid.UUID | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Flush one immutable audit record with secrets removed."""
        row = AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id if request_id is not None else current_request_id(),
            metadata_=sanitize(metadata or {}),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_for_resource(
        self,
        resource_type: str,
        resource_id: str,
    ) -> list[AuditLog]:
        """Return audit rows for one resource in insertion order."""
        statement = (
            select(AuditLog)
            .where(
                AuditLog.resource_type == resource_type,
                AuditLog.resource_id == resource_id,
            )
            .order_by(AuditLog.created_at, AuditLog.id)
        )
        return list((await self.session.scalars(statement)).all())
