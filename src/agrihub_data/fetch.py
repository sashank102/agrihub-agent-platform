"""Resumable downloader for registry sources.

Each file downloads to ``<name>.part`` with HTTP ``Range`` resume, is hashed
with sha256, checked against the registry pin when there is one (or, for a
file ``prune-raw`` deleted, against the sha256 recorded before pruning), and
renamed into place. ``manifest.json`` records the exact URL, size, sha256, version and
license of every file, and the resolved entries of every collection listing.
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from agrihub_data.paths import SpeciesPaths, species_paths
from agrihub_data.registry import (
    Source,
    SourceFile,
    SpeciesRegistry,
    Tier,
    load_species,
)

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA = "agrihub.data-manifest/v1"
USER_AGENT = "agrihub-data/0.1 (+https://github.com/sashank102/agrihub-agent-platform)"
CHUNK_BYTES = 1 << 20
_HREF = re.compile(r"""href\s*=\s*["']([^"'#?]+)["']""", re.IGNORECASE)


class FetchError(RuntimeError):
    """A required file could not be downloaded or failed its checksum."""


class ChecksumMismatchError(FetchError):
    """The downloaded bytes do not match the registry's pinned sha256."""


@dataclass(frozen=True)
class PlannedFile:
    """One file to fetch, resolved from a source or a collection entry."""

    source: Source
    url: str
    path: str
    sha256: str | None
    optional: bool

    @property
    def key(self) -> str:
        """Return the manifest key ``<source_id>/<path>``."""
        return f"{self.source.id}/{self.path}"


@dataclass
class FetchReport:
    """What one ``fetch`` call did."""

    downloaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    bytes_downloaded: int = 0
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """Return whether every required file is present."""
        return not self.failed


class Manifest:
    """``manifest.json`` with thread-safe, atomic updates."""

    def __init__(self, path: Path, species: str) -> None:
        """Load the manifest at ``path`` or start an empty one."""
        self.path = path
        self._lock = threading.Lock()
        if path.exists():
            self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"schema": MANIFEST_SCHEMA, "species": species, "files": {}, "collections": {}}

    @property
    def files(self) -> dict[str, dict[str, Any]]:
        """Return entries keyed by ``<source_id>/<path>``."""
        return self.data.setdefault("files", {})

    @property
    def collections(self) -> dict[str, dict[str, Any]]:
        """Return resolved collection listings keyed by source id."""
        return self.data.setdefault("collections", {})

    def entry(self, key: str) -> dict[str, Any] | None:
        """Return one file entry."""
        return self.files.get(key)

    def record(self, key: str, entry: dict[str, Any]) -> None:
        """Store one file entry and persist the manifest."""
        with self._lock:
            self.files[key] = entry
            self._write()

    def record_many(self, entries: dict[str, dict[str, Any]]) -> None:
        """Store several file entries and persist the manifest once."""
        with self._lock:
            self.files.update(entries)
            self._write()

    def record_collection(self, source_id: str, listing: dict[str, Any]) -> None:
        """Store a resolved collection listing and persist the manifest."""
        with self._lock:
            self.collections[source_id] = listing
            self._write()

    def source_files(self, source_id: str) -> list[dict[str, Any]]:
        """Return present file entries of one source in path order."""
        prefix = f"{source_id}/"
        return [
            entry
            for key, entry in sorted(self.files.items())
            if key.startswith(prefix) and entry.get("status") == "present"
        ]

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".json.tmp")
        staging.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        staging.replace(self.path)


def load_manifest(species: str, data_dir: Path | str | None = None) -> Manifest:
    """Open the manifest of one species."""
    return Manifest(species_paths(species, data_dir).manifest, species)


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def new_client(timeout: float = 120.0) -> httpx.Client:
    """Return an HTTP client that asks for unencoded bytes so resume offsets are exact."""
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=30.0),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )


def list_collection(client: httpx.Client, source: Source) -> list[str]:
    """Return the sorted entry names a collection listing links to."""
    if source.collection is None:
        return []
    response = client.get(source.collection.listing_url)
    response.raise_for_status()
    entries: set[str] = set()
    for href in _HREF.findall(response.text):
        name = unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1])
        if name and re.fullmatch(source.collection.entry_pattern, name):
            entries.add(name)
    return sorted(entries)


def plan_files(
    registry: SpeciesRegistry,
    tier: Tier,
    source_ids: Iterable[str] | None,
    client: httpx.Client,
    manifest: Manifest,
) -> list[PlannedFile]:
    """Resolve the files to fetch, reading collection listings once each."""
    wanted = set(source_ids or [])
    sources = [
        source
        for source in registry.sources_for(tier)
        if not wanted or source.id in wanted
    ]
    unknown = wanted - {source.id for source in sources}
    if unknown:
        raise FetchError(f"unknown or out-of-tier sources: {', '.join(sorted(unknown))}")
    planned: list[PlannedFile] = []
    for source in sources:
        planned.extend(_planned(source, file) for file in source.files)
        if source.collection is not None:
            entries = list_collection(client, source)
            if not entries:
                raise FetchError(f"{source.id}: listing {source.collection.listing_url} has no entries")
            manifest.record_collection(
                source.id,
                {
                    "listing_url": source.collection.listing_url,
                    "entries": entries,
                    "listed_at": _now(),
                },
            )
            for entry in entries:
                planned.extend(
                    _planned(
                        source,
                        SourceFile(
                            url=template.url.format(entry=entry),
                            path=template.path.format(entry=entry),
                            sha256=None,
                            optional=template.optional,
                        ),
                    )
                    for template in source.collection.files
                )
    return planned


