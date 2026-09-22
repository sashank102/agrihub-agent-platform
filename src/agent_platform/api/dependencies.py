"""FastAPI dependencies for application-owned resources."""

import uuid
from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.core.settings import Settings
from agent_platform.db.session import session_scope

_READY_ATTRIBUTES = (
    "session_factory",
    "graph_registry",
    "persistence",
    "run_manager",
)


async def require_ready(request: Request) -> None:
    """Reject business routes until lifespan initialization has finished."""
    state = request.app.state
    if not getattr(state, "ready", False):
        raise HTTPException(status_code=503, detail="service is not ready")
    if any(getattr(state, name, None) is None for name in _READY_ATTRIBUTES):
        raise HTTPException(status_code=503, detail="service is not ready")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Provide one transaction-scoped SQLAlchemy session."""
    async with session_scope(request.app.state.session_factory) as session:
        yield session


def get_runtime_settings(request: Request) -> Settings:
    """Return the settings captured when this app was created."""
    return request.app.state.settings


def get_development_principal(request: Request) -> uuid.UUID:
    """Return the fixed local principal; authentication arrives in Plan 06."""
    return request.app.state.settings.DEVELOPMENT_USER_ID
