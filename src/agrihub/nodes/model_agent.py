"""Model step for trait studies: choose registered models, run them and place their SNPs.

Every registered model is checked against the study (species, trait,
assembly, available result files). A chat model picks among the applicable
ones with a stated rationale, published as a ``select_model`` decision; an
invalid pick falls back to the default (precomputed results before catalog
hits). Each chosen adapter's SNPs keep their ``score_type`` and
``model_id``, and are placed and lifted like user SNPs. With no applicable
model the phase fails with the reasons.
"""

import asyncio
import json
import time
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from agrihub import events, models
from agrihub.configuration import StudyConfiguration
from agrihub.nodes.intake import StudyInputError, place_and_lift
from agrihub.state import StudyState, StudyWarning, TraitStudy, parse_study
from agrihub.trait_models.adapters import (
    Candidate,
    PrecomputedResultsAdapter,
    adapter_for,
    evaluate_models,
)
from agrihub.trait_models.registry import load_model_registry

MAX_STEPS = 2
_PRIORITY = {"precomputed_results": 0, "catalog_top_hits": 1}
SYSTEM = (
    "You choose which registered models propose SNPs for a post-GWAS candidate-gene study. "
    "Pick only from the applicable candidates listed, prefer a model run on this trait over published catalog hits, "
    "and say in one or two sentences why. Never mix score types into one number; each model keeps its own. "
    "Call select_models exactly once."
)


