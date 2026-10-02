"""LIS expression atlases: sample groups and per-gene means on the canonical assembly.

The source version names the dataset and its assembly
(``Wm82.gnm4.ann1.expr.Wm82.Sreedasyam_Plott_2023``). Replicates of one
``replicate_group`` are averaged into one sample; sample sheets without a
tissue column (Libault 2010) get their tissue from the sample name. Gene ids
on another assembly are mapped through :func:`canonical_map`; unmapped,
ambiguous and colliding source genes are counted in the build stats.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from agrihub_data.build.context import BuildContext, BuildError, tsv_rows
from agrihub_data.build.idmap import canonical_map
from agrihub_data.build.lis_genome import lis_assembly
from agrihub_data.registry import Source

UNIT = "TPM"
_DATASET = re.compile(r"^Wm82\.gnm(\d+)\.ann\d+\.expr\.[^.]+\.(.+)$")
_LIS_GENE_PREFIX = r"^[a-z]+\.Wm82\.gnm\d+\.ann\d+\."
TISSUE_ALIASES = {"nodules": "nodule", "leaves": "leaf", "green_pods": "pod", "apical_meristem": "shoot_apical_meristem"}
NAMED_TISSUES = (
    ("apical_meristem", "shoot_apical_meristem"),
    ("green_pod", "pod"),
    ("root_tip", "root_tip"),
    ("leaves", "leaf"),
    ("leaf", "leaf"),
    ("flower", "flower"),
    ("nodule", "nodule"),
    ("_rh", "root_hair"),
    ("root", "root"),
    ("seed", "seed"),
    ("stem", "stem"),
)
"""Name fragments that give a tissue when the sample sheet has none; checked in order."""


@dataclass(frozen=True)
class SampleGroup:
    """One averaged sample of a dataset."""

    sample: str
    tissue: str
    stage: str | None
    description: str | None


def dataset_of(source: Source) -> tuple[str, str]:
    """Return ``(LIS assembly number, dataset name)`` from a LIS expression source version."""
    match = _DATASET.match(source.version)
    if match is None:
        raise BuildError(f"{source.id}: version {source.version!r} is not a LIS expression dataset id")
    return match.group(1), match.group(2)


def build_lis_expression(ctx: BuildContext, source: Source) -> None:
    """Load one dataset's sample groups and mean expression per canonical gene and sample."""
    number, dataset = dataset_of(source)
    source_assembly = lis_assembly(ctx, number)
    canonical = ctx.registry.canonical_assembly
    groups = sample_groups(ctx.file(source, ".samples.tsv.gz"))
    members: dict[str, list[str]] = {}
    for identifier, group in groups.items():
        members.setdefault(group.sample, []).append(identifier)
    first = {group.sample: group for group in groups.values()}
    ctx.count(
        source.id,
        "samples",
        ctx.insert(
            "samples",
            (
                {
                    **ctx.base(source, canonical),
                    "dataset": dataset,
                    "sample": sample,
                    "tissue": first[sample].tissue,
                    "stage": first[sample].stage,
                    "condition": None,
                    "description": first[sample].description,
                    "n_replicates": len(identifiers),
                    "source_db": "LIS",
                }
                for sample, identifiers in sorted(members.items())
            ),
        ),
    )
    mapping = canonical_map(ctx, source_assembly)
    chosen: dict[str, str] = {}
    for source_gene, gene_id in sorted(mapping.genes.items(), key=lambda pair: (mapping.via[pair[0]] != "ancestor", pair[0])):
        if gene_id in chosen.values():
            ctx.count(source.id, "colliding_source_genes")
            continue
        chosen[source_gene] = gene_id
    connection = ctx.connection
    ctx.temp_table(
        "expr_map",
        {"source_gene": "VARCHAR", "gene_id": "VARCHAR"},
        ({"source_gene": source_gene, "gene_id": gene_id} for source_gene, gene_id in sorted(chosen.items())),
    )
    ctx.temp_table(
        "expr_samples",
        {"identifier": "VARCHAR", "sample": "VARCHAR", "tissue": "VARCHAR", "stage": "VARCHAR"},
        (
            {"identifier": identifier, "sample": group.sample, "tissue": group.tissue, "stage": group.stage}
            for identifier, group in sorted(groups.items())
        ),
    )
    values = ctx.file(source, ".values.tsv.gz")
    connection.execute(
        "CREATE TEMP TABLE expr_raw AS SELECT * FROM read_csv(?, delim = '\t', header = true, all_varchar = true, quote = '')",
        [str(values)],
    )
    columns = [str(row[0]) for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'expr_raw' ORDER BY ordinal_position").fetchall()]
    gene_column = columns[0]
    missing = sorted(set(columns[1:]) - set(groups))
    if missing:
        ctx.count(source.id, "value_columns_without_sample", len(missing))
    unmapped = connection.execute(
        f"SELECT count(*) FROM expr_raw WHERE regexp_replace(\"{gene_column}\", ?, '') NOT IN (SELECT source_gene FROM expr_map)",
        [_LIS_GENE_PREFIX],
    ).fetchone()
    ctx.count(source.id, "unmapped_source_genes", int(unmapped[0]) if unmapped else 0)
    ctx.count(source.id, "ambiguous_source_genes", len(mapping.ambiguous))
    inserted = connection.execute(
        f"""
        INSERT INTO expression (species, assembly, source_version, gene_id, source_gene_id, source_assembly,
                                dataset, sample, tissue, stage, value, unit, source_db)
        WITH long AS (
            UNPIVOT expr_raw ON COLUMNS(* EXCLUDE ("{gene_column}")) INTO NAME identifier VALUE raw_value
        )
        SELECT ?, ?, ?, m.gene_id, m.source_gene, ?, ?, s.sample, s.tissue, s.stage,
               round(avg(TRY_CAST(l.raw_value AS DOUBLE)), 4), ?, 'LIS'
        FROM long AS l
        JOIN expr_samples AS s ON s.identifier = l.identifier
        JOIN expr_map AS m ON m.source_gene = regexp_replace(l."{gene_column}", ?, '')
        WHERE TRY_CAST(l.raw_value AS DOUBLE) IS NOT NULL
        GROUP BY m.gene_id, m.source_gene, s.sample, s.tissue, s.stage
        """,
        [ctx.species, canonical, ctx.source_version(source), source_assembly, dataset, UNIT, _LIS_GENE_PREFIX],
    ).fetchone()
    ctx.count(source.id, "expression_rows", int(inserted[0]) if inserted else 0)
    for table in ("expr_raw", "expr_samples", "expr_map"):
        connection.execute(f"DROP TABLE {table}")


def sample_groups(path: Path) -> dict[str, SampleGroup]:
    """Return ``identifier -> sample group`` from a LIS ``samples.tsv`` sheet."""
    rows = tsv_rows(path, skip_comments=False)
    header = [name.lstrip("#").strip() for name in next(rows)]
    index = {name: position for position, name in enumerate(header)}

    def cell(fields: list[str], *names: str) -> str:
        for name in names:
            position = index.get(name)
            if position is not None and position < len(fields) and fields[position].strip():
                return fields[position].strip()
        return ""

    groups: dict[str, SampleGroup] = {}
    for fields in rows:
        identifier = cell(fields, "identifier")
        if not identifier:
            continue
        name = cell(fields, "name") or identifier
        group = cell(fields, "replicate_group") or name
        tissue = cell(fields, "tissue")
        stage = cell(fields, "developmental_stage", "development_stage")
        if "." in group and not stage:
            stage = group.split(".", 1)[1]
        groups[identifier] = SampleGroup(
            sample=group,
            tissue=normalize_tissue(tissue) if tissue else tissue_from_name(name),
            stage=stage or None,
            description=cell(fields, "description") or None,
        )
    return groups


def normalize_tissue(tissue: str) -> str:
    """Return a lower-case, underscore tissue label with plural and synonym forms folded."""
    label = re.sub(r"[^a-z0-9]+", "_", tissue.strip().casefold()).strip("_")
    return TISSUE_ALIASES.get(label, label)


def tissue_from_name(name: str) -> str:
    """Return the tissue a sample name implies, or ``unspecified``."""
    folded = name.casefold()
    for fragment, tissue in NAMED_TISSUES:
        if fragment in folded:
            return tissue
    return "unspecified"
