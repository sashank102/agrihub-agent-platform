"""Positioned GWAS catalogs: SoyBase GWAS locations and GWAS Atlas."""

import re
from typing import Any

from agrihub_data.build.chain import ChainLiftover
from agrihub_data.build.context import BuildContext, BuildError, tsv_rows
from agrihub_data.build.ontology import load_obo
from agrihub_data.build.resolve import GeneResolver
from agrihub_data.registry import Source, UnknownAssemblyError

_SOYBASE_NAME = re.compile(r"^(?P<study>.*\S)\s*-\s*(?P<marker>[^-\s]+)$")
_STUDY_NUMBER = re.compile(r"\s+\d+$")


def build_soybase_gwas(ctx: BuildContext, source: Source) -> None:
    """Load SoyBase GWAS SNP locations; rows keep the assembly SoyBase reports."""
    path = ctx.file(source, "soybase_gwas_locations.tsv")
    rows: dict[str, dict[str, Any]] = {}
    for fields in tsv_rows(path):
        if len(fields) < 4 or not fields[0].strip():
            continue
        name, chrom_name, position, reported = (field.strip() for field in fields[:4])
        assembly = _assembly(ctx, reported)
        if assembly is None:
            ctx.count(source.id, f"skipped_assembly:{reported or 'blank'}")
            continue
        chrom = ctx.chrom(assembly, chrom_name)
        if chrom is None or not position.isdigit():
            ctx.count(source.id, "skipped_position")
            continue
        if not ctx.in_bounds(assembly, chrom, int(position), int(position)):
            ctx.count(source.id, f"out_of_bounds:{assembly}")
            continue
        match = _SOYBASE_NAME.match(name)
        study = match.group("study") if match else name
        marker = match.group("marker") if match else name
        trait = _STUDY_NUMBER.sub("", study)
        hit_id = f"SoyBase|{name}"
        if hit_id in rows:
            ctx.count(source.id, "duplicates")
            continue
        ctx.count(source.id, f"rows:{assembly}")
        rows[hit_id] = {
            **ctx.base(source, assembly),
            "hit_id": hit_id,
            "source_db": "SoyBase GWAS",
            "study_id": study,
            "trait_name": trait,
            "trait_terms": [],
            "marker": marker,
            "chrom": chrom,
            "pos": int(position),
            "p_value": None,
            "pmid": None,
            "doi": None,
            "reported_genes": [],
            "placement": "reported",
        }
    ctx.count(source.id, "gwas_hits", ctx.insert("gwas_hits", rows.values()))


