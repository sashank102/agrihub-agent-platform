"""LD with a lead SNP and LD-based locus windows, computed with PLINK2.

Genotypes come from the bundle's LD panel (``Song_Hyten_2015``, SoySNP50K
genotypes of the USDA collection) or a study VCF placed under
``<species>/genotypes/``. A lead that is not a panel site is represented by
the nearest panel SNP with minor-allele frequency at least ``PROXY_MIN_MAF``
within ``PROXY_MAX_BP``; the proxy is reported.

PLINK2 runs ``--r2-unphased --ld-snp <lead> --ld-window-kb <window>
--ld-window-r2 <r2>``. The LD window runs from the furthest partner with
``r2 >= threshold`` on the left to the furthest on the right (always holding
the lead), capped at ``window_kb`` on each side, and is then extended to the
boundaries of the genes it cuts.
"""

import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data import external
from agrihub_data.availability import LD_PANEL_KIND, resource_paths
from agrihub_data.build.context import open_text
from agrihub_data.bundle import Bundle
from agrihub_data.paths import species_paths
from agrihub_data.query.common import registry_of, source_version
from agrihub_data.registry import normalize_chrom

DEFAULT_WINDOW_KB = 1_000
DEFAULT_R2 = 0.2
PROXY_MAX_BP = 50_000
PROXY_MIN_MAF = 0.05
PLINK_TIMEOUT_SECONDS = 300
MAX_PARTNERS_SHOWN = 15
PANEL_PREFIX = "panel:"


class LdUnavailableError(RuntimeError):
    """LD cannot be computed: no PLINK2, no genotypes, or the lead is not represented."""


class LdPartner(BaseModel):
    """A variant in LD with the lead."""

    variant_id: str
    chrom: str
    pos: int
    r2: float


class GenotypeSource(BaseModel):
    """Where genotypes come from: a bundled panel or a study VCF."""

    name: str
    kind: Literal["panel", "vcf"]
    path: Path
    format: Literal["pgen", "vcf"]


class LdWindow(BaseModel):
    """The LD neighbourhood of a lead SNP and the locus interval it implies."""

    species: str
    assembly: str
    lead: str
    chrom: str
    lead_pos: int
    tested_variant: str
    tested_pos: int
    proxy_distance_bp: int = 0
    genotypes: str
    r2_threshold: float
    window_kb: int
    n_partners: int
    start: int
    end: int
    ld_start: int
    ld_end: int
    span_bp: int
    extended_by_genes: list[str] = Field(default_factory=list)
    partners: list[LdPartner] = Field(default_factory=list)
    note: str | None = None
    source_version: str

    def gene_r2(self, gene_start: int, gene_end: int, flank_bp: int = 0) -> tuple[float, LdPartner | None]:
        """Return the best r2 of partners inside a gene (± ``flank_bp``), 1.0 when the tested variant is inside."""
        if gene_start - flank_bp <= self.tested_pos <= gene_end + flank_bp:
            return 1.0, None
        inside = [partner for partner in self.partners if gene_start - flank_bp <= partner.pos <= gene_end + flank_bp]
        best = max(inside, key=lambda partner: partner.r2, default=None)
        return (best.r2, best) if best else (0.0, None)

    def evidence(self) -> list[EvidenceItem]:
        """Return the LD window as one positional fact keyed to the lead SNP."""
        return [
            EvidenceItem(
                gene_id=self.lead,
                category="positional",
                subtype=f"ld_window:{self.genotypes}",
                value=self.model_dump(exclude={"source_version", "partners"}) | {"partners": [partner.model_dump() for partner in self.partners[:MAX_PARTNERS_SHOWN]]},
                source_db=f"PLINK2 r2 on {self.genotypes}",
                db_version=self.source_version,
                source_record=f"{self.assembly}:{self.chrom}:{self.lead_pos}|{self.genotypes}|r2>={self.r2_threshold}|{self.window_kb}kb",
            )
        ]


class GeneLd(BaseModel):
    """A gene's LD with the lead SNP of its locus."""

    gene_id: str
    lead: str
    r2: float
    via: str | None
    """The partner SNP in the gene, or ``None`` when the tested variant is inside it."""
    genotypes: str
    r2_threshold: float
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the gene's LD as one positional fact."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="positional",
                subtype=f"ld_r2:{self.genotypes}",
                value=self.model_dump(exclude={"gene_id", "source_version"}),
                source_db=f"PLINK2 r2 on {self.genotypes}",
                db_version=self.source_version,
                source_record=f"{self.lead}|{self.genotypes}|{self.gene_id}",
            )
        ]


