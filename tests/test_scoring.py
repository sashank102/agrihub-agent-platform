"""The deterministic rubric: categories, independence rules, tiers and stability."""

import random
from typing import Any

import pytest

from agrihub import scoring
from agrihub.evidence_store import evidence_id_for
from agrihub.state import CandidateGene, EvidenceItem, Finding, Locus, OrthologRef
from agrihub_data.query.traits import TraitProfile

PROFILE = TraitProfile(
    query="plant height",
    key="plant_height",
    species="soybean",
    keywords=["plant height", "dwarf", "determinate", "gibberellin"],
    expanded_ids=["GO:0009686", "GO:0010022"],
    seed_families=["TFL1", "GA20ox"],
)
LD_KB = 150.0
TFL1 = OrthologRef(species="arabidopsis", gene_id="AT5G03840", relation="many2one", n_methods=4, confidence="high")
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


def _gene(gene_id: str, distance: int = 0, locus: str = "L1", defline: str = "protein") -> CandidateGene:
    return CandidateGene(
        gene_id=gene_id, locus_id=locus, chrom="Gm18", start=1, end=2, distance_bp=distance, defline=defline
    )


def _item(gene_id: str, category: str, subtype: str, value: Any = None, **extra: Any) -> EvidenceItem:
    item = EvidenceItem(
        gene_id=gene_id,
        category=category,  # type: ignore[arg-type]
        subtype=subtype,
        value=value,
        source_db=extra.pop("source_db", "test"),
        db_version="1",
        source_record=extra.pop("record", f"{gene_id}:{subtype}"),
        **extra,
    )
    return item.model_copy(update={"evidence_id": evidence_id_for(item)})


def _known(gene_id: str, match: str = "ontology", pmids: list[str] | None = None) -> EvidenceItem:
    return _item(
        gene_id,
        "known_gene",
        f"known_gene:{match}",
        {
            "gene_id": gene_id,
            "symbols": ["GmDT1"],
            "trait_match": match,
            "matched": ["CO_336:0000027"],
            "trait_names": ["seed oil content"],
            "confidence": 5,
            "pmids": pmids or [],
            "dois": [],
            "weight": 1.0,
        },
        primary_citation=f"PMID:{pmids[0]}" if pmids else None,
    )


def _phenotype(gene_id: str, text: str, pmid: str | None = None, via: OrthologRef = TFL1) -> EvidenceItem:
    return _item(
        gene_id,
        "ortholog",
        "tair:phenotype",
        {"phenotype": text, "pmid": pmid},
        record=f"TAIR:{via.gene_id}:{text}",
        source_db="TAIR",
        quote=text,
        via_ortholog=via,
        primary_citation=f"PMID:{pmid}" if pmid else None,
    )


def _relevance(gene_id: str, kind: str, term: str, field: str, code: str | None = None) -> EvidenceItem:
    return _item(
        gene_id,
        "functional_annotation",
        f"relevance:{kind}:{term}",
        {"kind": kind, "term": term, "field": field, "evidence_code": code, "weight": 1.0},
        evidence_code=code,
    )


def _score(genes: list[CandidateGene], items: list[EvidenceItem], available: set[str] | None = None) -> scoring.StudyScores:
    return scoring.score_candidates(genes, items, profile=PROFILE, ld_kb=LD_KB, available=available if available is not None else {"A"})


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


def test_identical_inputs_give_identical_scores_in_any_order():
    genes = [_gene("g1", 0), _gene("g2", 40_000), _gene("g3", 90_000, defline="retrotransposon gag-pol")]
    items = [
        _known("g1", pmids=["111"]),
        _phenotype("g2", "dwarf plants", pmid="222"),
        _relevance("g2", "go", "GO:0009686", "go", "IEA"),
        _item("g3", "association", "gwas:GWAS Atlas", {"trait_match": "ontology", "p_value": 1e-9, "distance_to_core": 0}),
    ]
    first = _score(genes, items).model_dump()
    shuffled_items, shuffled_genes = items[:], genes[:]
    random.Random(7).shuffle(shuffled_items)
    random.Random(7).shuffle(shuffled_genes)
    assert _score(shuffled_genes, shuffled_items).model_dump() == first


