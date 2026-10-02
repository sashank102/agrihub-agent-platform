"""Typed ``agrihub.run-event/v1`` events sent through the LangGraph custom stream.

Every event is one envelope::

    {
      "schema": "agrihub.run-event/v1",
      "type": "<event type>",
      "event_id": "<uuid4 hex>",
      "ts": "<ISO-8601 UTC timestamp>",
      "agent": {"id", "name", "kind", "parent_id", "label"},
      "cause": {"type": "toolCall" | "send" | "edge", "tool_call_id"?},
      "ns": ["<checkpoint namespace part>", ...],
      "data": {...}
    }

The server names the SSE event ``custom`` for root nodes and
``custom|<ns>`` for subgraphs. Replaying a finished run from sequence 0
yields the same events in the same order.

Event types and their ``data``:

- ``run.phase``: ``phase`` (intake, model, loci, harvest, planning,
  specialists, ranking, reporting), ``status`` (started, completed, skipped,
  failed), optional ``detail``, optional ``warnings[]`` of
  ``{code, message, snp}`` and, on a failed intake, optional ``errors[]`` of
  ``{loc, message}``.
- ``orchestrator.plan``: ``summary`` and ``steps[]``.
- ``orchestrator.decision``: ``kind`` (dispatch, reflect, followup, finish),
  ``round`` (1 for the first dispatch, 2 for a follow-up), ``rationale`` (a
  stated summary, never raw reasoning), ``dispatched[]`` of ``{agent_id,
  specialist, focus_gene_ids, focus_loci, instructions, rationale}`` and
  ``rejected[]`` of ``{specialist, reason}``.
- ``agent.started``: ``focus`` (``gene_ids``, ``loci``, ``instructions``,
  ``rationale``, ``round``) and ``max_steps``. ``agent.id`` is the lane key.
- ``agent.step``: ``step``, ``max_steps`` and ``title``.
- ``agent.usage``: ``model``, ``input_tokens`` and ``output_tokens``, cumulative
  for the agent; the orchestrator reports its own usage too.
- ``agent.completed``: ``status`` (completed, failed), ``summary``,
  ``findings`` and ``duration_ms``.
- ``tool.started``: ``tool_call_id``, ``name`` and a truncated
  ``input_summary``.
- ``tool.finished``: ``tool_call_id``, ``name``, ``status`` (ok, error),
  a truncated ``output_summary``, ``evidence_ids[]`` and ``output_ref``.
- ``source.discovered``: ``source_id``, ``name``, ``version``, ``url`` and
  ``license``.
- ``evidence.progress``: ``category`` (the harvest step), ``done`` and
  ``total`` genes for it, and ``counts`` of stored evidence per category.
- ``artifact.created``: ``kind`` (loci_table, candidates_table, report,
  evidence_snapshot), ``title``, optional ``artifact_id`` and inline ``rows``.

Field names follow ``langchain-protocol`` lifecycle and AG-UI subagent
events as vocabulary only; the transport is the LangGraph custom stream.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.config import get_config, get_stream_writer
from pydantic import BaseModel, ConfigDict, Field

SCHEMA = "agrihub.run-event/v1"
SUMMARY_CHARS = 300

EventType = Literal[
    "run.phase",
    "orchestrator.plan",
    "orchestrator.decision",
    "agent.started",
    "agent.step",
    "agent.usage",
    "agent.completed",
    "tool.started",
    "tool.finished",
    "source.discovered",
    "evidence.progress",
    "artifact.created",
]
Phase = Literal[
    "intake",
    "model",
    "loci",
    "harvest",
    "planning",
    "specialists",
    "ranking",
    "reporting",
]
PhaseStatus = Literal["started", "completed", "skipped", "failed"]
AgentKind = Literal["pipeline", "orchestrator", "specialist", "verifier", "writer", "model"]
DecisionKind = Literal["dispatch", "reflect", "followup", "finish"]
CauseType = Literal["toolCall", "send", "edge"]


class AgentRef(BaseModel):
    """The agent lane an event belongs to."""

    id: str
    name: str
    kind: AgentKind
    parent_id: str | None = None
    label: str | None = None


class Cause(BaseModel):
    """Why the emitting agent is running."""

    type: CauseType
    tool_call_id: str | None = None


class RunEvent(BaseModel):
    """One ``agrihub.run-event/v1`` envelope."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: Literal["agrihub.run-event/v1"] = Field(default=SCHEMA, alias="schema")
    type: EventType
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    agent: AgentRef
    cause: Cause = Field(default_factory=lambda: Cause(type="edge"))
    ns: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

    def wire(self) -> dict[str, Any]:
        """Return the JSON object written to the custom stream."""
        return self.model_dump(mode="json", by_alias=True)


PIPELINE = AgentRef(id="pipeline", name="Study pipeline", kind="pipeline")
ORCHESTRATOR = AgentRef(id="orchestrator", name="Orchestrator", kind="orchestrator")


def emit(
    event_type: EventType,
    data: dict[str, Any] | None = None,
    *,
    agent: AgentRef | None = None,
    cause: Cause | None = None,
) -> RunEvent:
    """Build one event and write it to the graph's custom stream.

    Outside a running graph the event is built and returned but not written,
    so helpers are safe to call from unit tests and scripts.
    """
    config = _current_config()
    event = RunEvent(
        type=event_type,
        agent=agent or _agent_from_config(config) or PIPELINE,
        cause=cause or Cause(type="edge"),
        ns=_namespace(config),
        data=data or {},
    )
    if config is not None:
        try:
            writer = get_stream_writer()
        except (KeyError, RuntimeError):
            return event
        writer(event.wire())
    return event


