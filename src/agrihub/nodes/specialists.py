"""Specialist lanes. Each dispatch runs the specialist subgraph once.

The subgraph runs under config metadata ``agrihub_agent_id`` (the dispatch
id), so its events and tool calls land in that agent's lane. The single
``investigate`` node is a stub that calls the real evidence-store tools.
"""

import time
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from agrihub import events
from agrihub.configuration import StudyConfiguration
from agrihub.nodes.orchestrator import SPECIALIST_LABELS
from agrihub.state import SpecialistName, SpecialistTask
from agrihub.tools.store_tools import get_evidence, record_finding, store_for_config

SPECIALIST_CATEGORIES: dict[SpecialistName, tuple[str, ...]] = {
    "locus_variant": ("positional", "variant"),
    "qtl_gwas": ("association", "known_gene"),
    "function_orthology": ("functional_annotation", "ortholog"),
    "expression_network": ("expression", "network", "regulation"),
    "literature": ("literature",),
}


class SpecialistRunState(TypedDict, total=False):
    """Specialist subgraph state: the dispatch plus its results."""

    agent_id: str
    specialist: SpecialistName
    round: int
    focus_gene_ids: list[str]
    focus_loci: list[str]
    instructions: str
    rationale: str
    study: dict[str, Any]
    finding_ids: list[str]
    summary: str
    steps: int


async def investigate(state: SpecialistRunState, config: RunnableConfig) -> dict[str, Any]:
    """Review focus evidence and record one finding per gene with evidence."""
    started = time.monotonic()
    agent = _agent_for(state)
    max_steps = StudyConfiguration.from_runnable_config(config).max_specialist_steps
    events.agent_started(
        agent,
        focus={
            "gene_ids": list(state.get("focus_gene_ids") or []),
            "loci": list(state.get("focus_loci") or []),
            "instructions": state.get("instructions") or "",
        },
        max_steps=max_steps,
        cause=events.Cause(type="send", tool_call_id=agent.id),
    )
    specialist = state["specialist"]
    categories = SPECIALIST_CATEGORIES[specialist]
    store = store_for_config(config)
    candidates = store.query(state.get("focus_gene_ids") or [], categories)

    events.agent_step(agent, 1, max_steps, "Review triaged evidence")
    call_id = f"{agent.id}-t1"
    events.tool_started(
        agent,
        tool_call_id=call_id,
        name=get_evidence.name,
        input_summary=f"{len(candidates)} ids in {', '.join(categories)}",
    )
    fetched = await get_evidence.ainvoke(
        {"ids": [str(item.alias) for item in candidates]},
        config,
    )
    evidence = fetched["evidence"]
    events.tool_finished(
        agent,
        tool_call_id=call_id,
        name=get_evidence.name,
        output_summary=f"{len(evidence)} evidence items",
        evidence_ids=[item["alias"] for item in evidence],
    )

    events.agent_step(agent, 2, max_steps, "Record findings")
    finding_ids: list[str] = []
    by_gene: dict[str, list[str]] = {}
    for item in evidence:
        by_gene.setdefault(item["gene_id"], []).append(item["alias"])
    for number, (gene_id, aliases) in enumerate(by_gene.items(), start=1):
        call_id = f"{agent.id}-f{number}"
        events.tool_started(
            agent,
            tool_call_id=call_id,
            name=record_finding.name,
            input_summary=f"{gene_id}: {', '.join(aliases)}",
        )
        result = await record_finding.ainvoke(
            {
                "target": gene_id,
                "claim": f"Stub {specialist} review found {len(aliases)} evidence items.",
                "stance": "neutral",
                "strength": "weak",
                "evidence_ids": aliases,
            },
            config,
        )
        recorded = isinstance(result, dict) and result.get("status") == "recorded"
        if recorded:
            finding_ids.append(str(result["finding_id"]))
        events.tool_finished(
            agent,
            tool_call_id=call_id,
            name=record_finding.name,
            status="ok" if recorded else "error",
            output_summary=str(result["finding_id"]) if recorded else str(result),
            evidence_ids=aliases,
        )

    events.agent_usage(agent, model="stub", input_tokens=0, output_tokens=0)
    summary = (
        f"Reviewed {len(evidence)} evidence items on {len(by_gene)} genes; "
        f"recorded {len(finding_ids)} findings."
    )
    events.agent_completed(
        agent,
        summary=summary,
        findings=len(finding_ids),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return {"finding_ids": finding_ids, "summary": summary, "steps": 2}


def build_specialist_graph() -> Any:
    """Compile the specialist subgraph; it inherits the parent checkpointer."""
    builder = StateGraph(SpecialistRunState)
    builder.add_node("investigate", investigate)
    builder.add_edge(START, "investigate")
    builder.add_edge("investigate", END)
    return builder.compile(name="agrihub_specialist")


SPECIALIST_GRAPH = build_specialist_graph()


async def specialist(task: SpecialistTask, config: RunnableConfig) -> dict[str, Any]:
    """Run one dispatch in its own subgraph namespace and agent lane."""
    agent = _agent_for(task)
    metadata = {
        **(config.get("metadata") or {}),
        "agrihub_agent_id": agent.id,
        "agrihub_agent": agent.model_dump(),
    }
    result = await SPECIALIST_GRAPH.ainvoke(dict(task), {"metadata": metadata})
    return {
        "findings": list(result.get("finding_ids") or []),
        "specialist_results": [
            {
                "agent_id": agent.id,
                "specialist": task["specialist"],
                "round": task["round"],
                "status": "completed",
                "finding_ids": list(result.get("finding_ids") or []),
                "summary": result.get("summary") or "",
                "steps": int(result.get("steps") or 0),
            }
        ],
    }


def _agent_for(task: SpecialistTask | SpecialistRunState) -> events.AgentRef:
    genes = len(task.get("focus_gene_ids") or [])
    loci = len(task.get("focus_loci") or [])
    return events.AgentRef(
        id=str(task["agent_id"]),
        name=SPECIALIST_LABELS[task["specialist"]],
        kind="specialist",
        parent_id=events.ORCHESTRATOR.id,
        label=f"{genes} genes, {loci} loci",
    )
