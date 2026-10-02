"""The orchestrator agent: read the triage, dispatch specialists with ``Send``, and decide on a follow-up round.

The model has ``inspect_triage``, ``get_evidence``, ``dispatch_specialists``,
``think`` and ``finish_research``. A dispatch is validated against the
study's candidates, loci and enabled specialists; invalid entries are
rejected and reported in the ``orchestrator.decision`` event. Each accepted
entry becomes one ``Send("specialist")`` whose ``agent_id`` (the UI lane key)
is ``<dispatch tool-call id>-<specialist>``.

The first call plans round 1. When ``collect`` returns with rounds left, the
orchestrator reads the delta brief and either dispatches a narrower
follow-up (decision kind ``followup``) or finishes. If the model fails or
uses up its steps, round 1 falls back to the deterministic plan of
:mod:`agrihub.planning` and later rounds finish.
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command, Send
from pydantic import BaseModel, Field

from agrihub import budget, events, models, planning
from agrihub.configuration import StudyConfiguration
from agrihub.nodes.harvest import render_brief
from agrihub.prompts import PromptContext, render
from agrihub.prompts.orchestrator import ORCHESTRATOR
from agrihub.state import SPECIALISTS, SpecialistName, SpecialistTask, StudyState
from agrihub.tools import agent_tools
from agrihub.tools.store_tools import store_for_config

SPECIALIST_LABELS = {
    "locus_variant": "Locus and variant specialist",
    "qtl_gwas": "QTL and GWAS specialist",
    "function_orthology": "Function and orthology specialist",
    "expression_network": "Expression and network specialist",
    "literature": "Literature specialist",
}
MAX_CANDIDATES_SHOWN = 40
MORE_GENES_PER_LOCUS = 15


class DispatchSpec(BaseModel):
    """One specialist assignment."""

    specialist: SpecialistName = Field(description="Which specialist to run.")
    focus_gene_ids: list[str] = Field(default_factory=list, description="Candidate gene ids from the candidate list.")
    focus_loci: list[str] = Field(default_factory=list, description="Locus ids such as L1.")
    instructions: str = Field(description="What this specialist should check for these genes.")
    rationale: str = Field(description="One sentence: why this specialist on these genes, naming the evidence.")


class Skip(BaseModel):
    """A specialist deliberately left out."""

    specialist: SpecialistName = Field(description="The specialist left out.")
    reason: str = Field(description="Why it is not needed.")


@dataclass
class Decision:
    """How one orchestrator call ended."""

    kind: str
    summary: str
    call_id: str | None = None
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    fallback: str | None = None


@dataclass
class View:
    """What the orchestrator may look at and dispatch in this call."""

    study: dict[str, Any]
    brief: dict[str, Any]
    candidates: dict[str, dict[str, Any]]
    loci: dict[str, dict[str, Any]]
    enabled: list[str]
    round: int
    max_rounds: int
    max_genes: int
    delta: dict[str, Any] | None
    previous: list[dict[str, Any]]
    matrix: list[dict[str, Any]]
    profile: dict[str, Any] | None

    @property
    def followup(self) -> bool:
        """Return whether this call decides a follow-up round."""
        return self.round > 1


async def orchestrator(state: StudyState, config: RunnableConfig) -> Command:
    """Plan round 1, or decide a follow-up after ``collect``, and dispatch specialists."""
    settings = StudyConfiguration.from_runnable_config(config)
    view = await asyncio.to_thread(_view, state, config, settings)
    if not view.followup:
        events.phase("planning")
    previous = list(state.get("orchestrator_usage") or [])
    decision, usage = await _decide(view, settings, config, prior=previous)
    named = {item["specialist"] for item in decision.rejected}
    decision.rejected += [
        {"specialist": name, "reason": "disabled for this study"}
        for name in SPECIALISTS
        if name not in view.enabled and name not in named
    ]
    if decision.kind == "finish":
        events.decision("finish", decision.summary, round=view.round - 1 if view.followup else 1, rejected=decision.rejected)
        if view.followup:
            events.phase("specialists", "completed", detail=_specialists_detail(state))
        else:
            events.phase("planning", "completed", detail="no specialists dispatched")
        return Command(update={"orchestrator_usage": [usage]}, goto="rank_verify")

    tasks = _tasks(view, decision)
    dispatched = [
        {
            "agent_id": task["agent_id"],
            "specialist": task["specialist"],
            "focus_gene_ids": task["focus_gene_ids"],
            "focus_loci": task["focus_loci"],
            "instructions": task["instructions"],
            "rationale": task["rationale"],
        }
        for task in tasks
    ]
    if not view.followup:
        events.plan(
            decision.summary,
            [f"{SPECIALIST_LABELS[task['specialist']]}: {task['instructions']}" for task in tasks]
            + ["Collect findings", "Rank and verify candidates", "Write the report"],
        )
    events.decision(
        "followup" if view.followup else "dispatch",
        decision.summary if decision.fallback is None else f"Fallback plan ({decision.fallback}). {decision.summary}",
        round=view.round,
        dispatched=dispatched,
        rejected=decision.rejected,
    )
    if not view.followup:
        events.phase("planning", "completed", detail=f"{len(tasks)} specialists dispatched")
        events.phase("specialists")
    return Command(
        update={
            "dispatches": [*(state.get("dispatches") or []), *({**item, "round": view.round} for item in dispatched)],
            "round": view.round,
            "orchestrator_usage": [usage],
        },
        goto=[Send("specialist", dict(task)) for task in tasks],
    )


def _view(state: StudyState, config: RunnableConfig, settings: StudyConfiguration) -> View:
    study = state.get("study") or {}
    brief = state.get("triage_brief") or {}
    requested = study.get("specialists_enabled") or SPECIALISTS
    matrix: list[dict[str, Any]] = []
    if brief.get("matrix_ref") and (config.get("configurable") or {}).get("run_id"):
        output = store_for_config(config).get_output(str(brief["matrix_ref"])) or {}
        matrix = list(output.get("rows") or [])
    return View(
        study=study,
        brief=brief,
        candidates={str(ref["gene_id"]): ref for ref in state.get("candidates") or []},
        loci={str(locus["locus_id"]): locus for locus in state.get("loci") or []},
        enabled=[name for name in SPECIALISTS if name in requested],
        round=int(state.get("round") or 0) + 1,
        max_rounds=settings.max_rounds,
        max_genes=settings.max_focus_genes,
        delta=state.get("delta_brief"),
        previous=list(state.get("dispatches") or []),
        matrix=matrix,
        profile=_profile(study),
    )


def _profile(study: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from agrihub.nodes.harvest import harvest_context

        profile = harvest_context(study).profile
    except Exception:  # noqa: BLE001
        return None
    return {
        "key": profile.key,
        "terms": [f"{term.term_id} {term.name}" for term in profile.terms[:6]],
        "keywords": profile.keywords[:10],
        "seed_families": profile.seed_families[:8],
    }


async def _decide(
    view: View,
    settings: StudyConfiguration,
    config: RunnableConfig,
    *,
    prior: list[dict[str, Any]],
) -> tuple[Decision, dict[str, Any]]:
    tools = _tools(view)
    by_name = {tool.name: tool for tool in tools}
    model_name = settings.orchestrator_model
    usage: dict[str, Any] = {"model": model_name, "round": view.round, "input_tokens": 0, "output_tokens": 0, "calls": 0}
    before = (sum(int(item.get("input_tokens") or 0) for item in prior), sum(int(item.get("output_tokens") or 0) for item in prior))
    messages: list[BaseMessage] = [
        SystemMessage(content=render(ORCHESTRATOR, _context(view, settings))),
        HumanMessage(content=_briefing(view)),
    ]
    try:
        model = models.tool_model(model_name, tools, max_tokens=settings.model_max_tokens, max_retries=settings.model_max_retries)
    except Exception as exc:  # noqa: BLE001
        return _fallback(view, f"model {model_name} unavailable: {_short(exc)}"), usage
    rejected_calls = 0
    for step in range(1, settings.max_orchestrator_steps + 1):
        history = budget.compact_history(messages, settings.history_tool_results)
        if step == settings.max_orchestrator_steps:
            history.append(HumanMessage(content="This is your last step: call dispatch_specialists or finish_research now."))
        try:
            response = await model.ainvoke(history, config)
        except Exception as exc:  # noqa: BLE001
            if budget.is_rejected_request(exc) and rejected_calls < budget.MAX_REJECTED_CALLS:
                rejected_calls += 1
                messages.append(HumanMessage(content=budget.rejection_feedback(exc)))
                continue
            return _fallback(view, f"orchestrator model failed: {_short(exc)}"), usage
        rejected_calls = 0
        _add_usage(usage, response)
        events.agent_usage(
            events.ORCHESTRATOR,
            model=model_name,
            input_tokens=before[0] + usage["input_tokens"],
            output_tokens=before[1] + usage["output_tokens"],
        )
        messages.append(response)
        calls = list(getattr(response, "tool_calls", None) or [])
        if not calls:
            messages.append(HumanMessage(content="Decide now: call dispatch_specialists or finish_research."))
            continue
        decision: Decision | None = None
        for call in calls:
            tool = by_name.get(call["name"])
            if decision is not None or tool is None:
                note = "ignored: the decision is already made" if decision is not None else f"unknown tool {call['name']}"
                messages.append(ToolMessage(content=note, tool_call_id=call["id"], name=call["name"], status="error"))
                continue
            try:
                result = await tool.ainvoke({**call, "type": "tool_call"}, config)
            except Exception as exc:  # noqa: BLE001
                result = ToolMessage(content=f"error: {_short(exc)}", tool_call_id=call["id"], name=call["name"], status="error")
            if isinstance(result, ToolMessage):
                result = result.model_copy(update={"content": budget.clip_tool_output(str(result.content), settings.tool_output_chars)})
            messages.append(result)
            artifact = getattr(result, "artifact", None)
            if call["name"] == "dispatch_specialists" and isinstance(artifact, dict) and artifact.get("accepted"):
                decision = Decision(
                    kind="dispatch",
                    summary=str(call["args"].get("summary") or "Dispatch specialists."),
                    call_id=str(call["id"]),
                    accepted=list(artifact["accepted"]),
                    rejected=list(artifact["rejected"]),
                )
            elif call["name"] == "finish_research" and getattr(result, "status", "success") != "error":
                decision = Decision(kind="finish", summary=str(call["args"].get("reason") or "No further research needed."))
        if decision is not None:
            return decision, usage
    return _fallback(view, f"no decision within {settings.max_orchestrator_steps} steps"), usage


def _fallback(view: View, reason: str) -> Decision:
    trait = str(view.study.get("trait_text") or "the trait")
    if view.followup:
        return Decision(kind="finish", summary=f"Finishing without a follow-up round: {reason}.", fallback=reason)
    summary, dispatches, skipped = planning.plan_from_brief(view.brief, view.enabled, view.max_genes, trait)
    accepted, rejected, _ = validate(view, dispatches, skipped)
    return Decision(kind="dispatch", summary=summary, call_id=f"fallback_r{view.round}", accepted=accepted, rejected=rejected, fallback=reason)


def validate(
    view: View,
    dispatches: list[dict[str, Any]],
    skipped: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Split dispatches into accepted and rejected ones; return notes for the model."""
    by_case = {gene_id.casefold(): gene_id for gene_id in view.candidates}
    loci = {locus_id.casefold(): locus_id for locus_id in view.loci}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for raw in dispatches:
        specialist = str(raw.get("specialist") or "")
        if specialist not in SPECIALISTS:
            rejected.append({"specialist": specialist or "unknown", "reason": "not a specialist"})
            continue
        if specialist not in view.enabled:
            rejected.append({"specialist": specialist, "reason": "disabled for this study"})
            continue
        if specialist in seen:
            rejected.append({"specialist": specialist, "reason": "already dispatched in this call"})
            continue
        declined = planning.unavailable_reason(view.brief, specialist)
        if declined is not None:
            rejected.append({"specialist": specialist, "reason": f"no data for its domains: {declined}"})
            continue
        genes = [str(gene).strip() for gene in raw.get("focus_gene_ids") or [] if str(gene).strip()]
        unknown_genes = [gene for gene in genes if gene.casefold() not in by_case]
        focus_loci = [str(locus).strip() for locus in raw.get("focus_loci") or [] if str(locus).strip()]
        unknown_loci = [locus for locus in focus_loci if locus.casefold() not in loci]
        if unknown_genes or unknown_loci:
            reason = "; ".join(
                part
                for part in (
                    f"unknown gene ids: {', '.join(unknown_genes)}" if unknown_genes else "",
                    f"unknown loci: {', '.join(unknown_loci)}" if unknown_loci else "",
                )
                if part
            )
            rejected.append({"specialist": specialist, "reason": reason})
            continue
        resolved = list(dict.fromkeys(by_case[gene.casefold()] for gene in genes))
        if not resolved and not focus_loci:
            rejected.append({"specialist": specialist, "reason": "no focus genes or loci"})
            continue
        if not str(raw.get("instructions") or "").strip() or not str(raw.get("rationale") or "").strip():
            rejected.append({"specialist": specialist, "reason": "missing instructions or rationale"})
            continue
        if len(resolved) > view.max_genes:
            notes.append(f"{specialist}: kept the first {view.max_genes} of {len(resolved)} genes")
            resolved = resolved[: view.max_genes]
        seen.add(specialist)
        accepted.append(
            {
                "specialist": specialist,
                "focus_gene_ids": resolved,
                "focus_loci": list(dict.fromkeys(loci[locus.casefold()] for locus in focus_loci)),
                "instructions": str(raw.get("instructions") or "").strip(),
                "rationale": str(raw.get("rationale") or "").strip(),
            }
        )
    for item in skipped or []:
        name = str(item.get("specialist") or "")
        if name in view.enabled and name not in seen and not any(entry["specialist"] == name for entry in rejected):
            rejected.append({"specialist": name, "reason": str(item.get("reason") or "not needed")})
    return accepted, rejected, notes


