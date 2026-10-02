"""Batch evidence harvest over every candidate gene, and the triage brief.

Harvest calls the query functions directly (no LLM wrappers). Each step
runs over the candidates in chunks in a worker thread, stores the rows'
``evidence()`` and reports ``evidence.progress`` for its category; the
steps run concurrently, at most ``max_concurrency`` of the run config at a
time when it is set. QTL, GWAS and known-gene overlaps are queried in
gene mode, so every stored item is keyed to a gene on the study assembly.
Locus-level QTL and GWAS context goes into the triage brief only.

The brief tells the orchestrator, per locus, the top genes by provisional
score, the genes with positional evidence only, the evidence domains with no
coverage and the curated trait genes found. It is kept under
``BRIEF_TOKEN_BUDGET`` tokens as rendered text.
"""

import asyncio
import json
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from agrihub import events, scoring
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.locus_builder import candidate_ref, load_candidates
from agrihub.state import CandidateGene, EvidenceItem, Locus, SourceRef, StudyState
from agrihub_data.availability import DomainStatus, domain_status
from agrihub_data.bundle import Bundle, open_bundle
from agrihub_data.query import annotation, orthology, overlap
from agrihub_data.query.common import AssemblyMismatchError, Region
from agrihub_data.query.loci import gene_regions
from agrihub_data.query.traits import TraitProfile, map_trait
from agrihub_data.registry import load_species

HARVEST_CHUNK = 48
BRIEF_TOKEN_BUDGET = 4_000
CHARS_PER_TOKEN = 4
POSITIONAL_ONLY_SHOWN = 8
HARVESTED_CATEGORIES = ("positional", "functional_annotation", "ortholog", "association", "known_gene")


@dataclass(frozen=True)
class HarvestContext:
    """What every harvest step needs besides the gene chunk."""

    bundle: Bundle
    assembly: str
    profile: TraitProfile
    canonical: bool
    domains: dict[str, DomainStatus] = field(default_factory=dict)

    @property
    def available(self) -> set[str]:
        """Return the evidence domains the bundle and installed binaries can serve."""
        return {key for key, status in self.domains.items() if status.available}


@dataclass(frozen=True)
class HarvestStep:
    """One evidence source run over the candidates."""

    category: str
    query: Callable[[HarvestContext, list[str]], list[BaseModel]]
    canonical_only: bool = False


def _annotation(context: HarvestContext, genes: list[str]) -> list[BaseModel]:
    return [row for row in annotation.gene_annotation(context.bundle, genes, context.assembly) if row.found]


def _orthologs(context: HarvestContext, genes: list[str]) -> list[BaseModel]:
    return list(orthology.get_orthologs(context.bundle, genes, ["arabidopsis"]))


def _arabidopsis(context: HarvestContext, genes: list[str]) -> list[BaseModel]:
    return list(orthology.arabidopsis_knowledge(context.bundle, genes))


def _relevance(context: HarvestContext, genes: list[str]) -> list[BaseModel]:
    return [row for row in annotation.annotation_relevance(context.bundle, genes, context.profile, context.assembly) if row.matches]


def _qtl(context: HarvestContext, genes: list[str]) -> list[BaseModel]:
    return [
        hit
        for region in gene_regions(context.bundle, genes, context.assembly)
        for hit in overlap.qtl_overlap(
            context.bundle,
            region,
            context.profile,
            assembly=context.assembly,
            trait_only=True,
            marker_flank_bp=overlap.QTL_MARKER_FLANK_BP,
        )
    ]


def _gwas(context: HarvestContext, genes: list[str]) -> list[BaseModel]:
    return [
        hit
        for region in gene_regions(context.bundle, genes, context.assembly, overlap.GWAS_GENE_FLANK_BP)
        for hit in overlap.gwas_catalog_overlap(
            context.bundle, region, context.profile, assembly=context.assembly, trait_only=True
        )
    ]


def _known_genes(context: HarvestContext, genes: list[str]) -> list[BaseModel]:
    return list(overlap.known_trait_genes(context.bundle, context.profile, gene_ids=genes, assembly=context.assembly))


STEPS: tuple[HarvestStep, ...] = (
    HarvestStep("functional_annotation", _annotation),
    HarvestStep("orthologs", _orthologs, canonical_only=True),
    HarvestStep("arabidopsis", _arabidopsis, canonical_only=True),
    HarvestStep("annotation_relevance", _relevance),
    HarvestStep("qtl", _qtl),
    HarvestStep("gwas_catalog", _gwas),
    HarvestStep("known_genes", _known_genes, canonical_only=True),
)


