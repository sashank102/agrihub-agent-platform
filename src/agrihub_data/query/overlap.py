"""QTL, GWAS catalog and known trait genes overlapping regions.

Every overlap is computed on one assembly. QTL, GWAS hits and known genes
keep the assembly they were placed on, and a region on another assembly is
refused with :class:`AssemblyMismatchError` rather than compared.

Gene mode: a region labeled with a gene id and carrying the gene body as its
core (:func:`agrihub_data.query.loci.gene_regions`) keys every fact to that
gene and reports distances to the gene body. Harvest uses gene mode so every
evidence item names a real gene.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import (
    AssemblyMismatchError,
    Region,
    registry_of,
    require_same_assembly,
    resolve_region,
)
from agrihub_data.query.traits import TraitProfile

WIDE_QTL_BP = 5_000_000
GWAS_GENE_FLANK_BP = 50_000
"""Gene-mode flank for catalog GWAS hits: in the gene, within 10 kb or within 50 kb."""
QTL_MARKER_FLANK_BP = 50_000
"""Gene-mode distance within which a single-marker QTL counts as near the gene."""
OverlapType = Literal["qtl_contains_window", "window_contains_qtl", "partial", "marker_within"]
QtlKind = Literal["interval", "marker"]
TraitMatch = Literal["ontology", "keyword", "none"]


class QtlHit(BaseModel):
    """A placed QTL overlapping a region.

    ``kind="marker"`` is a QTL placed from one marker: a point, reported as
    ``marker_within`` with its distance to the region core, never as an
    interval overlap.
    """

    label: str
    qtl_id: str
    study_id: str
    qtl_name: str
    trait_name: str
    trait_terms: list[str]
    assembly: str
    chrom: str
    start: int
    end: int
    span_bp: int
    n_markers: int
    n_markers_placed: int
    placement: str
    kind: QtlKind = "interval"
    overlap_type: OverlapType
    overlap_bp: int
    distance_to_core: int = 0
    wide: bool
    trait_match: TraitMatch
    matched: list[str] = Field(default_factory=list)
    publication_doi: str | None = None
    source_db: str
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the overlap as one association fact keyed by ``qtl_id`` and overlap type."""
        return [
            EvidenceItem(
                gene_id=self.label,
                category="association",
                subtype=f"qtl:{self.overlap_type}",
                value=self.model_dump(exclude={"label", "source_db", "source_version"}),
                source_db=self.source_db,
                db_version=self.source_version,
                source_record=self.qtl_id,
                primary_citation=f"doi:{self.publication_doi}" if self.publication_doi else None,
                quote=self.trait_name,
            )
        ]