def _tools(view: View) -> list[BaseTool]:
    def inspect_triage(locus_id: str | None = None) -> str:
        """Show the triage of one locus with more ranked candidates, or a summary of every locus.

        Args:
            locus_id: A locus id such as L1; omit for the overview.
        """
        return _inspect(view, locus_id)

    def dispatch_specialists(summary: str, dispatches: list[DispatchSpec], skipped: list[Skip] = []) -> tuple[str, dict[str, Any]]:
        """Dispatch specialists, each on its own focus genes and loci with instructions and a one-sentence rationale.

        Args:
            summary: One sentence on the overall plan, shown to the user.
            dispatches: One entry per specialist; gene ids must come from the candidate list.
            skipped: Specialists you leave out on purpose, with the reason.
        """
        raw = [item.model_dump() if isinstance(item, BaseModel) else dict(item) for item in dispatches]
        skips = [item.model_dump() if isinstance(item, BaseModel) else dict(item) for item in skipped or []]
        accepted, rejected, notes = validate(view, raw, skips)
        lines = [f"accepted {len(accepted)}: " + (", ".join(f"{item['specialist']} ({len(item['focus_gene_ids'])} genes)" for item in accepted) or "none")]
        lines += [f"rejected {item['specialist']}: {item['reason']}" for item in rejected]
        lines += notes
        if not accepted:
            lines.append("Nothing was dispatched. Fix the entries (use ids from the candidate list) and call dispatch_specialists again, or finish_research.")
        return "\n".join(lines), {"accepted": accepted, "rejected": rejected}

    def finish_research(reason: str) -> str:
        """End research without (further) specialists.

        Args:
            reason: One sentence on why no further round is needed.
        """
        return "Finishing."

    return [
        StructuredTool.from_function(inspect_triage, parse_docstring=True),
        agent_tools.get_evidence,
        StructuredTool.from_function(dispatch_specialists, parse_docstring=True, response_format="content_and_artifact", handle_validation_error=True),
        agent_tools.think,
        StructuredTool.from_function(finish_research, parse_docstring=True),
    ]


