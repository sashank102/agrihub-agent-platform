"""Request and response shapes for the supported Agent Protocol subset."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ThreadCreateRequest(BaseModel):
    """Fields accepted by the LangGraph SDK thread creator."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    thread_id: uuid.UUID | None = None
    if_exists: str | None = None
    supersteps: list[dict[str, Any]] | None = None
    ttl: Any | None = None


class ThreadSearchRequest(BaseModel):
    """Supported thread filters plus accepted advanced SDK fields."""

    metadata: dict[str, Any] | None = None
    ids: list[uuid.UUID] | None = None
    limit: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    status: str | None = None
    sort_by: str | None = None
    sort_order: str | None = None
    select: list[str] | None = None
    values: dict[str, Any] | None = None
    extract: dict[str, Any] | None = None


class CheckpointResponse(BaseModel):
    """Checkpoint identity used by the SDK."""

    thread_id: str
    checkpoint_ns: str = ""
    checkpoint_id: str | None = None
    checkpoint_map: dict[str, Any] | None = None


class ThreadStateResponse(BaseModel):
    """Current LangGraph state in Agent Protocol field names."""

    values: Any = Field(default_factory=dict)
    next: list[str] = Field(default_factory=list)
    checkpoint: CheckpointResponse
    metadata: dict[str, Any] | None = None
    created_at: str | None = None
    parent_checkpoint: CheckpointResponse | None = None
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class ThreadResponse(BaseModel):
    """Platform metadata represented as an SDK thread."""

    thread_id: str
    created_at: datetime
    updated_at: datetime
    state_updated_at: datetime
    metadata: dict[str, Any]
    status: Literal["idle", "busy", "interrupted", "error"]
    values: Any = Field(default_factory=dict)
    interrupts: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class ThreadHistoryRequest(BaseModel):
    """History query used by ``threads.getHistory``."""

    limit: int = Field(default=10, ge=1, le=1000)
    before: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None


class RunStreamRequest(BaseModel):
    """Run fields sent by current LangGraph SDK clients."""

    input: dict[str, Any] | None = None
    assistant_id: str
    config: dict[str, Any] | None = None
    context: Any | None = None
    metadata: dict[str, Any] | None = None
    stream_mode: str | list[str] | None = None
    command: dict[str, Any] | None = None
    stream_subgraphs: bool | None = None
    stream_resumable: bool | None = None
    feedback_keys: list[str] | None = None
    interrupt_before: str | list[str] | None = None
    interrupt_after: str | list[str] | None = None
    checkpoint: dict[str, Any] | None = None
    webhook: str | None = None
    multitask_strategy: str | None = None
    on_completion: str | None = None
    on_disconnect: str | None = None
    after_seconds: float | None = None
    if_not_exists: str | None = None
    checkpoint_during: bool | None = None
    durability: str | None = None
