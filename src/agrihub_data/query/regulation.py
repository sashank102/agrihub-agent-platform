"""Transcription-factor status, regulators, targets and SNPs in regulatory intervals."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import (
    Region,
    registry_of,
    resolve_region,
    source_version,
)

MAX_LISTED = 12
PROMOTER_BP = 2_000
"""Upstream distance within which a TFBS or conserved element is attributed to a gene's promoter."""
RegionKind = Literal["tfbs", "cns"]


class Partner(BaseModel):
    """A regulator or target, with how the link was predicted."""

    gene_id: str
    family: str | None = None
    evidence: list[str] = Field(default_factory=list)


class RegulationRecord(BaseModel):
    """What PlantTFDB and PlantRegMap say about one gene's regulation."""

    gene_id: str
    is_tf: bool
    family: str | None = None
    motif_ids: list[str] = Field(default_factory=list)
    n_targets: int = 0
    targets: list[Partner] = Field(default_factory=list)
    n_regulators: int = 0
    regulators: list[Partner] = Field(default_factory=list)
    focus_targets: list[str] = Field(default_factory=list)
    """Targets that are among the queried genes."""
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the TF status and regulatory links as one regulation fact (nothing when there is neither)."""
        if not self.is_tf and not self.n_regulators:
            return []
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="regulation",
                subtype="regulation:tf" if self.is_tf else "regulation:target",
                value=self.model_dump(exclude={"gene_id", "source_version"}),
                source_db="PlantTFDB/PlantRegMap",
                db_version=self.source_version,
                source_record=f"PlantRegMap:{self.gene_id}",
            )
        ]


class RegulatoryHit(BaseModel):
    """A SNP inside a promoter TFBS or a conserved element."""

    label: str
    snp: str
    chrom: str
    pos: int
    assembly: str
    kind: RegionKind
    region_id: str
    start: int
    end: int
    tf_gene_id: str | None = None
    tf_family: str | None = None
    score: float | None = None
    gene_id: str | None = None
    """The gene whose promoter (within ``PROMOTER_BP`` upstream) or body holds the interval."""
    relation: str | None = None
    source_db: str
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the hit as one regulation fact keyed to the gene it sits in front of (or the locus label)."""
        return [
            EvidenceItem(
                gene_id=self.gene_id or self.label,
                category="regulation",
                subtype=f"{self.kind}_hit:{self.region_id}",
                value=self.model_dump(exclude={"source_db", "source_version"}),
                source_db=self.source_db,
                db_version=self.source_version,
                source_record=f"{self.assembly}:{self.chrom}:{self.pos}|{self.region_id}",
            )
        ]


def get_regulation(bundle: Bundle, gene_ids: list[str]) -> list[RegulationRecord]:
    """Return TF family, motifs, targets and regulators of each gene, in input order."""
    canonical = registry_of(bundle).canonical_assembly
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    placeholders = ", ".join("?" for _ in wanted)
    families = {str(gene): str(family) for gene, family in bundle.rows_raw("SELECT gene_id, family FROM tf WHERE assembly = ?", [canonical])}
    motifs = {
        str(row["gene_id"]): list(row["motif_ids"] or [])
        for row in bundle.rows(f"SELECT gene_id, motif_ids FROM tf WHERE assembly = ? AND gene_id IN ({placeholders})", [canonical, *wanted])
    }
    links = bundle.rows(
        "SELECT tf_gene_id, target_gene_id, list(DISTINCT evidence ORDER BY evidence) AS evidence FROM regulation "
        f"WHERE assembly = ? AND (tf_gene_id IN ({placeholders}) OR target_gene_id IN ({placeholders})) GROUP BY 1, 2 ORDER BY 1, 2",
        [canonical, *wanted, *wanted],
    )
    version = source_version(bundle, "plantregmap_soybean")
    focus = set(wanted)
    records = []
    for gene_id in wanted:
        targets = [row for row in links if row["tf_gene_id"] == gene_id]
        regulators = [row for row in links if row["target_gene_id"] == gene_id]
        targets.sort(key=lambda row: (row["target_gene_id"] not in focus, -len(row["evidence"]), row["target_gene_id"]))
        regulators.sort(key=lambda row: (-len(row["evidence"]), row["tf_gene_id"]))
        records.append(
            RegulationRecord(
                gene_id=gene_id,
                is_tf=gene_id in families,
                family=families.get(gene_id),
                motif_ids=motifs.get(gene_id, []),
                n_targets=len(targets),
                targets=[_partner(row["target_gene_id"], row, families) for row in targets[:MAX_LISTED]],
                n_regulators=len(regulators),
                regulators=[_partner(row["tf_gene_id"], row, families) for row in regulators[:MAX_LISTED]],
                focus_targets=[str(row["target_gene_id"]) for row in targets if row["target_gene_id"] in focus and row["target_gene_id"] != gene_id],
                source_version=version,
            )
        )
    return records


