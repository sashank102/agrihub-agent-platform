"""Async persistence operations for artifacts."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.models import Artifact, Run, Thread


class ArtifactRepository:
    """Persist small outputs and external references under user ownership."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to a caller-owned session."""
        self.session = session

    async def create(
        self,
        *,
        owner_user_id: uuid.UUID,
        thread_id: uuid.UUID,
        kind: str,
        media_type: str,
        run_id: uuid.UUID | None = None,
        text_content: str | None = None,
        content: dict[str, Any] | None = None,
        external_uri: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """Add an artifact for an owned thread and optional matching run."""
        owned_thread_id = await self.session.scalar(
            select(Thread.id).where(
                Thread.id == thread_id,
                Thread.owner_user_id == owner_user_id,
            )
        )
        if owned_thread_id is None:
            raise LookupError("thread is not owned by the user")
        if run_id is not None:
            matching_run_id = await self.session.scalar(
                select(Run.id).where(
                    Run.id == run_id,
                    Run.thread_id == thread_id,
                )
            )
            if matching_run_id is None:
                raise LookupError("run does not belong to the artifact thread")

        artifact = Artifact(
            owner_user_id=owner_user_id,
            thread_id=thread_id,
            run_id=run_id,
            kind=kind,
            media_type=media_type,
            text_content=text_content,
            content=content,
            external_uri=external_uri,
            metadata_=metadata or {},
        )
        self.session.add(artifact)
        await self.session.flush()
        return artifact

    async def get_for_owner(
        self,
        artifact_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> Artifact | None:
        """Return an artifact only when the owner matches."""
        return await self.session.scalar(
            select(Artifact).where(
                Artifact.id == artifact_id,
                Artifact.owner_user_id == owner_user_id,
            )
        )

    async def list_for_thread(
        self,
        thread_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> list[Artifact]:
        """List artifacts for one owned thread."""
        statement = (
            select(Artifact)
            .where(
                Artifact.thread_id == thread_id,
                Artifact.owner_user_id == owner_user_id,
            )
            .order_by(Artifact.created_at, Artifact.id)
        )
        return list((await self.session.scalars(statement)).all())

    async def update(
        self,
        artifact: Artifact,
        *,
        text_content: str | None = None,
        content: dict[str, Any] | None = None,
        external_uri: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        """Update mutable artifact content and flush."""
        if text_content is not None:
            artifact.text_content = text_content
        if content is not None:
            artifact.content = content
        if external_uri is not None:
            artifact.external_uri = external_uri
        if metadata is not None:
            artifact.metadata_ = metadata
        await self.session.flush()
        return artifact
