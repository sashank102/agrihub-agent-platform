"""Rank every candidate, re-check the top claims, and record window sensitivity.

The rubric scores harvest evidence. Supporting findings add only evidence
they cite that harvest did not already store, once. The verifier lane
re-checks the top claims against a different source and never adds a claim.
Every gene's explanation is kept in the evidence store as the
``score_explanations`` output, and the full ranking is the
``candidates_full`` artifact.
"""

import asyncio
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from agrihub import events, models, scoring, verifier
from agrihub.configuration import StudyConfiguration, run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.harvest import (
    HarvestContext,
    brief_domains,
    harvest_context,
    harvest_genes,
)
from agrihub.nodes.locus_builder import genes_at_flank, load_candidates
from agrihub.state import CandidateGene, Finding, Locus, RankedCandidate, StudyState
from agrihub.tools.store_tools import get_evidence
from agrihub_data.registry import load_species

TABLE_COLUMNS = (
    "rank",
    "gene_id",
    "locus_id",
    "symbol",
    "rank_in_locus",
    "score",
    "tier",
    "category_points",
    "stability",
    "chrom",
    "start",
    "end",
    "distance_bp",
    "overlaps_snp",
    "nearest_snp",
    "lead_snp",
    "defline",
    "shortlist",
    "verifier_status",
)
"""The ``candidates_table`` artifact columns, a subset of ``RankedCandidate``."""