def _inspect(view: View, locus_id: str | None) -> str:
    entries = {str(entry["locus_id"]): entry for entry in view.brief.get("loci") or []}
    if not locus_id:
        lines = ["loci overview:"]
        for entry in entries.values():
            tops = ", ".join(f"{gene['gene_id']} {gene.get('score')} {gene.get('tier')}" for gene in entry.get("top") or [])
            lines.append(
                f"{entry['locus_id']} {entry.get('region')} lead {entry.get('lead_snp')} genes={entry.get('n_genes')} "
                f"no_coverage={','.join(entry.get('no_coverage') or []) or '-'} top: {tops}"
            )
        return "\n".join(lines)
    key = next((name for name in entries if name.casefold() == locus_id.casefold()), None)
    if key is None:
        return f"unknown locus {locus_id}; known: {', '.join(entries)}"
    entry = entries[key]
    rows = sorted((row for row in view.matrix if row.get("locus_id") == key), key=lambda row: -float(row.get("score") or 0))
    lines = [json.dumps(entry, ensure_ascii=False, separators=(",", ":")), f"ranked candidates of {key} (score, tier, evidence counts):"]
    for row in rows[:MORE_GENES_PER_LOCUS]:
        ref = view.candidates.get(str(row["gene_id"])) or {}
        counts = ",".join(f"{name}={count}" for name, count in (row.get("counts") or {}).items())
        lines.append(f"{row['gene_id']} score={row.get('score')} {row.get('tier')} dist={ref.get('distance_bp')} {counts}")
    return "\n".join(lines)


