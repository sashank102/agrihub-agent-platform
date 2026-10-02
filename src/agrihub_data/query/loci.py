"""Locus windows and the genes inside them."""

from typing import Literal

from pydantic import BaseModel

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle, open_bundle
from agrihub_data.query.common import Region, registry_of, resolve_region
from agrihub_data.query.ld import (
    DEFAULT_R2,
    DEFAULT_WINDOW_KB,
    LdUnavailableError,
    LdWindow,
    ld_window,
)
from agrihub_data.registry import load_species, normalize_chrom


class LocusWindow(BaseModel):
    """A window around a SNP: fixed (pos ± flank, clamped) or LD-based (``ld`` holds how it was found)."""

    species: str
    assembly: str
    chrom: str
    pos: int
    start: int
    end: int
    flank_bp: int
    mode: Literal["fixed", "ld"]
    clamped: bool
    warning: str | None = None
    ld: LdWindow | None = None

    def region(self, label: str | None = None) -> Region:
        """Return the window as a region labeled ``label``."""
        return Region(
            label=label,
            chrom=self.chrom,
            start=self.start,
            end=self.end,
            assembly=self.assembly,
            snp_pos=self.pos,
        )


class GeneInWindow(BaseModel):
    """One gene overlapping a window, with its distance to the window's SNP."""

    gene_id: str
    assembly: str
    chrom: str
    start: int
    end: int
    strand: str
    defline: str | None
    window: str
    snp_pos: int | None
    dist_to_snp: int | None
    overlaps_snp: bool
    source_db: str
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the positional fact that this gene lies in the window."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="positional",
                subtype="in_window",
                value={
                    "assembly": self.assembly,
                    "chrom": self.chrom,
                    "start": self.start,
                    "end": self.end,
                    "strand": self.strand,
                    "window": self.window,
                    "snp_pos": self.snp_pos,
                    "dist_to_snp": self.dist_to_snp,
                    "overlaps_snp": self.overlaps_snp,
                },
                source_db=self.source_db,
                db_version=self.source_version,
                source_record=f"{self.assembly}:{self.window}@{self.snp_pos or ''}",
                quote=self.defline,
            )
        ]


def define_locus(
    species: str,
    chrom: str,
    pos: int,
    mode: str = "fixed",
    flank_bp: int | None = None,
    assembly: str | None = None,
    *,
    r2: float = DEFAULT_R2,
    vcf_ref: str | None = None,
    label: str | None = None,
    bundle: Bundle | None = None,
) -> LocusWindow:
    """Return ``pos ± flank_bp`` clamped to the chromosome, or the SNP's LD window with ``mode="ld"``.

    The fixed flank defaults to the species window. An LD window
    (:func:`agrihub_data.query.ld.ld_window`) reaches the furthest SNP with
    ``r2`` or more within ``flank_bp`` (default 1 Mb) and is extended to gene
    boundaries; it is computed on the canonical assembly.

    Raises:
        ValueError: for an unknown mode or a position outside the chromosome.
        LdUnavailableError: for ``mode="ld"`` without PLINK2, genotypes or a usable panel SNP near the lead.
    """
    if mode not in {"fixed", "ld"}:
        raise ValueError(f"unknown window mode {mode!r}; use fixed or ld")
    registry = load_species(species)
    target = registry.assembly(assembly)
    name = normalize_chrom(species, chrom, target.id)
    chromosome = target.chromosome(name)
    length = chromosome.length if chromosome else None
    if pos < 1 or (length is not None and pos > length):
        raise ValueError(f"{name}:{pos} is outside {target.id} ({length} bp)")
    if mode == "ld":
        if target.id != registry.canonical_assembly:
            raise LdUnavailableError(f"LD windows are computed on {registry.canonical_assembly}; lift {name}:{pos} over first")
        window_kb = (flank_bp or DEFAULT_WINDOW_KB * 1_000) // 1_000
        found = ld_window(bundle or open_bundle(species), label or f"{name}:{pos}", name, pos, vcf_ref=vcf_ref, window_kb=window_kb, r2=r2)
        return LocusWindow(
            species=registry.species,
            assembly=target.id,
            chrom=name,
            pos=pos,
            start=found.start,
            end=found.end,
            flank_bp=max(pos - found.start, found.end - pos),
            mode="ld",
            clamped=False,
            warning=found.note,
            ld=found,
        )
    flank = registry.default_window.flank_bp if flank_bp is None else flank_bp
    if flank < 0:
        raise ValueError("flank_bp must not be negative")
    start = max(1, pos - flank)
    end = min(length, pos + flank) if length is not None else pos + flank
    warning = None
    limit = registry.default_window.warn_above_bp
    if limit is not None and flank > limit:
        warning = (
            f"±{flank // 1000} kb is wider than the {registry.species} default of "
            f"±{limit // 1000} kb; typical LD is about {registry.typical_ld_kb:g} kb"
        )
    return LocusWindow(
        species=registry.species,
        assembly=target.id,
        chrom=name,
        pos=pos,
        start=start,
        end=end,
        flank_bp=flank,
        mode="fixed",
        clamped=start != pos - flank or end != pos + flank,
        warning=warning,
    )


