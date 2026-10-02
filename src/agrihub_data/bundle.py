"""Read-only access to a built species bundle.

A bundle is opened once per process with ``read_only=True``. DuckDB
connections must not be shared between threads, so every thread that queries
gets its own cursor (``connection.cursor()``), created under a lock. Query
functions are synchronous; async callers run them with ``asyncio.to_thread``.
"""

import threading
from pathlib import Path
from typing import Any

import duckdb

from agrihub_data.paths import species_paths

SCHEMA_VERSION = "2"
DATA_TABLES: tuple[str, ...] = (
    "genes",
    "id_map",
    "annotation",
    "go_annot",
    "orthologs",
    "qtl",
    "gwas_hits",
    "known_genes",
    "markers",
    "ontology_terms",
    "trait_map",
    "phenotypes",
    "gene_publications",
    "ncbi_genes",
    "ncbi_gene_pubmed",
    "ncbi_gene_go",
)
NON_GENOMIC_TABLES = frozenset({"ontology_terms", "trait_map", "sources"})
"""Tables whose rows must carry ``assembly = 'none'``."""
EXTRA_ASSEMBLY_COLUMNS: dict[str, tuple[str, ...]] = {
    "id_map": ("to_assembly",),
    "orthologs": ("target_assembly",),
    "known_genes": ("source_assembly",),
}
POSITIONAL_TABLES: dict[str, tuple[str, str]] = {
    "genes": ("start", "end"),
    "markers": ("start", "end"),
    "qtl": ("start", "end"),
    "gwas_hits": ("pos", "pos"),
}


class BundleMissingError(FileNotFoundError):
    """The species has no built bundle."""


class Bundle:
    """A read-only DuckDB bundle with one cursor per querying thread."""

    def __init__(self, path: Path, species: str) -> None:
        """Open ``path`` read-only."""
        if not path.exists():
            raise BundleMissingError(
                f"no {species} bundle at {path}; run "
                f"`agrihub-data fetch --species {species} --tier core` and `build`"
            )
        self.path = path
        self.species = species
        self._connection = duckdb.connect(str(path), read_only=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._cursors: list[duckdb.DuckDBPyConnection] = []
        self.mtime = path.stat().st_mtime_ns
        self.info = {str(key): str(value) for key, value in self.rows_raw("SELECT key, value FROM bundle_info")}
        self._versions = {
            str(key): str(value) for key, value in self.rows_raw("SELECT source_id, source_version FROM sources")
        }

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """Return this thread's cursor, creating it on first use."""
        cursor = getattr(self._local, "cursor", None)
        if cursor is None:
            with self._lock:
                cursor = self._connection.cursor()
                self._cursors.append(cursor)
            self._local.cursor = cursor
        return cursor

    def rows_raw(self, sql: str, parameters: list[Any] | tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Run a query on this thread's cursor and return tuples."""
        return self.cursor().execute(sql, list(parameters)).fetchall()

    def rows(self, sql: str, parameters: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a query on this thread's cursor and return dictionaries."""
        cursor = self.cursor().execute(sql, list(parameters))
        names = [column[0] for column in cursor.description or []]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def source_versions(self) -> dict[str, str]:
        """Return ``source_id -> source_version`` for every source in the bundle."""
        return dict(self._versions)

    def close(self) -> None:
        """Close every cursor and the connection."""
        with self._lock:
            for cursor in self._cursors:
                cursor.close()
            self._cursors.clear()
            self._connection.close()


_open: dict[Path, Bundle] = {}
_open_lock = threading.Lock()


def open_bundle(species: str, data_dir: Path | str | None = None) -> Bundle:
    """Return the process-wide bundle for a species, reopening it after a rebuild."""
    path = species_paths(species, data_dir).bundle.resolve()
    with _open_lock:
        bundle = _open.get(path)
        if bundle is not None and path.exists() and bundle.mtime != path.stat().st_mtime_ns:
            bundle.close()
            bundle = None
        if bundle is None:
            bundle = Bundle(path, species)
            _open[path] = bundle
        return bundle


def close_bundles() -> None:
    """Close every open bundle (tests and rebuilds)."""
    with _open_lock:
        for bundle in _open.values():
            bundle.close()
        _open.clear()
