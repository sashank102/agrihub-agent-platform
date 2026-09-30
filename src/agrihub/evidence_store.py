"""Run-scoped evidence ledger backed by one DuckDB file per run.

The file lives at ``${AGRIHUB_RUN_DIR:-<project>/var/runs}/<run_id>/evidence.duckdb``.
``evidence_id`` is ``sha1(source_db|source_record|gene_id|subtype)[:12]``, so
the same fact harvested twice collapses to one row. Each run also numbers its
evidence ``E1, E2, ...`` in insertion order; tools accept either form.
Findings are numbered ``F1, F2, ...`` and may only cite stored evidence.
"""

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb

from agrihub.configuration import PROJECT_ROOT
from agrihub.state import EvidenceItem, Finding

RUN_DIR_ENV = "AGRIHUB_RUN_DIR"
DEFAULT_RUN_DIR = PROJECT_ROOT / "var" / "runs"
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
    return Path(os.environ.get(RUN_DIR_ENV) or DEFAULT_RUN_DIR)


def evidence_id_for(item: EvidenceItem) -> str:
    """Return the content-derived id that deduplicates repeated facts."""
    key = "|".join((item.source_db, item.source_record, item.gene_id, item.subtype))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


class EvidenceStore:
    """Evidence and findings for one run.

    Calls are serialized with a lock because DuckDB connections are not
    thread-safe and synchronous LangChain tools run in executor threads.
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
        stored copy is returned in their place.
        """
        batch = list(items)
        with self._lock:
            connection = self._require_connection()
            ordinal = self._max_ordinal(connection, "evidence")
            stored: dict[str, EvidenceItem] = {}
            ids = [evidence_id_for(item) for item in batch]
            known = {
                str(row[0])
                for row in connection.execute("SELECT evidence_id FROM evidence").fetchall()
            }
            duplicates = [evidence_id for evidence_id in dict.fromkeys(ids) if evidence_id in known]
            for existing in self._select_items(connection, "evidence_id", duplicates):
                if existing.evidence_id:
                    stored[existing.evidence_id] = existing
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

    def _bulk_insert(
        self,
        connection: duckdb.DuckDBPyConnection,
        rows: list[dict[str, Any]],
    ) -> None:
        # Binding costs ~0.1 ms per parameter in DuckDB's Python client, so a
        # harvest of thousands of rows is staged as NDJSON and read in one scan.
        staging = self.directory / f".insert-{uuid.uuid4().hex}.ndjson"
        try:
            with staging.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            connection.execute(
                f"INSERT INTO evidence SELECT {', '.join(_EVIDENCE_COLUMNS)} "
                "FROM read_json(?, format = 'newline_delimited', columns = "
                f"{_EVIDENCE_COLUMN_TYPES})",
                [str(staging)],
            )
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