def test_a_pmid_shared_by_b_and_c_is_credited_once():
    genes = [_gene("dt1"), _gene("control", locus="L2")]
    items = [
        _known("dt1", pmids=["20421496"]),
        _phenotype("dt1", "determinate inflorescence", pmid="20421496"),
        _phenotype("control", "determinate inflorescence", pmid="20421496"),
    ]
    scores = _score(genes, items)
    dt1 = scores.genes["dt1"]
    assert dt1.categories["B"].points == 25 and dt1.categories["C"].points == 0
    assert [item.reason for item in dt1.categories["C"].dropped] == ["PMID:20421496 is already credited in B"]
    assert scores.genes["control"].categories["C"].points > 0


def test_positional_only_genes_are_t4_and_curated_trait_genes_are_t1():
    genes = [_gene("near", 0), _gene("dt1", 100_000), _gene("other_trait", 0)]
    scores = _score(genes, [_known("dt1"), _known("other_trait", match="none")])
    near = scores.genes["near"]
    assert near.tier == "T4" and near.categories["A"].points == 20
    assert all(category.points == 0 for code, category in near.categories.items() if code != "A")
    assert scores.genes["dt1"].tier == "T1"
    other = scores.genes["other_trait"]
    assert other.tier == "T4" and other.categories["B"].points == 0
    assert "other traits" in other.categories["B"].dropped[0].reason


def test_t2_needs_ortholog_function_near_the_snp_and_t3_is_functional_only():
    genes = [_gene("close", 20_000), _gene("far", 300_000, locus="L3"), _gene("keyword", 300_000, locus="L2")]
    items = [
        _phenotype("close", "dwarf, determinate"),
        _phenotype("far", "dwarf, determinate"),
        _relevance("keyword", "keyword", "gibberellin", "defline"),
    ]
    scores = _score(genes, items)
    assert scores.genes["close"].categories["C"].points >= 12 and scores.genes["close"].tier == "T2"
    assert scores.genes["far"].tier == "T3"
    assert scores.genes["keyword"].tier == "T3" and scores.genes["keyword"].categories["D"].points == 4


def test_projected_go_and_curated_symbols_are_not_counted_twice():
    items = [
        _phenotype("g1", "dwarf"),
        _relevance("g1", "go", "GO:0009686", "go", "IEA"),
        _relevance("g1", "go", "GO:0010022", "go", "IMP"),
        _known("g2"),
        _relevance("g2", "family", "TFL1", "symbol"),
    ]
    scores = _score([_gene("g1"), _gene("g2", locus="L2")], items)
    g1 = scores.genes["g1"].categories["D"]
    assert g1.points == 10 and [item.reason for item in g1.dropped] == [
        "computational GO is not independent of the ortholog credited in C"
    ]
    g2 = scores.genes["g2"].categories["D"]
    assert g2.points == 0 and "curated symbol" in g2.dropped[0].reason


def test_within_a_category_the_runner_up_counts_a_quarter():
    rubric = scoring.load_rubric()
    items = [
        _item("g1", "association", "qtl:qtl_contains_window", NARROW_TRAIT_QTL, record="q1"),
        _item("g1", "association", "qtl:partial", {**NARROW_TRAIT_QTL, "overlap_type": "partial"}, record="q2"),
        _item("g1", "association", "qtl:partial", {**NARROW_TRAIT_QTL, "overlap_type": "partial"}, record="q3"),
    ]
    g = _score([_gene("g1")], items).genes["g1"].categories["G"]
    expected = min(rubric.categories["G"].max, _qtl() + rubric.second_best * _qtl(overlap_type="partial"))
    assert g.points == pytest.approx(expected, abs=0.01) and len(g.evidence_ids) == 3


def test_paralogs_in_one_locus_share_ortholog_credit_and_are_flagged():
    alone = _score([_gene("a")], [_phenotype("a", "dwarf")]).genes["a"].categories["C"].points
    shared = _score([_gene("a"), _gene("b", 5_000)], [_phenotype("a", "dwarf"), _phenotype("b", "dwarf")])
    assert shared.genes["a"].categories["C"].points == pytest.approx(alone / 2**0.5, abs=0.01)
    assert shared.genes["a"].flags == ["paralog: shares AT5G03840 ortholog credit with b"]
    other_locus = _score([_gene("a"), _gene("b", locus="L2")], [_phenotype("a", "dwarf"), _phenotype("b", "dwarf")])
    assert other_locus.genes["a"].categories["C"].points == alone and not other_locus.genes["a"].flags


