"""The known-gene benchmark builds pseudo-studies, holds the answer out and writes a scorecard."""

import csv
import io
import subprocess
import sys
from pathlib import Path

import pytest
from agrihub_fixtures import FixtureBundle

from agrihub.benchmark import (
    build_pseudo_studies,
    by_distance,
    holdout,
    rank_in_locus,
    recall,
    recall_interval,
    run_benchmark,
)
from agrihub_data.bundle import open_bundle
from agrihub_data.query import overlap
from agrihub_data.query.traits import map_trait

pytestmark = pytest.mark.usefixtures("fake_llm")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pseudo_studies_use_trait_matched_catalog_hits_near_curated_genes(fixture_env: FixtureBundle):
    [study] = build_pseudo_studies(open_bundle("soybean"), ("plant height",))
    [target] = study.targets
    assert (target.gene_id, target.chrom, target.pos, target.p_value) == ("Glyma.19G194300", "Gm19", 45_150_000, 1e-8)
    assert target.distance_bp == 45_150_000 - 45_101_500 and target.held_out_hits == [target.hit_id]
    assert study.study["snps"] == [{"raw": target.marker, "chrom": "Gm19", "pos": 45_150_000}]


def test_holdout_hides_the_target_record_and_its_hits_then_restores_them(fixture_env: FixtureBundle):
    bundle = open_bundle("soybean")
    profile = map_trait("plant height", "soybean", bundle)
    [study] = build_pseudo_studies(bundle, ("plant height",))
    [target] = study.targets
    before = {hit.gene_id for hit in overlap.known_trait_genes(bundle, profile)}
    assert target.gene_id in before
    region = overlap.Region(chrom="Gm19", start=45_100_000, end=45_200_000, assembly="Wm82.a2.v1")
    with holdout({target.gene_id}, set(target.held_out_hits)):
        assert target.gene_id not in {hit.gene_id for hit in overlap.known_trait_genes(bundle, profile)}
        assert target.hit_id not in {hit.hit_id for hit in overlap.gwas_catalog_overlap(bundle, region, profile)}
    assert {hit.gene_id for hit in overlap.known_trait_genes(bundle, profile)} == before


def test_ranks_and_recall():
    rows = [
        {"locus_id": "L1", "gene_id": "a", "distance_bp": 500, "start": 10, "rank_in_locus": 1},
        {"locus_id": "L1", "gene_id": "b", "distance_bp": 0, "start": 20, "rank_in_locus": 2},
        {"locus_id": "L2", "gene_id": "c", "distance_bp": 0, "start": 5, "rank_in_locus": 1},
    ]
    assert rank_in_locus(rows, "L1", "a", by_distance) == 2
    assert rank_in_locus(rows, "L1", "z", by_distance) is None
    assert recall([1, 2, None, 4], 3) == 0.5 and recall([], 1) is None
    low, high = recall_interval([1, 2, None, 4], 3) or (None, None)
    assert low is not None and high is not None and 0.0 <= low <= 0.5 <= high <= 1.0
    assert recall_interval([1, 1, 1], 1) == (1.0, 1.0) and recall_interval([], 1) is None


def test_the_benchmark_scores_rubric_agents_and_distance_and_measures_claims(fixture_env: FixtureBundle):
    scorecard = run_benchmark(open_bundle("soybean"), traits=("plant height",), model="agrihub-fake:poster", smoke=True)
    methods = {row["method"]: row for row in scorecard.rows}
    assert set(methods) == {"rubric_only", "rubric_no_priors", "agents"}
    assert all(row["in_locus"] and row["rank"] is not None and row["distance_rank"] is not None for row in scorecard.rows)
    summary = {row["method"]: row for row in scorecard.summary()}
    assert set(summary) == {
        "distance only",
        "rubric only (no agents)",
        "rubric without seed families / keyword priors",
        "agents (full pipeline) - pending real model",
    }
    assert all(row["targets"] == 1 for row in summary.values())
    assert all(row["ci@3"] is not None for row in summary.values())
    agents = next(run for run in scorecard.runs if run["method"] == "agents")
    assert agents["input_tokens"] > 0 and agents["seconds"] > 0
    assert agents["resolvable"] == agents["cited"] > 0
    assert agents["claims"] == agents["verified"] + agents["unverified"] + agents["contradicted"]
    assert [row["method"] for row in scorecard.smoke] == ["rubric_only", "agents"]
    assert {row["gene_id"] for row in scorecard.smoke} == {"Glyma.18G092200"}
    markdown = scorecard.markdown()
    assert "| distance only | 1 |" in markdown and "## Smoke case: poster SNPs" in markdown
    assert "| agents (full pipeline) - pending real model | 1 |" in markdown and "bootstrap 95% intervals" in markdown
    assert "unsupported rate is expected to be near 100%" in markdown
    parsed = list(csv.DictReader(io.StringIO(scorecard.csv_text())))
    assert {row["method"] for row in parsed} == {"rubric_only", "rubric_no_priors", "agents"}


def test_the_runner_writes_a_scorecard(fixture_env: FixtureBundle, tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "eval/soybean_benchmark.py", "--traits", "plant height", "--no-smoke", "--out", str(tmp_path), "--name", "card"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ},
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    card = (tmp_path / "card.md").read_text(encoding="utf-8")
    assert card.startswith("# Soybean known-gene benchmark") and "the real-model run is pending" in card
    assert (tmp_path / "card.csv").read_text(encoding="utf-8").startswith("trait,method,")
