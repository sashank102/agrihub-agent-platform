"""Write the structured report and export the evidence ledger.

The writer is still deterministic: it lays out the real loci, the rubric
ranking and its stability, the specialists' findings per candidate, and the
limitations of this build. The LLM writer that adds prose arrives in plan 7.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.harvest import bundle_sources
from agrihub.scoring import load_rubric
from agrihub.state import (
    Locus,
    RankedCandidate,
    Report,
    SourceRef,
    StudyState,
    StudyWarning,
)
from agrihub_data.bundle import BundleMissingError, open_bundle

ArtifactSink = Callable[..., Awaitable[str | None]]
"""Persist one artifact and return its id.

Called as ``sink(config, kind=..., title=..., content=..., metadata=...)``.
"""


def make_writer(artifact_sink: ArtifactSink | None = None) -> Callable[..., Any]:
    """Return the writer node bound to an optional artifact sink."""

    async def writer(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
        events.phase("reporting")
        store = EvidenceStore.for_run(run_id_from_config(config))
        report, snapshot, snapshot_path = await asyncio.to_thread(_prepare, state, store)
        report_id = None
        snapshot_id = None
        if artifact_sink is not None:
            report_id = await artifact_sink(
                config,
                kind="report",
                title=report.title,
                content=report.model_dump(mode="json"),
                metadata={"schema": "agrihub.report/v1"},
            )
            snapshot_id = await artifact_sink(
                config,
                kind="evidence_snapshot",
                title="Evidence ledger",
                content=snapshot,
                metadata={
                    "evidence": len(snapshot["evidence"]),
                    "findings": len(snapshot["findings"]),
                    "path": str(snapshot_path),
                },
            )
        events.artifact_created("evidence_snapshot", title="Evidence ledger", artifact_id=snapshot_id)
        events.artifact_created("report", title=report.title, artifact_id=report_id)
        store.close()
        events.phase("reporting", "completed")
        return {
            "report": report.model_dump(mode="json"),
            "messages": [AIMessage(content=report.markdown)],
            "run_status": "completed",
        }

    return writer


def _prepare(state: StudyState, store: EvidenceStore) -> tuple[Report, dict[str, Any], Path]:
    study = state.get("study") or {}
    try:
        sources = [source for source, _ in bundle_sources(open_bundle(str(study.get("species") or "")))]
    except BundleMissingError:
        sources = []
    report = _report(state, store, sources)
    return report, store.snapshot(), store.export()


def _report(state: StudyState, store: EvidenceStore, sources: list[SourceRef]) -> Report:
    study = state.get("study") or {}
    scoring = state.get("scoring") or {}
    loci = [Locus.model_validate(raw) for raw in state.get("loci") or []]
    candidates = [RankedCandidate.model_validate(raw) for raw in state.get("ranking") or []]
    warnings = [StudyWarning.model_validate(raw) for raw in state.get("warnings") or []]
    sensitivity = scoring.get("window_sensitivity") or {}
    stability = {
        "flanks_bp": sensitivity.get("flanks_bp") or [],
        "top_k": sensitivity.get("top_k"),
        "loci": sensitivity.get("loci") or [],
    }
    title = f"{study.get('trait_text')} candidate genes in {study.get('species')}"
    limitations = _limitations(study, scoring, warnings)
    return Report(
        title=title,
        species=str(study.get("species") or ""),
        assembly=str(study.get("assembly") or ""),
        trait=str(study.get("trait_text") or ""),
        mode=study.get("mode") or "snps",
        provenance={
            "window": study.get("window") or {},
            "model": state.get("model_result") or {},
            "rounds": int(state.get("round") or 0),
            "rubric_version": scoring.get("rubric_version"),
            "score_explanations_ref": scoring.get("explanations_ref"),
            "evidence_matrix_ref": (state.get("triage_brief") or {}).get("matrix_ref"),
            "tiers": scoring.get("tiers") or {},
        },
        loci=loci,
        candidates=candidates,
        stability=stability,
        warnings=warnings,
        limitations=limitations,
        suggested_validations=[
            "Haplotype analysis of the top candidates in the association panel.",
            "qRT-PCR of the top candidates in lines with extreme phenotypes.",
            "Mutant or transgenic validation of tier T1/T2 candidates.",
        ],
        sources=sources,
        evidence_count=store.count(),
        finding_count=len(store.findings()),
        markdown=_markdown(title, study, loci, candidates, warnings, limitations),
    )


def _limitations(study: dict[str, Any], scoring: dict[str, Any], warnings: list[StudyWarning]) -> list[str]:
    rubric = load_rubric()
    known = scoring.get("known_gene_records") or {}
    missing = [f"{code} ({spec.name.lower()})" for code, spec in rubric.categories.items() if not spec.available]
    window = study.get("window") or {}
    items = [
        (
            f"Known-gene coverage is thin: the bundle has {known.get('total', 0)} curated LIS trait genes "
            f"({known.get('canonical', 0)} on the canonical assembly) because the SoyBase gene-symbol registry "
            "could not be downloaded. Tier T1 needs such a record, so T1 is rare, and a gene without one is "
            "not evidence against it."
        ),
        (
            f"Categories {', '.join(missing)} have no data in the core bundle yet; they score 0 for every gene "
            "and are reported as not available, not as negative evidence."
        ),
        f"Category A uses distance only; {rubric.positional.not_available}.",
        (
            f"Loci use fixed ±{int(window.get('flank_bp') or 0) // 1000} kb windows; LD-based windows need a "
            "genotype VCF. Window sensitivity at 50/100/250 kb is reported per candidate."
        ),
        (
            "The orchestrator and specialists are language-model agents whose findings cite stored evidence; findings "
            "are listed per candidate but do not change scores. The verifier agent is not built yet, so the ranking "
            "is the rubric alone and no claim has been independently re-checked."
        ),
    ]
    if scoring.get("skipped_steps"):
        items.append(
            "Orthology, TAIR and curated-gene evidence are keyed to the canonical assembly and were skipped for "
            f"{study.get('assembly')}: {', '.join(scoring['skipped_steps'])}."
        )
    if warnings:
        items.append(f"{len(warnings)} input warnings: " + "; ".join(warning.message for warning in warnings[:3]))
    return items


def _markdown(
    title: str,
    study: dict[str, Any],
    loci: list[Locus],
    candidates: list[RankedCandidate],
    warnings: list[StudyWarning],
    limitations: list[str],
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Assembly {study.get('assembly')}; {len(loci)} loci; {len(candidates)} ranked candidates. "
        "Scores come from the deterministic rubric; agent findings are counted per candidate but do not change scores.",
        "",
        "## Loci",
        "",
        "| Locus | Region | Lead SNP | SNPs | Genes |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        f"| {locus.locus_id} | {locus.chrom}:{locus.start}-{locus.end} | {locus.lead_snp} | "
        f"{len(locus.snp_positions) or 1} | {locus.n_genes}{' (capped)' if locus.genes_capped else ''} |"
        for locus in loci
    )
    for locus in loci:
        rows = sorted(
            (item for item in candidates if item.locus_id == locus.locus_id),
            key=lambda item: item.rank_in_locus or 0,
        )
        if not rows:
            continue
        lines.extend(
            [
                "",
                f"## {locus.locus_id} candidates",
                "",
                "| # | Gene | Position | Distance | Tier | Score | Share | Evidence | Findings | Stability |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        lines.extend(
            f"| {item.rank_in_locus} | {item.gene_id}{f' ({item.symbol})' if item.symbol else ''} | "
            f"{_position(item)} | {_distance(item)} | {item.tier} | "
            f"{item.score:g} | {item.share_of_locus or 0:.2f} | {'; '.join(item.reasons[:3])} | {_findings(item)} | {item.stability or ''} |"
            for item in rows
        )
    if warnings:
        lines.extend(["", "## Input warnings", ""])
        lines.extend(f"- {warning.message}" for warning in warnings)
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {item}" for item in limitations)
    return "\n".join(lines)


def _position(item: RankedCandidate) -> str:
    if item.chrom is None or item.start is None or item.end is None:
        return ""
    return f"{item.chrom}:{item.start}-{item.end} ({item.strand})"


def _distance(item: RankedCandidate) -> str:
    if item.distance_bp is None:
        return ""
    if item.overlaps_snp:
        return f"overlaps {item.nearest_snp or item.lead_snp or 'SNP'}"
    return f"{item.distance_bp / 1000:.1f} kb to {item.nearest_snp or item.lead_snp or 'SNP'}"


def _findings(item: RankedCandidate) -> str:
    parts = []
    if item.supporting_findings:
        parts.append("supports " + ", ".join(item.supporting_findings))
    if item.conflicting_findings:
        parts.append("conflicts " + ", ".join(item.conflicting_findings))
    return "; ".join(parts)
