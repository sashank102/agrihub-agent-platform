"""Rank candidates per locus. A placeholder until the scoring rubric exists."""

import math
from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.state import RankedCandidate, StudyState

DISTANCE_SCALE_BP = 100_000


async def rank_verify(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Score each top gene by distance and supporting findings."""
    events.phase("ranking")
    study = state.get("study") or {}
    top_k = int(study.get("top_k_per_locus") or 5)
    store = EvidenceStore.for_run(run_id_from_config(config))
    findings = store.findings()
    ranked: list[RankedCandidate] = []
    by_locus: dict[str, list[dict[str, Any]]] = {}
    for candidate in state.get("candidates") or []:
        by_locus.setdefault(str(candidate["locus_id"]), []).append(candidate)
    for locus_id, candidates in by_locus.items():
        scored: list[RankedCandidate] = []
        for candidate in candidates:
            gene_id = str(candidate["gene_id"])
            gene_findings = [finding for finding in findings if finding.target == gene_id]
            supporting = [f for f in gene_findings if f.stance != "conflicts"]
            conflicting = [f for f in gene_findings if f.stance == "conflicts"]
            positional = 20 * math.exp(-int(candidate["distance_bp"]) / DISTANCE_SCALE_BP)
            scored.append(
                RankedCandidate(
                    rank=1,
                    gene_id=gene_id,
                    locus_id=locus_id,
                    score=round(positional + 5 * len(supporting), 2),
                    tier="T3" if supporting else "T4",
                    supporting_findings=[str(f.finding_id) for f in supporting],
                    conflicting_findings=[str(f.finding_id) for f in conflicting],
                    evidence_ids=sorted(
                        {evidence_id for f in gene_findings for evidence_id in f.evidence_ids}
                    ),
                )
            )
        scored.sort(key=lambda item: (-item.score, item.gene_id))
        ranked.extend(scored[:top_k])
    ranked.sort(key=lambda item: (-item.score, item.gene_id))
    ranking = [
        item.model_copy(update={"rank": rank}).model_dump(mode="json")
        for rank, item in enumerate(ranked, start=1)
    ]
    events.phase("ranking", "completed", detail=f"{len(ranking)} candidates ranked")
    return {"ranking": ranking}
