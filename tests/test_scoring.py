"""The deterministic rubric: QTL and GWAS weighting."""

from typing import Any

from agrihub import scoring

NARROW_TRAIT_QTL = {
    "kind": "interval",
    "placement": "markers",
    "n_markers_placed": 3,
    "span_bp": 600_000,
    "wide": False,
    "overlap_type": "qtl_contains_window",
    "trait_match": "ontology",
    "distance_to_core": 0,
}


def _qtl(**changes: Any) -> float:
    return scoring.qtl_points({**NARROW_TRAIT_QTL, **changes})[0]


def _gwas(**changes: Any) -> float:
    hit = {"trait_match": "ontology", "p_value": 1e-9, "distance_to_core": 0, **changes}
    return scoring.gwas_points(hit)[0]


def test_wide_and_single_marker_qtls_count_less_than_a_narrow_multi_marker_trait_qtl():
    narrow = _qtl()
    wide = _qtl(span_bp=5_500_000, wide=True, n_markers_placed=2)
    single = _qtl(kind="marker", placement="single_marker", n_markers_placed=1, span_bp=201, distance_to_core=0)
    far_single = _qtl(kind="marker", placement="single_marker", n_markers_placed=1, distance_to_core=40_000)
    assert narrow == scoring.load_rubric().qtl.base
    assert narrow > single > far_single > 0
    assert narrow > wide > 0
    assert _qtl(kind="marker", distance_to_core=80_000) == 0
    assert _qtl(trait_match="keyword") < narrow and _qtl(trait_match="none") == 0
    assert _qtl(overlap_type="partial") < narrow
    assert "(wide)" in scoring.qtl_points({**NARROW_TRAIT_QTL, "wide": True, "span_bp": 6_000_000})[1]


def test_gwas_hits_weigh_distance_trait_match_and_p_value():
    inside = _gwas()
    assert inside > _gwas(distance_to_core=8_000) > _gwas(distance_to_core=30_000) > _gwas(distance_to_core=200_000) > 0
    assert _gwas(trait_match="keyword") < inside and _gwas(trait_match="none") == 0
    assert _gwas(p_value=1e-6) < inside and _gwas(p_value=None) < _gwas(p_value=1e-6)
    assert "in_gene" in scoring.gwas_points({"trait_match": "ontology", "distance_to_core": 0})[1]
