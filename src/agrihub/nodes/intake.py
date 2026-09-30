"""Validate the study request and route by input mode."""

from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Overwrite

from agrihub import events
from agrihub.state import SnpInput, SnpStudy, StudyState, parse_study


async def intake(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Validate the study, dedupe SNPs, and reset per-study state.

    The form guarantees completeness, so there is no clarification step.
    """
    events.phase("intake")
    raw = state.get("study")
    if not raw:
        raise ValueError("agrihub_study input needs a 'study' object")
    study = parse_study(raw)
    snps: list[dict[str, Any]] = []
    detail = "trait mode: SNPs come from the model step"
    if isinstance(study, SnpStudy):
        unique = _dedupe(study.snps)
        snps = [snp.model_dump(mode="json") for snp in unique]
        detail = f"{len(unique)} unique SNPs of {len(study.snps)} submitted"
    events.phase("intake", "completed", detail=detail)
    return {
        "study": study.model_dump(mode="json"),
        "snps": snps,
        "model_result": {},
        "loci": [],
        "candidates": [],
        "triage_brief": {},
        "dispatches": [],
        "findings": Overwrite([]),
        "specialist_results": Overwrite([]),
        "round": 0,
        "ranking": [],
        "report": None,
        "run_status": "running",
    }


def route_after_intake(state: StudyState) -> Literal["model_agent", "locus_builder"]:
    """Send trait studies through the model agent and SNP studies to loci."""
    if (state.get("study") or {}).get("mode") == "trait":
        return "model_agent"
    return "locus_builder"


def _dedupe(snps: list[SnpInput]) -> list[SnpInput]:
    seen: set[tuple[str, ...]] = set()
    unique: list[SnpInput] = []
    for snp in snps:
        if snp.chrom is not None and snp.pos is not None:
            key: tuple[str, ...] = ("pos", snp.chrom.lower(), str(snp.pos))
        else:
            key = ("marker", str(snp.marker_id).lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(snp)
    return unique
