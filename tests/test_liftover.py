"""Lee -> Wm82.a2 liftover through one-to-one pangene gene anchors."""

import csv
import random
import statistics
from pathlib import Path

import pytest
from agrihub_fixtures import FixtureBundle

from agent_platform.core.settings import get_data_paths
from agrihub.nodes.intake import check_study
from agrihub_data.bundle import close_bundles, open_bundle
from agrihub_data.query.liftover import Anchor, GeneAnchorLiftover, liftover_for
from agrihub_data.registry import load_species
from agrihub_data.verify import verify

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SISTER_TEAM = PROJECT_ROOT.parent / "Results" / "2_Sep" / "Lee"
REAL_BUNDLE = get_data_paths().data_dir / "soybean" / "bundle.duckdb"


def _anchor(gene: str, from_start: int, start: int, *, length: int = 2_000, from_strand: str = "+", strand: str = "+") -> Anchor:
    return Anchor(gene, "Gm01", from_start, from_start + length, from_strand, f"W{gene}", "Gm01", start, start + length, strand)


def _liftover(*anchors: Anchor, lengths: dict[str, int] | None = None) -> GeneAnchorLiftover:
    return GeneAnchorLiftover("Lee.gnm2", "Wm82.a2.v1", list(anchors), lengths=lengths or {})


def test_a_position_inside_an_anchor_gene_maps_by_offset_and_strand():
    lift = _liftover(_anchor("a", 100_000, 90_000), _anchor("b", 200_000, 190_000, from_strand="+", strand="-"))
    same = lift.lift("Gm01", 100_500)
    assert (same.chrom, same.pos, same.confidence, same.anchors) == ("Gm01", 90_500, "high", ("Wa",))
    assert same.method == "pangene_gene_anchor" and "inside a / Wa" in same.detail
    flipped = lift.lift("Gm01", 200_500)
    assert flipped.pos == 192_000 - 500


def test_between_anchors_the_median_interpolation_is_used_and_an_outlier_is_ignored():
    anchors = [_anchor(f"g{index}", 100_000 * index, 100_000 * index - 10_000) for index in range(1, 9)]
    anchors[1] = _anchor("g2", 200_000, 5_000_000)
    lifted = _liftover(*anchors).lift("Gm01", 450_000)
    assert lifted.ok and abs(int(lifted.pos or 0) - 440_000) <= 2_000
    assert lifted.confidence in {"high", "medium"}


def test_an_inverted_segment_interpolates_with_a_negative_slope():
    anchors = [_anchor(f"g{index}", 100_000 * index, 1_000_000 - 100_000 * index, from_strand="+", strand="-") for index in range(1, 9)]
    lifted = _liftover(*anchors).lift("Gm01", 450_000)
    assert lifted.ok and abs(int(lifted.pos or 0) - 550_000) <= 2_000


def test_unconvertible_positions_say_why():
    lift = _liftover(_anchor("a", 100_000, 90_000), _anchor("b", 200_000, 190_000), lengths={"Gm01": 200_000})
    far = lift.lift("Gm01", 5_000_000)
    assert not far.ok and "no Lee.gnm2 anchor within 1 Mb" in far.detail
    assert "no Lee.gnm2 anchors on Gm02" in lift.lift("Gm02", 10).detail
    past_end = lift.lift("Gm01", 230_000)
    assert not past_end.ok and "outside Gm01 on Wm82.a2.v1" in past_end.detail
    scrambled = _liftover(*[_anchor(f"g{index}", 100_000 * index, value) for index, value in enumerate([10, 30_000_000, 5_000_000, 20_000_000, 1_000, 25_000_000, 8_000_000, 2_000_000], 1)])
    rearranged = scrambled.lift("Gm01", 450_000)
    assert not rearranged.ok and "flanking anchors disagree" in rearranged.detail


def test_the_fixture_build_keeps_one_to_one_same_chromosome_anchors(fixture_env: FixtureBundle):
    stats = fixture_env.build_report.stats["lis_lee_gnm2"]
    assert stats["anchors"] == 5 and stats["pairs_on_other_chromosome"] == 1 and stats["genes_off_chromosomes"] == 1
    assert fixture_env.build_report.tables["lift_anchors"] == 5
    assert verify("soybean", data_dir=fixture_env.data_dir).ok
    lift = liftover_for(open_bundle("soybean"), "Lee.gnm2", "Wm82.a2.v1")
    assert lift.size == 5


