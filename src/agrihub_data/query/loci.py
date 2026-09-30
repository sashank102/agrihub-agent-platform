"""Locus windows and the genes inside them."""

from typing import Literal

from pydantic import BaseModel

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import Region, registry_of, resolve_region
from agrihub_data.registry import load_species, normalize_chrom


class LocusWindow(BaseModel):
    """A fixed window around a SNP, clamped to its chromosome."""

    species: str
    assembly: str
    chrom: str
    pos: int
    start: int
    end: int
    flank_bp: int
    mode: Literal["fixed"]
    clamped: bool
    warning: str | None = None

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
) -> LocusWindow:
    """Return ``pos ± flank_bp`` clamped to the chromosome, using the species default flank.

    Raises:
        ValueError: for ``mode="ld"``, which needs a genotype VCF (a later plan),
            or a position outside the chromosome.
    """
    if mode != "fixed":
        raise ValueError("only mode='fixed' is available; LD windows need a genotype VCF")
    registry = load_species(species)
    target = registry.assembly(assembly)
    name = normalize_chrom(species, chrom, target.id)
    chromosome = target.chromosome(name)
    length = chromosome.length if chromosome else None
    if pos < 1 or (length is not None and pos > length):
        raise ValueError(f"{name}:{pos} is outside {target.id} ({length} bp)")
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