def test_shares_are_a_softmax_per_locus_and_missing_categories_are_not_available():
    genes = [_gene("g1", 0), _gene("g2", 50_000), _gene("g3", 0, locus="L2", defline="Retrotransposon protein")]
    scores = _score(genes, [_known("g2")])
    assert sum(gene.share_of_locus for gene in scores.ranked("L1")) == pytest.approx(1.0, abs=1e-3)
    assert [gene.gene_id for gene in scores.ranked("L1")] == ["g2", "g1"]
    assert scores.genes["g3"].share_of_locus == 1.0
    assert scores.genes["g3"].score == 15 and scores.genes["g3"].penalties[0].points == 5
    for code in ("E", "F"):
        category = scores.genes["g1"].categories[code]
        assert category.available is False and category.points == 0 and category.note


def test_explain_score_lists_evidence_ids_per_category():
    items = [_known("dt1", pmids=["1"]), _phenotype("dt1", "dwarf", pmid="2"), _item("dt1", "positional", "in_window", {})]
    scores = _score([_gene("dt1", 10_000)], items)
    explained = scoring.explain_score(scores, "dt1")
    assert explained["tier"] == "T1" and explained["rank_in_locus"] == 1
    assert explained["categories"]["A"]["evidence_ids"] == [items[2].evidence_id]
    assert explained["categories"]["B"]["evidence_ids"] == [items[0].evidence_id]
    assert explained["categories"]["C"]["evidence_ids"] == [items[1].evidence_id]
    assert explained["categories"]["E"]["available"] is False
    assert set(scores.genes["dt1"].evidence_ids()) == {item.evidence_id for item in items}
    with pytest.raises(KeyError):
        scoring.explain_score(scores, "missing")


def test_window_sensitivity_reranks_each_locus_at_every_flank():
    locus = Locus(locus_id="L1", lead_snp="s", chrom="Gm18", start=1, end=500_001, assembly="Wm82.a2.v1", window_method="fixed")
    near, mid, far = _gene("near", 0), _gene("mid", 80_000), _gene("far", 200_000)
    scores = _score([near, mid, far], [_known("far")])
    members = {50_000: [near], 100_000: [near, mid], 250_000: [near, mid, far]}
    rescored: list[list[str]] = []

    def rescore(genes: list[CandidateGene]) -> scoring.StudyScores:
        rescored.append([gene.gene_id for gene in genes])
        return _score(genes, [])

    result = scoring.window_sensitivity([locus], scores, lambda _, flank: members[flank], rescore, top_k=1)
    assert rescored == []
    assert result.loci[0].top == {"50kb": ["near"], "100kb": ["near"], "250kb": ["far"]}
    assert result.genes["far"].ranks == {"50kb": None, "100kb": None, "250kb": 1}
    assert result.genes["far"].stable is False and result.genes["far"].label == "top 1 only at 250kb"
    assert result.genes["near"].label == "top 1 only at 50kb, 100kb"
    extra = _gene("extra", 1_000)
    members[250_000] = [near, mid, far, extra]
    scoring.window_sensitivity([locus], scores, lambda _, flank: members[flank], rescore, top_k=1)
    assert rescored == [["extra"]]


def _expression(gene_id: str, dataset: str = "Sreedasyam_Plott_2023", **value: Any) -> list[EvidenceItem]:
    profile = {"max_value": value.get("max_value", 40.0), "trait_max_value": value.get("trait_max_value", 40.0), "trait_max_sample": "shoot_tip.standard"}
    specificity = {"tau": value.get("tau", 0.9), "trait_tissue_top": value.get("top", True), "top_tissue": "shoot_tip", "trait_z": value.get("z", 2.0)}
    for key in ("tissue_means", "trait_tissues", "trait_fold"):
        if key in value:
            specificity[key] = value[key]
    return [
        _item(gene_id, "expression", f"expression_profile:{dataset}", profile, record=f"{dataset}:{gene_id}:profile"),
        _item(gene_id, "expression", f"tissue_specificity:{dataset}", specificity, record=f"{dataset}:{gene_id}:tau"),
    ]