async def harvest(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Store evidence for every candidate, score it provisionally, and build the triage brief."""
    events.phase("harvest")
    study = state.get("study") or {}
    loci = [Locus.model_validate(raw) for raw in state.get("loci") or []]
    store = EvidenceStore.for_run(run_id_from_config(config))
    candidates = await asyncio.to_thread(load_candidates, list(state.get("candidates") or []), store)
    context = await asyncio.to_thread(harvest_context, study)
    _announce_sources(context)
    gene_ids = [gene.gene_id for gene in candidates]
    skipped = [step.category for step in STEPS if step.canonical_only and not context.canonical]
    gate = asyncio.Semaphore(int(config.get("max_concurrency") or 0) or len(STEPS))

    async def run(step: HarvestStep) -> None:
        async with gate:
            await _run_step(step, context, gene_ids, store)

    await asyncio.gather(*(run(step) for step in STEPS if step.category not in skipped))
    result = await asyncio.to_thread(
        _triage,
        study,
        loci,
        candidates,
        context,
        store,
        int(study.get("top_k_per_locus") or 5),
        skipped,
    )
    events.artifact_created(
        "candidates_table",
        title="Candidate genes (provisional scores)",
        rows=result["rows"],
    )
    brief = result["brief"]
    events.phase(
        "harvest",
        "completed",
        detail=(
            f"{brief['evidence_items']} evidence items for {len(candidates)} genes; "
            f"triage brief ~{brief['token_estimate']} tokens"
        ),
    )
    return {"triage_brief": brief, "candidates": result["candidates"]}


def harvest_context(study: dict[str, Any]) -> HarvestContext:
    """Open the study's bundle and map its trait."""
    species = str(study.get("species") or "")
    registry = load_species(species)
    bundle = open_bundle(registry.species)
    assembly = registry.assembly(study.get("assembly")).id
    return HarvestContext(
        bundle=bundle,
        assembly=assembly,
        profile=map_trait(str(study.get("trait_text") or ""), registry.species, bundle),
        canonical=assembly == registry.canonical_assembly,
        domains=domain_status(registry.species),
    )


def harvest_genes(context: HarvestContext, gene_ids: list[str], store: EvidenceStore) -> list[EvidenceItem]:
    """Run every step over ``gene_ids`` without events and return their stored evidence."""
    for step in STEPS:
        if step.canonical_only and not context.canonical:
            continue
        for start in range(0, len(gene_ids), HARVEST_CHUNK):
            _store_rows(step.query(context, gene_ids[start : start + HARVEST_CHUNK]), store)
    return store.query(gene_ids)


async def _run_step(step: HarvestStep, context: HarvestContext, gene_ids: list[str], store: EvidenceStore) -> None:
    total = len(gene_ids)
    if not total:
        events.evidence_progress(store.counts_by_category(), done=0, total=0, category=step.category)
        return
    for start in range(0, total, HARVEST_CHUNK):
        chunk = gene_ids[start : start + HARVEST_CHUNK]

        def work(chunk: list[str] = chunk) -> dict[str, int]:
            _store_rows(step.query(context, chunk), store)
            return store.counts_by_category()

        counts = await asyncio.to_thread(work)
        events.evidence_progress(counts, done=min(total, start + len(chunk)), total=total, category=step.category)


def _store_rows(rows: list[BaseModel], store: EvidenceStore) -> None:
    items = [item for row in rows for item in getattr(row, "evidence")()]
    if items:
        store.put_items(items)


def bundle_sources(bundle: Bundle) -> list[tuple[SourceRef, str | None]]:
    """Return each registered source in the bundle with its stamped version and homepage."""
    registry = load_species(bundle.species)
    sources = []
    for source_id, version in sorted(bundle.source_versions().items()):
        try:
            source = registry.source(source_id)
        except KeyError:
            continue
        license_text = source.license + (" (academic use only)" if source.academic_only else "")
        sources.append((SourceRef(source_id=source_id, name=source.name, version=version, license=license_text), source.homepage))
    return sources


def _announce_sources(context: HarvestContext) -> None:
    for source, homepage in bundle_sources(context.bundle):
        events.source_discovered(
            source_id=source.source_id,
            name=source.name,
            version=source.version,
            url=homepage,
            license=source.license,
        )


def _triage(
    study: dict[str, Any],
    loci: list[Locus],
    candidates: list[CandidateGene],
    context: HarvestContext,
    store: EvidenceStore,
    top_k: int,
    skipped: list[str],
) -> dict[str, Any]:
    gene_ids = [gene.gene_id for gene in candidates]
    items = store.query(gene_ids) if gene_ids else []
    symbols = _symbols(items)
    candidates = [gene.model_copy(update={"symbol": symbols.get(gene.gene_id) or gene.symbol}) for gene in candidates]
    registry = load_species(context.bundle.species)
    scores = scoring.score_candidates(candidates, items, profile=context.profile, ld_kb=registry.typical_ld_kb)
    per_gene: dict[str, Counter[str]] = {gene_id: Counter() for gene_id in gene_ids}
    for item in items:
        per_gene.setdefault(item.gene_id, Counter())[item.category] += 1
    matrix_ref = store.put_output(
        "evidence_matrix",
        {
            "header": "evidence items per gene and category, with provisional rubric points",
            "rows": [
                {
                    "gene_id": gene.gene_id,
                    "locus_id": gene.locus_id,
                    "counts": dict(sorted(per_gene.get(gene.gene_id, Counter()).items())),
                    "points": scores.genes[gene.gene_id].points(),
                    "score": scores.genes[gene.gene_id].score,
                    "tier": scores.genes[gene.gene_id].tier,
                }
                for gene in candidates
            ],
        },
    )
    known = [
        item
        for item in items
        if item.category == "known_gene" and isinstance(item.value, dict)
    ]
    counts = store.counts_by_category()
    brief: dict[str, Any] = {
        "schema": "agrihub.triage-brief/v1",
        "study": {
            "species": study.get("species"),
            "assembly": context.assembly,
            "trait": study.get("trait_text"),
            "profile": context.profile.key,
        },
        "evidence_items": sum(counts.values()),
        "evidence_by_category": counts,
        "matrix_ref": matrix_ref,
        "domains": brief_domains(context.domains),
        "skipped": skipped,
        "loci": [
            _locus_brief(locus, scores, per_gene, known, context, top_k)
            for locus in loci
        ],
    }
    brief["top_genes"] = {entry["locus_id"]: [gene["gene_id"] for gene in entry["top"]] for entry in brief["loci"]}
    brief = fit_brief(brief)
    rows = [
        {
            "gene_id": gene.gene_id,
            "locus_id": gene.locus_id,
            "symbol": gene.symbol,
            "rank_in_locus": gene.rank_in_locus,
            "score": gene.score,
            "tier": gene.tier,
            "share_of_locus": gene.share_of_locus,
        }
        for locus in loci
        for gene in scores.ranked(locus.locus_id)[:top_k]
    ]
    return {"brief": brief, "rows": rows, "candidates": [candidate_ref(gene) for gene in candidates]}


def _locus_brief(
    locus: Locus,
    scores: scoring.StudyScores,
    per_gene: dict[str, Counter[str]],
    known: list[EvidenceItem],
    context: HarvestContext,
    top_k: int,
) -> dict[str, Any]:
    ranked = scores.ranked(locus.locus_id)
    members = {gene.gene_id for gene in ranked}
    covered: Counter[str] = Counter()
    for gene_id in members:
        covered.update(per_gene.get(gene_id, Counter()))
    positional_only = [
        gene.gene_id
        for gene in ranked
        if all(points == 0 for code, points in gene.points().items() if code != "A")
    ]
    curated = []
    for item in known:
        if item.gene_id in members and isinstance(item.value, dict):
            curated.append(
                {
                    "gene_id": item.gene_id,
                    "symbols": (item.value.get("symbols") or [])[:2],
                    "trait_match": item.value.get("trait_match"),
                }
            )
    return {
        "locus_id": locus.locus_id,
        "region": f"{locus.chrom}:{locus.start}-{locus.end}",
        "lead_snp": locus.lead_snp,
        "snps": len(locus.snp_positions) or 1,
        "n_genes": len(ranked),
        "top": [_gene_brief(gene) for gene in ranked[:top_k]],
        "positional_only": {"count": len(positional_only), "gene_ids": positional_only[:POSITIONAL_ONLY_SHOWN]},
        "known_genes": curated,
        "no_coverage": [category for category in HARVESTED_CATEGORIES if not covered.get(category)],
        "context": _locus_context(locus, context),
    }


def _gene_brief(gene: scoring.GeneScore) -> dict[str, Any]:
    reasons = [
        f"{code} {category.points:g}: {category.reasons[0]}"
        for code, category in gene.categories.items()
        if code != "A" and category.points > 0 and category.reasons
    ]
    entry: dict[str, Any] = {
        "gene_id": gene.gene_id,
        "score": gene.score,
        "tier": gene.tier,
        "share": gene.share_of_locus,
        "dist": gene.distance_bp,
        "why": reasons[:3],
    }
    if gene.symbol:
        entry["symbol"] = gene.symbol
    if gene.flags:
        entry["flags"] = gene.flags[:1]
    return entry


def _locus_context(locus: Locus, context: HarvestContext) -> dict[str, str]:
    """Summarize QTLs and catalog GWAS hits over the whole locus window (not stored as gene evidence)."""
    region = Region(
        label=locus.locus_id,
        chrom=locus.chrom,
        start=max(1, locus.start),
        end=max(1, locus.end),
        assembly=locus.assembly,
        snp_pos=locus.lead_pos,
    )
    try:
        qtls = overlap.qtl_overlap(context.bundle, region, context.profile, assembly=context.assembly)
        hits = overlap.gwas_catalog_overlap(context.bundle, region, context.profile, assembly=context.assembly)
    except AssemblyMismatchError as exc:
        return {"qtl": str(exc), "gwas": str(exc)}
    intervals = [hit for hit in qtls if hit.kind == "interval"]
    matched = [hit for hit in intervals if hit.trait_match != "none"]
    if intervals:
        spans = sorted(hit.span_bp for hit in intervals)
        qtl_text = (
            f"{len(intervals)} interval QTLs ({len(matched)} for this trait, "
            f"{sum(1 for hit in intervals if hit.wide)} wide, median span {spans[len(spans) // 2] / 1e6:.1f} Mb)"
        )
    else:
        qtl_text = "no interval QTLs"
    markers = [hit for hit in qtls if hit.kind == "marker"]
    if markers:
        qtl_text += f"; {len(markers)} single-marker QTLs ({sum(1 for hit in markers if hit.trait_match != 'none')} for this trait)"
    traits = Counter(hit.trait_name.strip().casefold() for hit in hits)
    trait_hits = [hit for hit in hits if hit.trait_match != "none"]
    if hits:
        top = ", ".join(f"{name} ({count})" for name, count in traits.most_common(3))
        gwas_text = f"{len(hits)} catalog GWAS hits, {len(trait_hits)} for this trait; top traits: {top}"
    else:
        gwas_text = "no catalog GWAS hits"
    return {"qtl": qtl_text, "gwas": gwas_text}


def _symbols(items: list[EvidenceItem]) -> dict[str, str]:
    symbols: dict[str, str] = {}
    for item in items:
        if item.category == "known_gene" and isinstance(item.value, dict) and item.value.get("symbols"):
            symbols.setdefault(item.gene_id, str(item.value["symbols"][0]))
    return symbols


def brief_domains(domains: dict[str, DomainStatus]) -> dict[str, Any]:
    """Return the evidence domains the bundle serves and why the others are missing."""
    return {
        "available": sorted(key for key, status in domains.items() if status.available),
        "unavailable": {key: str(status.reason) for key, status in sorted(domains.items()) if not status.available},
    }


def render_brief(brief: dict[str, Any]) -> str:
    """Return the brief as the compact text the orchestrator reads."""
    return json.dumps(
        {key: value for key, value in brief.items() if key not in {"token_estimate", "top_genes"}},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def brief_tokens(brief: dict[str, Any]) -> int:
    """Estimate the rendered brief's size in tokens (four characters per token)."""
    return math.ceil(len(render_brief(brief)) / CHARS_PER_TOKEN)


def fit_brief(brief: dict[str, Any], budget: int = BRIEF_TOKEN_BUDGET) -> dict[str, Any]:
    """Trim per-locus lists until the rendered brief fits ``budget`` tokens."""
    trims = (
        lambda entry: entry["positional_only"].update(gene_ids=entry["positional_only"]["gene_ids"][:3]),
        lambda entry: [gene.pop("flags", None) for gene in entry["top"]],
        lambda entry: [gene.update(why=gene["why"][:1]) for gene in entry["top"]],
        lambda entry: entry.update(known_genes=entry["known_genes"][:3]),
        lambda entry: entry.update(top=entry["top"][:3]),
        lambda entry: entry["positional_only"].update(gene_ids=[]),
        lambda entry: entry.update(context={}),
        lambda entry: [gene.update(why=[reason[:80] for reason in gene["why"]]) for gene in entry["top"]],
        lambda entry: entry.update(top=entry["top"][:1]),
        lambda entry: [gene.pop("why", None) for gene in entry["top"]],
    )
    for trim in trims:
        if brief_tokens(brief) <= budget:
            break
        for entry in brief["loci"]:
            trim(entry)
    brief["top_genes"] = {entry["locus_id"]: [gene["gene_id"] for gene in entry["top"]] for entry in brief["loci"]}
    brief["token_estimate"] = brief_tokens(brief)
    return brief
