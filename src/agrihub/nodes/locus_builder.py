"""Turn SNPs into merged loci and the candidate genes inside them.

Each SNP gets a fixed window (``define_locus``, clamped to its chromosome),
or with ``window.mode == "ld"`` its LD window on the bundled panel or the
study's ``genotype_vcf_ref`` (falling back to the fixed window, with a
warning, when LD cannot be computed); windows that overlap or touch on one
chromosome merge into one ``Locus``. The
genes of each locus come from ``genes_in_window`` on the study assembly,
nearest to any of its SNPs first, capped at ``max_genes_per_locus``. The full
gene record is stored as positional evidence; state keeps only candidate
refs (:func:`candidate_ref`), which :func:`load_candidates` turns back into
``CandidateGene`` objects.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig

from agrihub import events
from agrihub.configuration import run_id_from_config
from agrihub.evidence_store import EvidenceStore
from agrihub.state import (
    CandidateGene,
    EvidenceItem,
    Locus,
    SnpInput,
    StudyState,
    StudyWarning,
    Window,
)
from agrihub_data.bundle import Bundle, BundleMissingError, open_bundle
from agrihub_data.query.common import Region
from agrihub_data.query.ld import LdUnavailableError, LdWindow, gene_ld
from agrihub_data.query.loci import GeneInWindow, define_locus, genes_in_window
from agrihub_data.registry import load_species


@dataclass(frozen=True)
class SnpWindow:
    """One SNP and its clamped window."""

    snp: SnpInput
    chrom: str
    pos: int
    start: int
    end: int
    ld: LdWindow | None = None


@dataclass(frozen=True)
class LdRequest:
    """How LD windows are asked for: the r2 threshold and an optional study VCF."""

    r2: float
    vcf_ref: str | None = None


def ld_request(study: dict[str, Any]) -> LdRequest | None:
    """Return the study's LD settings, or ``None`` for fixed windows."""
    window = Window.model_validate(study.get("window") or {})
    if window.mode != "ld":
        return None
    return LdRequest(r2=window.r2, vcf_ref=study.get("genotype_vcf_ref"))


