"""Variant location classes from gene models, and consequences from VEP or SnpEff.

The location class needs no alleles: CDS, splice (intronic within
``SPLICE_BP`` of an exon edge), 5'/3' UTR, intron, upstream (within
``UPSTREAM_BP`` of the transcription start), downstream (within
``DOWNSTREAM_BP`` of the end) or intergenic, per gene, from the CDS and UTR
intervals of every transcript (``gene_parts``). With REF and ALT known,
consequences come from Ensembl VEP with the Ensembl Plants 63 cache (offline,
in the ``ensemblorg/ensembl-vep`` image when no native VEP is installed), or
SnpEff when VEP is missing. Chromosomes are translated to Ensembl's ``1``-``20``.
"""

import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data import external
from agrihub_data.availability import VEP_CACHE_KIND, resource_paths
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import registry_of, source_version
from agrihub_data.registry import load_species, normalize_chrom

UPSTREAM_BP = 2_000
DOWNSTREAM_BP = 500
SPLICE_BP = 8
VEP_TIMEOUT_SECONDS = 600
VEP_FIELDS = ("Uploaded_variation", "Location", "Allele", "Gene", "Feature", "Consequence", "IMPACT", "Amino_acids", "Codons", "Protein_position", "STRAND")
IMPACT_ORDER = ("HIGH", "MODERATE", "LOW", "MODIFIER")
LocationClass = Literal["CDS", "splice", "five_prime_UTR", "three_prime_UTR", "intron", "upstream", "downstream", "intergenic"]
LOCATION_ORDER: tuple[LocationClass, ...] = ("CDS", "splice", "five_prime_UTR", "three_prime_UTR", "intron", "upstream", "downstream", "intergenic")


class VariantInput(BaseModel):
    """A variant to annotate; ``ref``/``alt`` enable consequences."""

    id: str | None = None
    chrom: str
    pos: int = Field(ge=1)
    ref: str | None = None
    alt: str | None = None

    @property
    def label(self) -> str:
        """Return the id, or ``chrom:pos``."""
        return self.id or f"{self.chrom}:{self.pos}"


class Consequence(BaseModel):
    """One predicted consequence on one transcript."""

    gene_id: str | None = None
    transcript_id: str | None = None
    terms: list[str]
    impact: str
    amino_acids: str | None = None
    codons: str | None = None
    protein_position: str | None = None