def build_gwas_atlas(ctx: BuildContext, source: Source) -> None:
    """Load GWAS Atlas associations on registered assemblies and the PPTO ontology.

    ``source.assembly_aliases`` maps the file's ``Ref ver`` spellings to
    registered assemblies; rows on other assemblies are counted and dropped.
    ``params.lift`` maps an assembly to ``{to, source, file}``: rows on it are
    lifted through that source's chain file and keep ``placement =
    lifted:<from assembly>:<chrom>:<pos>``. ``GaP_id`` is unique only within a
    study and blank in some, so hits are keyed by study and id, or by study,
    position and trait when the id is blank; a hit reported under several
    models or environments keeps its smallest p-value. Reported genes are found with the
    registry's gene id patterns and resolved to canonical genes.
    """
    path = ctx.file(source, ".txt.gz")
    rows_iter = tsv_rows(path, skip_comments=False)
    header = [name.lstrip("#").strip() for name in next(rows_iter)]
    index = {name: position for position, name in enumerate(header)}
    lifts = _chain_lifts(ctx, source)
    gene_pattern = ctx.registry.gene_id_pattern()
    resolver = GeneResolver.load(ctx)
    locations = {
        str(gene): (str(chrom), int(start), int(end))
        for gene, chrom, start, end in ctx.connection.execute(
            'SELECT gene_id, chrom, start, "end" FROM genes WHERE assembly = ?', [ctx.registry.canonical_assembly]
        ).fetchall()
    }

    def cell(fields: list[str], name: str) -> str:
        position = index.get(name)
        if position is None or position >= len(fields):
            return ""
        value = fields[position].strip()
        return "" if value in {"-", "NA", "NULL"} else value

    rows: dict[str, dict[str, Any]] = {}
    trait_terms: set[tuple[str, str]] = set()
    for fields in rows_iter:
        reported = cell(fields, "Ref ver")
        assembly = ctx.registry.source_assembly(source, reported)
        if assembly is None:
            ctx.count(source.id, f"skipped_assembly:{reported or 'blank'}")
            continue
        chrom = ctx.chrom(assembly, cell(fields, "Chr"))
        position = cell(fields, "Pos")
        if chrom is None or not position.isdigit():
            ctx.count(source.id, "skipped_position")
            continue
        if not ctx.in_bounds(assembly, chrom, int(position), int(position)):
            ctx.count(source.id, f"out_of_bounds:{assembly}")
            continue
        pos = int(position)
        placement = "reported"
        if assembly in lifts:
            target, liftover = lifts[assembly]
            lifted = liftover.lift(chrom, pos)
            if lifted is None or not ctx.in_bounds(target, lifted[0], lifted[1], lifted[1]):
                ctx.count(source.id, f"lift_failed:{assembly}")
                continue
            placement = f"lifted:{assembly}:{chrom}:{pos}"
            ctx.count(source.id, f"lifted:{assembly}->{target}")
            assembly, (chrom, pos) = target, lifted
        trait = cell(fields, "Trait")
        gap_id = cell(fields, "GaP_id") or f"{chrom}:{pos}:{trait}"
        hit_id = f"GWASAtlas|{cell(fields, 'StudyId')}|{gap_id}"
        if hit_id in rows:
            ctx.count(source.id, "duplicate_ids")
            p_value = _float(cell(fields, "P-value"))
            kept = rows[hit_id]["p_value"]
            if p_value is not None and (kept is None or p_value < kept):
                rows[hit_id]["p_value"] = p_value
            continue
        term = cell(fields, "Trait accession")
        if term:
            trait_terms.add((trait, term))
        genes = set()
        for found in (match.group(0) for match in gene_pattern.finditer(cell(fields, "Reported gene(S)"))):
            gene, via = resolver.resolve(found)
            ctx.count(source.id, f"reported_genes:{via}")
            genes.add(gene or found)
            if gene in locations and assembly == ctx.registry.canonical_assembly:
                ctx.count(source.id, f"reported_gene_distance:{_distance_band(locations[gene], chrom, pos)}")
        rows[hit_id] = {
            **ctx.base(source, assembly),
            "hit_id": hit_id,
            "source_db": "GWAS Atlas",
            "study_id": cell(fields, "StudyId"),
            "trait_name": trait,
            "trait_terms": [term] if term else [],
            "marker": cell(fields, "Reported location") or None,
            "chrom": chrom,
            "pos": pos,
            "p_value": _float(cell(fields, "P-value")),
            "pmid": cell(fields, "PMID") or None,
            "doi": None,
            "reported_genes": sorted(genes),
            "placement": placement,
        }
        ctx.count(source.id, f"rows:{assembly}")
    ctx.count(source.id, "gwas_hits", ctx.insert("gwas_hits", rows.values()))
    ctx.count(
        source.id,
        "trait_map",
        ctx.insert(
            "trait_map",
            (
                {
                    **ctx.base(source, "none"),
                    "trait_name": trait,
                    "term_id": term,
                    "study_id": None,
                    "source_db": "GWAS Atlas",
                }
                for trait, term in sorted(trait_terms)
            ),
        ),
    )
    ontology = ctx.optional_file(source, "PPTO.obo.gz")
    if ontology is not None:
        load_obo(ctx, source, ontology, prefixes={"PPTO"})


def _distance_band(location: tuple[str, int, int], chrom: str, pos: int) -> str:
    """Bucket the distance from a hit to a gene it reports (checks id mappings)."""
    gene_chrom, start, end = location
    if gene_chrom != chrom:
        return "other_chromosome"
    distance = 0 if start <= pos <= end else min(abs(pos - start), abs(pos - end))
    for limit, label in ((0, "inside"), (10_000, "<=10kb"), (100_000, "<=100kb"), (1_000_000, "<=1Mb")):
        if distance <= limit:
            return label
    return ">1Mb"


def _chain_lifts(ctx: BuildContext, source: Source) -> dict[str, tuple[str, ChainLiftover]]:
    """Return ``from assembly -> (target assembly, liftover)`` for ``params.lift``."""
    lifts: dict[str, tuple[str, ChainLiftover]] = {}
    for from_name, spec in (source.params.get("lift") or {}).items():
        from_assembly = ctx.registry.assembly(str(from_name)).id
        target = ctx.registry.assembly(str(spec["to"])).id
        chain_source = ctx.registry.source(str(spec["source"]))
        path = ctx.file(chain_source, str(spec["file"]))
        liftover = ChainLiftover.read(
            path,
            lambda name, assembly=from_assembly: ctx.chrom(assembly, name),
            lambda name, assembly=target: ctx.chrom(assembly, name),
        )
        if not liftover.size:
            raise BuildError(f"{source.id}: {path.name} has no chains between registered chromosomes")
        ctx.count(source.id, f"chain_blocks:{from_assembly}", liftover.size)
        lifts[from_assembly] = (target, liftover)
    return lifts


def _assembly(ctx: BuildContext, reported: str) -> str | None:
    if not reported:
        return None
    try:
        return ctx.registry.assembly(reported).id
    except UnknownAssemblyError:
        return None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None
