"""Persist ``agrihub_study`` artifacts and release run resources under the run's identity."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from agent_platform.db.repositories import ArtifactRepository
from agent_platform.db.session import AsyncSessionFactory, session_scope
from agrihub.evidence_store import close_run

StudyArtifactSink = Callable[..., Awaitable[str | None]]


def study_artifact_sink(session_factory: AsyncSessionFactory) -> StudyArtifactSink:
    """Return a sink that writes JSON artifacts for the run in ``config``.

    ``user_id``, ``thread_id`` and ``run_id`` are read from
    ``configurable``, where the run manager writes the authenticated
    identity. Graph calls without that identity store nothing.
    """

    async def sink(
        config: dict[str, Any],
        *,
        kind: str,
        title: str,
        content: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        configurable = config.get("configurable") or {}
        try:
            owner_user_id = uuid.UUID(str(configurable["user_id"]))
            thread_id = uuid.UUID(str(configurable["thread_id"]))
            run_id = uuid.UUID(str(configurable["run_id"]))
        except (KeyError, ValueError):
            return None
        async with session_scope(session_factory) as session:
            artifact = await ArtifactRepository(session).create(
                owner_user_id=owner_user_id,
                thread_id=thread_id,
                run_id=run_id,
                kind=kind,
                media_type="application/json",
                content=content,
                metadata={"title": title, **(metadata or {})},
            )
            return str(artifact.id)

    return sink


async def close_study_store(run_id: uuid.UUID) -> None:
    """Close the run's evidence store if this process still holds it open."""
    await asyncio.to_thread(close_run, str(run_id))