def test_expression_scores_trait_tissue_level_and_specificity():
    genes = [_gene("specific"), _gene("enriched"), _gene("low"), _gene("silent")]
    items = [
        *_expression("specific"),
        *_expression("enriched", tau=0.4, top=False, z=1.8, trait_max_value=4.0),
        *_expression("low", tau=0.1, top=False, z=0.1, trait_max_value=0.6, max_value=2.0),
        *_expression("silent", tau=None, top=False, z=None, trait_max_value=0.1, max_value=0.2),
    ]
    scores = _score(genes, items, {"A", "E", "F"})
    assert scores.genes["specific"].categories["E"].points == 8 + 0.25 * 2
    # tau 0.4 is below the enrichment floor and has no 2x fold, so only the trait-TPM band counts.
    assert scores.genes["enriched"].categories["E"].points == 1
    assert scores.genes["low"].categories["E"].points == 0
    assert [penalty.points for penalty in scores.genes["silent"].penalties] == [3.0]
    assert scores.genes["specific"].categories["E"].available and scores.genes["specific"].tier == "T4"


def test_network_scores_seed_propagation_and_seed_neighbours():
    items = [
        _item("near", "network", "seed_propagation:string", {"empirical_p": 0.004, "trait_key": "plant_height", "network": "string"}),
        _item("near", "network", "coexpression:atted:seed1", {"neighbor_is_seed": True, "neighbor_id": "seed1", "score": 6.5}),
        _item("weak", "network", "seed_propagation:string", {"empirical_p": 0.08, "trait_key": "plant_height", "network": "string"}),
        _item("seed", "network", "seed_propagation:string", {"empirical_p": 0.001, "is_seed": True}),
        _item("far", "network", "neighbor:string:x", {"neighbor_is_seed": False, "neighbor_id": "x", "score": 0.9}),
    ]
    scores = _score([_gene(name) for name in ("near", "weak", "seed", "far")], items, {"A", "E", "F"})
    assert scores.genes["near"].categories["F"].points == 10
    assert scores.genes["weak"].categories["F"].points == 3
    assert scores.genes["seed"].categories["F"].points == 0 and scores.genes["far"].categories["F"].points == 0


def test_ld_r2_replaces_distance_and_variants_add_bonuses():
    far = _gene("far", 120_000)
    items = [
        _item("far", "positional", "ld_r2:Song_Hyten_2015", {"r2": 0.9, "lead": "S18_9263941", "genotypes": "Song_Hyten_2015"}),
        _item("far", "variant", "consequence:missense_variant", {"impact": "MODERATE", "variant": "S18_1", "consequences": [{"terms": ["missense_variant"]}]}),
        _item("far", "regulation", "tfbs_hit:t1", {"snp": "S18_1", "relation": "promoter"}),
        _item("utr", "variant", "location:five_prime_UTR", {"location_class": "five_prime_UTR", "variant": "S18_2"}),
        _item("intron", "variant", "location:intron", {"location_class": "intron", "variant": "S18_3"}),
    ]
    scores = _score([far, _gene("utr", 60_000), _gene("intron", 0)], items)
    a = scores.genes["far"].categories["A"]
    assert a.points == 20.0 and a.reasons[0].startswith("r2 0.9 with S18_9263941") and len(a.evidence_ids) == 3
    distance_only = _score([_gene("far", 120_000)], []).genes["far"].categories["A"].points
    assert scores.genes["utr"].categories["A"].points == pytest.approx(round(20 * 2.718281828 ** (-60_000 / 150_000) + 2, 2), abs=0.01)
    assert scores.genes["intron"].categories["A"].points == 20.0 and distance_only < 10
    weak_ld = _item("near", "positional", "ld_r2:Song_Hyten_2015", {"r2": 0.3, "lead": "S18_9263941", "genotypes": "Song_Hyten_2015"})
    near = _score([_gene("near", 0)], [weak_ld]).genes["near"].categories["A"]
    assert near.points == 20.0 and weak_ld.evidence_id not in near.evidence_ids


def test_trait_matching_pathways_score_in_d():
    item = _item("ga", "functional_annotation", "pathway:PMN SoyCyc:PWY-5070", {"pathway_name": "gibberellin biosynthesis I", "matched": ["gibberellin"]})
    other = _item("ga", "functional_annotation", "pathway:PMN SoyCyc:PWY-1", {"pathway_name": "starch degradation", "matched": []})
    scores = _score([_gene("ga")], [item, other])
    assert scores.genes["ga"].categories["D"].points == 4.0 and scores.genes["ga"].categories["D"].evidence_ids == [item.evidence_id]


