"""Specialist lanes: one parameterized ReAct subgraph per dispatch.

A ``SpecialistSpec`` names the prompt, the tools, and optionally a step
budget and model for one specialist. The subgraph runs ``prepare`` (system
prompt and assignment), then ``agent`` (one model call, ``agent.step`` and
cumulative ``agent.usage``) and ``tools`` (every tool call invoked as a full
ToolCall dict, so its id reaches ``tool.started``/``tool.finished``) until
the model calls ``specialist_done``, stops calling tools, or uses up its
steps.

The ``specialist`` node in the study graph opens and closes the lane. It
runs the subgraph with ``merge_configs(config, ...)`` so the run id,
callbacks and checkpointer carry over, under metadata ``agrihub_agent_id``
(the dispatch id) so tool writes land in this lane. Any failure is caught
there: the lane closes with ``agent.completed(status=failed)`` and the other
specialists continue.
"""

import asyncio
import json
import operator
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs
from langchain_core.tools import BaseTool
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agrihub import budget, events, models
from agrihub.configuration import StudyConfiguration
from agrihub.nodes.locus_builder import load_candidates
from agrihub.nodes.orchestrator import SPECIALIST_LABELS
from agrihub.prompts import AgentPrompt, PromptContext, render
from agrihub.prompts.expression_network import EXPRESSION_NETWORK
from agrihub.prompts.function_orthology import FUNCTION_ORTHOLOGY
from agrihub.prompts.literature import LITERATURE
from agrihub.prompts.locus_variant import LOCUS_VARIANT
from agrihub.prompts.qtl_gwas import QTL_GWAS
from agrihub.state import SpecialistName, SpecialistTask
from agrihub.tools import bundle_tools as bt
from agrihub.tools import extended_tools as et
from agrihub.tools import literature_tools as lt
from agrihub.tools import variant_tools as vt
from agrihub.tools.agent_tools import COMMON_TOOLS
from agrihub.tools.store_tools import store_for_config
from agrihub_data.availability import (
    DomainStatus,
    domain_status,
    tool_is_served,
    unavailable_lines,
)
from agrihub_data.registry import UnknownSpeciesError

DONE_TOOL = "specialist_done"
INPUT_SUMMARY_CHARS = 240


@dataclass(frozen=True)
class SpecialistSpec:
    """What makes one specialist: its prompt, its tools, and optional budget and model overrides."""

    name: SpecialistName
    prompt: AgentPrompt
    tools: tuple[BaseTool, ...]
    max_steps: int | None = None
    model: str | None = None

    def bound_tools(self, settings: StudyConfiguration, available: set[str] | None = None) -> list[BaseTool]:
        """Return the tools for this run.

        ``web_search`` is bound only when the web fallback is enabled; with
        ``available`` domains given, tools whose every domain is unavailable
        are left out (the prompt lists those domains as gaps).
        """
        return [
            tool
            for tool in self.tools
            if (tool.name != "web_search" or settings.enable_web_fallback)
            and (available is None or tool_is_served(tool.name, available))
        ]


SPECS: dict[SpecialistName, SpecialistSpec] = {
    "locus_variant": SpecialistSpec(
        "locus_variant",
        LOCUS_VARIANT,
        (
            bt.genes_in_window,
            vt.annotate_variants,
            et.snp_in_tfbs_or_cns,
            vt.ld_with_lead,
            bt.define_locus,
            vt.homeologs,
            vt.gene_haplotypes,
            bt.gene_annotation,
            bt.map_gene_ids,
            bt.liftover,
            bt.resolve_marker,
            bt.normalize_chrom,
            *COMMON_TOOLS,
        ),
    ),
    "qtl_gwas": SpecialistSpec(
        "qtl_gwas",
        QTL_GWAS,
        (bt.map_trait, bt.qtl_overlap, bt.gwas_catalog_overlap, bt.known_trait_genes, *COMMON_TOOLS),
    ),
    "function_orthology": SpecialistSpec(
        "function_orthology",
        FUNCTION_ORTHOLOGY,
        (bt.gene_annotation, bt.annotation_relevance, bt.get_orthologs, bt.arabidopsis_knowledge, et.get_pathways, bt.map_trait, *COMMON_TOOLS),
    ),
    "expression_network": SpecialistSpec(
        "expression_network",
        EXPRESSION_NETWORK,
        (
            *et.EXPRESSION_TOOLS,
            et.seed_propagation,
            et.coexpression_neighbors,
            et.network_neighbors,
            et.get_regulation,
            et.get_pathways,
            bt.gene_annotation,
            bt.map_trait,
            *COMMON_TOOLS,
        ),
        max_steps=6,
    ),
    "literature": SpecialistSpec(
        "literature",
        LITERATURE,
        (*lt.LITERATURE_TOOLS, lt.web_search, *COMMON_TOOLS),
    ),
}