def snp_in_tfbs_or_cns(bundle: Bundle, snps: list[Region], assembly: str | None = None) -> list[RegulatoryHit]:
    """Return the promoter TFBS and conserved elements each SNP falls in, with the gene they belong to.

    Each region is a SNP (``start == end`` or ``snp_pos``) labeled with its id.
    Intervals are on the canonical assembly; a SNP on another assembly is refused.
    """
    registry = registry_of(bundle)
    families = {str(gene): str(family) for gene, family in bundle.rows_raw("SELECT gene_id, family FROM tf")}
    versions = bundle.source_versions()
    hits: list[RegulatoryHit] = []
    for raw in snps:
        snp = resolve_region(registry, raw, assembly)
        if snp.assembly != registry.canonical_assembly:
            raise ValueError(f"regulatory intervals are on {registry.canonical_assembly}; lift {snp.name} over first")
        pos = snp.snp_pos or snp.start
        for row in bundle.rows(
            'SELECT kind, region_id, start, "end", tf_gene_id, score, source_db, source_version FROM regulatory_regions '
            'WHERE assembly = ? AND chrom = ? AND start <= ? AND "end" >= ? ORDER BY kind DESC, start, region_id',
            [snp.assembly, snp.chrom, pos, pos],
        ):
            gene_id, relation = _owner(bundle, str(snp.assembly), snp.chrom, int(row["start"]), int(row["end"]))
            hits.append(
                RegulatoryHit(
                    label=snp.label or f"{snp.chrom}:{pos}",
                    snp=snp.label or f"{snp.chrom}:{pos}",
                    chrom=snp.chrom,
                    pos=pos,
                    assembly=str(snp.assembly),
                    kind=row["kind"],
                    region_id=row["region_id"],
                    start=int(row["start"]),
                    end=int(row["end"]),
                    tf_gene_id=row["tf_gene_id"],
                    tf_family=families.get(str(row["tf_gene_id"])) if row["tf_gene_id"] else None,
                    score=row["score"],
                    gene_id=gene_id,
                    relation=relation,
                    source_db=row["source_db"],
                    source_version=versions.get("plantregmap_soybean", str(row["source_version"])),
                )
            )
    return hits


def _owner(bundle: Bundle, assembly: str, chrom: str, start: int, end: int) -> tuple[str | None, str | None]:
    """Return the gene whose body or ``PROMOTER_BP`` upstream window holds an interval, nearest first."""
    rows = bundle.rows(
        'SELECT gene_id, start, "end", strand FROM genes WHERE assembly = ? AND chrom = ? AND "end" >= ? AND start <= ?',
        [assembly, chrom, start - PROMOTER_BP, end + PROMOTER_BP],
    )
    best: tuple[int, str, str] | None = None
    for row in rows:
        gene_start, gene_end, strand = int(row["start"]), int(row["end"]), str(row["strand"])
        if gene_start <= end and start <= gene_end:
            candidate = (0, str(row["gene_id"]), "in_gene")
        elif strand == "-" and gene_end < start <= gene_end + PROMOTER_BP:
            candidate = (start - gene_end, str(row["gene_id"]), "promoter")
        elif strand != "-" and gene_start - PROMOTER_BP <= end < gene_start:
            candidate = (gene_start - end, str(row["gene_id"]), "promoter")
        else:
            continue
        if best is None or candidate < best:
            best = candidate
    return (best[1], best[2]) if best else (None, None)


def _partner(gene_id: Any, row: dict[str, Any], families: dict[str, str]) -> Partner:
    return Partner(gene_id=str(gene_id), family=families.get(str(gene_id)), evidence=list(row["evidence"] or []))