def test_intake_lifts_lee_snps_before_loci_and_warns_about_the_rest(fixture_env: FixtureBundle):
    check = check_study(
        {
            "mode": "snps",
            "species": "soybean",
            "assembly": "Lee.gnm2",
            "trait_text": "plant height",
            "snps": [
                {"raw": "S18_10263941", "chrom": "18", "pos": 10_263_941, "score": 0.02, "method": "gnnexplainer"},
                {"raw": "S18_52620945", "chrom": "18", "pos": 52_620_945},
                {"raw": "S18_61000000", "chrom": "18", "pos": 61_000_000},
            ],
        }
    )
    assert not check.errors, check.detail
    assert check.study is not None and check.study.assembly == "Wm82.a2.v1"
    assert check.study.lifted_from_assembly == "Lee.gnm2"
    [snp] = check.placed
    assert (snp.chrom, snp.pos, snp.score, snp.method) == ("Gm18", 9_263_941, 0.02, "gnnexplainer")
    assert snp.lifted_from is not None
    assert snp.lifted_from.model_dump(include={"assembly", "chrom", "pos", "method", "confidence"}) == {
        "assembly": "Lee.gnm2",
        "chrom": "Gm18",
        "pos": 10_263_941,
        "method": "pangene_gene_anchor",
        "confidence": "high",
    }
    codes = [warning.code for warning in check.warnings]
    assert codes.count("unlifted") == 2 and "lifted_assembly" in codes
    summary = next(warning for warning in check.warnings if warning.code == "lifted_assembly")
    assert summary.message.startswith("1 of 3 SNPs were lifted from Lee.gnm2 to Wm82.a2.v1") and "2 could not be lifted" in summary.message


def test_many_positions_past_chromosome_ends_suggest_another_assembly(fixture_env: FixtureBundle):
    snps = [{"raw": f"S18_{9_263_941 + index}", "chrom": "18", "pos": 9_263_941 + index} for index in range(60)]
    study = {"mode": "snps", "species": "soybean", "assembly": "Wm82.a2.v1", "trait_text": "plant height"}
    past_end = [{"raw": f"S18_{60_537_871 + index}", "chrom": "18", "pos": 60_537_871 + index} for index in range(2)]
    one_typo = check_study({**study, "snps": [*snps, past_end[0]]})
    assert "assembly_mismatch_suspected" not in {warning.code for warning in one_typo.warnings}
    mismatched = check_study({**study, "snps": [*snps, *past_end]})
    warning = next(item for item in mismatched.warnings if item.code == "assembly_mismatch_suspected")
    assert warning.message.startswith("2 of 62 positions (3%) fall past chromosome ends on Wm82.a2.v1")


def _sister_rows() -> list[dict[str, str]]:
    return [row for path in sorted(SISTER_TEAM.glob("*.csv")) for row in csv.DictReader(path.open(encoding="utf-8"))]


@pytest.mark.bundle
@pytest.mark.skipif(not REAL_BUNDLE.exists() or not SISTER_TEAM.is_dir(), reason="needs the soybean bundle and the sister-team CSVs")
def test_sister_team_positions_lift_from_lee_gnm2_and_agree_with_their_own_wm82_ranges():
    registry = load_species("soybean")
    lengths = {chromosome.name: chromosome.length for chromosome in registry.assembly("Wm82.a2.v1").chromosomes}
    try:
        lift = liftover_for(open_bundle("soybean"), "Lee.gnm2", "Wm82.a2.v1", lengths)
        rows = _sister_rows()
        lee = registry.assembly("Lee.gnm2")
        assert all(int(row["pos"]) <= (lee.chromosome(f"Gm{int(row['chrom']):02d}") or lee.chromosomes[0]).length for row in rows)
        results = [(row, lift.lift(f"Gm{int(row['chrom']):02d}", int(row["pos"]))) for row in rows]
        converted = [(row, result) for row, result in results if result.ok]
        assert len(converted) >= 0.99 * len(rows)
        ranged = [(row, result) for row, result in converted if row.get("Wm82_start")]
        agree = sum(1 for row, result in ranged if int(row["Wm82_start"]) - 250_000 <= int(result.pos or 0) <= int(row["Wm82_end"]) + 250_000)
        assert agree >= 0.9 * len(ranged)
        sample = random.Random(7).sample(list(lift._by_chrom["Gm18"]), 200)
        errors = []
        for anchor in sample:
            probe = lift.lift(anchor.from_chrom, anchor.from_end + 20_000, exclude=anchor.from_gene_id)
            truth = anchor.end + 20_000 if anchor.strand == anchor.from_strand else anchor.start - 20_000
            if probe.confidence == "high":
                errors.append(abs(int(probe.pos or 0) - truth))
        assert len(errors) >= 150 and statistics.median(errors) < 5_000
    finally:
        close_bundles()
