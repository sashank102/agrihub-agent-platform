"""``agrihub-data prune-raw`` deletes raw files only after the bundle verifies and keeps checksums for re-fetching."""

import gzip
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from agrihub_fixtures import DataServer, fixture_registry, publish_fixture_files

from agrihub_data.build import BuildError, build
from agrihub_data.bundle import close_bundles
from agrihub_data.cli import main
from agrihub_data.fetch import fetch
from agrihub_data.prune import disk_usage, prune_raw
from agrihub_data.registry import register_species, unregister_species
from agrihub_data.verify import verify


@pytest.fixture
def built(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Fetch and build a private core fixture bundle; return the data and server directories."""
    root = tmp_path / "server"
    root.mkdir()
    server = DataServer(root)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    registry = fixture_registry(server.base_url)
    publish_fixture_files(registry, root)
    register_species(registry)
    data_dir = tmp_path / "data"
    try:
        assert fetch("soybean", "core", data_dir=data_dir, concurrency=4).ok
        build("soybean", "core", data_dir=data_dir)
        yield data_dir, root
    finally:
        unregister_species("soybean")
        close_bundles()
        server.shutdown()
        server.server_close()


def _manifest(data_dir: Path) -> dict:
    return json.loads((data_dir / "soybean" / "manifest.json").read_text(encoding="utf-8"))


def test_nothing_is_deleted_when_the_bundle_does_not_verify(built: tuple[Path, Path]):
    data_dir, _ = built
    raw = data_dir / "soybean" / "raw"
    target = next(path for path in sorted(raw.rglob("*")) if path.is_file())
    target.write_bytes(target.read_bytes() + b"\n")
    report = prune_raw("soybean", data_dir=data_dir)
    assert not report.ok and report.deleted == []
    assert any("sha256 changed since fetch" in problem for problem in report.problems)
    assert all(entry["status"] == "present" for entry in _manifest(data_dir)["files"].values() if entry["status"] != "absent")


def test_prune_keeps_the_manifest_and_a_refetch_restores_identical_files(built: tuple[Path, Path], capsys: pytest.CaptureFixture[str]):
    data_dir, server_root = built
    raw = data_dir / "soybean" / "raw"
    before = disk_usage("soybean", data_dir)["tiers"]["core"]
    assert before["files"] > 0 and before["raw_bytes"] > 0

    assert main(["--data-dir", str(data_dir), "prune-raw", "--species", "soybean", "--keep", "core"]) == 0
    assert "deleted 0 raw files" in capsys.readouterr().out

    dry = prune_raw("soybean", data_dir=data_dir, dry_run=True)
    assert dry.ok and len(dry.deleted) == before["files"] and dry.bytes_freed > 0
    assert any(path.is_file() for path in raw.rglob("*"))

    assert main(["--data-dir", str(data_dir), "prune-raw", "--species", "soybean"]) == 0
    assert f"deleted {before['files']} raw files" in capsys.readouterr().out
    assert not raw.exists() or not any(path.is_file() for path in raw.rglob("*"))
    files = _manifest(data_dir)["files"]
    pruned = {key: entry for key, entry in files.items() if entry["status"] == "pruned"}
    assert len(pruned) == before["files"] and all(entry["sha256"] and entry["url"] and entry["pruned_at"] for entry in pruned.values())
    after = disk_usage("soybean", data_dir)["tiers"]["core"]
    assert (after["files"], after["raw_bytes"], after["pruned_files"]) == (0, 0, before["files"])
    assert verify("soybean", data_dir=data_dir).ok

    with pytest.raises(BuildError, match="were pruned, run agrihub-data fetch to restore them"):
        build("soybean", "core", data_dir=data_dir)

    key = "gwas_atlas/gwas_association_result_for_soyean.txt.gz"
    served = server_root / key
    original = served.read_bytes()
    with gzip.GzipFile(served, "wb", mtime=0) as handle:
        handle.write(gzip.decompress(original) + b"\n")
    refetched = fetch("soybean", "core", data_dir=data_dir, concurrency=4)
    assert set(refetched.failed) == {key} and "changed upstream since it was pruned" in refetched.failed[key]

    served.write_bytes(original)
    restored = fetch("soybean", "core", data_dir=data_dir, concurrency=4)
    assert restored.ok
    assert {key: entry["sha256"] for key, entry in _manifest(data_dir)["files"].items() if key in pruned} == {
        key: entry["sha256"] for key, entry in pruned.items()
    }
    assert verify("soybean", data_dir=data_dir).ok
    build("soybean", "core", data_dir=data_dir)
