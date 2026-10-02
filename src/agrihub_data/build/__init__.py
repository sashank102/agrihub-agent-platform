"""Build a species bundle from fetched files with parser plugins.

Each registry source names a ``parser``. Plugins run in the order of
``PARSERS``, which respects the tables each one reads (``after``). The bundle
is written to ``bundle.duckdb.building`` and renamed into place only when
every plugin succeeds, so tools never see a half-built bundle.
"""

import fcntl
import hashlib
import json
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb

from agrihub_data.build.context import BuildContext, BuildError
from agrihub_data.build.expression import build_lis_expression
from agrihub_data.build.gwas import build_gwas_atlas, build_soybase_gwas
from agrihub_data.build.lis_evidence import (
    build_lis_gene_functions,
    build_lis_gwas,
    build_lis_qtl,
)
from agrihub_data.build.lis_genome import (
    build_lis_annotation,
    build_lis_markers,
    build_lis_pangenes,
)
from agrihub_data.build.ncbi_gene import build_ncbi_gene
from agrihub_data.build.networks import build_atted, build_string
from agrihub_data.build.ontology import build_ontology
from agrihub_data.build.orthology import build_ensembl_compara, build_plaza_orthology
from agrihub_data.build.pathways import build_plant_reactome, build_pmn_pathways
from agrihub_data.build.regulation import build_plantregmap, build_planttfdb
from agrihub_data.build.tair import build_tair
from agrihub_data.build.variation import build_gmhapmap, build_ld_panel, build_lis_synteny, build_vep_cache
from agrihub_data.bundle import DATA_TABLES, SCHEMA_VERSION
from agrihub_data.fetch import Manifest
from agrihub_data.paths import SpeciesPaths, species_paths
from agrihub_data.registry import (
    NON_GENOMIC_ASSEMBLY,
    Source,
    SpeciesRegistry,
    Tier,
    load_species,
)

__all__ = ["BuildError", "BuildReport", "PARSERS", "Parser", "build"]


@dataclass(frozen=True)
class Parser:
    """One plugin: the function run per source and the plugins it reads from."""

    name: str
    run: Callable[[BuildContext, Source], None]
    after: tuple[str, ...] = ()


PARSERS: tuple[Parser, ...] = (
    Parser("ontology", build_ontology),
    Parser("lis_annotation", build_lis_annotation),
    Parser("lis_pangenes", build_lis_pangenes),
    Parser("lis_markers", build_lis_markers),
    Parser("lis_qtl", build_lis_qtl, after=("lis_annotation", "lis_markers")),
    Parser("lis_gwas", build_lis_gwas, after=("lis_markers",)),
    Parser("soybase_gwas", build_soybase_gwas),
    Parser("gwas_atlas", build_gwas_atlas),
    Parser("lis_gene_functions", build_lis_gene_functions, after=("lis_annotation", "lis_pangenes")),
    Parser("ensembl_compara", build_ensembl_compara, after=("lis_annotation",)),
    Parser("plaza_orthology", build_plaza_orthology, after=("lis_annotation", "lis_pangenes")),
    Parser("tair", build_tair),
    Parser("ncbi_gene", build_ncbi_gene, after=("lis_annotation",)),
    Parser("lis_expression", build_lis_expression, after=("lis_annotation", "lis_pangenes")),
    Parser("string", build_string, after=("lis_annotation",)),
    Parser("atted", build_atted, after=("lis_annotation", "lis_pangenes")),
    Parser("planttfdb", build_planttfdb, after=("lis_annotation",)),
    Parser("plantregmap", build_plantregmap, after=("lis_annotation",)),
    Parser("pmn_pathways", build_pmn_pathways, after=("lis_annotation",)),
    Parser("plant_reactome", build_plant_reactome, after=("lis_annotation",)),
    Parser("lis_synteny", build_lis_synteny, after=("lis_annotation",)),
    Parser("vep_cache", build_vep_cache),
    Parser("ld_panel", build_ld_panel),
    Parser("gmhapmap", build_gmhapmap, after=("lis_annotation",)),
)


@dataclass
class BuildReport:
    """Row counts, parser counters and timing of one build."""

    bundle: Path
    seconds: float
    bytes: int
    tables: dict[str, int]
    stats: dict[str, dict[str, int]] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)