class VariantLocation(BaseModel):
    """Where a variant sits relative to one gene, and what VEP or SnpEff predicts there."""

    variant: str
    assembly: str
    chrom: str
    pos: int
    ref: str | None = None
    alt: str | None = None
    gene_id: str | None
    strand: str | None = None
    location_class: LocationClass
    distance_bp: int = 0
    transcripts: list[str] = Field(default_factory=list)
    consequences: list[Consequence] = Field(default_factory=list)
    impact: str | None = None
    """The most severe predicted impact on this gene (HIGH, MODERATE, LOW, MODIFIER)."""
    method: Literal["gene_models", "vep", "snpeff"] = "gene_models"
    note: str | None = None
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the location (and consequence) as one variant fact keyed to the gene, or the variant when intergenic."""
        top = self.consequences[0].terms[0] if self.consequences else None
        return [
            EvidenceItem(
                gene_id=self.gene_id or self.variant,
                category="variant",
                subtype=f"consequence:{top}" if top else f"location:{self.location_class}",
                value=self.model_dump(exclude={"source_version"}),
                source_db="Ensembl VEP" if self.method == "vep" else "SnpEff" if self.method == "snpeff" else "AgriHub gene-model overlap",
                db_version=self.source_version,
                source_record=f"{self.assembly}:{self.chrom}:{self.pos}:{self.ref or ''}>{self.alt or ''}|{self.variant}",
            )
        ]


def location_classes(bundle: Bundle, variants: list[VariantInput], assembly: str | None = None) -> list[VariantLocation]:
    """Return one location per (variant, nearby gene); an intergenic variant gets one row with its nearest gene."""
    registry = registry_of(bundle)
    target = registry.assembly(assembly).id
    stamped = bundle.rows_raw("SELECT min(source_version) FROM genes WHERE assembly = ?", [target])
    version = str(stamped[0][0]) if stamped and stamped[0][0] else "gene models"
    results: list[VariantLocation] = []
    for variant in variants:
        chrom = normalize_chrom(registry.species, variant.chrom, target)
        genes = bundle.rows(
            'SELECT gene_id, start, "end", strand FROM genes WHERE assembly = ? AND chrom = ? AND "end" >= ? AND start <= ? ORDER BY start',
            [target, chrom, variant.pos - UPSTREAM_BP, variant.pos + UPSTREAM_BP],
        )
        parts = _parts(bundle, target, [str(gene["gene_id"]) for gene in genes])
        common = {"variant": variant.label, "assembly": target, "chrom": chrom, "pos": variant.pos, "ref": variant.ref, "alt": variant.alt, "source_version": version}
        found = []
        for gene in genes:
            klass, distance, transcripts = classify(variant.pos, int(gene["start"]), int(gene["end"]), str(gene["strand"]), parts.get(str(gene["gene_id"]), []))
            if klass is not None:
                found.append(VariantLocation(gene_id=str(gene["gene_id"]), strand=str(gene["strand"]), location_class=klass, distance_bp=distance, transcripts=transcripts, **common))
        if not found:
            nearest = _nearest_gene(bundle, target, chrom, variant.pos)
            found.append(
                VariantLocation(
                    gene_id=None,
                    location_class="intergenic",
                    distance_bp=nearest[1] if nearest else 0,
                    note=f"nearest gene {nearest[0]} at {nearest[1]} bp" if nearest else "no gene on this chromosome",
                    **common,
                )
            )
        found.sort(key=lambda row: (LOCATION_ORDER.index(row.location_class), row.distance_bp, row.gene_id or ""))
        results.extend(found)
    return results


def classify(
    pos: int,
    start: int,
    end: int,
    strand: str,
    parts: list[tuple[str, str, int, int]],
) -> tuple[LocationClass | None, int, list[str]]:
    """Return the location class of ``pos`` for one gene, its distance outside the gene, and the transcripts involved.

    ``parts`` are ``(transcript, part, start, end)``; the most severe class over
    the gene's transcripts wins. Returns ``None`` when the gene is too far.
    """
    if start <= pos <= end:
        hits: dict[LocationClass, set[str]] = defaultdict(set)
        exons: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for transcript, part, part_start, part_end in parts:
            exons[transcript].append((part_start, part_end))
            if part_start <= pos <= part_end:
                hits[cast(LocationClass, part)].add(transcript)
        for transcript, intervals in exons.items():
            merged = _merge(intervals)
            if any(part_start <= pos <= part_end for part_start, part_end in merged):
                continue
            inside = merged[0][0] < pos < merged[-1][1] if merged else False
            if inside and any(abs(pos - edge) <= SPLICE_BP for interval in merged for edge in interval):
                hits["splice"].add(transcript)
            elif inside:
                hits["intron"].add(transcript)
        if not hits:
            return "intron", 0, []
        best = min(hits, key=LOCATION_ORDER.index)
        return best, 0, sorted(hits[best])
    upstream = start - pos if strand != "-" else pos - end
    if 0 < upstream <= UPSTREAM_BP:
        return "upstream", upstream, []
    downstream = pos - end if strand != "-" else start - pos
    if 0 < downstream <= DOWNSTREAM_BP:
        return "downstream", downstream, []
    return None, 0, []


def annotate_variants(bundle: Bundle, variants: list[VariantInput], assembly: str | None = None) -> tuple[list[VariantLocation], list[str]]:
    """Return location classes, with VEP (or SnpEff) consequences for variants with alleles, and notes on what was not run."""
    locations = location_classes(bundle, variants, assembly)
    with_alleles = [variant for variant in variants if variant.ref and variant.alt]
    notes: list[str] = []
    if not with_alleles:
        return locations, ["consequences need REF and ALT alleles; reported the gene-model location class only"]
    registry = registry_of(bundle)
    if (assembly and registry.assembly(assembly).id != registry.canonical_assembly):
        return locations, [f"consequences use the Ensembl cache on {registry.canonical_assembly}; lift the variants over first"]
    runner = external.vep_runner()
    caches = resource_paths(bundle.species, VEP_CACHE_KIND, bundle.path.parent.parent)
    if runner is not None and caches:
        try:
            output = run_vep(with_alleles, runner, caches[0], registry.species)
            return _attach(locations, parse_vep_tab(output), "vep", source_version(bundle, "ensembl_vep_cache")), notes
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            notes.append(f"VEP failed ({type(exc).__name__}: {str(exc)[:160]}); trying SnpEff")
    elif runner is None:
        notes.append("Ensembl VEP is not installed")
    else:
        notes.append("the VEP cache is not unpacked; build the heavy tier")
    snpeff = external.snpeff_path()
    if snpeff is not None:
        try:
            output = run_snpeff(with_alleles, snpeff, registry.species)
            return _attach(locations, parse_snpeff_vcf(output), "snpeff", "SnpEff Glycine_max"), notes
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            notes.append(f"SnpEff failed ({type(exc).__name__}: {str(exc)[:160]})")
    else:
        notes.append("SnpEff is not installed")
    notes.append("consequences unavailable; reported the gene-model location class only")
    return locations, notes


def ensembl_chrom(species: str, chrom: str) -> str:
    """Return Ensembl's name for a canonical chromosome (``Gm18`` -> ``18``); scaffolds keep their name."""
    canonical = normalize_chrom(species, chrom)
    chromosome = load_species(species).assembly().chromosome(canonical)
    return str(chromosome.number) if chromosome and chromosome.number else canonical


