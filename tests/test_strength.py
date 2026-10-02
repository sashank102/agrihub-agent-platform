"""Finding strength is capped by what the cited evidence can support."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agrihub import strength
from agrihub.evidence_store import EvidenceStore
from agrihub.state import EvidenceItem, OrthologRef
from agrihub.tools import agent_tools


def _item(category: str, subtype: str, value: dict[str, Any], **extra: Any) -> EvidenceItem:
    return EvidenceItem(
        gene_id="Glyma.18G092200",
        category=category,  # type: ignore[arg-type]
        subtype=subtype,
        value=value,
        source_db="test",
        db_version="1",
        source_record=f"{subtype}:{sorted(value.items())}",
        **extra,
    )


WIDE_QTL = _item(
    "association",
    "qtl:qtl_contains_window",
    {"kind": "interval", "span_bp": 5_500_000, "n_markers_placed": 2, "wide": True, "trait_match": "ontology", "qtl_name": "Plant height 3-6"},
)
NARROW_QTL = _item("association", "qtl:partial", {"kind": "interval", "span_bp": 800_000, "n_markers_placed": 3, "wide": False, "trait_match": "ontology"})
SINGLE_MARKER_QTL = _item("association", "qtl:marker_within", {"kind": "marker", "span_bp": 1, "n_markers_placed": 1, "trait_match": "ontology"})
KEYWORD_QTL = _item("association", "qtl:partial", {"kind": "interval", "span_bp": 300_000, "n_markers_placed": 2, "trait_match": "keyword"})
NEAR_GWAS = _item("association", "gwas:GWAS Atlas", {"distance_to_core": 12_000, "trait_match": "ontology"})
FAR_GWAS = _item("association", "gwas:GWAS Atlas", {"distance_to_core": 80_000, "trait_match": "ontology"})
KEYWORD_GWAS = _item("association", "gwas:SoyBase", {"distance_to_core": 0, "trait_match": "keyword"})


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([WIDE_QTL], "weak"),
        ([SINGLE_MARKER_QTL], "weak"),
        ([KEYWORD_QTL], "weak"),
        ([NARROW_QTL], "moderate"),
        ([NEAR_GWAS], "moderate"),
        ([FAR_GWAS], "weak"),
        ([KEYWORD_GWAS], "weak"),
        ([NARROW_QTL, NEAR_GWAS], "strong"),
        ([WIDE_QTL, NEAR_GWAS], "moderate"),
        ([_item("known_gene", "known_gene:ontology", {"trait_match": "ontology", "confidence": 4})], "strong"),
        ([_item("known_gene", "known_gene:keyword", {"trait_match": "keyword", "confidence": 4})], "moderate"),
        ([_item("known_gene", "known_gene:none", {"trait_match": "none", "confidence": 5})], "weak"),
        ([_item("functional_annotation", "relevance:keyword:dwarf", {"kind": "keyword"})], "weak"),
        ([_item("functional_annotation", "go:GO:0009686", {"id": "GO:0009686"}, evidence_code="IEA")], "weak"),
        ([_item("functional_annotation", "go:GO:0009686", {"id": "GO:0009686"}, evidence_code="IMP")], "moderate"),
        ([_item("positional", "in_window", {"dist_to_snp": 0})], "weak"),
        ([_item("expression", "tissue_specificity:Sreedasyam", {"tau": 0.91, "trait_tissue_top": True})], "moderate"),
        ([_item("expression", "tissue_specificity:Sreedasyam", {"tau": 0.91, "trait_tissue_top": False})], "weak"),
        ([_item("network", "seed_propagation:string", {"empirical_p": 0.004})], "moderate"),
        ([_item("network", "seed_propagation:string", {"empirical_p": 0.2})], "weak"),
        ([_item("variant", "consequence:missense_variant", {"impact": "MODERATE"})], "moderate"),
        ([_item("variant", "location:intron", {"location_class": "intron"})], "weak"),
        ([_item("literature", "passage:causal-experimental", {})], "strong"),
        ([_item("literature", "passage:association", {})], "moderate"),
        ([_item("literature", "gene2pubmed", {})], "weak"),
        ([], "weak"),
    ],
)
def test_max_strength_follows_the_evidence(items: list[EvidenceItem], expected: str):
    assert strength.max_strength(items).strength == expected


def test_orthology_confidence_decides_transferred_phenotypes():
    medium = OrthologRef(species="arabidopsis", gene_id="AT5G03840", relation="many2one", n_methods=3, confidence="medium")
    low = medium.model_copy(update={"confidence": "low"})
    phenotype = {"phenotype": "dwarf"}
    assert strength.item_cap(_item("ortholog", "tair:phenotype", phenotype, via_ortholog=medium)).strength == "moderate"
    assert strength.item_cap(_item("ortholog", "tair:phenotype", phenotype, via_ortholog=low)).strength == "weak"
    assert strength.item_cap(_item("ortholog", "tair:phenotype", phenotype)).strength == "weak"
    assert strength.item_cap(_item("ortholog", "ortholog:arabidopsis", {"target_gene_id": "AT5G03840"}, via_ortholog=medium)).strength == "weak"


def test_capped_reports_only_downgrades():
    assert strength.capped("weak", [WIDE_QTL]) == ("weak", None)
    allowed, note = strength.capped("moderate", [WIDE_QTL])
    assert allowed == "weak"
    assert note is not None and "from moderate to weak" in note and "5.50 Mb" in note


def test_record_finding_downgrades_a_moderate_claim_on_a_wide_qtl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path))
    store = EvidenceStore.for_run("run-cap")
    try:
        saved = store.put_items([WIDE_QTL, NARROW_QTL])
        config = {"configurable": {"run_id": "run-cap"}, "metadata": {"agrihub_agent_id": "call_qtl_gwas"}}
        claim = {"target": "Glyma.18G092200", "claim": "A plant-height QTL overlaps the gene.", "stance": "supports", "strength": "moderate"}
        content, artifact = asyncio.run(
            agent_tools.record_finding.coroutine(**claim, evidence_ids=[str(saved[0].alias)], config=config)  # type: ignore[misc]
        )
        assert "(supports, weak)" in content and "strength capped from moderate to weak" in content
        assert artifact["strength"] == "weak"
        kept, _ = asyncio.run(agent_tools.record_finding.coroutine(**claim, evidence_ids=[str(saved[1].alias)], config=config))  # type: ignore[misc]
        assert "(supports, moderate)" in kept and "capped" not in kept
        assert [finding.strength for finding in store.findings()] == ["weak", "moderate"]
    finally:
        store.close()
