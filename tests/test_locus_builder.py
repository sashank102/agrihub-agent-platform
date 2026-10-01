"""Fixed windows merge into loci; genes are listed nearest first and capped."""

import asyncio
import uuid
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle

from agrihub.evidence_store import EvidenceStore
from agrihub.nodes.intake import place_snps, window_warnings
from agrihub.nodes.locus_builder import (
    build_loci,
    genes_at_flank,
    load_candidates,
    locus_builder,
)
from agrihub.state import SnpInput, parse_study
from agrihub_data.bundle import open_bundle
from agrihub_data.registry import load_species

GM18_LENGTH = 58_018_742
FLANK = 250_000


def _snp(raw: str, chrom: str, pos: int, **extra: Any) -> SnpInput:
    return SnpInput(raw=raw, chrom=chrom, pos=pos, **extra)


def _loci(*snps: SnpInput, flank: int = FLANK):
    return build_loci("soybean", "Wm82.a2.v1", list(snps), flank)


def test_overlapping_windows_merge_and_keep_the_strongest_snp_as_lead():
    loci = _loci(
        _snp("S18_9263941", "Gm18", 9_263_941, p_value=1e-6),
        _snp("S18_9300000", "Gm18", 9_300_000, p_value=1e-9),
        _snp("S5_2899164", "Gm05", 2_899_164),
    )
    assert [(locus.locus_id, locus.chrom) for locus in loci] == [("L1", "Gm05"), ("L2", "Gm18")]
    merged = loci[1]
    assert (merged.start, merged.end) == (9_263_941 - FLANK, 9_300_000 + FLANK)
    assert merged.lead_snp == "S18_9300000" and merged.supporting_snps == ["S18_9263941"]
    assert merged.merged_from == ["S18_9263941", "S18_9300000"]
    assert merged.snp_positions == {"S18_9263941": 9_263_941, "S18_9300000": 9_300_000}
    assert loci[0].merged_from == [] and loci[0].lead_pos == 2_899_164


def test_touching_windows_merge_but_a_one_bp_gap_does_not():
    first = 1_000_000
    touching = _loci(_snp("a", "Gm02", first), _snp("b", "Gm02", first + 2 * FLANK + 1))
    assert len(touching) == 1 and touching[0].merged_from == ["a", "b"]
    apart = _loci(_snp("a", "Gm02", first), _snp("b", "Gm02", first + 2 * FLANK + 2))
    assert [(locus.start, locus.end) for locus in apart] == [
        (first - FLANK, first + FLANK),
        (first + FLANK + 2, first + 3 * FLANK + 2),
    ]


def test_loci_follow_chromosome_order_and_without_p_values_the_score_picks_the_lead():
    loci = _loci(
        _snp("ten", "Gm10", 5_000_000),
        _snp("two", "Gm02", 5_000_000, score=0.2),
        _snp("two_b", "Gm02", 5_100_000, score=0.9),
    )
    assert [locus.chrom for locus in loci] == ["Gm02", "Gm10"]
    assert loci[0].lead_snp == "two_b"


def test_windows_are_clamped_at_chromosome_ends():
    start, end = _loci(_snp("start", "Gm18", 100), _snp("end", "Gm18", GM18_LENGTH - 10))
    assert (start.start, start.end) == (1, 100 + FLANK)
    assert (end.start, end.end) == (GM18_LENGTH - 10 - FLANK, GM18_LENGTH)


def _run_node(study: dict[str, Any]) -> tuple[dict[str, Any], EvidenceStore]:
    run_id = uuid.uuid4().hex
    parsed = parse_study(study)
    placed, _ = place_snps(parsed, list(parsed.snps))
    state = {"study": parsed.model_dump(mode="json"), "snps": [snp.model_dump(mode="json") for snp in placed]}
    result = asyncio.run(locus_builder(state, {"configurable": {"run_id": run_id}}))
    return result, EvidenceStore.for_run(run_id)


STUDY = {
    "mode": "snps",
    "species": "soybean",
    "assembly": "Wm82.a2.v1",
    "trait_text": "plant height",
    "snps": [
        {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_941},
        {"raw": "S18_9300000", "chrom": "18", "pos": 9_300_000},
        {"raw": "S18_end", "chrom": "Gm18", "pos": GM18_LENGTH - 1_000},
    ],
}