class SpecialistRunState(TypedDict, total=False):
    """Specialist subgraph state: the dispatch, the conversation and the lane's tallies."""

    agent_id: str
    specialist: SpecialistName
    round: int
    focus_gene_ids: list[str]
    focus_loci: list[str]
    instructions: str
    rationale: str
    study: dict[str, Any]
    context: dict[str, Any]
    messages: Annotated[list[AnyMessage], add_messages]
    available: list[str]
    """Evidence domains the study's bundle serves, fixed when the lane starts."""
    step: int
    max_steps: int
    tool_calls: int
    finding_ids: Annotated[list[str], operator.add]
    usage: dict[str, Any]
    done: bool
    summary: str
    closed_with_tool: bool


def max_steps_for(spec: SpecialistSpec, settings: StudyConfiguration) -> int:
    """Return a specialist's step budget: its own cap, never above the configured one."""
    return min(spec.max_steps or settings.max_specialist_steps, settings.max_specialist_steps)


async def prepare(state: SpecialistRunState, config: RunnableConfig) -> dict[str, Any]:
    """Write the system prompt and the assignment message."""
    settings = StudyConfiguration.from_runnable_config(config)
    spec = SPECS[state["specialist"]]
    max_steps = max_steps_for(spec, settings)
    study = state.get("study") or {}
    profile = study.get("profile") or {}
    statuses = await asyncio.to_thread(study_domains, study)
    available = {key for key, status in statuses.items() if status.available}
    context = PromptContext(
        species=str(study.get("species") or ""),
        assembly=str(study.get("assembly") or ""),
        trait=str(study.get("trait_text") or ""),
        max_steps=max_steps,
        profile_key=profile.get("key"),
        profile_terms=tuple(profile.get("terms") or ()),
        profile_keywords=tuple(profile.get("keywords") or ()),
        seed_families=tuple(profile.get("seed_families") or ()),
        focus_gene_ids=tuple(state.get("focus_gene_ids") or ()),
        focus_loci=tuple(state.get("focus_loci") or ()),
        unavailable=unavailable_lines(statuses, spec.prompt.domains),
    )
    tools = tuple(tool.name for tool in spec.bound_tools(settings, available))
    assignment = await asyncio.to_thread(_assignment, state, config, max_steps)
    return {
        "available": sorted(available),
        "messages": [SystemMessage(content=render(spec.prompt, context, tools=tools)), HumanMessage(content=assignment)],
        "step": 0,
        "max_steps": max_steps,
        "tool_calls": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "done": False,
    }