def test_low_tau_expression_and_housekeeping_specificity_stay_within_the_trait_band():
    broad = _expression("broad", tau=0.24, top=False, z=3.0, trait_max_value=40.0)
    folded = _expression(
        "folded",
        tau=0.42,
        top=True,
        z=1.8,
        trait_max_value=4.0,
        tissue_means={"shoot_tip": 3.0, "root": 1.0, "leaf": 1.0},
        trait_tissues=["shoot_tip"],
    )
    quiet = _expression("quiet", tau=0.09, top=True, z=2.0, trait_max_value=20.0)
    genes = [
        _gene("broad", defline="50S ribosomal protein L22"),
        _gene("folded", defline="ribosomal RNA methyltransferase"),
        _gene("quiet", defline="arginine/serine-rich coiled coil protein"),
    ]
    scores = _score(genes, [*broad, *folded, *quiet], {"A", "E"})
    assert scores.genes["broad"].categories["E"].points <= 2
    assert scores.genes["quiet"].categories["E"].points <= 2
    assert scores.genes["folded"].categories["E"].points <= 2
    assert scores.genes["broad"].tier == "T4"
    with_function = _score(
        [_gene("both")],
        [*_expression("both"), _relevance("both", "keyword", "gibberellin", "defline")],
        {"A", "D", "E"},
    )
    assert with_function.genes["both"].tier == "T3"


def test_panel_ld_leaves_the_snp_containing_gene_ahead_of_an_ld_neighbour():
    contained = _gene("snp", 0).model_copy(update={"overlaps_snp": True})
    neighbour = _gene("neighbour", 22_000)

    def linked(gene_id: str) -> EvidenceItem:
        return _item(gene_id, "positional", "ld_r2:panel", {"r2": 1.0, "lead": "S18_9263941", "genotypes": "panel"})

    scores = _score([contained, neighbour], [linked("snp"), linked("neighbour")])
    snp_a = scores.genes["snp"].categories["A"].points
    neighbour_a = scores.genes["neighbour"].categories["A"].points
    assert snp_a == 20.0
    assert neighbour_a <= 20 * 0.85 + 0.5
    assert snp_a > neighbour_a
    nonspecific = _expression("neighbour", tau=0.2, top=False, z=2.2, trait_max_value=30.0)
    compared = _score([contained, neighbour], [linked("snp"), linked("neighbour"), *nonspecific], {"A", "E"})
    assert compared.genes["snp"].score >= compared.genes["neighbour"].score


def test_supporting_findings_add_evidence_once_and_conflicts_are_recorded():
    profile = _expression("g1")
    finding = Finding(
        finding_id="F1",
        agent_id="lane",
        target="g1",
        claim="expressed in the trait tissue",
        stance="supports",
        strength="weak",
        evidence_ids=[item.evidence_id or "" for item in profile],
    )
    again = Finding(
        finding_id="F2",
        agent_id="lane",
        target="g1",
        claim="the same expression fact",
        stance="supports",
        strength="weak",
        evidence_ids=[item.evidence_id or "" for item in profile],
    )
    base = _score([_gene("g1")], profile, {"A", "E"})
    doubled = scoring.score_candidates(
        [_gene("g1")],
        profile,
        profile=PROFILE,
        ld_kb=LD_KB,
        available={"A", "E"},
        findings=[finding, again],
        finding_items=profile,
    )
    assert doubled.genes["g1"].score == base.genes["g1"].score
    via_finding = scoring.score_candidates(
        [_gene("g1")],
        [],
        profile=PROFILE,
        ld_kb=LD_KB,
        available={"A", "E"},
        findings=[finding],
        finding_items=profile,
    )
    assert via_finding.genes["g1"].categories["E"].points == base.genes["g1"].categories["E"].points
    conflict = finding.model_copy(update={"finding_id": "F9", "stance": "conflicts", "claim": "the passage disagrees"})
    flagged = scoring.score_candidates(
        [_gene("g1")],
        profile,
        profile=PROFILE,
        ld_kb=LD_KB,
        available={"A", "E"},
        findings=[conflict],
    )
    assert flagged.genes["g1"].score == base.genes["g1"].score
    assert "conflict: F9" in flagged.genes["g1"].flags
