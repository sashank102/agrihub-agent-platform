"""Checks that a built bundle is complete and never mixes assemblies."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from agrihub_data.bundle import (
    DATA_TABLES,
    EXTRA_ASSEMBLY_COLUMNS,
    NON_GENOMIC_TABLES,
    POSITIONAL_TABLES,
    SCHEMA_VERSION,
    tables_for,
)
from agrihub_data.fetch import Manifest, sha256_file
from agrihub_data.paths import species_paths
from agrihub_data.registry import (
    NON_GENOMIC_ASSEMBLY,
    TIERS,
    SpeciesRegistry,
    load_species,
)


@dataclass
class VerifyReport:
    """Problems found in a bundle and the row count of every table."""

    bundle: Path
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether no problem was found."""
        return not self.problems


def verify(
    species: str,
    *,
    data_dir: Path | str | None = None,
    checksums: bool = True,
) -> VerifyReport:
    """Verify a species bundle.

    Checks that the bundle has the current schema version, every data table
    of its tier and below is non-empty, every row carries species,
    source_version and a registered assembly (``none`` only in non-genomic
    tables), positional rows use canonical chromosome names within the
    registered length, heavy resources are unpacked where the bundle says,
    and fetched files still match their manifest sha256.
    """
    registry = load_species(species)
    paths = species_paths(registry.species, data_dir)
    report = VerifyReport(bundle=paths.bundle)
    if not paths.bundle.exists():
        report.problems.append(f"no bundle at {paths.bundle}")
        return report
    registered = registry.registered_assemblies()
    connection = duckdb.connect(str(paths.bundle), read_only=True)
    try:
        info = dict(connection.execute("SELECT key, value FROM bundle_info").fetchall())
        if info.get("species") != registry.species:
            report.problems.append(f"bundle_info species is {info.get('species')!r}")
        if info.get("schema_version") != SCHEMA_VERSION:
            report.problems.append(f"schema_version is {info.get('schema_version')!r}, expected {SCHEMA_VERSION}; rebuild the bundle")
            return report
        tier = info.get("tier") if info.get("tier") in TIERS else "core"
        required = set(tables_for(tier))  # type: ignore[arg-type]
        for table in (*DATA_TABLES, "sources"):
            count = int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # type: ignore[index]
            report.counts[table] = count
            if count == 0 and (table == "sources" or table in required):
                report.problems.append(f"{table} is empty")
            _check_provenance(connection, table, registered, report)
        for table, (start, end) in POSITIONAL_TABLES.items():
            _check_positions(connection, registry, table, start, end, report)
        _check_positions(
            connection,
            registry,
            "lift_anchors",
            "from_start",
            "from_end",
            report,
            assembly_column="from_assembly",
            chrom_column="from_chrom",
        )
        for resource_id, relative in connection.execute("SELECT resource_id, path FROM resources").fetchall():
            if not (paths.root / str(relative)).exists():
                report.problems.append(f"resource {resource_id} is missing at {relative}; rebuild the heavy tier")
    finally:
        connection.close()
    if checksums:
        _check_checksums(Manifest(paths.manifest, registry.species), paths.raw, report)
    return report


def _check_provenance(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    registered: set[str],
    report: VerifyReport,
) -> None:
    blank = connection.execute(
        f"SELECT count(*) FROM {table} WHERE coalesce(species, '') = '' OR coalesce(source_version, '') = ''"
    ).fetchone()
    if blank and blank[0]:
        report.problems.append(f"{table}: {blank[0]} rows lack species or source_version")
    columns = ("assembly", *EXTRA_ASSEMBLY_COLUMNS.get(table, ()))
    for column in columns:
        values = {
            str(row[0]): int(row[1])
            for row in connection.execute(f"SELECT {column}, count(*) FROM {table} GROUP BY 1").fetchall()
        }
        if table in NON_GENOMIC_TABLES:
            allowed = {NON_GENOMIC_ASSEMBLY}
        elif column == "assembly":
            allowed = registered
        else:
            allowed = registered | {NON_GENOMIC_ASSEMBLY}
        for value, count in sorted(values.items()):
            if value not in allowed:
                report.problems.append(f"{table}.{column}: {count} rows on unregistered assembly {value!r}")


def _check_positions(
    connection: duckdb.DuckDBPyConnection,
    registry: SpeciesRegistry,
    table: str,
    start: str,
    end: str,
    report: VerifyReport,
    *,
    assembly_column: str = "assembly",
    chrom_column: str = "chrom",
) -> None:
    rows = connection.execute(
        f'SELECT {assembly_column}, {chrom_column}, min("{start}"), max("{end}"), count(*) FROM {table} '
        f"WHERE {chrom_column} IS NOT NULL GROUP BY 1, 2"
    ).fetchall()
    for assembly_id, chrom, lowest, highest, count in rows:
        if assembly_id not in {assembly.id for assembly in registry.assemblies}:
            continue
        assembly = registry.assembly(assembly_id)
        chromosome = assembly.chromosome(chrom)
        if chromosome is None:
            pattern = assembly.scaffold_pattern
            if pattern and re.fullmatch(pattern, str(chrom)):
                continue
            report.problems.append(f"{table}: {count} rows on non-canonical chromosome {chrom!r} ({assembly_id})")
            continue
        if lowest is not None and int(lowest) < 1:
            report.problems.append(f"{table}: {chrom} ({assembly_id}) has positions below 1")
        if highest is not None and int(highest) > chromosome.length:
            report.problems.append(
                f"{table}: {chrom} ({assembly_id}) reaches {highest} beyond its length {chromosome.length}"
            )


def _check_checksums(manifest: Manifest, raw: Path, report: VerifyReport) -> None:
    for key, entry in sorted(manifest.files.items()):
        if entry.get("status") != "present":
            continue
        path = raw / key
        if not path.exists():
            report.problems.append(f"fetched file missing: {key}")
        elif sha256_file(path) != entry.get("sha256"):
            report.problems.append(f"sha256 changed since fetch: {key}")