def _context(view: View, settings: StudyConfiguration) -> PromptContext:
    profile = view.profile or {}
    round_rule = (
        "A follow-up is allowed only if it is narrower than round 1; otherwise finish."
        if view.followup
        else ("No follow-up round is allowed after this one." if view.max_rounds <= 1 else "After the specialists report you may order one narrower follow-up.")
    )
    return PromptContext(
        species=str(view.study.get("species") or ""),
        assembly=str(view.study.get("assembly") or ""),
        trait=str(view.study.get("trait_text") or ""),
        max_steps=settings.max_orchestrator_steps,
        profile_key=profile.get("key"),
        profile_terms=tuple(profile.get("terms") or ()),
        profile_keywords=tuple(profile.get("keywords") or ()),
        seed_families=tuple(profile.get("seed_families") or ()),
        extra={
            "max_focus_genes": str(view.max_genes),
            "round": str(view.round),
            "max_rounds": str(view.max_rounds),
            "round_rule": round_rule,
        },
    )


def _briefing(view: View) -> str:
    by_locus: dict[str, list[str]] = {}
    for gene_id, ref in view.candidates.items():
        by_locus.setdefault(str(ref.get("locus_id")), []).append(gene_id)
    ranked = {str(row["gene_id"]): float(row.get("score") or 0) for row in view.matrix}
    lines = [
        f"Study: {view.study.get('species')} {view.study.get('assembly')}, trait \"{view.study.get('trait_text')}\", "
        f"{len(view.loci)} loci, {len(view.candidates)} candidate genes. Round {view.round} of at most {view.max_rounds}.",
        f"Specialists enabled: {', '.join(view.enabled)}.",
        f"Valid locus ids: {', '.join(view.loci)}.",
        "Candidate gene ids by locus (highest provisional score first):",
    ]
    for locus_id, genes in by_locus.items():
        genes.sort(key=lambda gene: -ranked.get(gene, 0.0))
        more = len(genes) - MAX_CANDIDATES_SHOWN
        lines.append(f"{locus_id}: {', '.join(genes[:MAX_CANDIDATES_SHOWN])}" + (f" (+{more} more via inspect_triage)" if more > 0 else ""))
    lines += ["Triage brief (JSON):", render_brief(view.brief)]
    if view.followup:
        lines += [
            "Round 1 dispatches:",
            *(
                f"- {item.get('specialist')} ({item.get('agent_id')}): {', '.join(item.get('focus_gene_ids') or [])}"
                for item in view.previous
            ),
            "Delta brief (JSON):",
            json.dumps(view.delta or {}, ensure_ascii=False, separators=(",", ":")),
            "Decide: one narrower follow-up with dispatch_specialists, or finish_research.",
        ]
    else:
        lines.append("Decide which specialists examine which candidates, then call dispatch_specialists.")
    return "\n".join(lines)