def vcf_text(variants: list[VariantInput], species: str) -> str:
    """Return a minimal VCF of the variants with Ensembl chromosome names."""
    lines = ["##fileformat=VCFv4.2", "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"]
    for variant in sorted(variants, key=lambda item: (ensembl_chrom(species, item.chrom).zfill(3), item.pos)):
        lines.append(f"{ensembl_chrom(species, variant.chrom)}\t{variant.pos}\t{variant.label}\t{variant.ref}\t{variant.alt}\t.\t.\t.")
    return "\n".join(lines) + "\n"


def run_vep(variants: list[VariantInput], runner: external.VepRunner, cache: Path, species: str) -> str:
    """Run VEP offline on the variants and return its tab-separated output."""
    cache_root = cache.parent.parent
    version = cache.name.split("_", 1)[0]
    ensembl_species = cache.parent.name
    with tempfile.TemporaryDirectory(prefix="agrihub-vep-") as work:
        Path(work, "input.vcf").write_text(vcf_text(variants, species), encoding="utf-8")
        arguments = [
            "--offline", "--cache", "--species", ensembl_species, "--cache_version", version, "--format", "vcf",
            "--tab", "--fields", ",".join(VEP_FIELDS), "--no_stats", "--force_overwrite", "--distance", str(UPSTREAM_BP),
        ]
        if runner.kind == "docker":
            Path(work).chmod(0o777)
            command = [
                "docker", "run", "--rm", "--network", "none",
                "-v", f"{cache_root}:/opt/vep/.vep:ro", "-v", f"{work}:/work",
                runner.target, "vep", *arguments, "--dir_cache", "/opt/vep/.vep", "-i", "/work/input.vcf", "-o", "/work/output.tsv",
            ]
        else:
            command = [runner.target, *arguments, "--dir_cache", str(cache_root), "-i", f"{work}/input.vcf", "-o", f"{work}/output.tsv"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=VEP_TIMEOUT_SECONDS, check=False)
        output = Path(work, "output.tsv")
        if completed.returncode != 0 or not output.exists():
            raise ValueError((completed.stderr or completed.stdout or "no output").strip().splitlines()[-1][:300])
        return output.read_text(encoding="utf-8")


