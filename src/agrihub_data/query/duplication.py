"""Homeologs from recent-duplication synteny and GmHapMap haplotypes by gene."""

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import registry_of, source_version

MAX_SNPS_SHOWN = 20


class HomeologPair(BaseModel):
    """A gene and its homeolog from the soybean recent whole-genome duplication."""

    gene_id: str
    homeolog_id: str
    homeolog_chrom: str | None = None
    homeolog_defline: str | None = None
    block_id: str
    median_ks: float | None = None
    family: str
    offset_bp: int
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the pair as one positional fact about ``gene_id``."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="positional",
                subtype=f"homeolog:{self.homeolog_id}",
                value=self.model_dump(exclude={"gene_id", "source_version"}),
                source_db="LIS synteny",
                db_version=self.source_version,
                source_record=f"{self.block_id}|{self.gene_id}|{self.homeolog_id}",
                quote=self.homeolog_defline,
            )
        ]


class HaplotypeSnp(BaseModel):
    """One SNP of a gene's haplotypes."""

    snp_id: str
    pos: int
    alleles: str | None = None
    genotypes: list[str]


class NonSynonymousSnp(BaseModel):
    """A GmHapMap non-synonymous SNP in the gene."""

    variant_id: str
    pos: int
    ref: str
    alt: str
    alt_freq: float | None = None


class GeneHaplotypes(BaseModel):
    """GmHapMap haplotypes of one gene and its non-synonymous SNPs."""

    gene_id: str
    chrom: str
    n_snps: int
    haplotypes: list[str]
    snps: list[HaplotypeSnp] = Field(default_factory=list)
    nonsynonymous: list[NonSynonymousSnp] = Field(default_factory=list)
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the haplotype summary as one variant fact."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="variant",
                subtype="haplotypes:GmHapMap",
                value=self.model_dump(exclude={"gene_id", "source_version"}),
                source_db="GmHapMap",
                db_version=self.source_version,
                source_record=f"GmHapMap:{self.gene_id}",
            )
        ]


def homeologs(bundle: Bundle, gene_ids: list[str]) -> list[HomeologPair]:
    """Return the homeologs of each gene, best placed first."""
    canonical = registry_of(bundle).canonical_assembly
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    rows = bundle.rows(
        "SELECT h.*, g.chrom AS homeolog_chrom, g.defline AS homeolog_defline FROM homeologs AS h "
        "LEFT JOIN genes AS g ON g.assembly = h.assembly AND g.gene_id = h.homeolog_id "
        f"WHERE h.assembly = ? AND h.gene_id IN ({', '.join('?' for _ in wanted)}) ORDER BY h.gene_id, h.offset_bp, h.homeolog_id",
        [canonical, *wanted],
    )
    order = {gene: index for index, gene in enumerate(wanted)}
    pairs = [
        HomeologPair(
            gene_id=row["gene_id"],
            homeolog_id=row["homeolog_id"],
            homeolog_chrom=row["homeolog_chrom"],
            homeolog_defline=row["homeolog_defline"],
            block_id=row["block_id"],
            median_ks=row["median_ks"],
            family=row["family"],
            offset_bp=int(row["offset_bp"]),
            source_version=row["source_version"],
        )
        for row in rows
    ]
    pairs.sort(key=lambda pair: (order[pair.gene_id], pair.offset_bp))
    return pairs


def gene_haplotypes(bundle: Bundle, gene_ids: list[str]) -> list[GeneHaplotypes]:
    """Return GmHapMap haplotype SNPs and non-synonymous SNPs per gene, in input order."""
    canonical = registry_of(bundle).canonical_assembly
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return []
    placeholders = ", ".join("?" for _ in wanted)
    snps: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bundle.rows(f"SELECT * FROM gene_haplotypes WHERE assembly = ? AND gene_id IN ({placeholders}) ORDER BY gene_id, pos", [canonical, *wanted]):
        snps[str(row["gene_id"])].append(row)
    spans = {
        str(row["gene_id"]): row
        for row in bundle.rows(f'SELECT gene_id, chrom, start, "end" FROM genes WHERE assembly = ? AND gene_id IN ({placeholders})', [canonical, *wanted])
    }
    version = source_version(bundle, "gmhapmap")
    results = []
    for gene_id in wanted:
        rows = snps.get(gene_id, [])
        span = spans.get(gene_id)
        if not rows or span is None:
            continue
        nonsyn = bundle.rows(
            "SELECT variant_id, pos, ref, alt, alt_freq FROM variants WHERE panel = 'GmHapMap_NonSyn' AND assembly = ? AND chrom = ? AND pos BETWEEN ? AND ? ORDER BY pos",
            [canonical, span["chrom"], span["start"], span["end"]],
        )
        results.append(
            GeneHaplotypes(
                gene_id=gene_id,
                chrom=str(span["chrom"]),
                n_snps=len(rows),
                haplotypes=[str(name) for name in rows[0]["haplotypes"] or []],
                snps=[HaplotypeSnp(snp_id=row["snp_id"], pos=row["pos"], alleles=row["alleles"], genotypes=list(row["genotypes"] or [])) for row in rows[:MAX_SNPS_SHOWN]],
                nonsynonymous=[NonSynonymousSnp(**row) for row in nonsyn],
                source_version=version,
            )
        )
    return results
