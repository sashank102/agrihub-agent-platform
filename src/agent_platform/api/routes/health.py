"""Process liveness and dependency readiness routes."""

import uuid

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from agent_platform.db.session import session_scope

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Report process liveness without probing dependencies."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    """Probe SQLAlchemy and both LangGraph PostgreSQL resources."""
    if not getattr(request.app.state, "ready", False):
        raise HTTPException(status_code=503, detail="service is not ready")
    try:
        async with session_scope(request.app.state.session_factory) as session:
            await session.execute(text("SELECT 1"))
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
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="database persistence is not ready",
        ) from exc
    return {"status": "ready"}