async def rank_verify(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Score every candidate, keep the top genes of each locus, and report their stability."""
    events.phase("ranking")
    study = state.get("study") or {}
    top_k = int(study.get("top_k_per_locus") or 5)
    loci = [Locus.model_validate(raw) for raw in state.get("loci") or []]
    store = EvidenceStore.for_run(run_id_from_config(config))
    candidates = await asyncio.to_thread(load_candidates, list(state.get("candidates") or []), store)
    ranking, summary = await asyncio.to_thread(_rank, study, loci, candidates, store, top_k)
    await _verifier_lane(summary, config)
    rows = [{key: item[key] for key in TABLE_COLUMNS if key in item} for item in ranking]
    events.artifact_created("candidates_table", title="Ranked candidates", rows=rows)
    events.artifact_created("candidates_full", title="Full candidate ranking", rows=rows)
    events.phase(
        "ranking",
        "completed",
        detail=f"{len(ranking)} candidates ranked across {len(loci)} loci (rubric v{summary['rubric_version']})",
    )
    return {"ranking": ranking, "scoring": summary}


def _rank(
    study: dict[str, Any],
    loci: list[Locus],
    candidates: list[CandidateGene],
    store: EvidenceStore,
    top_k: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    context = harvest_context(study, loci)
    registry = load_species(context.bundle.species)
    gene_ids = [gene.gene_id for gene in candidates]
    items = store.query(gene_ids) if gene_ids else []
    findings = store.findings()
    scores = scoring.score_candidates(
        candidates,
        items,
        profile=context.profile,
        ld_kb=registry.typical_ld_kb,
        available=context.categories,
        findings=findings,
        finding_items=items,
    )

    def rescore(genes: list[CandidateGene]) -> scoring.StudyScores:
        extra = harvest_genes(context, [gene.gene_id for gene in genes], store)
        return scoring.score_candidates(genes, extra, profile=context.profile, ld_kb=registry.typical_ld_kb, available=context.categories)

    sensitivity = scoring.window_sensitivity(
        loci,
        scores,
        lambda locus, flank: genes_at_flank(context.bundle, locus, flank),
        rescore,
        top_k=top_k,
    )
    explanations_ref = store.put_output(
        "score_explanations",
        {
            "header": f"rubric v{scores.rubric_version} explanation per gene",
            "rows": [scores.explain(gene_id) for gene_id in sorted(scores.genes)],
        },
    )
    positions = {gene.gene_id: gene for gene in candidates}
    ranked: list[RankedCandidate] = []
    for locus in loci:
        for gene in scores.ranked(locus.locus_id):
            ranked.append(
                _candidate(gene, findings, sensitivity, positions.get(gene.gene_id), locus).model_copy(
                    update={"shortlist": (gene.rank_in_locus or 0) <= top_k}
                )
            )
    verdicts = verifier.verify_claims(
        ranked,
        items,
        findings,
        species=str(study.get("species") or ""),
        trait=str(study.get("trait_text") or ""),
    )
    by_gene: dict[str, list[Any]] = {}
    for verdict in verdicts:
        by_gene.setdefault(verdict.gene_id, []).append(verdict)
    ranked = [
        item.model_copy(update={"verifier_status": verifier.status_for(by_gene.get(item.gene_id, []))})
        for item in ranked
    ]
    order = {locus.locus_id: index for index, locus in enumerate(loci)}
    ranked.sort(key=lambda item: (-item.score, order.get(item.locus_id, 0), item.rank_in_locus or 0, item.gene_id))
    ranking = [item.model_copy(update={"rank": rank}).model_dump(mode="json") for rank, item in enumerate(ranked, start=1)]
    summary = {
        "rubric_version": scores.rubric_version,
        "explanations_ref": explanations_ref,
        "window_sensitivity": sensitivity.model_dump(mode="json"),
        "known_gene_records": _known_gene_counts(context),
        "skipped_steps": [] if context.canonical else ["orthologs", "arabidopsis", "known_genes"],
        "categories": sorted(context.categories),
        "domains": brief_domains(context.domains),
        "tiers": {tier: sum(1 for gene in scores.genes.values() if gene.tier == tier) for tier in ("T1", "T2", "T3", "T4")},
        "verification": [verdict.model_dump(mode="json") for verdict in verdicts],
        "top_k_per_locus": top_k,
    }
    return ranking, summary


async def _verifier_lane(summary: dict[str, Any], config: RunnableConfig) -> None:
    """Open the verifier lane, ask the model to re-read claims, and keep the deterministic verdicts.

    The model may call ``get_evidence``. Anything it says is ignored: verdicts
    were already computed and the lane never records a new claim.
    """
    verdicts = summary.get("verification") or []
    started = time.monotonic()
    events.agent_started(
        events.VERIFIER,
        focus={"claims": len(verdicts), "instructions": "Re-check top claims. Do not add claims."},
        max_steps=2,
    )
    events.agent_step(events.VERIFIER, 1, 2, f"Re-check {len(verdicts)} claims")
    settings = StudyConfiguration.from_runnable_config(config)
    try:
        model = models.tool_model(
            settings.verifier_model,
            [get_evidence],
            max_tokens=settings.model_max_tokens,
            max_retries=settings.model_max_retries,
        )
        preview = [{"claim_id": item.get("claim_id"), "status": item.get("status"), "gene_id": item.get("gene_id")} for item in verdicts[:12]]
        response = await model.ainvoke(
            [
                SystemMessage(content="You are the AgriHub verifier. Do not add claims. The statuses below are final."),
                HumanMessage(content=f"Claims: {preview}"),
            ],
            config,
        )
        metadata = getattr(response, "usage_metadata", None) or {}
        events.agent_usage(
            events.VERIFIER,
            model=settings.verifier_model,
            input_tokens=int(metadata.get("input_tokens") or 0),
            output_tokens=int(metadata.get("output_tokens") or 0),
        )
    except Exception as exc:  # noqa: BLE001
        events.agent_completed(
            events.VERIFIER,
            summary=f"Verifier failed: {type(exc).__name__}: {exc}",
            findings=0,
            duration_ms=int((time.monotonic() - started) * 1000),
            status="failed",
        )
        return
    counts = {status: sum(1 for item in verdicts if item.get("status") == status) for status in ("verified", "unverified", "contradicted")}
    events.agent_completed(
        events.VERIFIER,
        summary=f"Checked {len(verdicts)} claims: {counts['verified']} verified, {counts['unverified']} unverified, {counts['contradicted']} contradicted.",
        findings=0,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _candidate(
    gene: scoring.GeneScore,
    findings: list[Finding],
    sensitivity: scoring.WindowSensitivity,
    position: CandidateGene | None,
    locus: Locus,
) -> RankedCandidate:
    own = [finding for finding in findings if finding.target == gene.gene_id]
    stability = sensitivity.genes.get(gene.gene_id)
    placed: dict[str, Any] = {}
    if position is not None:
        placed = {
            "chrom": position.chrom or locus.chrom,
            "start": position.start,
            "end": position.end,
            "strand": position.strand,
            "distance_bp": position.distance_bp,
            "overlaps_snp": position.overlaps_snp,
            "nearest_snp": position.nearest_snp,
            "defline": position.defline,
        }
    return RankedCandidate(
        rank=1,
        gene_id=gene.gene_id,
        locus_id=gene.locus_id,
        symbol=gene.symbol,
        lead_snp=locus.lead_snp,
        **placed,
        rank_in_locus=gene.rank_in_locus,
        score=gene.score,
        share_of_locus=gene.share_of_locus,
        tier=gene.tier,
        category_points=gene.points(),
        reasons=[
            f"{code}: {category.reasons[0]}"
            for code, category in gene.categories.items()
            if category.points > 0 and category.reasons
        ],
        stability=stability.label if stability else None,
        flags=gene.flags,
        supporting_findings=[str(finding.finding_id) for finding in own if finding.stance == "supports"],
        conflicting_findings=[str(finding.finding_id) for finding in own if finding.stance == "conflicts"],
        evidence_ids=gene.evidence_ids(),
    )


def _known_gene_counts(context: HarvestContext) -> dict[str, int]:
    registry = load_species(context.bundle.species)
    rows = context.bundle.rows_raw(
        "SELECT count(*), count(*) FILTER (WHERE assembly = ?) FROM known_genes",
        [registry.canonical_assembly],
    )
    total, canonical = rows[0] if rows else (0, 0)
    return {"total": int(total), "canonical": int(canonical)}
