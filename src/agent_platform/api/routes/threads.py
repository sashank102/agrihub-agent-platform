"""Thin Agent Protocol thread routes."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.api.dependencies import (
    get_development_principal,
    get_session,
)
from agent_platform.api.schemas import (
    ThreadCreateRequest,
    ThreadResponse,
    ThreadSearchRequest,
    ThreadStateResponse,
)
from agent_platform.services.thread_service import ThreadService

router = APIRouter(prefix="/threads")
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
PrincipalDependency = Annotated[uuid.UUID, Depends(get_development_principal)]


def _service(
    request: Request,
    session: AsyncSession,
    principal: uuid.UUID,
) -> ThreadService:
    return ThreadService(
        session=session,
        registry=request.app.state.graph_registry,
        owner_user_id=principal,
        default_graph_id=request.app.state.settings.DEVELOPMENT_GRAPH_ID,
    )


@router.post("", response_model=ThreadResponse)
async def create_thread(
    body: ThreadCreateRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ThreadResponse:
    """Create one local-principal thread."""
    try:
        return await _service(request, session, principal).create(body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="assistant not found") from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="thread already exists") from exc


@router.post("/search", response_model=list[ThreadResponse])
async def search_threads(
    body: ThreadSearchRequest,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> list[ThreadResponse]:
    """Search local-principal thread metadata."""
    return await _service(request, session, principal).search(body)


@router.get("/{thread_id}", response_model=ThreadResponse)
async def get_thread(
    thread_id: uuid.UUID,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ThreadResponse:
    """Return one accessible thread."""
    try:
        return await _service(request, session, principal).get(thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="thread not found") from exc


@router.get("/{thread_id}/state", response_model=ThreadStateResponse)
async def get_thread_state(
    thread_id: uuid.UUID,
    request: Request,
    session: SessionDependency,
    principal: PrincipalDependency,
) -> ThreadStateResponse:
    """Return current values from LangGraph's checkpoint."""
    try:
        return await _service(request, session, principal).get_state(thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="thread not found") from exc