def phase(
    name: Phase,
    status: PhaseStatus = "started",
    *,
    detail: str | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> RunEvent:
    """Mark a study phase as started, completed, skipped, or failed."""
    data: dict[str, Any] = {"phase": name, "status": status}
    if detail:
        data["detail"] = detail
    if warnings:
        data["warnings"] = list(warnings)
    if errors:
        data["errors"] = list(errors)
    return emit("run.phase", data, agent=PIPELINE)


def plan(summary: str, steps: list[str]) -> RunEvent:
    """Publish the orchestrator's research plan."""
    return emit(
        "orchestrator.plan",
        {"summary": summary, "steps": list(steps)},
        agent=ORCHESTRATOR,
    )


def decision(
    kind: DecisionKind,
    rationale: str,
    *,
    round: int = 1,
    dispatched: list[dict[str, Any]] | None = None,
    rejected: list[dict[str, Any]] | None = None,
) -> RunEvent:
    """Publish an orchestrator decision with its stated rationale."""
    return emit(
        "orchestrator.decision",
        {
            "kind": kind,
            "round": round,
            "rationale": _summary(rationale),
            "dispatched": list(dispatched or []),
            "rejected": list(rejected or []),
        },
        agent=ORCHESTRATOR,
    )


def agent_started(
    agent: AgentRef,
    *,
    focus: dict[str, Any],
    max_steps: int,
    cause: Cause | None = None,
) -> RunEvent:
    """Open an agent lane."""
    return emit(
        "agent.started",
        {"focus": focus, "max_steps": max_steps},
        agent=agent,
        cause=cause,
    )


def agent_step(agent: AgentRef, step: int, max_steps: int, title: str) -> RunEvent:
    """Report the step an agent is working on."""
    return emit(
        "agent.step",
        {"step": step, "max_steps": max_steps, "title": _summary(title)},
        agent=agent,
    )


def agent_usage(
    agent: AgentRef,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> RunEvent:
    """Report cumulative model usage for an agent."""
    return emit(
        "agent.usage",
        {
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        agent=agent,
    )


def agent_completed(
    agent: AgentRef,
    *,
    summary: str,
    findings: int,
    duration_ms: int,
    status: Literal["completed", "failed"] = "completed",
) -> RunEvent:
    """Close an agent lane."""
    return emit(
        "agent.completed",
        {
            "status": status,
            "summary": _summary(summary),
            "findings": findings,
            "duration_ms": duration_ms,
        },
        agent=agent,
    )


def tool_started(
    agent: AgentRef,
    *,
    tool_call_id: str,
    name: str,
    input_summary: str,
) -> RunEvent:
    """Record that an agent invoked a tool."""
    return emit(
        "tool.started",
        {
            "tool_call_id": tool_call_id,
            "name": name,
            "input_summary": _summary(input_summary),
        },
        agent=agent,
        cause=Cause(type="toolCall", tool_call_id=tool_call_id),
    )


def tool_finished(
    agent: AgentRef,
    *,
    tool_call_id: str,
    name: str,
    output_summary: str,
    evidence_ids: list[str] | None = None,
    output_ref: str | None = None,
    status: Literal["ok", "error"] = "ok",
) -> RunEvent:
    """Record a tool result without its full output."""
    return emit(
        "tool.finished",
        {
            "tool_call_id": tool_call_id,
            "name": name,
            "status": status,
            "output_summary": _summary(output_summary),
            "evidence_ids": list(evidence_ids or []),
            "output_ref": output_ref,
        },
        agent=agent,
        cause=Cause(type="toolCall", tool_call_id=tool_call_id),
    )


def source_discovered(
    *,
    source_id: str,
    name: str,
    version: str,
    url: str | None = None,
    license: str | None = None,
    agent: AgentRef | None = None,
) -> RunEvent:
    """Announce a data source that contributed evidence."""
    return emit(
        "source.discovered",
        {
            "source_id": source_id,
            "name": name,
            "version": version,
            "url": url,
            "license": license,
        },
        agent=agent,
    )


def evidence_progress(
    counts: dict[str, int],
    *,
    done: int,
    total: int,
    category: str | None = None,
) -> RunEvent:
    """Report how many genes one harvest step has covered, with stored evidence counts."""
    data: dict[str, Any] = {"counts": dict(counts), "done": done, "total": total}
    if category is not None:
        data["category"] = category
    return emit("evidence.progress", data, agent=PIPELINE)


def artifact_created(
    kind: str,
    *,
    title: str,
    artifact_id: str | None = None,
    rows: list[dict[str, Any]] | None = None,
    agent: AgentRef | None = None,
) -> RunEvent:
    """Announce a table or report the UI can render."""
    data: dict[str, Any] = {"kind": kind, "title": title, "artifact_id": artifact_id}
    if rows is not None:
        data["rows"] = rows
    return emit("artifact.created", data, agent=agent)


def _current_config() -> dict[str, Any] | None:
    try:
        return dict(get_config())
    except RuntimeError:
        return None


def _agent_from_config(config: dict[str, Any] | None) -> AgentRef | None:
    metadata = (config or {}).get("metadata") or {}
    agent = metadata.get("agrihub_agent")
    if isinstance(agent, dict):
        return AgentRef.model_validate(agent)
    agent_id = metadata.get("agrihub_agent_id")
    if agent_id:
        return AgentRef(id=str(agent_id), name=str(agent_id), kind="specialist")
    return None


def _namespace(config: dict[str, Any] | None) -> list[str]:
    configurable = (config or {}).get("configurable") or {}
    raw = str(configurable.get("checkpoint_ns") or "")
    return [part for part in raw.split("|") if part]


def _summary(text: str) -> str:
    if len(text) <= SUMMARY_CHARS:
        return text
    return text[: SUMMARY_CHARS - 1] + "…"
