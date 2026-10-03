"""Delete raw downloads once a bundle verifies, keeping the manifest and checksums so re-fetching is reproducible.

A pruned file stays in ``manifest.json`` with status ``pruned`` and its URL,
size and sha256. ``fetch`` downloads it again and refuses bytes that differ
from the recorded sha256 unless forced. Tools never read ``raw/``; only
``build`` does, so a pruned species needs a fetch before it can be rebuilt.
"""

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agrihub_data.fetch import Manifest
from agrihub_data.paths import species_paths
from agrihub_data.registry import TIERS, Tier, load_species
from agrihub_data.verify import verify

PRUNED = "pruned"


@dataclass
class PruneReport:
    """What one ``prune_raw`` call deleted or would delete."""

    deleted: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    bytes_freed: int = 0
    bytes_kept: int = 0
    dry_run: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the bundle verified, so pruning was allowed."""
        return not self.problems


def prune_raw(
    species: str,
    *,
    keep: Tier | None = None,
    data_dir: Path | str | None = None,
    dry_run: bool = False,
) -> PruneReport:
    """Delete the raw files of a species whose bundle verifies.

    ``keep`` keeps the raw files of sources up to and including that tier
    (``core`` keeps what a core rebuild needs). Nothing is deleted when the
    bundle does not verify, including its download checksums.
    """
    registry = load_species(species)
    paths = species_paths(registry.species, data_dir)
    report = PruneReport(dry_run=dry_run)
    checked = verify(registry.species, data_dir=data_dir, checksums=True)
    if not checked.ok:
        report.problems = list(checked.problems)
        return report
    kept_sources = {source.id for source in registry.sources_for(keep)} if keep else set()
    manifest = Manifest(paths.manifest, registry.species)
    pruned_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    updates: dict[str, dict[str, Any]] = {}
    for key, entry in sorted(manifest.files.items()):
        if entry.get("status") != "present":
            continue
        size = int(entry.get("size") or 0)
        if entry.get("source_id") in kept_sources:
            report.kept.append(key)
            report.bytes_kept += size
            continue
        report.deleted.append(key)
        report.bytes_freed += size
        updates[key] = {**entry, "status": PRUNED, "pruned_at": pruned_at}
    if dry_run or not updates:
        return report
    for key in updates:
        (paths.raw / key).unlink(missing_ok=True)
    manifest.record_many(updates)
    _remove_empty_dirs(paths.raw)
    return report


def disk_usage(species: str, data_dir: Path | str | None = None) -> dict[str, Any]:
    """Return bytes on disk per tier (raw downloads present and pruned), plus the bundle and unpacked resources."""
    registry = load_species(species)
    paths = species_paths(registry.species, data_dir)
    manifest = Manifest(paths.manifest, registry.species)
    tier_of = {source.id: source.tier for source in registry.sources}
    tiers: dict[str, dict[str, int]] = {
        tier: {"sources": len([s for s in registry.sources if s.tier == tier and s.status == "active"]), "files": 0, "raw_bytes": 0, "pruned_files": 0, "pruned_bytes": 0}
        for tier in (*TIERS, "unregistered")
    }
    for key, entry in manifest.files.items():
        row = tiers[tier_of.get(str(entry.get("source_id")), "unregistered")]
        size = int(entry.get("size") or 0)
        if entry.get("status") == "present":
            on_disk = paths.raw / key
            row["files"] += 1
            row["raw_bytes"] += on_disk.stat().st_size if on_disk.exists() else 0
        elif entry.get("status") == PRUNED:
            row["pruned_files"] += 1
            row["pruned_bytes"] += size
    if not any(tiers["unregistered"].values()):
        tiers.pop("unregistered")
    resources = {name: _tree_bytes(paths.root / name) for name in ("ld", "vep") if (paths.root / name).exists()}
    return {
        "tiers": tiers,
        "bundle_bytes": paths.bundle.stat().st_size if paths.bundle.exists() else 0,
        "resources_bytes": resources,
        "other_bytes": _tree_bytes(paths.root) - _tree_bytes(paths.raw) - sum(resources.values()) - (paths.bundle.stat().st_size if paths.bundle.exists() else 0),
    }


def _tree_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def _remove_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
        if not any(directory.iterdir()):
            shutil.rmtree(directory, ignore_errors=True)
