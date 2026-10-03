"""Request and response shapes for the supported Agent Protocol subset."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChromosomeResponse(BaseModel):
    """A canonical chromosome name, its length and accepted aliases."""

    name: str
    length: int
    aliases: list[str] = Field(default_factory=list)


class AssemblyResponse(BaseModel):
    """One registered assembly of a species."""

    id: str
    aliases: list[str]
    description: str
    canonical: bool
    lift_to: str | None = None
    """Positions on this assembly are lifted to ``lift_to`` before loci are built."""
    chromosomes: list[ChromosomeResponse]


class SourceResponse(BaseModel):
    """A data source with its license terms."""

    id: str
    name: str
    tier: Literal["core", "extended", "heavy"]
    status: Literal["active", "planned"]
    version: str
    license: str
    academic_only: bool
    homepage: str | None = None


class LdSupportResponse(BaseModel):
    """Whether LD windows can be computed, and with which panels."""

    available: bool
    panels: list[str] = Field(default_factory=list)
    reason: str | None = None


class DomainResponse(BaseModel):
    """Whether one evidence domain can be served, and why not."""

    available: bool
    reason: str | None = None


class SpeciesResponse(BaseModel):
    """Everything the study form validates against for one species."""

    species: str
    scientific_name: str
    common_name: str
    taxon_id: int
    canonical_assembly: str
    default_window: dict[str, int | None]
    typical_ld_kb: float
    ld_note: str
    assemblies: list[AssemblyResponse]
    chromosome_prefixes: list[str]
    linkouts: list[dict[str, Any]]
    tiers: dict[str, dict[str, Any]]
    bundle: dict[str, Any] | None
    ld: LdSupportResponse
    domains: dict[str, DomainResponse] = Field(default_factory=dict)
    sources: list[SourceResponse]


class StudyWarningResponse(BaseModel):
    """Something intake would change, drop or doubt about the study input."""

    code: str
    message: str
    snp: str | None = None


class StudyIssueResponse(BaseModel):
    """A reason the study cannot run, located in the request (``["snps", 2, "pos"]``)."""

    loc: list[str | int]
    message: str


class StudyPreviewResponse(BaseModel):
    """Fixed-window loci the study would build and their distinct candidate genes."""

    loci: list[dict[str, Any]]
    genes: int


class StudyValidationResponse(BaseModel):
    """The server's dry run of a study: intake validation, placement and a locus preview."""

    study_normalized: dict[str, Any] | None
    placed_snps: list[dict[str, Any]]
    warnings: list[StudyWarningResponse]
    errors: list[StudyIssueResponse]
    detail: str
    preview: StudyPreviewResponse


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


class RunSummaryResponse(BaseModel):
    """One run of a thread, enough to rejoin or replay its event stream."""

    run_id: str
    status: str
    created_at: datetime
    finished_at: datetime | None = None


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