def build(
    species: str,
    tier: Tier = "core",
    *,
    data_dir: Path | str | None = None,
) -> BuildReport:
    """Build ``bundle.duckdb`` for a species from its fetched ``tier`` sources."""
    started = time.monotonic()
    registry = load_species(species)
    paths = species_paths(registry.species, data_dir)
    manifest = Manifest(paths.manifest, registry.species)
    sources = registry.sources_for(tier)
    names = {parser.name for parser in PARSERS}
    unknown = sorted({str(source.parser) for source in sources} - names)
    if unknown:
        raise BuildError(f"sources name unknown parsers: {', '.join(unknown)}")
    missing = [source.id for source in sources if not manifest.source_files(source.id)]
    if missing:
        raise BuildError(f"not fetched: {', '.join(missing)}; run agrihub-data fetch first")
    present = {str(source.parser) for source in sources}
    for parser in PARSERS:
        if parser.name in present:
            absent = [name for name in parser.after if name not in present]
            if absent:
                raise BuildError(f"{parser.name} needs {', '.join(absent)} in the {tier} tier")

    with _exclusive(paths.root / ".build.lock"):
        return _build_locked(registry, tier, paths, manifest, sources, started)


@contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock so two builds of one species never share staging."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BuildError(f"another build holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _build_locked(
    registry: SpeciesRegistry,
    tier: Tier,
    paths: SpeciesPaths,
    manifest: Manifest,
    sources: list[Source],
    started: float,
) -> BuildReport:
    target = paths.bundle
    building = target.with_name(target.name + ".building")
    staging = paths.root / ".build-staging"
    building.unlink(missing_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    connection = duckdb.connect(str(building))
    connection.execute("SET enable_progress_bar = false")
    context = BuildContext(
        registry=registry,
        tier=tier,
        connection=connection,
        paths=paths,
        manifest=manifest,
        staging_dir=staging,
    )
    timings: dict[str, float] = {}
    try:
        connection.execute(resources.files("agrihub_data").joinpath("schema.sql").read_text(encoding="utf-8"))
        for parser in PARSERS:
            for source in sources:
                if source.parser != parser.name:
                    continue
                step = time.monotonic()
                parser.run(context, source)
                timings[source.id] = round(time.monotonic() - step, 2)
        _write_sources(context, sources)
        tables = {
            table: int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # type: ignore[index]
            for table in DATA_TABLES
        }
        _write_info(context, tier, tables)
        connection.execute("CHECKPOINT")
    except BaseException:
        connection.close()
        building.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    connection.close()
    building.replace(target)
    return BuildReport(
        bundle=target,
        seconds=round(time.monotonic() - started, 1),
        bytes=target.stat().st_size,
        tables=tables,
        stats={name: dict(sorted(counter.items())) for name, counter in context.stats.items()},
        timings=timings,
    )


def _write_sources(ctx: BuildContext, sources: list[Source]) -> None:
    rows: list[dict[str, Any]] = []
    for source in sources:
        entries = ctx.manifest.source_files(source.id)
        rows.append(
            {
                "species": ctx.species,
                "assembly": NON_GENOMIC_ASSEMBLY,
                "source_version": ctx.source_version(source),
                "source_id": source.id,
                "name": source.name,
                "license": source.license,
                "academic_only": source.academic_only,
                "homepage": source.homepage,
                "citation": source.citation,
                "files": len(entries),
                "bytes": sum(int(entry.get("size") or 0) for entry in entries),
            }
        )
    ctx.insert("sources", rows)


def _write_info(ctx: BuildContext, tier: Tier, tables: dict[str, int]) -> None:
    checksums = sorted(
        f"{key}={entry.get('sha256')}"
        for key, entry in ctx.manifest.files.items()
        if entry.get("status") == "present"
    )
    registry_text = ctx.registry.model_dump_json()
    info = {
        "species": ctx.species,
        "tier": tier,
        "schema_version": SCHEMA_VERSION,
        "canonical_assembly": ctx.registry.canonical_assembly,
        "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "registry_sha256": hashlib.sha256(registry_text.encode()).hexdigest(),
        "inputs_sha256": hashlib.sha256("\n".join(checksums).encode()).hexdigest(),
        "table_counts": json.dumps(tables, sort_keys=True),
        "parser_stats": json.dumps(
            {name: dict(sorted(counter.items())) for name, counter in sorted(ctx.stats.items())},
            sort_keys=True,
        ),
    }
    ctx.connection.executemany(
        "INSERT INTO bundle_info VALUES (?, ?)",
        sorted(info.items()),
    )
