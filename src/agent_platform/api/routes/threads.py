"""Thin Agent Protocol thread routes."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from agent_platform.api.dependencies import (
    get_development_principal,
    require_ready,
)
from agent_platform.api.schemas import (
    ThreadCreateRequest,
    ThreadHistoryRequest,
    ThreadResponse,
    ThreadSearchRequest,
    ThreadStateResponse,
)
from agent_platform.services.errors import CheckpointReferenceError
from agent_platform.services.thread_service import ThreadService

router = APIRouter(
    prefix="/threads",
    dependencies=[Depends(require_ready)],
)
PrincipalDependency = Annotated[uuid.UUID, Depends(get_development_principal)]


def _service(request: Request, principal: uuid.UUID) -> ThreadService:
    return ThreadService(
        session_factory=request.app.state.session_factory,
        registry=request.app.state.graph_registry,
        owner_user_id=principal,
        default_graph_id=request.app.state.settings.DEVELOPMENT_GRAPH_ID,
    )


@router.post("", response_model=ThreadResponse)
async def create_thread(
    body: ThreadCreateRequest,
    request: Request,
    principal: PrincipalDependency,
) -> ThreadResponse:
    """Create one local-principal thread."""
    try:
        return await _service(request, principal).create(body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="assistant not found") from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="thread already exists") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="thread not found") from exc


@router.post("/search", response_model=list[ThreadResponse])
async def search_threads(
    body: ThreadSearchRequest,
    request: Request,
    principal: PrincipalDependency,
) -> list[ThreadResponse]:
    """Search local-principal thread metadata."""
    return await _service(request, principal).search(body)


@router.get("/{thread_id}", response_model=ThreadResponse)
async def get_thread(
    thread_id: uuid.UUID,
    request: Request,
    principal: PrincipalDependency,
) -> ThreadResponse:
    """Return one accessible thread."""
    try:
        return await _service(request, principal).get(thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="thread not found") from exc


@router.get("/{thread_id}/state", response_model=ThreadStateResponse)
async def get_thread_state(
    thread_id: uuid.UUID,
    request: Request,
    principal: PrincipalDependency,
) -> ThreadStateResponse:
    """Return current values from LangGraph's checkpoint."""
    try:
        return await _service(request, principal).get_state(thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="thread not found") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="assistant not found") from exc


@router.post("/{thread_id}/history", response_model=list[ThreadStateResponse])
async def thread_history(
    thread_id: uuid.UUID,
    body: ThreadHistoryRequest,
    request: Request,
    principal: PrincipalDependency,
) -> list[ThreadStateResponse]:
    """Return checkpoint history for one accessible thread."""
    try:
        return await _service(request, principal).history(thread_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="thread not found") from exc
    except CheckpointReferenceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="assistant not found") from exc
