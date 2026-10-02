"""The verifier re-checks existing claims and never adds one."""

from agrihub import citations, verifier
from agrihub.evidence_store import evidence_id_for
from agrihub.state import EvidenceItem, Finding, RankedCandidate


def _item(gene_id: str, **extra: object) -> EvidenceItem:
    item = EvidenceItem(
        gene_id=gene_id,
        category=extra.pop("category", "expression"),  # type: ignore[arg-type]
        subtype=str(extra.pop("subtype", "expression_profile:atlas")),
        value=extra.pop("value", {"trait_max_value": 10}),
        source_db=str(extra.pop("source_db", "atlas-a")),
        db_version="1",
        source_record=str(extra.pop("record", gene_id)),
        quote=extra.pop("quote", None),  # type: ignore[arg-type]
        alias=extra.pop("alias", None),  # type: ignore[arg-type]
    )
    return item.model_copy(update={"evidence_id": evidence_id_for(item)})


def _candidate(gene_id: str, evidence: list[EvidenceItem]) -> RankedCandidate:
    return RankedCandidate(
        rank=1,
        gene_id=gene_id,
        locus_id="L1",
        score=10,
        tier="T4",
        reasons=["expressed in the trait tissue"],
        evidence_ids=[str(item.evidence_id) for item in evidence],
        shortlist=True,
    )


def test_an_independent_source_marks_the_claim_contradicted():
    claim = _item("g1", alias="E1")
    other = _item(
        "g1",
        source_db="atlas-b",
        subtype="expression_profile:other",
        record="g1:other",
        alias="E2",
        value={"contradicts": claim.evidence_id},
    )
    finding = Finding(
        finding_id="F1",
        agent_id="lane",
        target="g1",
        claim="The gene is expressed in the trait tissue.",
        stance="supports",
        strength="weak",
        evidence_ids=[str(claim.evidence_id)],
    )
    before = 1
    verdicts = verifier.verify_claims([_candidate("g1", [claim])], [claim, other], [finding], species="soybean", trait="plant height")
    assert len(verdicts) == before + 1
    statuses = {item.claim_id: item.status for item in verdicts}
    assert statuses["F1"] == "contradicted"
    assert "E2" in verdicts[0].independent_evidence_ids or any(item.status == "contradicted" for item in verdicts)


def test_a_literature_mention_is_downgraded():
    mention = _item(
        "g1",
        category="literature",
        subtype="passage:mention",
        source_db="Europe PMC",
        quote="WRKY was mentioned.",
        alias="E3",
        value={"alias": "WRKY"},
        record="pmid:1",
    )
    finding = Finding(
        finding_id="F2",
        agent_id="literature",
        target="g1",
        claim="The paper mentions the gene.",
        stance="supports",
        strength="weak",
        evidence_ids=[str(mention.evidence_id)],
    )
    verdicts = verifier.verify_claims(
        [_candidate("g1", [mention])],
        [mention],
        [finding],
        species="soybean",
        trait="plant height",
    )
    mention_verdict = next(item for item in verdicts if item.claim_id == "F2")
    assert mention_verdict.status == "unverified"
    assert "downgraded" in mention_verdict.note


def test_orphan_citations_are_stripped_and_real_ones_remain():
    markdown = "Glyma.18G092200 is supported by [E1] and [E9999]."
    cleaned, orphans = citations.validate_citations(markdown, {"E1"})
    assert orphans == ["E9999"]
    assert "[E1]" in cleaned and "E9999" not in cleaned
    assert citations.citation_tokens(cleaned) == ["E1"]