def genotype_source(species: str, vcf_ref: str | None = None, data_dir: Path | str | None = None) -> GenotypeSource:
    """Return the genotypes to use: the named or first bundled panel, or a VCF under ``<species>/genotypes/``.

    Raises:
        LdUnavailableError: when no panel is built or the VCF does not exist.
        ValueError: when ``vcf_ref`` is a path outside the genotypes directory.
    """
    if vcf_ref and not vcf_ref.startswith(PANEL_PREFIX):
        if "/" in vcf_ref or "\\" in vcf_ref or vcf_ref.startswith("."):
            raise ValueError("genotype_vcf_ref names a file in the species genotypes directory, not a path")
        path = species_paths(species, data_dir).root / "genotypes" / vcf_ref
        if not path.is_file():
            raise LdUnavailableError(f"no genotype VCF {vcf_ref} in {path.parent}")
        return GenotypeSource(name=vcf_ref, kind="vcf", path=path, format="vcf")
    wanted = vcf_ref.removeprefix(PANEL_PREFIX) if vcf_ref else None
    for path in resource_paths(species, LD_PANEL_KIND, data_dir):
        name = path.name.split(".", 1)[0]
        if wanted in (None, name):
            return GenotypeSource(name=name, kind="panel", path=path, format="pgen" if path.suffix == ".pgen" else "vcf")
    raise LdUnavailableError(f"no LD panel{' ' + wanted if wanted else ''} is built for {species}; build the heavy tier")


def ld_window(
    bundle: Bundle,
    lead: str,
    chrom: str,
    pos: int,
    *,
    vcf_ref: str | None = None,
    window_kb: int = DEFAULT_WINDOW_KB,
    r2: float = DEFAULT_R2,
) -> LdWindow:
    """Return the LD window of a lead SNP on the canonical assembly.

    Raises:
        LdUnavailableError: when PLINK2 or genotypes are missing, or the lead has no usable panel SNP nearby.
    """
    registry = registry_of(bundle)
    canonical = registry.canonical_assembly
    name = normalize_chrom(registry.species, chrom, canonical)
    plink2 = external.plink2_path()
    if plink2 is None:
        raise LdUnavailableError("PLINK2 is not installed (set AGRIHUB_PLINK2 or run scripts/setup_heavy_tools.sh)")
    source = genotype_source(registry.species, vcf_ref, bundle.path.parent.parent)
    tested, tested_pos = _tested_variant(bundle, source, name, pos)
    partners = run_ld(plink2, source, tested, window_kb, r2, registry.species)
    note = None if tested_pos == pos else f"{lead} is not a {source.name} site; tested the nearest common panel SNP {tested} ({abs(tested_pos - pos)} bp away)"
    window = window_from_partners(name, pos, tested_pos, partners, window_kb, r2)
    start, end, genes = extend_to_genes(bundle, canonical, name, window[0], window[1])
    chromosome = registry.assembly(canonical).chromosome(name)
    if chromosome is not None:
        end = min(end, chromosome.length)
    if not partners:
        note = (note + "; " if note else "") + f"no {source.name} SNP within {window_kb} kb has r2 >= {r2:g} with it"
    return LdWindow(
        species=registry.species,
        assembly=canonical,
        lead=lead,
        chrom=name,
        lead_pos=pos,
        tested_variant=tested,
        tested_pos=tested_pos,
        proxy_distance_bp=abs(tested_pos - pos),
        genotypes=source.name,
        r2_threshold=r2,
        window_kb=window_kb,
        n_partners=len(partners),
        start=max(1, start),
        end=end,
        ld_start=window[0],
        ld_end=window[1],
        span_bp=end - max(1, start) + 1,
        extended_by_genes=genes,
        partners=sorted(partners, key=lambda partner: -partner.r2),
        note=note,
        source_version=source_version(bundle, "lis_panel_song_hyten_2015") if source.kind == "panel" else source.name,
    )


def gene_ld(window: LdWindow, genes: list[tuple[str, int, int]]) -> list[GeneLd]:
    """Return each gene's best r2 with the lead, for genes with any LD at or above the window threshold."""
    rows = []
    for gene_id, start, end in genes:
        value, partner = window.gene_r2(start, end)
        if value >= window.r2_threshold:
            rows.append(
                GeneLd(
                    gene_id=gene_id,
                    lead=window.lead,
                    r2=round(value, 4),
                    via=partner.variant_id if partner else None,
                    genotypes=window.genotypes,
                    r2_threshold=window.r2_threshold,
                    source_version=window.source_version,
                )
            )
    return rows


def window_from_partners(chrom: str, lead_pos: int, tested_pos: int, partners: list[LdPartner], window_kb: int, r2: float) -> tuple[int, int]:
    """Return ``(start, end)``: the furthest partners at or above ``r2`` on each side, holding the lead, capped at ``window_kb``."""
    cap = window_kb * 1_000
    kept = [partner.pos for partner in partners if partner.chrom == chrom and partner.r2 >= r2 and abs(partner.pos - tested_pos) <= cap]
    start = min([lead_pos, tested_pos, *kept])
    end = max([lead_pos, tested_pos, *kept])
    return max(1, start, tested_pos - cap), min(end, tested_pos + cap)