def test_genes_are_nearest_to_any_snp_first_and_stored_as_positional_evidence(fixture_env: FixtureBundle):
    result, store = _run_node(STUDY)
    l1, l2 = result["loci"]
    assert (l1["n_genes"], l2["n_genes"]) == (3, 0)
    assert l2["end"] == GM18_LENGTH
    refs = [ref for ref in result["candidates"] if ref["locus_id"] == "L1"]
    assert [(ref["gene_id"], ref["distance_bp"], ref.get("nearest_snp")) for ref in refs] == [
        ("Glyma.18G092200", 0, "S18_9263941"),
        ("Glyma.18G092300", 14_604, "S18_9263941"),
        ("Glyma.18G092000", 22_436, "S18_9263941"),
    ]
    genes = load_candidates(result["candidates"], store)
    assert (genes[0].chrom, genes[0].start, genes[0].end, genes[0].strand) == ("Gm18", 9_262_392, 9_267_008, "-")
    assert genes[0].overlaps_snp and genes[0].defline.startswith("WRKY")
    positional = store.query(["Glyma.18G092200"], ["positional"])
    assert [item.subtype for item in positional] == ["in_window"]
    store.close()


def test_the_per_locus_cap_keeps_the_nearest_genes_and_warns(fixture_env: FixtureBundle):
    result, store = _run_node({**STUDY, "max_genes_per_locus": 1})
    assert result["loci"][0]["n_genes"] == 1 and result["loci"][0]["genes_capped"] is True
    assert [ref["gene_id"] for ref in result["candidates"]] == ["Glyma.18G092200"]
    assert [warning["code"] for warning in result["warnings"]] == ["genes_capped"]
    store.close()


def test_genes_at_a_smaller_flank_are_a_subset_with_the_same_distances(fixture_env: FixtureBundle):
    result, store = _run_node(STUDY)
    study = parse_study(STUDY)
    locus = _loci(*place_snps(study, list(study.snps))[0])[0]
    near = genes_at_flank(open_bundle("soybean"), locus, 10_000)
    assert [(gene.gene_id, gene.distance_bp) for gene in near] == [("Glyma.18G092200", 0)]
    wide = genes_at_flank(open_bundle("soybean"), locus, FLANK)
    assert [gene.gene_id for gene in wide] == [ref["gene_id"] for ref in result["candidates"] if ref["locus_id"] == "L1"]
    store.close()


def test_a_window_wider_than_twice_the_typical_ld_is_flagged():
    registry = load_species("soybean")
    assert window_warnings(registry, parse_study(STUDY)) == []
    wide = parse_study({**STUDY, "window": {"mode": "fixed", "flank_bp": 400_000}})
    [warning] = window_warnings(registry, wide)
    assert warning.code == "window_exceeds_ld" and "150 kb" in warning.message


@pytest.mark.parametrize(
    ("snps", "codes"),
    [
        (
            [
                {"raw": "S5_2899164", "chrom": "Gm05", "pos": 2_899_164},
                {"raw": "S5_2899500", "chrom": "5", "pos": 2_899_500},
                {"raw": "bad", "chrom": "Gm99", "pos": 10},
            ],
            {"chromosome_aliases", "unknown_chromosome"},
        ),
        (
            [
                {"raw": "S18_9263941", "chrom": "18", "pos": 9_263_000},
                {"raw": "BARC_1.01_Gm18_9199987_A_G", "marker_id": "BARC_1.01_Gm18_9199987_A_G"},
                {"raw": "ss715631025", "marker_id": "ss715631025"},
            ],
            {"position_mismatch", "a1_position", "duplicate"},
        ),
        (
            [
                {"raw": "18 9263941"},
                {"raw": "BARC_1.01_Gm18_9199987_A_G"},
                {"raw": "ss715631025"},
                {"raw": "S18_99999999"},
            ],
            {"a1_position", "duplicate", "out_of_bounds"},
        ),
    ],
)
def test_intake_placement_warnings(fixture_env: FixtureBundle, snps: list[dict[str, Any]], codes: set[str]):
    study = parse_study({**STUDY, "snps": snps})
    placed, warnings = place_snps(study, list(study.snps))
    assert {warning.code for warning in warnings} == codes
    assert {snp.chrom for snp in placed} <= {"Gm05", "Gm18"}
    if "a1_position" in codes:
        barc = next(snp for snp in placed if snp.raw.startswith("BARC"))
        assert (barc.chrom, barc.pos) == ("Gm18", 9_250_001)
    if "out_of_bounds" in codes:
        assert ("18 9263941", "Gm18", 9_263_941) in {(snp.raw, snp.chrom, snp.pos) for snp in placed}
