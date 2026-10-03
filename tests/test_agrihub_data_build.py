"""Fetch against a local HTTP server, build the fixture bundle, and verify it."""

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import duckdb
import pytest
from agrihub_fixtures import DataServer, FixtureBundle, packaged_soybean

from agrihub_data.build import PARSERS, BuildError, build
from agrihub_data.build.context import open_text
from agrihub_data.bundle import tables_for
from agrihub_data.fetch import fetch
from agrihub_data.paths import species_paths
from agrihub_data.registry import (
    Source,
    SpeciesRegistry,
    load_species,
    register_species,
    unregister_species,
)
from agrihub_data.verify import verify

PAYLOAD = b"".join(f"line {index}\tvalue\n".encode() for index in range(5_000))


def _tiny_registry(base: str, *, sha256: str | None = None) -> SpeciesRegistry:
    source = Source.model_validate(
        {
            "id": "tiny",
            "name": "Tiny source",
            "tier": "core",
            "version": "1",
            "license": "CC0",
            "assemblies": ["Wm82.a2.v1"],
            "parser": "ontology",
            "files": [
                {"url": f"{base}/tiny/payload.tsv", "path": "payload.tsv", "sha256": sha256},
                {"url": f"{base}/tiny/missing.tsv", "path": "missing.tsv", "optional": True},
            ],
        }
    )
    return packaged_soybean().model_copy(update={"sources": [source]})


@pytest.fixture
def tiny(data_server: DataServer, tmp_path: Path):
    (data_server.root / "tiny").mkdir()
    (data_server.root / "tiny" / "payload.tsv").write_bytes(PAYLOAD)
    yield data_server, tmp_path / "data"
    unregister_species("soybean")


def test_fetch_resumes_a_cut_download_and_records_the_checksum(tiny):
    server, data_dir = tiny
    expected = hashlib.sha256(PAYLOAD).hexdigest()
    register_species(_tiny_registry(server.base_url, sha256=expected))
    server.cut_once.add("/tiny/payload.tsv")

    report = fetch("soybean", data_dir=data_dir, concurrency=1, attempts=3, backoff_seconds=0.01)

    assert report.ok, report.failed
    assert report.downloaded == ["tiny/payload.tsv"]
    assert report.absent == ["tiny/missing.tsv"]
    ranges = [header for path, header in server.requests if path == "/tiny/payload.tsv"]
    assert ranges[0] is None and ranges[1] == f"bytes={len(PAYLOAD) // 2}-"
    paths = species_paths("soybean", data_dir)
    stored = paths.source_dir("tiny") / "payload.tsv"
    assert stored.read_bytes() == PAYLOAD
    assert not stored.with_name("payload.tsv.part").exists()
    entry = json.loads(paths.manifest.read_text())["files"]["tiny/payload.tsv"]
    assert (entry["sha256"], entry["size"], entry["status"]) == (expected, len(PAYLOAD), "present")
    assert entry["url"] == f"{server.base_url}/tiny/payload.tsv"
    assert entry["license"] == "CC0"

    again = fetch("soybean", data_dir=data_dir, concurrency=1)
    assert again.skipped == ["tiny/payload.tsv"] and not again.downloaded


def test_fetch_resumes_from_an_existing_part_file(tiny):
    server, data_dir = tiny
    register_species(_tiny_registry(server.base_url))
    part = species_paths("soybean", data_dir).source_dir("tiny") / "payload.tsv.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(PAYLOAD[:1000])

    assert fetch("soybean", data_dir=data_dir, concurrency=1).ok
    assert [header for path, header in server.requests if path == "/tiny/payload.tsv"] == ["bytes=1000-"]
    assert (part.parent / "payload.tsv").read_bytes() == PAYLOAD


def test_fetch_rejects_bytes_that_do_not_match_the_pinned_checksum(tiny):
    server, data_dir = tiny
    register_species(_tiny_registry(server.base_url, sha256="0" * 64))

    report = fetch("soybean", data_dir=data_dir, concurrency=1)

    assert not report.ok
    assert "sha256 mismatch" in report.failed["tiny/payload.tsv"]
    folder = species_paths("soybean", data_dir).source_dir("tiny")
    assert not (folder / "payload.tsv").exists() and not (folder / "payload.tsv.part").exists()


def test_fetch_fails_a_required_file_that_is_missing(tiny):
    server, data_dir = tiny
    registry = _tiny_registry(server.base_url)
    registry.sources[0].files[1].optional = False
    register_species(registry)

    report = fetch("soybean", data_dir=data_dir, concurrency=1)

    assert report.failed == {"tiny/missing.tsv": f"404 Not Found: {server.base_url}/tiny/missing.tsv"}


