"""Heavy tier: the VEP cache, LD reference panels, GmHapMap haplotypes and homeologs.

The VEP cache and LD panels live next to the bundle (``<species>/vep``,
``<species>/ld``) and are listed in ``resources``. A panel is converted to a
PLINK2 ``.pgen`` with allele frequencies when PLINK2 is installed at build
time; otherwise the resource points at the fetched VCF, which ``ld_with_lead``
can read directly once PLINK2 is installed. Panel sites go to ``variants``.

Homeologs come from recent-duplication synteny blocks, which pair regions
rather than genes: for each gene in a block's region, the homeolog is the
gene in the paired region that shares its PANTHER subfamily (else family)
and lies nearest the gene's projected position.
"""

import json
import re
import shutil
import subprocess
import tarfile
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agrihub_data import external
from agrihub_data.availability import LD_PANEL_KIND, VEP_CACHE_KIND
from agrihub_data.build.context import BuildContext, BuildError, open_text
from agrihub_data.build.gff import features
from agrihub_data.build.regulation import canonical_genes
from agrihub_data.registry import Source

PLINK_TIMEOUT_SECONDS = 1_800
_MATCH = re.compile(r"^(?P<prefix>.*\.)?(?P<chrom>[^.:]+):(?P<start>\d+)\.\.(?P<end>\d+)$")
_DIVERSITY = re.compile(r"\.div\.(?P<panel>[^.]+)$")


def build_vep_cache(ctx: BuildContext, source: Source) -> None:
    """Unpack the VEP cache tarball under ``<species>/vep`` and list it as a resource."""
    tarball = ctx.file(source, ".tar.gz")
    target = ctx.paths.root / "vep"
    staging = ctx.paths.root / "vep.building"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(tarball) as archive:
        archive.extractall(staging, filter="data")
    caches = sorted(path for path in staging.glob("*/*") if path.is_dir() and re.match(r"^\d+_", path.name))
    if len(caches) != 1:
        shutil.rmtree(staging, ignore_errors=True)
        raise BuildError(f"{source.id}: expected one <species>/<version>_<assembly> cache directory, found {len(caches)}")
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    cache = target / caches[0].relative_to(staging)
    chromosomes = sorted(path.name for path in cache.iterdir() if path.is_dir())
    ctx.count(source.id, "cache_chromosomes", len(chromosomes))
    _resource(
        ctx,
        source,
        VEP_CACHE_KIND,
        VEP_CACHE_KIND,
        cache.relative_to(ctx.paths.root).as_posix(),
        {"species": cache.parent.name, "cache_version": cache.name.split("_", 1)[0], "assembly": cache.name.split("_", 1)[1], "chromosomes": len(chromosomes)},
    )


def build_ld_panel(ctx: BuildContext, source: Source) -> None:
    """List a genotype panel's sites in ``variants`` and register it as an LD panel."""
    vcf = ctx.file(source, ".vcf.gz")
    panel = panel_name(source)
    canonical = ctx.registry.canonical_assembly
    frequencies: dict[str, float] = {}
    plink2 = external.plink2_path()
    details: dict[str, Any] = {"panel": panel, "format": "vcf"}
    relative = vcf.relative_to(ctx.paths.root).as_posix()
    if plink2 is not None:
        prefix = convert_panel(plink2, vcf, ctx.paths.root / "ld", panel)
        frequencies = read_frequencies(prefix.with_suffix(".afreq"))
        relative = prefix.with_suffix(".pgen").relative_to(ctx.paths.root).as_posix()
        details = {"panel": panel, "format": "pgen"}
        ctx.count(source.id, "converted_to_pgen")
    else:
        ctx.count(source.id, "kept_vcf_without_plink2")

    def sites() -> Iterator[dict[str, Any]]:
        with open_text(vcf) as handle:
            for line in handle:
                if line.startswith("##"):
                    continue
                if line.startswith("#"):
                    details["samples"] = len(line.rstrip("\n").split("\t")) - 9
                    continue
                fields = line.split("\t", 8)
                chrom = ctx.chrom(canonical, fields[0])
                if chrom is None:
                    ctx.count(source.id, "unplaced_sites")
                    continue
                yield {
                    **ctx.base(source, canonical),
                    "panel": panel,
                    "variant_id": fields[2],
                    "chrom": chrom,
                    "pos": int(fields[1]),
                    "ref": fields[3],
                    "alt": fields[4],
                    "alt_freq": frequencies.get(fields[2]),
                    "source_db": "LIS",
                }

    ctx.count(source.id, "sites", ctx.insert("variants", sites()))
    _resource(ctx, source, f"{LD_PANEL_KIND}:{panel}", LD_PANEL_KIND, relative, details)