async def model_agent(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Choose and run models for a trait study and return their placed SNPs."""
    events.phase("model")
    started = time.monotonic()
    study = parse_study(state.get("study") or {})
    if not isinstance(study, TraitStudy):
        raise StudyInputError("the model step needs a trait study")
    events.agent_started(events.MODEL, focus={"species": study.species, "trait": study.trait_text}, max_steps=MAX_STEPS)
    candidates = await asyncio.to_thread(evaluate_models, study)
    applicable = [item for item in candidates if item.applicability.ok]
    rejected = [
        {"specialist": item.model_id, "reason": "; ".join(item.applicability.reasons) or "not applicable"}
        for item in candidates
        if not item.applicability.ok
    ]
    if not applicable:
        message = f"No applicable model is registered for {study.trait_text} in {study.species}"
        detail = message + (": " + "; ".join(f"{row['specialist']}: {row['reason']}" for row in rejected) if rejected else ".")
        events.decision("select_model", message + ".", rejected=rejected, selected=[], agent=events.MODEL)
        events.agent_completed(events.MODEL, summary=message, findings=0, duration_ms=_ms(started), status="failed")
        events.phase("model", "failed", detail=detail)
        raise StudyInputError(detail)
    choice, rationale = await _choose(study, applicable, config)
    selected = []
    snps: list[dict[str, Any]] = []
    warnings: list[StudyWarning] = []
    runs: list[dict[str, Any]] = []
    queue = list(choice)
    tried: set[str] = set()
    while queue:
        candidate, dataset = queue.pop(0)
        tried.add(candidate.model_id)
        spec = load_model_registry().get(candidate.model_id)
        adapter = adapter_for(spec)
        ranked = await asyncio.to_thread(adapter.run, study, dataset)
        given_on = ranked[0].assembly if ranked else candidate.assembly
        placed, placed_warnings = await asyncio.to_thread(place_and_lift, study, [snp.as_input() for snp in ranked], given_on)
        warnings.extend(placed_warnings)
        snps.extend(snp.model_dump(mode="json") for snp in placed)
        source = adapter.path_for(dataset) if isinstance(adapter, PrecomputedResultsAdapter) and dataset else None
        runs.append(
            {
                "model_id": spec.id,
                "name": spec.name,
                "adapter": spec.adapter,
                "label": spec.label,
                "score_type": spec.score_type,
                "dataset": dataset,
                "assembly": given_on,
                "lifted_to": study.assembly if given_on != study.assembly else None,
                "proposed": len(ranked),
                "placed": len(placed),
                "source_file": source,
                "notes": spec.notes,
                "benchmarks": [benchmark.model_dump() for benchmark in spec.benchmarks],
            }
        )
        selected.append(
            {
                "model_id": spec.id,
                "name": spec.name,
                "label": spec.label,
                "score_type": spec.score_type,
                "dataset": dataset,
                "assembly": given_on,
            }
        )
        if not queue and not snps:
            remaining = [item for item in default_order(applicable) if item.model_id not in tried]
            if remaining:
                rationale += f" {spec.name} placed no SNPs, so {remaining[0].name} was used next."
                queue.append((remaining[0], _dataset(study, remaining[0], None)))
    events.decision("select_model", rationale, rejected=rejected, selected=selected, agent=events.MODEL)
    unique, duplicates = _dedupe(snps)
    warnings.extend(duplicates)
    summary = "; ".join(f"{run['name']}{' ' + run['dataset'] if run['dataset'] else ''}: {run['placed']} of {run['proposed']} SNPs placed" for run in runs)
    events.agent_completed(events.MODEL, summary=summary, findings=0, duration_ms=_ms(started))
    dumped = [warning.model_dump(exclude_none=True) for warning in warnings]
    if not unique:
        events.phase("model", "failed", detail=f"the chosen models proposed no placeable SNPs: {summary}", warnings=dumped)
        raise StudyInputError(f"the chosen models proposed no placeable SNPs: {summary}")
    events.phase("model", "completed", detail=summary, warnings=dumped)
    return {
        "snps": unique,
        "model_result": {
            "rationale": rationale,
            "runs": runs,
            "score_types": sorted({str(run["score_type"]) for run in runs}),
            "rejected": rejected,
            "snp_count": len(unique),
        },
        "warnings": [*(state.get("warnings") or []), *dumped],
    }


def _dedupe(snps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[StudyWarning]]:
    """Keep the first SNP at each position; a later model's SNP there is reported, not merged."""
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    warnings: list[StudyWarning] = []
    for snp in snps:
        key = (str(snp["chrom"]), int(snp["pos"]))
        if key in seen:
            first = seen[key]
            warnings.append(
                StudyWarning(
                    code="duplicate",
                    message=f"{snp['raw']} ({snp.get('model_id')}) is at the same position as {first['raw']} ({first.get('model_id')}); kept once, scores not combined",
                    snp=snp["raw"],
                )
            )
            continue
        seen[key] = snp
        kept.append(snp)
    return kept, warnings


def default_order(applicable: list[Candidate]) -> list[Candidate]:
    """Return applicable models in the default preference: precomputed results before catalog hits."""
    return sorted(applicable, key=lambda item: _PRIORITY.get(item.adapter, 9))


def default_choice(study: TraitStudy, applicable: list[Candidate]) -> list[tuple[Candidate, str | None]]:
    """Return the preferred applicable model and dataset, honouring the study's preferences."""
    preferred = default_order(applicable)
    wanted = [item for item in preferred if any(_prefers(study, item, pref) for pref in study.model_preferences)]
    first = (wanted or preferred)[0]
    return [(first, _dataset(study, first, None))]


def _prefers(study: TraitStudy, candidate: Candidate, preference: str) -> bool:
    model_id, _, dataset = preference.partition(":")
    if model_id == candidate.model_id:
        return True
    return preference in candidate.applicability.datasets or dataset in candidate.applicability.datasets


def _dataset(study: TraitStudy, candidate: Candidate, requested: str | None) -> str | None:
    datasets = candidate.applicability.datasets
    if not datasets:
        return None
    for preference in [requested or "", *study.model_preferences]:
        name = preference.partition(":")[2] or preference
        if name in datasets:
            return name
    return datasets[-1]


async def _choose(
    study: TraitStudy,
    applicable: list[Candidate],
    config: RunnableConfig,
) -> tuple[list[tuple[Candidate, str | None]], str]:
    """Ask the chat model to pick; fall back to the default choice when it does not pick validly."""
    settings = StudyConfiguration.from_runnable_config(config)
    picked: dict[str, Any] = {}

    @tool
    def select_models(model_ids: list[str], rationale: str, datasets: list[str] | None = None) -> str:
        """Choose the models whose SNPs the study uses.

        Args:
            model_ids: Ids of applicable candidates, most preferred first.
            rationale: One or two sentences on why these models fit the study.
            datasets: For precomputed results, the dataset to read (for example PH_2019).
        """
        picked.update({"model_ids": list(model_ids), "rationale": rationale, "datasets": list(datasets or [])})
        return "recorded"

    candidates_json = json.dumps(
        [
            {
                "model_id": item.model_id,
                "name": item.name,
                "label": item.label,
                "score_type": item.score_type,
                "assembly": item.assembly,
                "datasets": item.applicability.datasets,
                "benchmarks": item.benchmarks,
            }
            for item in applicable
        ],
        separators=(",", ":"),
    )
    messages: list[BaseMessage] = [
        SystemMessage(content=SYSTEM),
        HumanMessage(
            content=(
                f'Study: {study.species} ({study.assembly}), trait "{study.trait_text}". '
                f"Preferences: {', '.join(study.model_preferences) or 'none'}.\n"
                f"Candidates JSON: {candidates_json}"
            )
        ),
    ]
    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        model = models.tool_model(settings.orchestrator_model, [select_models], max_tokens=settings.model_max_tokens, max_retries=settings.model_max_retries)
        for _ in range(MAX_STEPS):
            response = await model.ainvoke(messages, config)
            metadata = getattr(response, "usage_metadata", None) or {}
            usage["input_tokens"] += int(metadata.get("input_tokens") or 0)
            usage["output_tokens"] += int(metadata.get("output_tokens") or 0)
            events.agent_usage(events.MODEL, model=settings.orchestrator_model, **usage)
            messages.append(response)
            calls = list(getattr(response, "tool_calls", None) or [])
            for call in calls:
                if call["name"] == "select_models":
                    await select_models.ainvoke(call["args"])
                    messages.append(ToolMessage(content="recorded", tool_call_id=str(call["id"]), name="select_models"))
            if picked:
                break
            messages.append(HumanMessage(content="Call select_models now."))
    except Exception as exc:  # noqa: BLE001
        picked.clear()
        picked["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    by_id = {item.model_id: item for item in applicable}
    chosen = [by_id[model_id] for model_id in dict.fromkeys(picked.get("model_ids") or []) if model_id in by_id]
    if not chosen:
        fallback = default_choice(study, applicable)
        reason = picked.get("error") or ("the pick named no applicable model" if picked else "no model was picked")
        names = ", ".join(item.name for item, _ in fallback)
        return fallback, f"Using {names}, the default for this trait ({reason})."
    datasets = list(picked.get("datasets") or [])
    choice = [(item, _dataset(study, item, datasets[index] if index < len(datasets) else None)) for index, item in enumerate(chosen)]
    return choice, str(picked.get("rationale") or "Chosen by the model agent.")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