def extend_to_genes(bundle: Bundle, assembly: str, chrom: str, start: int, end: int) -> tuple[int, int, list[str]]:
    """Extend an interval to the full span of the genes overlapping its two ends."""
    extended: list[str] = []
    for edge in (start, end):
        for gene_id, gene_start, gene_end in bundle.rows_raw(
            'SELECT gene_id, start, "end" FROM genes WHERE assembly = ? AND chrom = ? AND start <= ? AND "end" >= ? ORDER BY start',
            [assembly, chrom, edge, edge],
        ):
            if int(gene_start) < start or int(gene_end) > end:
                extended.append(str(gene_id))
            start, end = min(start, int(gene_start)), max(end, int(gene_end))
    return start, end, sorted(set(extended))


def run_ld(plink2: Path, source: GenotypeSource, variant: str, window_kb: int, r2: float, species: str) -> list[LdPartner]:
    """Run PLINK2 ``--r2-unphased`` for one variant and return its partners."""
    with tempfile.TemporaryDirectory(prefix="agrihub-ld-") as work:
        prefix = Path(work) / "ld"
        inputs = ["--pfile", str(source.path.with_suffix(""))] if source.format == "pgen" else ["--vcf", str(source.path), "--set-all-var-ids", "@:#"]
        command = [
            str(plink2), *inputs, "--allow-extra-chr", "--r2-unphased", "--ld-snp", variant,
            "--ld-window-kb", str(window_kb), "--ld-window-r2", str(r2), "--threads", "2", "--out", str(prefix),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=PLINK_TIMEOUT_SECONDS, check=False)
        output = prefix.with_suffix(".vcor")
        if completed.returncode != 0 or not output.exists():
            message = (completed.stderr or completed.stdout or "no output").strip().splitlines()
            raise LdUnavailableError(f"plink2 failed for {variant}: {message[-1] if message else 'no output'}")
        return parse_vcor(output.read_text(encoding="utf-8"), species)


def parse_vcor(text: str, species: str) -> list[LdPartner]:
    """Parse PLINK2 ``.vcor`` (or PLINK 1.9 ``.ld``) output into partners of the tested variant."""
    partners: list[LdPartner] = []
    header: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if line.startswith("#") or fields[0] == "CHR_A":
            header = [name.lstrip("#") for name in fields]
            continue
        row = dict(zip(header, fields, strict=False))
        value = row.get("UNPHASED_R2") or row.get("PHASED_R2") or row.get("R2")
        chrom = row.get("CHROM_B") or row.get("CHR_B")
        position = row.get("POS_B") or row.get("BP_B")
        if value is None or chrom is None or position is None:
            continue
        try:
            canonical = normalize_chrom(species, chrom)
        except ValueError:
            continue
        partners.append(LdPartner(variant_id=row.get("ID_B") or row.get("SNP_B") or f"{chrom}:{position}", chrom=canonical, pos=int(position), r2=round(float(value), 4)))
    return partners


def _tested_variant(bundle: Bundle, source: GenotypeSource, chrom: str, pos: int) -> tuple[str, int]:
    """Return the panel variant at the lead, or the nearest common one within ``PROXY_MAX_BP``."""
    if source.kind == "vcf":
        return f"{vcf_chrom(source.path, registry_of(bundle).species, chrom)}:{pos}", pos
    rows = bundle.rows_raw(
        "SELECT variant_id, pos, alt_freq FROM variants WHERE panel = ? AND chrom = ? AND pos BETWEEN ? AND ? ORDER BY abs(pos - ?), pos",
        [source.name, chrom, pos - PROXY_MAX_BP, pos + PROXY_MAX_BP, pos],
    )
    for variant_id, site, frequency in rows:
        if int(site) == pos:
            return str(variant_id), pos
        if frequency is None or min(float(frequency), 1 - float(frequency)) >= PROXY_MIN_MAF:
            return str(variant_id), int(site)
    raise LdUnavailableError(f"no common {source.name} SNP within {PROXY_MAX_BP // 1000} kb of {chrom}:{pos}")


def vcf_chrom(path: Path, species: str, chrom: str) -> str:
    """Return how a VCF spells a canonical chromosome, from its contig lines or records.

    Raises:
        LdUnavailableError: when the VCF has no sequence that normalizes to ``chrom``.
    """
    with open_text(path) as handle:
        for line in handle:
            if line.startswith("##contig=<ID="):
                candidate = line.removeprefix("##contig=<ID=").split(",", 1)[0].rstrip(">\n")
            elif line.startswith("#"):
                continue
            else:
                candidate = line.split("\t", 1)[0]
            try:
                if normalize_chrom(species, candidate) == chrom:
                    return candidate
            except ValueError:
                continue
    raise LdUnavailableError(f"{path.name} has no records on {chrom}")
