"""``agrihub-data``: fetch, build, verify and inspect species bundles."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from agrihub_data.build import BuildError, build
from agrihub_data.catalog import bundle_status
from agrihub_data.fetch import FetchError, Manifest, fetch
from agrihub_data.paths import species_paths
from agrihub_data.registry import TIERS, load_species, species_names
from agrihub_data.verify import verify


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return its exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")
    try:
        return int(args.handler(args))
    except (BuildError, FetchError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agrihub-data", description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="override AGRIHUB_DATA_DIR")
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch_parser = commands.add_parser("fetch", help="download a tier's sources (resumable)")
    fetch_parser.add_argument("--species", required=True, choices=species_names())
    fetch_parser.add_argument("--tier", default="core", choices=TIERS)
    fetch_parser.add_argument("--source", action="append", help="limit to one source id (repeatable)")
    fetch_parser.add_argument("--concurrency", type=int, default=6)
    fetch_parser.add_argument("--force", action="store_true", help="download again even if present")
    fetch_parser.set_defaults(handler=_fetch)

    build_parser = commands.add_parser("build", help="build bundle.duckdb from fetched files")
    build_parser.add_argument("--species", required=True, choices=species_names())
    build_parser.add_argument("--tier", default="core", choices=TIERS)
    build_parser.set_defaults(handler=_build)

    verify_parser = commands.add_parser("verify", help="check a built bundle")
    verify_parser.add_argument("--species", required=True, choices=species_names())
    verify_parser.add_argument("--no-checksums", action="store_true", help="skip re-hashing downloads")
    verify_parser.set_defaults(handler=_verify)

    status_parser = commands.add_parser("status", help="show fetch and build state")
    status_parser.add_argument("--species", choices=species_names())
    status_parser.add_argument("--json", action="store_true")
    status_parser.set_defaults(handler=_status)
    return parser


def _fetch(args: argparse.Namespace) -> int:
    report = fetch(
        args.species,
        args.tier,
        sources=args.source,
        data_dir=args.data_dir,
        concurrency=args.concurrency,
        force=args.force,
    )
    print(
        f"fetched {len(report.downloaded)} files ({report.bytes_downloaded / 1e6:.1f} MB), "
        f"{len(report.skipped)} already present, {len(report.absent)} optional absent, "
        f"{len(report.failed)} failed in {report.seconds:.1f}s"
    )
    for key, error in sorted(report.failed.items()):
        print(f"  FAILED {key}: {error}", file=sys.stderr)
    return 0 if report.ok else 1


def _build(args: argparse.Namespace) -> int:
    report = build(args.species, args.tier, data_dir=args.data_dir)
    print(f"built {report.bundle} ({report.bytes / 1e6:.1f} MB) in {report.seconds:.1f}s")
    for table, count in report.tables.items():
        print(f"  {table:18} {count:>10,}")
    for source_id, seconds in report.timings.items():
        counters = ", ".join(f"{key}={value}" for key, value in report.stats.get(source_id, {}).items())
        print(f"  [{source_id} {seconds:.1f}s] {counters}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    report = verify(args.species, data_dir=args.data_dir, checksums=not args.no_checksums)
    for table, count in report.counts.items():
        print(f"  {table:18} {count:>10,}")
    for problem in report.problems:
        print(f"  PROBLEM {problem}", file=sys.stderr)
    print("verify: ok" if report.ok else f"verify: {len(report.problems)} problems")
    return 0 if report.ok else 1


def _status(args: argparse.Namespace) -> int:
    rows = [_species_status(name, args.data_dir) for name in ([args.species] if args.species else species_names())]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        bundle = row["bundle"]
        built = f"built {bundle['built_at']} ({bundle['megabytes']} MB, tier {bundle['tier']})" if bundle else "not built"
        print(
            f"{row['species']:8} {row['canonical_assembly']:26} sources {row['fetched_sources']}/"
            f"{row['core_sources']} core fetched, {row['files']} files ({row['megabytes']} MB); {built}"
        )
    return 0


def _species_status(species: str, data_dir: Path | None) -> dict[str, Any]:
    registry = load_species(species)
    paths = species_paths(species, data_dir)
    manifest = Manifest(paths.manifest, species)
    core = registry.sources_for("core")
    present = [entry for entry in manifest.files.values() if entry.get("status") == "present"]
    status = bundle_status(species, data_dir)
    bundle = None
    if status is not None:
        bundle = {"built_at": status["built_at"], "tier": status["tier"], "megabytes": round(status["bytes"] / 1e6, 1)}
    return {
        "species": species,
        "canonical_assembly": registry.canonical_assembly,
        "core_sources": len(core),
        "fetched_sources": sum(1 for source in core if manifest.source_files(source.id)),
        "files": len(present),
        "megabytes": round(sum(int(entry.get("size") or 0) for entry in present) / 1e6, 1),
        "bundle": bundle,
    }


if __name__ == "__main__":
    raise SystemExit(main())
