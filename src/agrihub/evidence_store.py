"""Run-scoped evidence ledger backed by one DuckDB file per run.

The file lives at ``$AGRIHUB_RUN_DIR/<run_id>/evidence.duckdb``; see
``agent_platform.core.settings.DataPaths`` for the development default.
Each run numbers its evidence ``E1, E2, ...`` in insertion order; tools accept
either the alias or the ``evidence_id``. Findings are numbered ``F1, F2, ...``
and may only cite stored evidence. Full tool results are kept as outputs
``O1, O2, ...`` so a wrapper can return a truncated view plus an ``output_ref``.

Evidence id granularity
-----------------------
``evidence_id`` is ``sha1(source_db|source_record|gene_id|subtype)[:12]``.
Two items that agree on those four fields are the same fact and collapse to
one row; ``value``, ``quote`` and ``retrieved_at`` are not hashed, so a later
harvest of the same fact returns the stored copy. Every producer must make
``source_record`` and ``subtype`` specific enough that distinct facts never
share all four:

- ``source_record`` names the upstream record plus whatever query context
  makes the fact true: ``qtl_id`` for a QTL, ``study|marker|trait`` for a
  GWAS hit, ``assembly:chrom:start-end`` for window membership, the target
  gene for an ortholog call.
- ``subtype`` names the kind of fact about that record: the QTL overlap type
  (``qtl:contains``, ``qtl:partial``), the annotation term (``go:GO:0003700``),
  the ortholog target species.
- Locus-level facts use the locus id (``L1``) as ``gene_id``.
"""

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from agrihub import configuration
from agrihub.state import EvidenceItem, Finding

