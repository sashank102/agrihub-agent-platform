"""Process liveness and dependency readiness routes."""

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from agent_platform.db.session import session_scope
from agent_platform.process_lock import (
    API_PROCESS_ADVISORY_LOCK_CLASSID,
    API_PROCESS_ADVISORY_LOCK_OBJID,
)

router = APIRouter()


def _project_root() -> Path:
    """Find the directory that contains alembic.ini."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "alembic.ini").is_file():
            return parent
    raise RuntimeError("alembic.ini was not found")


def expected_alembic_head() -> str:
    """Return the head revision shipped with this build."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(_project_root() / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise RuntimeError("alembic has no head revision")
    return head


@router.get("/health")
async def health() -> dict[str, str]:
    """Report process liveness without probing dependencies."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, object]:
    """Require PostgreSQL, schema revision, lock, graph, and run manager."""
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(status_code=503, detail="service is not ready")
    manager = getattr(request.app.state, "run_manager", None)
    registry = getattr(request.app.state, "graph_registry", None)
    settings = request.app.state.settings
    if manager is None or registry is None:
        raise HTTPException(status_code=503, detail="service is not ready")
    if getattr(manager, "_closed", True) or getattr(manager, "_shutting_down", True):
        raise HTTPException(status_code=503, detail="run manager is not ready")
    if registry.resolve(settings.DEVELOPMENT_GRAPH_ID) is None:
        raise HTTPException(status_code=503, detail="graph registry is not ready")
    checks: dict[str, object] = {}
    try:
        expected = expected_alembic_head()
        async with session_scope(request.app.state.session_factory) as session:
            await session.execute(text("SELECT 1"))
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
            lock_held = await session.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' "
                    "AND classid = :classid AND objid = :objid AND granted"
                    ")"
                ),
                {
                    "classid": API_PROCESS_ADVISORY_LOCK_CLASSID,
                    "objid": API_PROCESS_ADVISORY_LOCK_OBJID,
                },
            )
        checks["alembic_revision"] = revision
        if revision != expected:
            raise HTTPException(
                status_code=503,
                detail="database revision does not match this build",
            )
        if lock_held is not True:
            raise HTTPException(
                status_code=503,
                detail="process advisory lock is not held",
            )
        probe_id = f"ready-{uuid.uuid4()}"
        await request.app.state.persistence.checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": probe_id,
                    "checkpoint_ns": "",
                }
            }
        )
        await request.app.state.persistence.store.asearch((), limit=1)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="database persistence is not ready",
        ) from exc
    return {
        "status": "ready",
        "alembic_revision": checks["alembic_revision"],
        "process_lock": True,
        "graph_registry": True,
        "checkpointer": True,
        "store": True,
        "run_manager": True,
    }