async def agent(state: SpecialistRunState, config: RunnableConfig) -> dict[str, Any]:
    """Make one model call and report the step and the cumulative usage."""
    settings = StudyConfiguration.from_runnable_config(config)
    spec = SPECS[state["specialist"]]
    lane = _agent_for(state)
    model_name = spec.model or settings.specialist_model
    model = models.tool_model(
        model_name,
        spec.bound_tools(settings, _available(state)),
        max_tokens=settings.model_max_tokens,
        max_retries=settings.model_max_retries,
    )
    step = int(state.get("step") or 0) + 1
    max_steps = int(state.get("max_steps") or settings.max_specialist_steps)
    history = budget.compact_history(list(state.get("messages") or []), settings.history_tool_results)
    reminder = budget.budget_reminder(step - 1, max_steps)
    if reminder:
        history.append(HumanMessage(content=reminder))
    response: Any = None
    for attempt in range(budget.MAX_REJECTED_CALLS + 1):
        try:
            response = await model.ainvoke(history, config)
            break
        except Exception as exc:
            if not budget.is_rejected_request(exc) or attempt == budget.MAX_REJECTED_CALLS:
                raise
            history.append(HumanMessage(content=budget.rejection_feedback(exc)))
    calls = list(getattr(response, "tool_calls", None) or [])
    names = list(dict.fromkeys(call["name"] for call in calls))
    title = "Summarize and finish" if not calls or names == [DONE_TOOL] else ", ".join(name for name in names if name != DONE_TOOL)
    events.agent_step(lane, step, max_steps, title)
    metadata = getattr(response, "usage_metadata", None) or {}
    usage = dict(state.get("usage") or {})
    usage["input_tokens"] = int(usage.get("input_tokens") or 0) + int(metadata.get("input_tokens") or 0)
    usage["output_tokens"] = int(usage.get("output_tokens") or 0) + int(metadata.get("output_tokens") or 0)
    usage["model"] = model_name
    events.agent_usage(lane, model=model_name, input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"])
    update: dict[str, Any] = {"messages": [response], "step": step, "usage": usage}
    if not calls:
        update["done"] = True
        update["closed_with_tool"] = False
        update["summary"] = _text(response) or "Finished without a specialist_done tool call."
    return update


async def tools(state: SpecialistRunState, config: RunnableConfig) -> dict[str, Any]:
    """Run the last message's tool calls (bounded by the run's ``max_concurrency``) and report each one."""
    settings = StudyConfiguration.from_runnable_config(config)
    spec = SPECS[state["specialist"]]
    lane = _agent_for(state)
    by_name = {tool.name: tool for tool in spec.bound_tools(settings, _available(state))}
    last = (state.get("messages") or [])[-1]
    calls = list(getattr(last, "tool_calls", None) or [])
    limit = int(config.get("max_concurrency") or 0) or max(1, len(calls))
    gate = asyncio.Semaphore(limit)
    step = int(state.get("step") or 0)

    async def run(index: int, call: dict[str, Any]) -> tuple[ToolMessage, str | None]:
        async with gate:
            call_id = str(call.get("id") or f"{lane.id}-s{step}-{index}")
            name = str(call["name"])
            events.tool_started(lane, tool_call_id=call_id, name=name, input_summary=_args_summary(call.get("args") or {}))
            tool = by_name.get(name)
            if tool is None:
                message = ToolMessage(
                    content=f"unknown tool {name}; available: {', '.join(by_name)}",
                    tool_call_id=call_id,
                    name=name,
                    status="error",
                )
            else:
                try:
                    result = await tool.ainvoke({"name": name, "args": call.get("args") or {}, "id": call_id, "type": "tool_call"}, config)
                    message = result if isinstance(result, ToolMessage) else ToolMessage(content=str(result), tool_call_id=call_id, name=name)
                except GraphBubbleUp:
                    raise
                except Exception as exc:  # noqa: BLE001
                    message = ToolMessage(content=f"error: {type(exc).__name__}: {exc}"[:500], tool_call_id=call_id, name=name, status="error")
            content = budget.clip_tool_output(str(message.content), settings.tool_output_chars)
            message = message.model_copy(update={"content": content})
            artifact = message.artifact if isinstance(message.artifact, dict) else {}
            failed = message.status == "error" or content.startswith(("Error", "Finding rejected"))
            events.tool_finished(
                lane,
                tool_call_id=call_id,
                name=name,
                status="error" if failed else "ok",
                output_summary=content.strip().splitlines()[0] if content.strip() else "",
                evidence_ids=list(artifact.get("aliases") or artifact.get("evidence_ids") or []),
                output_ref=artifact.get("output_ref"),
            )
            finding = str(artifact["finding_id"]) if name == "record_finding" and artifact.get("finding_id") and not failed else None
            return message, finding

    results = await asyncio.gather(*(run(index, call) for index, call in enumerate(calls)))
    done_call = next((call for call in calls if call["name"] == DONE_TOOL), None)
    update: dict[str, Any] = {
        "messages": [message for message, _ in results],
        "finding_ids": [finding for _, finding in results if finding],
        "tool_calls": int(state.get("tool_calls") or 0) + len(calls),
    }
    if done_call is not None:
        update["done"] = True
        update["closed_with_tool"] = True
        update["summary"] = str((done_call.get("args") or {}).get("summary") or "Done.")
    elif step >= int(state.get("max_steps") or settings.max_specialist_steps):
        recorded = len(state.get("finding_ids") or []) + len(update["finding_ids"])
        update["done"] = True
        update["closed_with_tool"] = False
        update["summary"] = f"Step budget of {step} used up before specialist_done; {recorded} findings recorded."
    return update


def route_after_agent(state: SpecialistRunState) -> str:
    """Run tools unless the model stopped calling them."""
    return END if state.get("done") else "tools"


def route_after_tools(state: SpecialistRunState) -> str:
    """Loop back to the model until the lane is done."""
    return END if state.get("done") else "agent"


def build_specialist_graph() -> Any:
    """Compile the specialist subgraph; it inherits the parent checkpointer."""
    builder = StateGraph(SpecialistRunState)
    builder.add_node("prepare", prepare)
    builder.add_node("agent", agent)
    builder.add_node("tools", tools)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")
    builder.add_conditional_edges("agent", route_after_agent, ["tools", END])
    builder.add_conditional_edges("tools", route_after_tools, ["agent", END])
    return builder.compile(name="agrihub_specialist")


SPECIALIST_GRAPH = build_specialist_graph()


async def specialist(task: SpecialistTask, config: RunnableConfig) -> dict[str, Any]:
    """Open the lane, run the subgraph under this dispatch's metadata, and close the lane even on failure."""
    started = time.monotonic()
    lane = _agent_for(task)
    settings = StudyConfiguration.from_runnable_config(config)
    spec = SPECS[task["specialist"]]
    max_steps = max_steps_for(spec, settings)
    events.agent_started(
        lane,
        focus={
            "gene_ids": list(task.get("focus_gene_ids") or []),
            "loci": list(task.get("focus_loci") or []),
            "instructions": task.get("instructions") or "",
            "rationale": task.get("rationale") or "",
            "round": task.get("round") or 1,
        },
        max_steps=max_steps,
        cause=events.Cause(type="send", tool_call_id=lane.id),
    )
    child = merge_configs(
        config,
        {
            "metadata": {"agrihub_agent_id": lane.id, "agrihub_agent": lane.model_dump()},
            "recursion_limit": 2 * max_steps + 6,
        },
    )
    status: Literal["completed", "failed"] = "completed"
    result: dict[str, Any] = {}
    try:
        result = await SPECIALIST_GRAPH.ainvoke(dict(task), child)
        summary = str(result.get("summary") or "Done.")
    except GraphBubbleUp:
        raise
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        summary = f"Specialist failed: {type(exc).__name__}: {exc}"
    finding_ids = list(result.get("finding_ids") or [])
    if status == "failed":
        finding_ids = await asyncio.to_thread(
            lambda: [str(finding.finding_id) for finding in store_for_config(config).findings(agent_id=lane.id)]
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    events.agent_completed(lane, summary=summary, findings=len(finding_ids), duration_ms=duration_ms, status=status)
    usage = result.get("usage") or {}
    return {
        "findings": finding_ids,
        "specialist_results": [
            {
                "agent_id": lane.id,
                "specialist": task["specialist"],
                "round": task["round"],
                "status": status,
                "focus_gene_ids": list(task.get("focus_gene_ids") or []),
                "finding_ids": finding_ids,
                "summary": summary,
                "steps": int(result.get("step") or 0),
                "tool_calls": int(result.get("tool_calls") or 0),
                "model": usage.get("model") or spec.model or settings.specialist_model,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "duration_ms": duration_ms,
                "missing_specialist_done": not bool(result.get("closed_with_tool")),
            }
        ],
    }


def _assignment(state: SpecialistRunState, config: RunnableConfig, max_steps: int) -> str:
    study = state.get("study") or {}
    context = state.get("context") or {}
    gene_context = context.get("genes") or {}
    locus_context = context.get("loci") or {}
    genes = list(state.get("focus_gene_ids") or [])
    deflines: dict[str, str] = {}
    previous: list[str] = []
    if genes and (config.get("configurable") or {}).get("run_id"):
        store = store_for_config(config)
        refs = [
            {"gene_id": gene, "locus_id": (gene_context.get(gene) or {}).get("locus_id") or "", "distance_bp": (gene_context.get(gene) or {}).get("distance_bp") or 0}
            for gene in genes
        ]
        deflines = {candidate.gene_id: candidate.defline for candidate in load_candidates(refs, store)}
        for gene in genes:
            for finding in store.findings(target=gene):
                previous.append(f"- {finding.finding_id} on {gene} [{finding.stance}, {finding.strength}]: {finding.claim}")
    lines = [
        f"Assignment from the orchestrator (round {state.get('round') or 1}).",
        f"Instructions: {state.get('instructions') or ''}",
        f"Rationale: {state.get('rationale') or ''}",
        f"Tools take species=\"{study.get('species')}\"; coordinates are on {study.get('assembly')}. Budget: {max_steps} tool rounds.",
        "Focus genes:",
    ]
    for gene in genes:
        info = gene_context.get(gene) or {}
        distance = info.get("distance_bp")
        where = (
            f"overlaps {info.get('nearest_snp')}"
            if distance == 0
            else f"{int(distance) / 1000:.1f} kb from {info.get('nearest_snp')}"
            if distance is not None
            else "position unknown"
        )
        why = "; ".join(info.get("why") or []) or "positional only"
        lines.append(
            f"- {gene}" + (f" ({info['symbol']})" if info.get("symbol") else "")
            + f" in {info.get('locus_id')}, {where}; provisional score {info.get('score')} {info.get('tier')}; {why}"
            + (f" | {deflines[gene][:140]}" if deflines.get(gene) else "")
        )
    if locus_context:
        lines.append("Focus loci:")
        for locus_id, info in locus_context.items():
            lines.append(
                f"- {locus_id} {info.get('chrom')}:{info.get('start')}-{info.get('end')} lead {info.get('lead_snp')} "
                f"(pos {info.get('lead_pos')}), {info.get('n_genes')} genes"
            )
    lines.append("Findings already recorded on these genes:" if previous else "No findings are recorded on these genes yet.")
    lines += previous[:20]
    payload = {
        "agent_id": state.get("agent_id"),
        "specialist": state.get("specialist"),
        "round": state.get("round") or 1,
        "trait": study.get("trait_text"),
        "species": study.get("species"),
        "focus_gene_ids": genes,
        "focus_loci": [{"locus_id": locus_id, **info} for locus_id, info in locus_context.items()],
    }
    lines.append("Assignment JSON: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def study_domains(study: dict[str, Any]) -> dict[str, DomainStatus]:
    """Return the domain status of a study's species, or nothing when the species is unknown."""
    try:
        return domain_status(str(study.get("species") or ""))
    except UnknownSpeciesError:
        return {}


def _available(state: SpecialistRunState) -> set[str] | None:
    available = state.get("available")
    return set(available) if available is not None else None


def _agent_for(task: SpecialistTask | SpecialistRunState) -> events.AgentRef:
    genes = len(task.get("focus_gene_ids") or [])
    loci = len(task.get("focus_loci") or [])
    round_number = int(task.get("round") or 1)
    return events.AgentRef(
        id=str(task["agent_id"]),
        name=SPECIALIST_LABELS[task["specialist"]],
        kind="specialist",
        parent_id=events.ORCHESTRATOR.id,
        label=f"{genes} genes, {loci} loci" + (f", round {round_number}" if round_number > 1 else ""),
    )


def _args_summary(args: dict[str, Any]) -> str:
    text = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= INPUT_SUMMARY_CHARS else text[: INPUT_SUMMARY_CHARS - 1] + "…"


def _text(message: AIMessage | Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content).strip()
    return str(content).strip()