def test_fixture_fetch_resolves_collections_and_optional_files(fixture_bundle: FixtureBundle):
    report = fixture_bundle.fetch_report
    assert len(report.downloaded) == 44 and not report.failed
    assert sorted(report.absent) == [
        "lis_qtl/Gamma_x_Delta.qtl.Test_2021/README.Gamma_x_Delta.qtl.Test_2021.yml",
        "lis_qtl/Gamma_x_Delta.qtl.Test_2021/glyma.Gamma_x_Delta.qtl.Test_2021.obo.tsv.gz",
    ]
    manifest = json.loads(species_paths("soybean", fixture_bundle.data_dir).manifest.read_text())
    assert manifest["collections"]["lis_qtl"]["entries"] == [
        "Alpha_x_Beta.qtl.Test_2020",
        "Gamma_x_Delta.qtl.Test_2021",
    ]


def test_parsers_run_in_dependency_order():
    seen: set[str] = set()
    for parser in PARSERS:
        assert set(parser.after) <= seen, parser.name
        seen.add(parser.name)


def test_fixture_bundle_builds_and_verifies(fixture_bundle: FixtureBundle):
    report = fixture_bundle.build_report
    assert report.tables["genes"] == 7 + 7 + 2
    assert report.tables["gene_parts"] == 4
    assert report.tables["expression"] == report.tables["edges"] == report.tables["resources"] == 0
    assert report.tables["qtl"] == 6
    assert report.tables["known_genes"] == 4
    assert report.tables["ncbi_genes"] == 5
    assert report.tables["ncbi_gene_pubmed"] == 4
    assert report.tables["ncbi_gene_go"] == 3
    stats = report.stats
    assert stats["lis_wm82_a2"]["genes_out_of_bounds"] == 1
    assert stats["lis_wm82_a2"]["genes_skipped_unknown_seqid"] == 1
    assert stats["lis_wm82_a2"]["out_of_bounds:Wm82.gnm2.ann1.RVB6.markers"] == 1
    assert stats["lis_qtl"] == {
        "placement:lg_conflict": 1,
        "placement:markers": 3,
        "placement:single_marker": 1,
        "placement:unplaced": 1,
        "qtl": 6,
        "studies": 2,
        "trait_map": 3,
    }
    assert stats["soybase_gwas"]["skipped_assembly:Wm82.a2.a1"] == 1
    assert stats["soybase_gwas"]["out_of_bounds:Wm82.a2.v1"] == 1
    assert stats["gwas_atlas"]["skipped_assembly:Wm82.a9.v1"] == 1
    assert stats["gwas_atlas"]["duplicate_ids"] == 1
    assert stats["lis_gene_functions"]["unparsed_gene_model"] == 1
    assert stats["plaza_orthology"]["unmapped_source_genes"] == 1

    checked = verify("soybean", data_dir=fixture_bundle.data_dir)
    assert checked.ok, checked.problems
    core = set(tables_for("core"))
    assert all(count > 0 for table, count in checked.counts.items() if table in core)
    assert all(count == 0 for table, count in checked.counts.items() if table not in core | {"sources"})