DATABASE_NAME = "evidence.duckdb"
SNAPSHOT_NAME = "evidence_snapshot.json"
SNAPSHOT_SCHEMA = "agrihub.evidence-snapshot/v1"
_EVIDENCE_COLUMNS = (
    "ordinal",
    "evidence_id",
    "alias",
    "gene_id",
    "category",
    "subtype",
    "source_db",
    "source_record",
    "item",
)
_EVIDENCE_COLUMN_TYPES = (
    "{"
    + ", ".join(
        f"{column}: '{'INTEGER' if column == 'ordinal' else 'VARCHAR'}'"
        for column in _EVIDENCE_COLUMNS
    )
    + "}"
)
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ALIAS = re.compile(r"^E[1-9][0-9]*$")
# Above this many ids, binding one parameter per id costs more than staging
# the ids as NDJSON and semi-joining them against the primary key.
_INLINE_LOOKUP_LIMIT = 64

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS evidence (
        ordinal INTEGER NOT NULL,
        evidence_id VARCHAR PRIMARY KEY,
        alias VARCHAR NOT NULL UNIQUE,
        gene_id VARCHAR NOT NULL,
        category VARCHAR NOT NULL,
        subtype VARCHAR NOT NULL,
        source_db VARCHAR NOT NULL,
        source_record VARCHAR NOT NULL,
        item VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
        ordinal INTEGER NOT NULL,
        finding_id VARCHAR PRIMARY KEY,
        agent_id VARCHAR NOT NULL,
        target VARCHAR NOT NULL,
        stance VARCHAR NOT NULL,
        finding VARCHAR NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outputs (
        ordinal INTEGER NOT NULL,
        output_ref VARCHAR PRIMARY KEY,
        tool VARCHAR NOT NULL,
        payload VARCHAR NOT NULL
    )
    """,
)

_open_stores: dict[Path, "EvidenceStore"] = {}
_open_lock = threading.Lock()


class UnknownEvidenceError(ValueError):
    """A finding cited evidence ids that this run never stored."""

    def __init__(self, unknown: list[str]) -> None:
        """Keep the rejected ids and a message an agent can act on."""
        self.unknown = list(unknown)
        super().__init__(
            "Unknown evidence ids: "
            + ", ".join(self.unknown)
            + ". Cite only evidence_ids or E<n> aliases returned by evidence tools."
        )


def run_root() -> Path:
    """Return the directory that holds one subdirectory per run."""
    return configuration.run_root()


def evidence_id_for(item: EvidenceItem) -> str:
    """Return the content-derived id that deduplicates repeated facts."""
    key = "|".join((item.source_db, item.source_record, item.gene_id, item.subtype))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


class EvidenceStore:
    """Evidence and findings for one run.

    Methods are synchronous and serialized with a lock because one DuckDB
    connection must not be used from two threads at once. Async callers run
    them with ``asyncio.to_thread`` so the event loop keeps serving streams.
    """

    def __init__(self, run_id: str, *, root: Path | None = None) -> None:
        """Open or create the run's DuckDB file."""
        if not _SAFE_RUN_ID.match(run_id):
            raise ValueError("run_id must be a plain identifier")
        self.run_id = run_id
        self.directory = (root or run_root()) / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / DATABASE_NAME
        self._lock = threading.Lock()
        self._connection: duckdb.DuckDBPyConnection | None = duckdb.connect(
            str(self.path)
        )
        for statement in _SCHEMA:
            self._connection.execute(statement)
        self._evidence_ordinal = self._max_ordinal(self._connection, "evidence")

    @classmethod
    def for_run(cls, run_id: str, *, root: Path | None = None) -> "EvidenceStore":
        """Return the process-wide open store for a run, opening it once."""
        path = ((root or run_root()) / run_id / DATABASE_NAME).resolve()
        with _open_lock:
            store = _open_stores.get(path)
            if store is None or store._connection is None:
                store = cls(run_id, root=root)
                _open_stores[path] = store
            return store

    def close(self) -> None:
        """Close the connection and forget the cached handle."""
        with _open_lock:
            for path, store in list(_open_stores.items()):
                if store is self:
                    _open_stores.pop(path, None)
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def put_items(self, items: Iterable[EvidenceItem]) -> list[EvidenceItem]:
        """Store new evidence and return every item with its id and alias.

        Items whose ``evidence_id`` already exists are not inserted again; the
        stored copy is returned in their place. Existing ids are found with a
        primary-key lookup for small batches and a semi-join against the
        staged batch for large ones, never by reading every stored id.
        """
        batch = list(items)
        ids = [evidence_id_for(item) for item in batch]
        with self._lock:
            connection = self._require_connection()
            ordinal = self._evidence_ordinal
            stored: dict[str, EvidenceItem] = {
                str(existing.evidence_id): existing
                for existing in self._existing(connection, list(dict.fromkeys(ids)))
            }
            rows: list[dict[str, Any]] = []
            for item, evidence_id in zip(batch, ids, strict=True):
                if evidence_id in stored:
                    continue
                ordinal += 1
                saved = item.model_copy(
                    update={"evidence_id": evidence_id, "alias": f"E{ordinal}"}
                )
                stored[evidence_id] = saved
                rows.append(
                    {
                        "ordinal": ordinal,
                        "evidence_id": evidence_id,
                        "alias": saved.alias,
                        "gene_id": saved.gene_id,
                        "category": saved.category,
                        "subtype": saved.subtype,
                        "source_db": saved.source_db,
                        "source_record": saved.source_record,
                        "item": saved.model_dump_json(),
                    }
                )
            if rows:
                self._bulk_insert(connection, rows)
                self._evidence_ordinal = ordinal
            return [stored[evidence_id] for evidence_id in ids]

    def get(self, ids: Iterable[str]) -> list[EvidenceItem]:
        """Return stored items for ids or aliases in request order, skipping unknowns."""
        wanted = [str(identifier) for identifier in ids]
        found = self._lookup(wanted)
        return [found[key] for key in wanted if key in found]

    def resolve(self, ids: Iterable[str]) -> tuple[list[str], list[str]]:
        """Map ids or aliases to canonical evidence ids and list the unknown ones."""
        wanted = list(dict.fromkeys(str(identifier) for identifier in ids))
        found = self._lookup(wanted)
        known = list(
            dict.fromkeys(str(found[key].evidence_id) for key in wanted if key in found)
        )
        unknown = [key for key in wanted if key not in found]
        return known, unknown

    def query(
        self,
        gene_ids: Iterable[str] | None = None,
        categories: Iterable[str] | None = None,
        *,
        limit: int | None = None,
    ) -> list[EvidenceItem]:
        """Return evidence filtered by gene and category in insertion order."""
        clauses: list[str] = []
        parameters: list[Any] = []
        for column, values in (("gene_id", gene_ids), ("category", categories)):
            if values is None:
                continue
            selected = list(values)
            if not selected:
                return []
            clauses.append(f"{column} IN ({', '.join('?' for _ in selected)})")
            parameters.extend(selected)
        statement = "SELECT item FROM evidence"
        if clauses:
            statement += " WHERE " + " AND ".join(clauses)
        statement += " ORDER BY ordinal"
        if limit is not None:
            statement += " LIMIT ?"
            parameters.append(int(limit))
        with self._lock:
            rows = self._require_connection().execute(statement, parameters).fetchall()
        return [EvidenceItem.model_validate_json(row[0]) for row in rows]

    def count(self) -> int:
        """Return the number of distinct evidence items."""
        with self._lock:
            row = self._require_connection().execute(
                "SELECT count(*) FROM evidence"
            ).fetchone()
        return int(row[0]) if row else 0

    def counts_by_category(self) -> dict[str, int]:
        """Return the number of evidence items per category."""
        with self._lock:
            rows = self._require_connection().execute(
                "SELECT category, count(*) FROM evidence GROUP BY category ORDER BY category"
            ).fetchall()
        return {str(category): int(total) for category, total in rows}

    def put_finding(self, finding: Finding) -> Finding:
        """Store a finding whose evidence ids all exist and return it numbered.

        Raises:
            UnknownEvidenceError: when any cited id or alias is not stored.
        """
        known, unknown = self.resolve(finding.evidence_ids)
        if unknown:
            raise UnknownEvidenceError(unknown)
        with self._lock:
            connection = self._require_connection()
            ordinal = self._max_ordinal(connection, "findings") + 1
            saved = finding.model_copy(
                update={"finding_id": f"F{ordinal}", "evidence_ids": known}
            )
            connection.execute(
                "INSERT INTO findings VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ordinal,
                    saved.finding_id,
                    saved.agent_id,
                    saved.target,
                    saved.stance,
                    saved.model_dump_json(),
                ),
            )
        return saved

    def findings(
        self,
        *,
        agent_id: str | None = None,
        target: str | None = None,
    ) -> list[Finding]:
        """Return findings in the order they were recorded."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            parameters.append(agent_id)
        if target is not None:
            clauses.append("target = ?")
            parameters.append(target)
        statement = "SELECT finding FROM findings"
        if clauses:
            statement += " WHERE " + " AND ".join(clauses)
        statement += " ORDER BY ordinal"
        with self._lock:
            rows = self._require_connection().execute(statement, parameters).fetchall()
        return [Finding.model_validate_json(row[0]) for row in rows]

    def put_output(self, tool: str, payload: dict[str, Any]) -> str:
        """Keep one full tool result and return its ``O<n>`` reference.

        Outputs are run-local scratch for results a wrapper truncated; they
        are not part of the snapshot because they can be recomputed.
        """
        with self._lock:
            connection = self._require_connection()
            ordinal = self._max_ordinal(connection, "outputs") + 1
            output_ref = f"O{ordinal}"
            connection.execute(
                "INSERT INTO outputs VALUES (?, ?, ?, ?)",
                (
                    ordinal,
                    output_ref,
                    tool,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return output_ref

    def get_output(self, output_ref: str) -> dict[str, Any] | None:
        """Return a stored tool result, or ``None`` for an unknown reference."""
        with self._lock:
            row = self._require_connection().execute(
                "SELECT tool, payload FROM outputs WHERE output_ref = ?",
                [output_ref],
            ).fetchone()
        if row is None:
            return None
        return {"tool": str(row[0]), **json.loads(row[1])}

    def snapshot(self) -> dict[str, Any]:
        """Return the whole ledger as one JSON-compatible object."""
        return {
            "schema": SNAPSHOT_SCHEMA,
            "run_id": self.run_id,
            "evidence": [item.model_dump(mode="json") for item in self.query()],
            "findings": [finding.model_dump(mode="json") for finding in self.findings()],
        }

    def export(self, path: Path | None = None) -> Path:
        """Write the snapshot as JSON next to the database and return its path."""
        target = path or self.directory / SNAPSHOT_NAME
        target.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return target

    @classmethod
    def restore(
        cls,
        snapshot: dict[str, Any],
        *,
        run_id: str | None = None,
        root: Path | None = None,
    ) -> "EvidenceStore":
        """Rebuild a store from ``snapshot()`` output, keeping ids and aliases."""
        if snapshot.get("schema") != SNAPSHOT_SCHEMA:
            raise ValueError("not an agrihub evidence snapshot")
        store = cls.for_run(run_id or str(snapshot["run_id"]), root=root)
        items = [EvidenceItem.model_validate(raw) for raw in snapshot["evidence"]]
        items.sort(key=lambda item: _alias_number(item.alias))
        store.put_items(items)
        for raw in snapshot["findings"]:
            store.put_finding(Finding.model_validate(raw))
        return store

    def _lookup(self, keys: list[str]) -> dict[str, EvidenceItem]:
        with self._lock:
            connection = self._require_connection()
            found: dict[str, EvidenceItem] = {}
            for item in self._select_items(connection, "evidence_id", keys):
                found[str(item.evidence_id)] = item
            for item in self._select_items(connection, "alias", keys):
                found[str(item.alias)] = item
        return found

    def _existing(
        self,
        connection: duckdb.DuckDBPyConnection,
        evidence_ids: list[str],
    ) -> list[EvidenceItem]:
        if len(evidence_ids) <= _INLINE_LOOKUP_LIMIT:
            return self._select_items(connection, "evidence_id", evidence_ids)
        with self._staged({"evidence_id": value} for value in evidence_ids) as staging:
            rows = connection.execute(
                "SELECT e.item FROM evidence AS e SEMI JOIN read_json(?, "
                "format = 'newline_delimited', columns = {evidence_id: 'VARCHAR'}) "
                "AS s ON e.evidence_id = s.evidence_id",
                [str(staging)],
            ).fetchall()
        return [EvidenceItem.model_validate_json(row[0]) for row in rows]

    def _bulk_insert(
        self,
        connection: duckdb.DuckDBPyConnection,
        rows: list[dict[str, Any]],
    ) -> None:
        with self._staged(rows) as staging:
            connection.execute(
                f"INSERT INTO evidence SELECT {', '.join(_EVIDENCE_COLUMNS)} "
                "FROM read_json(?, format = 'newline_delimited', columns = "
                f"{_EVIDENCE_COLUMN_TYPES})",
                [str(staging)],
            )

    @contextmanager
    def _staged(self, rows: Iterable[dict[str, Any]]) -> Iterator[Path]:
        # Binding costs ~0.1 ms per parameter in DuckDB's Python client, so
        # batches of thousands of rows are staged as NDJSON and read in one scan.
        staging = self.directory / f".stage-{uuid.uuid4().hex}.ndjson"
        try:
            with staging.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            yield staging
        finally:
            staging.unlink(missing_ok=True)

    def _require_connection(self) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            raise RuntimeError("evidence store is closed")
        return self._connection

    @staticmethod
    def _max_ordinal(connection: duckdb.DuckDBPyConnection, table: str) -> int:
        row = connection.execute(f"SELECT coalesce(max(ordinal), 0) FROM {table}").fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _select_items(
        connection: duckdb.DuckDBPyConnection,
        column: str,
        values: list[str],
    ) -> list[EvidenceItem]:
        if not values:
            return []
        placeholders = ", ".join("?" for _ in values)
        rows = connection.execute(
            f"SELECT item FROM evidence WHERE {column} IN ({placeholders})",
            values,
        ).fetchall()
        return [EvidenceItem.model_validate_json(row[0]) for row in rows]


def _alias_number(alias: str | None) -> int:
    if alias and _ALIAS.match(alias):
        return int(alias[1:])
    return 0