async def locus_builder(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Build fixed windows, merge them into loci, and list candidate genes per locus."""
    events.phase("loci")
    study = state.get("study") or {}
    species = str(study.get("species") or "")
    assembly = str(study.get("assembly") or "")
    window = Window.model_validate(study.get("window") or {})
    cap = int(study.get("max_genes_per_locus") or 200)
    snps = [SnpInput.model_validate(raw) for raw in state.get("snps") or []]
    store = EvidenceStore.for_run(run_id_from_config(config))
    try:
        loci, candidates, warnings = await asyncio.to_thread(
            _build, species, assembly, snps, window.flank_bp, cap, store, ld_request(study)
        )
    except BundleMissingError as exc:
        events.phase("loci", "failed", detail=str(exc))
        raise
    events.artifact_created(
        "loci_table",
        title="Loci",
        rows=[locus.model_dump(mode="json") for locus in loci],
    )
    detail = f"{len(loci)} loci, {len(candidates)} candidate genes"
    events.phase(
        "loci",
        "completed",
        detail=detail,
        warnings=[warning.model_dump(exclude_none=True) for warning in warnings],
    )
    return {
        "loci": [locus.model_dump(mode="json") for locus in loci],
        "candidates": [candidate_ref(gene) for gene in candidates],
        "warnings": [*(state.get("warnings") or []), *(warning.model_dump(exclude_none=True) for warning in warnings)],
    }


def candidate_ref(gene: CandidateGene) -> dict[str, Any]:
    """Return the compact candidate ref kept in checkpointed state."""
    ref: dict[str, Any] = {"gene_id": gene.gene_id, "locus_id": gene.locus_id, "distance_bp": gene.distance_bp}
    if gene.nearest_snp:
        ref["nearest_snp"] = gene.nearest_snp
    if gene.symbol:
        ref["symbol"] = gene.symbol
    return ref


def load_candidates(refs: list[dict[str, Any]], store: EvidenceStore) -> list[CandidateGene]:
    """Rebuild full candidate records from refs and the stored window-membership evidence."""
    records: dict[str, EvidenceItem] = {}
    gene_ids = [str(ref["gene_id"]) for ref in refs]
    for item in store.query(gene_ids, ["positional"]) if gene_ids else []:
        if item.subtype == "in_window" and isinstance(item.value, dict):
            records.setdefault(item.gene_id, item)
    genes = []
    for ref in refs:
        record = records.get(str(ref["gene_id"]))
        value = record.value if record is not None and isinstance(record.value, dict) else {}
        strand = str(value.get("strand") or ".")
        genes.append(
            CandidateGene(
                gene_id=str(ref["gene_id"]),
                locus_id=str(ref["locus_id"]),
                symbol=ref.get("symbol"),
                chrom=str(value.get("chrom") or ""),
                start=int(value.get("start") or 0),
                end=int(value.get("end") or 0),
                strand=strand if strand in {"+", "-"} else ".",
                distance_bp=int(ref["distance_bp"]),
                overlaps_snp=int(ref["distance_bp"]) == 0,
                nearest_snp=ref.get("nearest_snp"),
                defline=(record.quote if record is not None else None) or "",
            )
        )
    return genes


def _build(
    species: str,
    assembly: str,
    snps: list[SnpInput],
    flank_bp: int,
    cap: int,
    store: EvidenceStore,
    ld: LdRequest | None = None,
) -> tuple[list[Locus], list[CandidateGene], list[StudyWarning]]:
    built: list[Locus] = []
    candidates: list[CandidateGene] = []
    warnings: list[StudyWarning] = []
    for locus, genes, in_window, warning, windows in _assign_genes(species, assembly, snps, flank_bp, cap, ld, warnings):
        store.put_items(item for gene in in_window for item in gene.evidence())
        for found in (window.ld for window in windows if window.ld is not None):
            linked = gene_ld(found, [(gene.gene_id, gene.start, gene.end) for gene in genes])
            store.put_items([*found.evidence(), *(item for row in linked for item in row.evidence())])
        candidates.extend(genes)
        built.append(locus)
        if warning is not None:
            warnings.append(warning)
    return built, candidates, warnings


def preview_loci(
    species: str,
    assembly: str,
    snps: list[SnpInput],
    flank_bp: int,
    cap: int,
    ld: LdRequest | None = None,
) -> tuple[list[Locus], list[StudyWarning]]:
    """Return the loci a study would build, with gene counts, without storing evidence.

    Raises:
        BundleMissingError: when the species bundle is not built.
    """
    loci: list[Locus] = []
    warnings: list[StudyWarning] = []
    for locus, _, _, warning, _ in _assign_genes(species, assembly, snps, flank_bp, cap, ld, warnings):
        loci.append(locus)
        if warning is not None:
            warnings.append(warning)
    return loci, warnings


def _assign_genes(
    species: str,
    assembly: str,
    snps: list[SnpInput],
    flank_bp: int,
    cap: int,
    ld: LdRequest | None = None,
    warnings: list[StudyWarning] | None = None,
) -> list[tuple[Locus, list[CandidateGene], list[GeneInWindow], StudyWarning | None, list[SnpWindow]]]:
    """Give each gene to the first locus that holds it and cap each locus at its nearest genes."""
    windows = snp_windows(species, assembly, snps, flank_bp, ld, warnings)
    loci = merge_windows(species, assembly, windows)
    if not loci:
        return []
    bundle = open_bundle(species)
    assigned: list[tuple[Locus, list[CandidateGene], list[GeneInWindow], StudyWarning | None, list[SnpWindow]]] = []
    placed: set[str] = set()
    for locus in loci:
        pairs = [pair for pair in zip(*locus_genes(bundle, locus), strict=True) if pair[0].gene_id not in placed]
        genes = [pair[0] for pair in pairs]
        in_window = [pair[1] for pair in pairs]
        capped = len(genes) > cap
        warning = None
        if capped:
            warning = StudyWarning(
                code="genes_capped",
                message=f"{locus.locus_id} has {len(genes)} genes; kept the {cap} nearest to its SNPs",
            )
            genes, in_window = genes[:cap], in_window[:cap]
        placed.update(gene.gene_id for gene in genes)
        members = [window for window in windows if window.snp.raw in locus.snp_positions]
        assigned.append(
            (locus.model_copy(update={"n_genes": len(genes), "genes_capped": capped}), genes, in_window, warning, members)
        )
    return assigned


def snp_windows(
    species: str,
    assembly: str,
    snps: list[SnpInput],
    flank_bp: int,
    ld: LdRequest | None = None,
    warnings: list[StudyWarning] | None = None,
) -> list[SnpWindow]:
    """Return each SNP's fixed window clamped to its chromosome, or its LD window when ``ld`` is set."""
    windows = []
    for snp in snps:
        if snp.chrom is None or snp.pos is None:
            continue
        if ld is not None:
            try:
                found = define_locus(species, snp.chrom, snp.pos, "ld", None, assembly, r2=ld.r2, vcf_ref=ld.vcf_ref, label=snp.raw)
                windows.append(SnpWindow(snp=snp, chrom=found.chrom, pos=snp.pos, start=found.start, end=found.end, ld=found.ld))
                if found.warning and warnings is not None:
                    warnings.append(StudyWarning(code="ld_proxy", message=found.warning, snp=snp.raw))
                continue
            except (LdUnavailableError, ValueError) as exc:
                if warnings is not None:
                    warnings.append(StudyWarning(code="ld_unavailable", message=f"LD window unavailable ({exc}); used the fixed window", snp=snp.raw))
        locus = define_locus(species, snp.chrom, snp.pos, "fixed", flank_bp, assembly)
        windows.append(SnpWindow(snp=snp, chrom=locus.chrom, pos=snp.pos, start=locus.start, end=locus.end))
    return windows


def build_loci(species: str, assembly: str, snps: list[SnpInput], flank_bp: int) -> list[Locus]:
    """Merge overlapping or touching fixed windows per chromosome into loci numbered in genome order."""
    return merge_windows(species, assembly, snp_windows(species, assembly, snps, flank_bp))


def merge_windows(species: str, assembly: str, windows: list[SnpWindow]) -> list[Locus]:
    """Merge overlapping or touching windows per chromosome into loci numbered in genome order.

    The lead SNP has the smallest p-value, then the highest score, then the
    first position. A locus is ``ld`` when all its windows are LD windows.
    """
    target = load_species(species).assembly(assembly)
    order = {chromosome.name: index for index, chromosome in enumerate(target.chromosomes)}
    windows = sorted(windows, key=lambda item: (order.get(item.chrom, len(order)), item.chrom, item.start, item.pos))
    groups: list[list[SnpWindow]] = []
    for item in windows:
        last = groups[-1] if groups else None
        if last and last[0].chrom == item.chrom and item.start <= max(member.end for member in last) + 1:
            last.append(item)
        else:
            groups.append([item])
    loci: list[Locus] = []
    for index, group in enumerate(groups, start=1):
        lead = min(group, key=_lead_key)
        loci.append(
            Locus(
                locus_id=f"L{index}",
                lead_snp=lead.snp.raw,
                supporting_snps=[member.snp.raw for member in group if member is not lead],
                chrom=group[0].chrom,
                start=min(member.start for member in group),
                end=max(member.end for member in group),
                assembly=target.id,
                window_method="ld" if all(member.ld is not None for member in group) else "fixed",
                merged_from=[member.snp.raw for member in group] if len(group) > 1 else [],
                lead_pos=lead.pos,
                snp_positions={member.snp.raw: member.pos for member in group},
            )
        )
    return loci


def locus_genes(bundle: Bundle, locus: Locus) -> tuple[list[CandidateGene], list[GeneInWindow]]:
    """Return the locus genes nearest to any of its SNPs first, with their window records."""
    region = Region(
        label=locus.locus_id,
        chrom=locus.chrom,
        start=max(1, locus.start),
        end=max(1, locus.end),
        assembly=locus.assembly,
        snp_pos=locus.lead_pos,
    )
    positions = locus.snp_positions or ({locus.lead_snp: locus.lead_pos} if locus.lead_pos else {})
    pairs: list[tuple[CandidateGene, GeneInWindow]] = []
    for gene in genes_in_window(bundle, region, locus.assembly):
        nearest, distance = _nearest_snp(gene, positions)
        record = gene.model_copy(update={"dist_to_snp": distance, "overlaps_snp": distance == 0})
        candidate = CandidateGene(
            gene_id=gene.gene_id,
            locus_id=locus.locus_id,
            chrom=gene.chrom,
            start=gene.start,
            end=gene.end,
            strand=gene.strand if gene.strand in {"+", "-"} else ".",
            distance_bp=distance,
            overlaps_snp=distance == 0,
            nearest_snp=nearest,
            defline=gene.defline or "",
        )
        pairs.append((candidate, record))
    pairs.sort(key=lambda pair: (pair[0].distance_bp, pair[0].start, pair[0].gene_id))
    return [pair[0] for pair in pairs], [pair[1] for pair in pairs]


def genes_at_flank(bundle: Bundle, locus: Locus, flank_bp: int) -> list[CandidateGene]:
    """Return the genes in the union of the locus's SNP windows at another flank.

    Distances stay those to the nearest SNP of the locus, so a gene's score
    does not depend on the flank; only which genes are in the windows does.
    """
    positions = locus.snp_positions or ({locus.lead_snp: locus.lead_pos} if locus.lead_pos else {})
    genes: dict[str, CandidateGene] = {}
    for pos in sorted(set(positions.values())):
        window = define_locus(bundle.species, locus.chrom, pos, "fixed", flank_bp, locus.assembly)
        part = locus.model_copy(update={"start": window.start, "end": window.end})
        for gene in locus_genes(bundle, part)[0]:
            genes.setdefault(gene.gene_id, gene)
    return sorted(genes.values(), key=lambda gene: (gene.distance_bp, gene.start, gene.gene_id))


def _nearest_snp(gene: GeneInWindow, positions: dict[str, int]) -> tuple[str | None, int]:
    best: tuple[int, str] | None = None
    for raw, pos in positions.items():
        distance = 0 if gene.start <= pos <= gene.end else min(abs(gene.start - pos), abs(gene.end - pos))
        if best is None or (distance, raw) < best:
            best = (distance, raw)
    if best is None:
        return None, 0
    return best[1], best[0]


def _lead_key(item: SnpWindow) -> tuple[float, str, float, int]:
    """Order SNPs by p-value, then by score, comparing scores only within one score type."""
    p_value = item.snp.p_value if item.snp.p_value is not None else 2.0
    score = -item.snp.score if item.snp.score is not None else 0.0
    return p_value, item.snp.score_type or "", score, item.pos