def fetch(
    species: str,
    tier: Tier = "core",
    *,
    sources: Iterable[str] | None = None,
    data_dir: Path | str | None = None,
    client: httpx.Client | None = None,
    concurrency: int = 6,
    force: bool = False,
    attempts: int = 4,
    backoff_seconds: float = 2.0,
) -> FetchReport:
    """Download every file of the tier's sources and update the manifest.

    Failed transfers are retried ``attempts`` times with exponential backoff,
    resuming from the ``.part`` file each time.
    """
    started = time.monotonic()
    registry = load_species(species)
    paths = species_paths(registry.species, data_dir)
    manifest = Manifest(paths.manifest, registry.species)
    report = FetchReport()
    owns_client = client is None
    http = client or new_client()
    try:
        planned = plan_files(registry, tier, sources, http, manifest)
        lock = threading.Lock()

        def work(item: PlannedFile) -> None:
            outcome, size, error = _fetch_one(http, item, paths, manifest, force, attempts, backoff_seconds)
            with lock:
                if outcome == "downloaded":
                    report.downloaded.append(item.key)
                    report.bytes_downloaded += size
                elif outcome == "skipped":
                    report.skipped.append(item.key)
                elif outcome == "absent":
                    report.absent.append(item.key)
                else:
                    report.failed[item.key] = error or "unknown error"

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            list(pool.map(work, planned))
    finally:
        if owns_client:
            http.close()
    report.seconds = time.monotonic() - started
    return report


def _fetch_one(
    client: httpx.Client,
    item: PlannedFile,
    paths: SpeciesPaths,
    manifest: Manifest,
    force: bool,
    attempts: int,
    backoff_seconds: float,
) -> tuple[str, int, str | None]:
    destination = paths.source_dir(item.source.id) / item.path
    existing = manifest.entry(item.key)
    if (
        not force
        and existing is not None
        and existing.get("url") == item.url
        and existing.get("status") == "present"
        and destination.exists()
        and destination.stat().st_size == existing.get("size")
        and (item.sha256 is None or existing.get("sha256") == item.sha256)
    ):
        return "skipped", 0, None
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    if force:
        part.unlink(missing_ok=True)
    try:
        headers = _download(client, item.url, part, attempts, backoff_seconds)
    except _NotFoundError:
        part.unlink(missing_ok=True)
        if item.optional:
            manifest.record(item.key, _entry(item, status="absent"))
            return "absent", 0, None
        return "failed", 0, f"404 Not Found: {item.url}"
    except (httpx.HTTPError, OSError) as exc:
        return "failed", 0, f"{type(exc).__name__}: {exc}"
    digest = sha256_file(part)
    if item.sha256 is not None and digest != item.sha256:
        part.unlink(missing_ok=True)
        return "failed", 0, (
            f"sha256 mismatch for {item.url}: expected {item.sha256}, got {digest}"
        )
    recorded = existing.get("sha256") if existing is not None and existing.get("status") == "pruned" else None
    if not force and recorded and existing is not None and existing.get("url") == item.url and digest != recorded:
        part.unlink(missing_ok=True)
        return "failed", 0, (
            f"{item.url} changed upstream since it was pruned: expected sha256 {recorded}, got {digest}; "
            "fetch with --force to accept the new file and rebuild"
        )
    size = part.stat().st_size
    part.replace(destination)
    manifest.record(
        item.key,
        _entry(
            item,
            status="present",
            size=size,
            sha256=digest,
            last_modified=headers.get("last-modified"),
            etag=headers.get("etag"),
        ),
    )
    return "downloaded", size, None


class _NotFoundError(Exception):
    pass


def _download(
    client: httpx.Client,
    url: str,
    part: Path,
    attempts: int,
    backoff_seconds: float,
) -> dict[str, str]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    raise _NotFoundError(url)
                if response.status_code == 416:
                    part.unlink(missing_ok=True)
                    raise httpx.HTTPStatusError(
                        "range not satisfiable; restarting",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                resumed = offset > 0 and response.status_code == 206
                # Unsized iteration writes bytes as they arrive, so a dropped
                # connection leaves everything received in the .part file.
                with part.open("ab" if resumed else "wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
                return {key.lower(): value for key, value in response.headers.items()}
        except _NotFoundError:
            raise
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code < 500 and exc.response.status_code != 416:
                raise
        except httpx.TransportError as exc:
            last_error = exc
        if attempt < attempts:
            delay = min(30.0, backoff_seconds * 2 ** (attempt - 1))
            logger.warning("retrying %s in %.0fs after %s", url, delay, last_error)
            time.sleep(delay)
    raise last_error or FetchError(f"no attempts made for {url}")


def _planned(source: Source, file: SourceFile) -> PlannedFile:
    return PlannedFile(
        source=source,
        url=file.url,
        path=file.path,
        sha256=file.sha256,
        optional=file.optional,
    )


def _entry(item: PlannedFile, *, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "source_id": item.source.id,
        "url": item.url,
        "path": item.path,
        "status": status,
        "version": item.source.version,
        "license": item.source.license,
        "academic_only": item.source.academic_only,
        "fetched_at": _now(),
        **{key: value for key, value in extra.items() if value is not None},
    }


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