def convert_panel(plink2: Path, vcf: Path, directory: Path, panel: str) -> Path:
    """Convert a VCF to ``<directory>/<panel>.pgen`` with allele frequencies; return the prefix."""
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / panel
    staging = directory / f"{panel}.building"
    command = [str(plink2), "--vcf", str(vcf), "--allow-extra-chr", "--max-alleles", "2", "--make-pgen", "--freq", "--threads", "4", "--out", str(staging)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=PLINK_TIMEOUT_SECONDS, check=False)
    if completed.returncode != 0:
        raise BuildError(f"plink2 could not convert {vcf.name}: {(completed.stderr or completed.stdout).strip().splitlines()[-1:]}")
    for suffix in (".pgen", ".pvar", ".psam", ".afreq", ".log"):
        Path(f"{staging}{suffix}").replace(Path(f"{prefix}{suffix}"))
    return prefix


def read_frequencies(path: Path) -> dict[str, float]:
    """Return ``variant id -> ALT frequency`` from a PLINK2 ``.afreq`` file."""
    frequencies: dict[str, float] = {}
    if not path.exists():
        return frequencies
    header: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if line.startswith("#"):
            header = [name.lstrip("#") for name in fields]
            continue
        row = dict(zip(header, fields, strict=False))
        try:
            frequencies[row["ID"]] = round(float(row["ALT_FREQS"]), 4)
        except (KeyError, ValueError):
            continue
    return frequencies


def panel_name(source: Source) -> str:
    """Return the panel name of a LIS diversity source (``Song_Hyten_2015``)."""
    match = _DIVERSITY.search(source.version)
    return match.group("panel") if match else source.id


def build_gmhapmap(ctx: BuildContext, source: Source) -> None:
    """Load GmHapMap haplotypes by gene and the sites of its non-synonymous SNPs."""
    known = canonical_genes(ctx)
    canonical = ctx.registry.canonical_assembly
    base = ctx.base(source, canonical)

    def haplotypes() -> Iterator[dict[str, Any]]:
        names: list[str] = []
        with open_text(ctx.file(source, ".haplotypes_by_gene.tsv.gz")) as handle:
            for line in handle:
                fields = line.rstrip("\n\r").split("\t")
                if line.startswith("#"):
                    names = [name for name in fields[6:] if name]
                    continue
                if len(fields) < 7 or fields[0] not in known:
                    ctx.count(source.id, "rows_without_gene")
                    continue
                chrom = ctx.chrom(canonical, fields[3])
                if chrom is None:
                    ctx.count(source.id, "unplaced_snps")
                    continue
                yield {
                    **base,
                    "gene_id": fields[0],
                    "snp_id": fields[1],
                    "chrom": chrom,
                    "pos": int(fields[4]),
                    "alleles": fields[2] or None,
                    "haplotypes": names[: len(fields) - 6],
                    "genotypes": fields[6 : 6 + len(names)],
                    "source_db": "GmHapMap",
                }

    ctx.count(source.id, "haplotype_snps", ctx.insert("gene_haplotypes", haplotypes()))
    nonsyn = ctx.optional_file(source, ".NonSynSNPs.vcf.gz")
    if nonsyn is None:
        return

    def sites() -> Iterator[dict[str, Any]]:
        with open_text(nonsyn) as handle:
            for line in handle:
                if line.startswith("#"):
                    continue
                fields = line.rstrip("\n").split("\t", 9)
                if len(fields) < 10:
                    continue
                chrom = ctx.chrom(canonical, fields[0])
                if chrom is None:
                    ctx.count(source.id, "unplaced_nonsyn")
                    continue
                genotypes = fields[9]
                alt = genotypes.count("1/1") * 2 + genotypes.count("0/1") + genotypes.count("1/0")
                called = 2 * (genotypes.count("/") - genotypes.count("./."))
                yield {
                    **base,
                    "panel": "GmHapMap_NonSyn",
                    "variant_id": fields[2],
                    "chrom": chrom,
                    "pos": int(fields[1]),
                    "ref": fields[3],
                    "alt": fields[4],
                    "alt_freq": round(alt / called, 4) if called else None,
                    "source_db": "GmHapMap",
                }

    ctx.count(source.id, "nonsyn_sites", ctx.insert("variants", sites()))