def test_every_row_carries_species_assembly_and_source_version(fixture_bundle: FixtureBundle):
    bundle = species_paths("soybean", fixture_bundle.data_dir).bundle
    connection = duckdb.connect(str(bundle), read_only=True)
    try:
        genes = connection.execute(
            "SELECT DISTINCT species, assembly, source_version FROM genes ORDER BY assembly"
        ).fetchall()
        qtl = connection.execute(
            'SELECT qtl_name, chrom, start, "end", n_markers_placed, placement FROM qtl '
            "WHERE study_id = 'Alpha_x_Beta.qtl.Test_2020' ORDER BY qtl_name"
        ).fetchall()
        hits = dict(
            connection.execute("SELECT hit_id, p_value FROM gwas_hits WHERE source_db = 'LIS/SoyBase GWAS'").fetchall()
        )
        soybase = connection.execute(
            "SELECT assembly, count(*) FROM gwas_hits WHERE source_db = 'SoyBase GWAS' GROUP BY 1 ORDER BY 1"
        ).fetchall()
        known = connection.execute(
            "SELECT source_gene_id, gene_id, assembly, mapping FROM known_genes ORDER BY source_gene_id"
        ).fetchall()
        rolling = connection.execute("SELECT DISTINCT source_version FROM ontology_terms WHERE ontology = 'GO'").fetchone()
    finally:
        connection.close()
    assert genes == [
        ("soybean", "Wm82.a2.v1", "Wm82.gnm2.ann1.RVB6"),
        ("soybean", "Wm82.a4.v1", "Wm82.gnm4.ann1.T8TQ"),
        ("soybean", "Wm82.a6.v1", "Wm82.gnm6.ann1.PKSW"),
    ]
    assert qtl == [
        ("Canopy wilt 1-1", None, None, None, 0, "unplaced"),
        ("Lodging 1-1", None, None, None, 0, "lg_conflict"),
        ("Plant height 1-1", "Gm18", 9100001, 9600021, 2, "markers"),
        ("Plant height 1-2", "Gm05", 2800001, 3000001, 2, "markers"),
        ("Seed oil 1-1", "Gm18", 9100001, 9100021, 1, "single_marker"),
    ]
    assert hits["mixed.gwas.Test_2019|ss715631025|Plant height"] == pytest.approx(1.2e-8)
    assert soybase == [("Wm82.a1.v1", 1), ("Wm82.a2.v1", 3)]
    assert known == [
        ("Glyma.05G032300", "Glyma.05G032200", "Wm82.a2.v1", "pangene"),
        ("Glyma.11G148362", "Glyma.11G148362", "Wm82.a4.v1", "unmapped"),
        ("Glyma.18G273600", "Glyma.18G273600", "Wm82.a2.v1", "ancestor"),
        ("Glyma.19G194300", "Glyma.19G194300", "Wm82.a2.v1", "ancestor"),
    ]
    assert rolling is not None and "fetched" in rolling[0] and "releases/2026-09-01" in rolling[0]


def test_verify_catches_a_row_on_an_unregistered_assembly(fixture_bundle: FixtureBundle, tmp_path: Path):
    source = species_paths("soybean", fixture_bundle.data_dir)
    target = species_paths("soybean", tmp_path)
    target.root.mkdir(parents=True)
    shutil.copyfile(source.bundle, target.bundle)
    connection = duckdb.connect(str(target.bundle))
    connection.execute(
        "INSERT INTO genes VALUES ('soybean', 'Wm99.a9.v1', 'x', 'Glyma.01G000001', 'Gm01', 1, 10, '+', NULL, NULL, 'test')"
    )
    connection.execute(
        "INSERT INTO trait_map VALUES ('soybean', 'Wm82.a2.v1', 'x', 'Plant height', 'TO:0000207', NULL, 'test')"
    )
    connection.execute(
        "INSERT INTO markers VALUES ('soybean', 'Wm82.a2.v1', 'x', 'm1', NULL, 'set', 'chr1', 5, 5, NULL, 'test')"
    )
    connection.close()
    register_species(fixture_bundle.registry)
    try:
        report = verify("soybean", data_dir=tmp_path, checksums=False)
    finally:
        unregister_species("soybean")
    assert not report.ok
    assert "genes.assembly: 1 rows on unregistered assembly 'Wm99.a9.v1'" in report.problems
    assert "trait_map.assembly: 1 rows on unregistered assembly 'Wm82.a2.v1'" in report.problems
    assert any("non-canonical chromosome 'chr1'" in problem for problem in report.problems)


def test_verify_notices_a_changed_download(fixture_bundle: FixtureBundle, tmp_path: Path):
    source = species_paths("soybean", fixture_bundle.data_dir)
    copy = tmp_path / "soybean"
    shutil.copytree(source.root, copy)
    (copy / "raw" / "soybase_gwas" / "soybase_gwas_locations.tsv").write_text("changed\n")
    register_species(fixture_bundle.registry)
    try:
        report = verify("soybean", data_dir=tmp_path)
    finally:
        unregister_species("soybean")
    assert report.problems == ["sha256 changed since fetch: soybase_gwas/soybase_gwas_locations.tsv"]


def test_build_refuses_sources_that_were_not_fetched(tmp_path: Path):
    with pytest.raises(BuildError, match="not fetched"):
        build("soybean", data_dir=tmp_path)


def test_open_text_sniffs_zip_archives_named_gz(tmp_path: Path):
    path = tmp_path / "ATH_GO_GOSLIM.txt.gz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ATH_GO_GOSLIM.txt", "!header\nAT1G01010\tx\n")
    with open_text(path) as handle:
        assert handle.read().splitlines() == ["!header", "AT1G01010\tx"]


def test_packaged_soybean_registry_pins_stable_files():
    registry = load_species("soybean")
    pinned = [file for source in registry.sources if not source.rolling for file in source.files]
    assert pinned and all(file.sha256 and file.size for file in pinned)
    assert all(not file.sha256 for source in registry.sources if source.rolling for file in source.files)
