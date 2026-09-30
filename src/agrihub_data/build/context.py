"""State shared by the parser plugins of one bundle build."""

import fnmatch
import gzip
import io
import json
import uuid
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import duckdb

from agrihub_data.fetch import Manifest
from agrihub_data.paths import SpeciesPaths
from agrihub_data.registry import (
    Source,
    SpeciesRegistry,
    Tier,
    UnknownChromosomeError,
    normalize_chrom,
)


class BuildError(RuntimeError):
    """The inputs cannot produce a valid bundle."""


@dataclass
class BuildContext:
    """What a parser needs: registry, inputs, the bundle connection and counters."""

    registry: SpeciesRegistry
    tier: Tier
    connection: duckdb.DuckDBPyConnection
    paths: SpeciesPaths
    manifest: Manifest
    staging_dir: Path
    stats: dict[str, Counter[str]] = field(default_factory=dict)
    _column_types: dict[str, dict[str, str]] = field(default_factory=dict)
    _chrom_cache: dict[tuple[str, str], str | None] = field(default_factory=dict)

    @property
    def species(self) -> str:
        """Return the bundle species."""
        return self.registry.species

    def files(self, source: Source) -> list[Path]:
        """Return the present downloads of a source in path order."""
        root = self.paths.source_dir(source.id)
        return [root / entry["path"] for entry in self.manifest.source_files(source.id)]

    def file(self, source: Source, pattern: str) -> Path:
        """Return the single download matching a glob, or ending with a plain suffix."""
        found = self.optional_file(source, pattern)
        if found is None:
            raise BuildError(f"{source.id}: no fetched file matches {pattern}; run fetch")
        return found

    def optional_file(self, source: Source, pattern: str) -> Path | None:
        """Return the download matching ``pattern``, or ``None`` if the source has none."""
        glob = pattern if any(char in pattern for char in "*?[") else f"*{pattern}"
        matches = [path for path in self.files(source) if fnmatch.fnmatchcase(path.name, glob)]
        if len(matches) > 1:
            raise BuildError(f"{source.id}: {len(matches)} files match {pattern}")
        return matches[0] if matches else None

    def source_version(self, source: Source) -> str:
        """Return the version stamped on rows from ``source``.

        Rolling sources change in place, so their version adds the fetch date.
        """
        if not source.rolling:
            return source.version
        dates = sorted(
            str(entry.get("fetched_at", ""))[:10]
            for entry in self.manifest.source_files(source.id)
        )
        return f"{source.version} (fetched {dates[0]})" if dates else source.version

    def base(self, source: Source, assembly: str, *, species: str | None = None) -> dict[str, str]:
        """Return the provenance columns every row carries."""
        return {
            "species": species or self.species,
            "assembly": assembly,
            "source_version": self.source_version(source),
        }

    def count(self, parser: str, key: str, amount: int = 1) -> None:
        """Add to a parser counter reported after the build."""
        self.stats.setdefault(parser, Counter())[key] += amount

    def chrom(self, assembly: str, name: str) -> str | None:
        """Normalize a source chromosome name through the registry, or ``None``."""
        key = (assembly, name)
        if key not in self._chrom_cache:
            try:
                self._chrom_cache[key] = normalize_chrom(self.species, name, assembly)
            except UnknownChromosomeError:
                self._chrom_cache[key] = None
        return self._chrom_cache[key]

    def in_bounds(self, assembly: str, chrom: str, start: int, end: int) -> bool:
        """Return whether ``start..end`` lies on the registered chromosome length."""
        chromosome = self.registry.assembly(assembly).chromosome(chrom)
        if chromosome is None:
            return start >= 1
        return 1 <= start <= end <= chromosome.length

    def insert(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        """Bulk-insert dictionaries by staging them as NDJSON; return the row count."""
        types = self._types(table)
        staging = self.staging_dir / f"{table}-{uuid.uuid4().hex}.ndjson"
        written = 0
        with staging.open("w", encoding="utf-8") as handle:
            for row in rows:
                unknown = set(row) - set(types)
                if unknown:
                    raise BuildError(f"{table} has no columns {sorted(unknown)}")
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                written += 1
        try:
            if written:
                columns = ", ".join(f'"{name}"' for name in types)
                spec = ", ".join(f"'{name}': '{kind}'" for name, kind in types.items())
                self.connection.execute(
                    f"INSERT INTO {table} ({columns}) SELECT {columns} FROM read_json(?, "
                    f"format = 'newline_delimited', columns = {{{spec}}})",
                    [str(staging)],
                )
        finally:
            staging.unlink(missing_ok=True)
        return written

    def _types(self, table: str) -> dict[str, str]:
        if table not in self._column_types:
            rows = self.connection.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()
            if not rows:
                raise BuildError(f"unknown table {table}")
            self._column_types[table] = {str(name): str(kind) for name, kind in rows}
        return self._column_types[table]


def open_text(path: Path) -> IO[str]:
    """Open a plain, gzip or single-member zip text file, sniffing the magic bytes.

    Some upstream files are zip archives named ``.gz``, so the suffix is not trusted.
    """
    with path.open("rb") as handle:
        magic = handle.read(4)
    if magic[:2] == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    if magic == b"PK\x03\x04":
        archive = zipfile.ZipFile(path)
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) != 1:
            raise BuildError(f"{path.name}: expected one file in the zip archive, found {len(members)}")
        return io.TextIOWrapper(archive.open(members[0]), encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def tsv_rows(path: Path, *, skip_comments: bool = True) -> Iterator[list[str]]:
    """Yield tab-separated fields, skipping blank and ``#`` lines."""
    with open_text(path) as handle:
        for line in handle:
            line = line.rstrip("\n\r")
            if not line.strip() or (skip_comments and line.startswith("#")):
                continue
            yield line.split("\t")


def split_list(value: str | None, separators: str = ",") -> list[str]:
    """Split a delimited cell into trimmed non-empty items."""
    if not value:
        return []
    items = [value]
    for separator in separators:
        items = [part for item in items for part in item.split(separator)]
    return [item.strip() for item in items if item.strip() and item.strip() not in {"NULL", "-", "NA"}]