class GwasCatalogHit(BaseModel):
    """A published GWAS association inside a region."""

    label: str
    hit_id: str
    source_db: str
    study_id: str
    trait_name: str
    trait_terms: list[str]
    marker: str | None
    assembly: str
    chrom: str
    pos: int
    p_value: float | None
    distance_to_snp: int | None
    distance_to_core: int = 0
    """Distance from the hit to the gene body in gene mode; 0 inside it."""
    pmid: str | None
    doi: str | None
    reported_genes: list[str]
    trait_match: TraitMatch
    matched: list[str] = Field(default_factory=list)
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the hit as one association fact keyed by ``study|marker|trait``."""
        citation = f"PMID:{self.pmid}" if self.pmid else f"doi:{self.doi}" if self.doi else None
        return [
            EvidenceItem(
                gene_id=self.label,
                category="association",
                subtype=f"gwas:{self.source_db}",
                value=self.model_dump(exclude={"label", "source_version"}),
                source_db=self.source_db,
                db_version=self.source_version,
                source_record=f"{self.study_id}|{self.marker or self.hit_id}|{self.trait_name}",
                primary_citation=citation,
                quote=self.trait_name,
            )
        ]


class KnownGeneHit(BaseModel):
    """A curated trait gene, with where it sits and how it matches the trait."""

    gene_id: str
    assembly: str
    symbols: list[str]
    symbol_long: str | None
    synopsis: str | None
    trait_terms: list[str]
    trait_names: list[str]
    trait_match: TraitMatch
    matched: list[str] = Field(default_factory=list)
    trait_key: str | None
    confidence: int | None
    pmids: list[str]
    dois: list[str]
    mapping: str
    source_gene_id: str
    source_assembly: str
    chrom: str | None = None
    start: int | None = None
    end: int | None = None
    label: str | None = None
    distance_to_snp: int | None = None
    weight: float
    source_db: str
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the curated record as one known-gene fact for this trait."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="known_gene",
                subtype=f"known_gene:{self.trait_match}",
                value=self.model_dump(exclude={"source_db", "source_version"}),
                source_db=self.source_db,
                db_version=self.source_version,
                source_record=f"{self.source_assembly}:{self.source_gene_id}|{self.trait_key or 'any'}",
                primary_citation=f"PMID:{self.pmids[0]}" if self.pmids else None,
                quote=self.synopsis,
            )
        ]


def qtl_overlap(
    bundle: Bundle,
    region: Region,
    profile: TraitProfile | None = None,
    *,
    assembly: str | None = None,
    trait_only: bool = False,
    marker_flank_bp: int = 0,
) -> list[QtlHit]:
    """Return placed QTLs overlapping ``region``, trait matches first, narrowest first.

    QTLs placed from two or more markers must overlap the region. A QTL placed
    from one marker is a point: it is returned as ``marker_within`` when the
    marker lies in the region or within ``marker_flank_bp`` of it.
    """
    resolved = _resolve(bundle, region, assembly)
    data_assembly = _assembly_of(bundle, "qtl", resolved)
    require_same_assembly(resolved, data_assembly, "QTL spans")
    rows = bundle.rows(
        'SELECT * FROM qtl WHERE assembly = ? AND chrom = ? AND "end" >= ? AND start <= ?',
        [data_assembly, resolved.chrom, max(1, resolved.start - marker_flank_bp), resolved.end + marker_flank_bp],
    )
    hits = []
    for row in rows:
        start, end = int(row["start"]), int(row["end"])
        kind: QtlKind = "marker" if row["placement"] == "single_marker" else "interval"
        overlaps_region = end >= resolved.start and start <= resolved.end
        if kind == "interval" and not overlaps_region:
            continue
        if kind == "marker":
            overlap: OverlapType = "marker_within"
        elif start <= resolved.start and end >= resolved.end:
            overlap = "qtl_contains_window"
        elif start >= resolved.start and end <= resolved.end:
            overlap = "window_contains_qtl"
        else:
            overlap = "partial"
        match, matched = _trait_match(profile, list(row["trait_terms"] or []), row["trait_name"])
        if trait_only and match == "none":
            continue
        hits.append(
            QtlHit(
                label=resolved.name,
                qtl_id=row["qtl_id"],
                study_id=row["study_id"],
                qtl_name=row["qtl_name"],
                trait_name=row["trait_name"],
                trait_terms=list(row["trait_terms"] or []),
                assembly=data_assembly,
                chrom=row["chrom"],
                start=start,
                end=end,
                span_bp=int(row["span_bp"]),
                n_markers=int(row["n_markers"]),
                n_markers_placed=int(row["n_markers_placed"]),
                placement=row["placement"],
                kind=kind,
                overlap_type=overlap,
                overlap_bp=max(0, min(end, resolved.end) - max(start, resolved.start) + 1),
                distance_to_core=resolved.distance_to_core(start, end),
                wide=int(row["span_bp"]) > WIDE_QTL_BP,
                trait_match=match,
                matched=matched,
                publication_doi=row["publication_doi"],
                source_db=row["source_db"],
                source_version=row["source_version"],
            )
        )
    hits.sort(key=lambda hit: (hit.trait_match == "none", hit.kind == "marker", hit.wide, hit.span_bp, hit.qtl_id))
    return hits


def gwas_catalog_overlap(
    bundle: Bundle,
    region: Region,
    profile: TraitProfile | None = None,
    *,
    max_p: float | None = None,
    assembly: str | None = None,
    trait_only: bool = False,
) -> list[GwasCatalogHit]:
    """Return catalog GWAS hits inside ``region``; hits without a p-value pass ``max_p``."""
    resolved = _resolve(bundle, region, assembly)
    target = str(resolved.assembly)
    others = bundle.rows_raw(
        "SELECT DISTINCT assembly FROM gwas_hits WHERE assembly <> ? AND chrom IS NOT NULL", [target]
    )
    rows = bundle.rows(
        "SELECT * FROM gwas_hits WHERE assembly = ? AND chrom = ? AND pos BETWEEN ? AND ? ORDER BY pos, hit_id",
        [target, resolved.chrom, resolved.start, resolved.end],
    )
    if not rows and not bundle.rows_raw("SELECT 1 FROM gwas_hits WHERE assembly = ? LIMIT 1", [target]) and others:
        raise AssemblyMismatchError(
            f"GWAS hits are on {', '.join(sorted(str(row[0]) for row in others))}; "
            f"region {resolved.name} is on {target}. Lift the region over first."
        )
    hits = []
    for row in rows:
        p_value = row["p_value"]
        if max_p is not None and p_value is not None and float(p_value) > max_p:
            continue
        match, matched = _trait_match(profile, list(row["trait_terms"] or []), row["trait_name"])
        if trait_only and match == "none":
            continue
        pos = int(row["pos"])
        hits.append(
            GwasCatalogHit(
                label=resolved.name,
                hit_id=row["hit_id"],
                source_db=row["source_db"],
                study_id=row["study_id"],
                trait_name=row["trait_name"],
                trait_terms=list(row["trait_terms"] or []),
                marker=row["marker"],
                assembly=target,
                chrom=row["chrom"],
                pos=pos,
                p_value=float(p_value) if p_value is not None else None,
                distance_to_snp=abs(pos - resolved.snp_pos) if resolved.snp_pos else None,
                distance_to_core=resolved.distance_to_core(pos),
                pmid=row["pmid"],
                doi=row["doi"],
                reported_genes=list(row["reported_genes"] or []),
                trait_match=match,
                matched=matched,
                source_version=row["source_version"],
            )
        )
    hits.sort(
        key=lambda hit: (
            hit.trait_match == "none",
            hit.p_value if hit.p_value is not None else 1.0,
            hit.distance_to_snp or 0,
        )
    )
    return hits


def known_trait_genes(
    bundle: Bundle,
    profile: TraitProfile | None = None,
    *,
    region: Region | None = None,
    gene_ids: list[str] | None = None,
    assembly: str | None = None,
) -> list[KnownGeneHit]:
    """Return curated trait genes in a region, for given genes, or matching a trait.

    With a region or gene list every curated gene on the canonical assembly is
    returned with its trait match; with neither, only genes that match the
    profile, including curated genes with no canonical-assembly counterpart.
    """
    registry = registry_of(bundle)
    canonical = registry.canonical_assembly
    clauses = ["k.assembly = ?"] if region is not None or gene_ids is not None else ["TRUE"]
    parameters: list[Any] = [canonical] if region is not None or gene_ids is not None else []
    resolved = None
    if region is not None:
        resolved = _resolve(bundle, region, assembly)
        require_same_assembly(resolved, canonical, "Curated trait genes")
        clauses.append('g.chrom = ? AND g."end" >= ? AND g.start <= ?')
        parameters.extend([resolved.chrom, resolved.start, resolved.end])
    if gene_ids is not None:
        wanted = list(dict.fromkeys(gene_ids))
        if not wanted:
            return []
        clauses.append(f"k.gene_id IN ({', '.join('?' for _ in wanted)})")
        parameters.extend(wanted)
    rows = bundle.rows(
        "SELECT k.*, g.chrom, g.start, g.\"end\" FROM known_genes AS k "
        "LEFT JOIN genes AS g ON g.assembly = k.assembly AND g.gene_id = k.gene_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY k.gene_id, k.source_gene_id",
        parameters,
    )
    hits = []
    for row in rows:
        text = " ".join(
            str(part)
            for part in (" ".join(row["symbols"] or []), row["symbol_long"], row["synopsis"], " ".join(row["trait_names"] or []))
            if part
        )
        match, matched = _trait_match(profile, list(row["trait_terms"] or []), text)
        if region is None and gene_ids is None and match == "none":
            continue
        start = int(row["start"]) if row["start"] is not None else None
        end = int(row["end"]) if row["end"] is not None else None
        distance = None
        if resolved is not None and resolved.snp_pos is not None and start is not None and end is not None:
            distance = 0 if start <= resolved.snp_pos <= end else min(abs(start - resolved.snp_pos), abs(end - resolved.snp_pos))
        hits.append(
            KnownGeneHit(
                gene_id=row["gene_id"],
                assembly=row["assembly"],
                symbols=list(row["symbols"] or []),
                symbol_long=row["symbol_long"],
                synopsis=row["synopsis"],
                trait_terms=list(row["trait_terms"] or []),
                trait_names=list(row["trait_names"] or []),
                trait_match=match,
                matched=matched,
                trait_key=profile.key if profile else None,
                confidence=row["confidence"],
                pmids=list(row["pmids"] or []),
                dois=list(row["dois"] or []),
                mapping=row["mapping"],
                source_gene_id=row["source_gene_id"],
                source_assembly=row["source_assembly"],
                chrom=row["chrom"],
                start=start,
                end=end,
                label=resolved.name if resolved else None,
                distance_to_snp=distance,
                weight=float(row["weight"]),
                source_db=row["source_db"],
                source_version=row["source_version"],
            )
        )
    hits.sort(key=lambda hit: (hit.trait_match == "none", hit.trait_match == "keyword", hit.distance_to_snp or 0, hit.gene_id))
    return hits


def _resolve(bundle: Bundle, region: Region, assembly: str | None) -> Region:
    return resolve_region(registry_of(bundle), region, assembly)


def _assembly_of(bundle: Bundle, table: str, region: Region) -> str:
    rows = bundle.rows_raw(f"SELECT DISTINCT assembly FROM {table} WHERE chrom IS NOT NULL ORDER BY 1")
    assemblies = [str(row[0]) for row in rows]
    if region.assembly in assemblies:
        return str(region.assembly)
    return assemblies[0] if assemblies else registry_of(bundle).canonical_assembly


def _trait_match(profile: TraitProfile | None, terms: list[str], text: str | None) -> tuple[TraitMatch, list[str]]:
    if profile is None:
        return "none", []
    matched = profile.matched_terms(terms)
    if matched:
        return "ontology", matched
    keywords = profile.matched_keywords(text)
    if keywords:
        return "keyword", keywords
    return "none", []
