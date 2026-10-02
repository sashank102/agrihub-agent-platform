"""Rank candidates per locus with the deterministic rubric and check window sensitivity.

The verifier agent that re-checks top claims against an independent source
arrives with the LLM agents; until then ranking is the rubric alone. Every
gene's explanation is kept in the evidence store as the
``score_explanations`` output.
"""

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events, scoring
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.harvest import (
    HarvestContext,
    brief_domains,
    harvest_context,
    harvest_genes,
)
from agrihub.nodes.locus_builder import genes_at_flank, load_candidates
from agrihub.state import CandidateGene, Finding, Locus, RankedCandidate, StudyState
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
    events.artifact_created(
        "candidates_table",
        title="Ranked candidates",
        rows=[{key: item[key] for key in TABLE_COLUMNS} for item in ranking],
    )
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
    scores = scoring.score_candidates(candidates, items, profile=context.profile, ld_kb=registry.typical_ld_kb, available=context.categories)

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
    findings = store.findings()
    positions = {gene.gene_id: gene for gene in candidates}
    ranked: list[RankedCandidate] = []
    for locus in loci:
        for gene in scores.ranked(locus.locus_id)[:top_k]:
            ranked.append(_candidate(gene, findings, sensitivity, positions.get(gene.gene_id), locus))
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
    }
    return ranking, summary


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