def build_lis_synteny(ctx: BuildContext, source: Source) -> None:
    """Pair genes of recent-duplication blocks into homeologs that share a PANTHER family."""
    canonical = ctx.registry.canonical_assembly
    genes: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for gene_id, chrom, start, end in ctx.connection.execute('SELECT gene_id, chrom, start, "end" FROM genes WHERE assembly = ? ORDER BY chrom, start', [canonical]).fetchall():
        genes[str(chrom)].append((int(start), int(end), str(gene_id)))
    families: dict[str, set[str]] = defaultdict(set)
    for gene_id, value in ctx.connection.execute("SELECT gene_id, value FROM annotation WHERE assembly = ? AND kind = 'panther'", [canonical]).fetchall():
        families[str(gene_id)].add(str(value))
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for number, feature in enumerate(features(ctx.file(source, ".gff3.gz"), {"syntenic_region"}), start=1):
        match = _MATCH.match(feature.attributes.get("matches", ""))
        chrom_a = ctx.chrom(canonical, feature.seqid)
        chrom_b = ctx.chrom(canonical, match.group("chrom")) if match else None
        if match is None or chrom_a is None or chrom_b is None:
            ctx.count(source.id, "blocks_unplaced")
            continue
        ctx.count(source.id, "blocks")
        block = f"HXNY:{chrom_a}:{feature.start}-{feature.end}|{chrom_b}:{match.group('start')}-{match.group('end')}"
        start_b, end_b = int(match.group("start")), int(match.group("end"))
        ks = _number(feature.attributes.get("median_Ks"))
        region_b = [gene for gene in genes.get(chrom_b, []) if gene[1] >= start_b and gene[0] <= end_b]
        for start, end, gene_id in genes.get(chrom_a, []):
            if end < feature.start or start > feature.end:
                continue
            fraction = ((start + end) / 2 - feature.start) / max(1, feature.end - feature.start)
            projected = start_b + (1 - fraction if feature.strand == "-" else fraction) * (end_b - start_b)
            best = _homeolog(gene_id, projected, region_b, families)
            if best is None:
                continue
            homeolog, family, offset = best
            for first, second in ((gene_id, homeolog), (homeolog, gene_id)):
                previous = pairs.get((first, second))
                if previous is None or offset < previous["offset_bp"]:
                    pairs[(first, second)] = {"block_id": block, "median_ks": ks, "family": family, "offset_bp": offset}
    base = ctx.base(source, canonical)
    ctx.count(
        source.id,
        "homeolog_pairs",
        ctx.insert(
            "homeologs",
            ({**base, "gene_id": first, "homeolog_id": second, **facts, "source_db": "LIS synteny"} for (first, second), facts in sorted(pairs.items())),
        ),
    )


def _homeolog(gene_id: str, projected: float, region: list[tuple[int, int, str]], families: dict[str, set[str]]) -> tuple[str, str, int] | None:
    own = families.get(gene_id, set())
    if not own:
        return None
    subfamilies = {family for family in own if ":" in family}
    best: tuple[int, int, str, str] | None = None
    for start, end, other in region:
        if other == gene_id:
            continue
        shared = families.get(other, set()) & own
        if not shared:
            continue
        exact = 0 if shared & subfamilies else 1
        offset = int(abs((start + end) / 2 - projected))
        candidate = (exact, offset, other, sorted(shared, key=lambda family: (":" not in family, family))[0])
        if best is None or candidate < best:
            best = candidate
    return (best[2], best[3], best[1]) if best else None


def _resource(ctx: BuildContext, source: Source, resource_id: str, kind: str, relative: str, details: dict[str, Any]) -> None:
    ctx.insert(
        "resources",
        [
            {
                **ctx.base(source, ctx.registry.canonical_assembly),
                "resource_id": resource_id,
                "kind": kind,
                "path": relative,
                "details": json.dumps(details, sort_keys=True),
                "source_db": source.name,
            }
        ],
    )


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None