def _tasks(view: View, decision: Decision) -> list[SpecialistTask]:
    study = {
        "species": view.study.get("species"),
        "assembly": view.study.get("assembly"),
        "trait_text": view.study.get("trait_text"),
        "profile": view.profile,
    }
    tops = {str(gene["gene_id"]): gene for entry in view.brief.get("loci") or [] for gene in entry.get("top") or []}
    scores = {str(row["gene_id"]): row for row in view.matrix}

    def gene_context(gene_id: str) -> dict[str, Any]:
        ref = view.candidates.get(gene_id) or {}
        top = tops.get(gene_id) or {}
        row = scores.get(gene_id) or {}
        return {
            "locus_id": ref.get("locus_id"),
            "distance_bp": ref.get("distance_bp"),
            "nearest_snp": ref.get("nearest_snp"),
            "symbol": ref.get("symbol") or top.get("symbol"),
            "score": top.get("score", row.get("score")),
            "tier": top.get("tier", row.get("tier")),
            "why": top.get("why") or [],
        }

    def locus_context(locus_id: str) -> dict[str, Any]:
        locus = view.loci.get(locus_id) or {}
        return {key: locus.get(key) for key in ("chrom", "start", "end", "lead_snp", "lead_pos", "n_genes")}

    return [
        {
            "agent_id": f"{decision.call_id}-{item['specialist']}",
            "specialist": item["specialist"],
            "round": view.round,
            "focus_gene_ids": list(item["focus_gene_ids"]),
            "focus_loci": list(item["focus_loci"]),
            "instructions": item["instructions"],
            "rationale": item["rationale"],
            "study": study,
            "context": {
                "genes": {gene_id: gene_context(gene_id) for gene_id in item["focus_gene_ids"]},
                "loci": {locus_id: locus_context(locus_id) for locus_id in item["focus_loci"]},
            },
        }
        for item in decision.accepted
    ]


def _specialists_detail(state: StudyState) -> str:
    results = list(state.get("specialist_results") or [])
    findings = sum(len(result.get("finding_ids") or []) for result in results)
    return f"{len(results)} specialist runs returned {findings} findings"


def _add_usage(usage: dict[str, Any], response: AIMessage | Any) -> None:
    metadata = getattr(response, "usage_metadata", None) or {}
    usage["input_tokens"] += int(metadata.get("input_tokens") or 0)
    usage["output_tokens"] += int(metadata.get("output_tokens") or 0)
    usage["calls"] += 1


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:120]