def run_snpeff(variants: list[VariantInput], snpeff: Path, species: str) -> str:
    """Run SnpEff with the Glycine_max database and return the annotated VCF."""
    completed = subprocess.run(
        [str(snpeff), "-noStats", "-canon", "Glycine_max"],
        input=vcf_text(variants, species),
        capture_output=True,
        text=True,
        timeout=VEP_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError((completed.stderr or "SnpEff failed").strip().splitlines()[-1][:300])
    return completed.stdout


def parse_vep_tab(text: str) -> dict[str, list[Consequence]]:
    """Return consequences per uploaded variant id from VEP ``--tab`` output, most severe first."""
    header: list[str] = []
    found: dict[str, list[Consequence]] = defaultdict(list)
    for line in text.splitlines():
        if line.startswith("##") or not line.strip():
            continue
        if line.startswith("#"):
            header = line.lstrip("#").split("\t")
            continue
        row = dict(zip(header, line.split("\t"), strict=False))
        gene = _blank(row.get("Gene"))
        found[row.get("Uploaded_variation", "")].append(
            Consequence(
                gene_id=_glyma(gene),
                transcript_id=_blank(row.get("Feature")),
                terms=[term for term in (row.get("Consequence") or "").split(",") if term],
                impact=(row.get("IMPACT") or "MODIFIER").upper(),
                amino_acids=_blank(row.get("Amino_acids")),
                codons=_blank(row.get("Codons")),
                protein_position=_blank(row.get("Protein_position")),
            )
        )
    for consequences in found.values():
        consequences.sort(key=lambda item: _impact_rank(item.impact))
    return dict(found)


def parse_snpeff_vcf(text: str) -> dict[str, list[Consequence]]:
    """Return consequences per variant id from the ``ANN`` field of a SnpEff VCF."""
    found: dict[str, list[Consequence]] = defaultdict(list)
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 8:
            continue
        info = dict(part.split("=", 1) for part in fields[7].split(";") if "=" in part)
        for annotation in (info.get("ANN") or "").split(","):
            parts = annotation.split("|")
            if len(parts) < 11:
                continue
            found[fields[2]].append(
                Consequence(
                    gene_id=_glyma(parts[4]) or _blank(parts[4]),
                    transcript_id=_blank(parts[6]),
                    terms=[term for term in parts[1].split("&") if term],
                    impact=parts[2].upper() or "MODIFIER",
                    amino_acids=_blank(parts[10]),
                )
            )
    for consequences in found.values():
        consequences.sort(key=lambda item: _impact_rank(item.impact))
    return dict(found)


def most_severe_impact(consequences: list[Consequence]) -> str | None:
    """Return the most severe impact among consequences."""
    impacts = [item.impact for item in consequences]
    return min(impacts, key=_impact_rank) if impacts else None


def _attach(locations: list[VariantLocation], consequences: dict[str, list[Consequence]], method: Literal["vep", "snpeff"], version: str) -> list[VariantLocation]:
    attached = []
    for location in locations:
        own = [item for item in consequences.get(location.variant, []) if location.gene_id is None or item.gene_id in {None, location.gene_id}]
        if location.variant in consequences:
            location = location.model_copy(update={"consequences": own, "impact": most_severe_impact(own), "method": method, "source_version": version})
        attached.append(location)
    return attached


def _parts(bundle: Bundle, assembly: str, gene_ids: list[str]) -> dict[str, list[tuple[str, str, int, int]]]:
    if not gene_ids:
        return {}
    parts: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
    for gene_id, transcript, part, start, end in bundle.rows_raw(
        f'SELECT gene_id, transcript_id, part, start, "end" FROM gene_parts WHERE assembly = ? AND gene_id IN ({", ".join("?" for _ in gene_ids)}) ORDER BY 1, 2, 4',
        [assembly, *gene_ids],
    ):
        parts[str(gene_id)].append((str(transcript), str(part), int(start), int(end)))
    return parts


def _nearest_gene(bundle: Bundle, assembly: str, chrom: str, pos: int) -> tuple[str, int] | None:
    rows = bundle.rows_raw(
        'SELECT gene_id, least(abs(start - ?), abs("end" - ?)) AS distance FROM genes WHERE assembly = ? AND chrom = ? ORDER BY distance, gene_id LIMIT 1',
        [pos, pos, assembly, chrom],
    )
    return (str(rows[0][0]), int(rows[0][1])) if rows else None


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _impact_rank(impact: str) -> int:
    return IMPACT_ORDER.index(impact) if impact in IMPACT_ORDER else len(IMPACT_ORDER)


def _blank(value: str | None) -> str | None:
    return None if value in {None, "", "-"} else value


def _glyma(value: str | None) -> str | None:
    if value and value.upper().startswith("GLYMA_"):
        return "Glyma." + value[6:]
    return None