def gene_regions(
    bundle: Bundle,
    gene_ids: list[str],
    assembly: str | None = None,
    flank_bp: int = 0,
) -> list[Region]:
    """Return one region per gene, labeled with the gene id, flanked and clamped to the chromosome.

    The gene body is kept as the region's core so overlaps report gene distances.

    Raises:
        ValueError: when an id is not a gene on the assembly.
    """
    registry = registry_of(bundle)
    target = registry.assembly(assembly)
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    rows = bundle.rows(
        'SELECT gene_id, chrom, start, "end" FROM genes WHERE assembly = ? '
        f"AND gene_id IN ({', '.join('?' for _ in wanted)})",
        [target.id, *wanted],
    )
    found = {row["gene_id"]: row for row in rows}
    missing = [gene for gene in wanted if gene not in found]
    if missing:
        raise ValueError(f"not genes on {target.id}: {', '.join(missing[:10])}; use map_gene_ids first")
    regions = []
    for gene in wanted:
        row = found[gene]
        start, end = int(row["start"]), int(row["end"])
        chromosome = target.chromosome(str(row["chrom"]))
        upper = end + flank_bp if chromosome is None else min(chromosome.length, end + flank_bp)
        regions.append(
            Region(
                label=gene,
                chrom=str(row["chrom"]),
                start=max(1, start - flank_bp),
                end=max(upper, end),
                assembly=target.id,
                core_start=start,
                core_end=end,
            )
        )
    return regions


def genes_in_window(
    bundle: Bundle,
    region: Region,
    assembly: str | None = None,
) -> list[GeneInWindow]:
    """Return genes overlapping ``region`` on its assembly, nearest to the SNP first."""
    registry = registry_of(bundle)
    resolved = resolve_region(registry, region, assembly)
    target = str(resolved.assembly)
    rows = bundle.rows(
        'SELECT gene_id, chrom, start, "end", strand, defline, source_db, source_version FROM genes '
        'WHERE assembly = ? AND chrom = ? AND "end" >= ? AND start <= ? ORDER BY start, gene_id',
        [target, resolved.chrom, resolved.start, resolved.end],
    )
    window = f"{resolved.chrom}:{resolved.start}-{resolved.end}"
    genes = []
    for row in rows:
        start, end = int(row["start"]), int(row["end"])
        snp = resolved.snp_pos
        overlaps = snp is not None and start <= snp <= end
        distance = None if snp is None else 0 if overlaps else min(abs(start - snp), abs(end - snp))
        genes.append(
            GeneInWindow(
                gene_id=str(row["gene_id"]),
                assembly=target,
                chrom=str(row["chrom"]),
                start=start,
                end=end,
                strand=str(row["strand"]),
                defline=row["defline"],
                window=window,
                snp_pos=snp,
                dist_to_snp=distance,
                overlaps_snp=overlaps,
                source_db=str(row["source_db"]),
                source_version=str(row["source_version"]),
            )
        )
    if resolved.snp_pos is not None:
        genes.sort(key=lambda gene: (gene.dist_to_snp or 0, gene.start))
    return genes
