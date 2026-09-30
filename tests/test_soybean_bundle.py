"""Spot checks against the real soybean core bundle (``agrihub-data fetch/build``)."""

import asyncio
import csv
import time
from pathlib import Path
from typing import Any

import pytest

from agent_platform.core.settings import get_data_paths
from agrihub.tools import bundle_tools
from agrihub_data.bundle import close_bundles, open_bundle
from agrihub_data.query import annotation, ids, loci, orthology, overlap, traits
from agrihub_data.registry import normalize_chrom
from agrihub_data.verify import verify

BUNDLE = get_data_paths().data_dir / "soybean" / "bundle.duckdb"
RESULTS = Path(__file__).resolve().parents[2] / "Results" / "2_Sep" / "Gene_name"
POSTER_SNPS = {"S5_2899164": ("Gm05", 2_899_164), "S18_9263941": ("Gm18", 9_263_941), "S18_51620945": ("Gm18", 51_620_945)}

pytestmark = [
    pytest.mark.bundle,
    pytest.mark.skipif(not BUNDLE.exists(), reason=f"no soybean bundle at {BUNDLE}"),
]


@pytest.fixture(scope="module")
def bundle():
    opened = open_bundle("soybean")
    yield opened
    close_bundles()


def test_bundle_verifies_with_plausible_counts(bundle):
    report = verify("soybean")
    assert report.ok, report.problems
    count = lambda sql: bundle.rows_raw(sql)[0][0]  # noqa: E731
    assert 55_000 < count("SELECT count(*) FROM genes WHERE assembly = 'Wm82.a2.v1'") < 57_000
    assert count("SELECT count(DISTINCT study_id) FROM qtl") == 313
    assert 3_000 < count("SELECT count(*) FROM gwas_hits WHERE source_db = 'SoyBase GWAS' AND assembly = 'Wm82.a2.v1'") < 3_200
    assert count("SELECT count(*) FROM known_genes") == 207
    assert count("SELECT count(*) FROM known_genes WHERE assembly = 'Wm82.a2.v1'") == 205
    assert bundle.info["canonical_assembly"] == "Wm82.a2.v1"


def test_glyma_18g092200_is_a_wrky_transcription_factor(bundle):
    wrky = annotation.gene_annotation(bundle, ["Glyma.18G092200"])[0]
    assert "WRKY" in (wrky.defline or "")
    assert "GO:0003700" in {term.id for term in wrky.go}
    assert "PF03106" in {term.id for term in wrky.domains["pfam"]}


def test_poster_windows_match_the_gene_name_results_on_shared_regions(bundle):
    if not RESULTS.exists():
        pytest.skip(f"no {RESULTS}")
    compared = 0
    for path in sorted(RESULTS.glob("*.csv")):
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        for chrom, pos in POSTER_SNPS.values():
            window = loci.define_locus("soybean", chrom, pos)
            ours = loci.genes_in_window(bundle, window.region())
            regions = {
                (int(row["region_start"]), int(row["region_end"]))
                for row in rows
                if normalize_chrom("soybean", row["chromosome"]) == chrom
                and int(row["region_end"]) >= window.start
                and int(row["region_start"]) <= window.end
            }
            for region_start, region_end in regions:
                low, high = max(region_start, window.start), min(region_end, window.end)
                theirs = {
                    row["Name"]
                    for row in rows
                    if normalize_chrom("soybean", row["chromosome"]) == chrom
                    and (int(row["region_start"]), int(row["region_end"])) == (region_start, region_end)
                    and int(row["gene_start"]) <= high
                    and int(row["gene_end"]) >= low
                }
                mine = {gene.gene_id for gene in ours if gene.start <= high and gene.end >= low}
                assert mine == theirs, (path.name, chrom, low, high, sorted(mine ^ theirs))
                compared += 1
    assert compared >= 2


def test_known_plant_height_genes_include_dt1_and_dt2(bundle):
    profile = traits.map_trait("plant height", "soybean", bundle)
    found = {hit.gene_id: hit for hit in overlap.known_trait_genes(bundle, profile)}
    assert "Glyma.19G194300" in found and "GmDT1" in found["Glyma.19G194300"].symbols
    assert "Glyma.18G273600" in found and "GmDT2" in found["Glyma.18G273600"].symbols


def test_markers_and_chromosome_aliases(bundle):
    barc = ids.resolve_marker("soybean", "BARC_1.01_Gm01_24939_A_G", None, bundle)
    a1 = [hit for hit in barc if hit.assembly == "Wm82.a1.v1"]
    assert a1 and all("a1_embedded_position" in hit.flags for hit in a1)
    assert any(hit.assembly == "Wm82.a2.v1" and hit.pos == 24_952 for hit in barc)
    assert {normalize_chrom("soybean", name) for name in ("Gm18", "Chr18", "chr18", "18", "glyma.Wm82.gnm2.Gm18")} == {"Gm18"}


def test_dt1_ortholog_is_tfl1_with_high_confidence(bundle):
    best = orthology.get_orthologs(bundle, ["Glyma.19G194300"])[0]
    assert (best.target_gene_id, best.confidence) == ("AT5G03840", "high")
    assert best.n_methods >= 3 and "compara" in best.methods


def test_whole_chromosome_query_keeps_the_event_loop_responsive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))

    async def scenario() -> tuple[float, list[float], dict[str, Any]]:
        ticks: list[float] = []
        running = True

        async def heartbeat() -> None:
            while running:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        message = await bundle_tools.genes_in_window.ainvoke(
            {
                "type": "tool_call",
                "id": "call-heartbeat",
                "name": "genes_in_window",
                "args": {"windows": [{"chrom": "Gm18", "start": 1, "end": 58_018_742, "snp_pos": 9_263_941}]},
            },
            {"configurable": {"run_id": "bundle-heartbeat"}},
        )
        elapsed = time.monotonic() - started
        running = False
        await beat
        return elapsed, [tick for tick in ticks if tick >= started], dict(message.artifact)

    elapsed, ticks, artifact = asyncio.run(scenario())
    assert artifact["total"] > 3_000 and artifact["truncated"]
    gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:], strict=False)]
    assert gaps and max(gaps) < 0.25, (elapsed, max(gaps))
    assert len(ticks) >= elapsed / 0.05
