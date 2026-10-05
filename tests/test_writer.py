"""The report writer drafts the summary from the facts pack; validation and the template keep it honest."""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agrihub import citations, models, summary
from agrihub.nodes.writer import _curated_coverage, _summarize
from agrihub.state import EvidenceRef, Locus, RankedCandidate, Report, SourceRef
from agrihub.summary import CandidateDraft, LocusDraft, WriterDraft

ALIAS_OF = {"ev1": "E1", "ev2": "E2", "ev3": "E3"}
KNOWN = {"E1", "E2", "E3", "lis"}


def _report() -> Report:
    ranked = [
        RankedCandidate(
            rank=1,
            gene_id="Glyma.19G194300",
            symbol="GmDT1",
            locus_id="L1",
            rank_in_locus=1,
            chrom="Gm19",
            start=45_300_000,
            end=45_302_000,
            distance_bp=57_825,
            nearest_snp="S19_45243000",
            score=61.2,
            share_of_locus=0.77,
            tier="T1",
            category_points={"A": 12.0, "B": 25.0, "G": 4.0},
            reasons=["A: 57825 bp from S19_45243000", "B: curated GmDT1 for this trait", "G: GWAS hit within 50 kb"],
            stability="top 5 at every window",
            evidence_ids=["ev1", "ev2"],
        ),
        RankedCandidate(
            rank=2,
            gene_id="Glyma.19G196000",
            locus_id="L1",
            rank_in_locus=2,
            distance_bp=73_499,
            nearest_snp="S19_45243000",
            score=8.0,
            share_of_locus=0.08,
            tier="T4",
            category_points={"A": 8.0},
            reasons=["A: 73499 bp from S19_45243000"],
            evidence_ids=["ev3"],
        ),
    ]
    return Report(
        title="plant height candidate genes in soybean",
        species="soybean",
        assembly="Wm82.a2.v1",
        trait="plant height",
        mode="snps",
        loci=[
            Locus(
                locus_id="L1",
                lead_snp="S19_45243000",
                chrom="Gm19",
                start=45_000_000,
                end=45_500_000,
                assembly="Wm82.a2.v1",
                window_method="fixed",
                n_genes=2,
            )
        ],
        candidates=ranked,
        candidates_full=ranked,
        citations=[
            EvidenceRef(alias="E1", evidence_id="ev1", gene_id=ranked[0].gene_id, source_db="LIS", category="curated", subtype="trait_gene", quote="GmDT1"),
            EvidenceRef(alias="E2", evidence_id="ev2", gene_id=ranked[0].gene_id, source_db="GWAS", category="association", subtype="gwas_hit"),
            EvidenceRef(alias="E3", evidence_id="ev3", gene_id=ranked[1].gene_id, source_db="Gene models", category="positional", subtype="in_window"),
        ],
        sources=[SourceRef(source_id="lis", name="LIS", version="2024")],
        limitations=["Only one locus was studied.", "Expression data is from one atlas."],
        suggested_validations=["Test a Glyma.19G194300 knockout for plant height."],
    )


class _Scripted:
    """A model stand-in that answers every call with one prepared message."""

    def __init__(self, response: AIMessage) -> None:
        self.response = response

    async def ainvoke(self, _messages: Any, _config: Any = None) -> AIMessage:
        return self.response


def _draft_call(draft: WriterDraft) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "write_summary", "args": draft.model_dump(), "id": "w1"}])


def _write(report: Report, writer_model: str = "agrihub-fake:poster") -> Report:
    facts = summary.build_facts(report, [], ALIAS_OF)
    return asyncio.run(_summarize(report, facts, KNOWN, {"configurable": {"writer_model": writer_model}}))


def test_the_t1_caveat_names_the_curated_sources_only_when_coverage_is_thin() -> None:
    soybean = {"genes": 206, "genome_genes": 56_044, "sources": ["LIS gene_functions"], "assembly": "Wm82.a2.v1"}
    assert _curated_coverage(soybean, "soybean") == (
        "Curated trait-gene coverage is thin: the soybean bundle has curated records for 206 genes on Wm82.a2.v1 "
        "(LIS gene_functions), 0.4% of its 56044 genes. Tier T1 needs such a record, so T1 is rare, and a gene "
        "without one is not evidence against it."
    )
    rice = {"genes": 19_800, "genome_genes": 36_061, "sources": ["Oryzabase", "RAP-DB curated genes"], "assembly": "IRGSP-1.0"}
    assert _curated_coverage(rice, "rice") is None
    empty = {"genes": 0, "genome_genes": 34_027, "sources": [], "assembly": "Sorghum_bicolor_NCBIv3"}
    assert "no gene can reach tier T1" in (_curated_coverage(empty, "sorghum") or "")


def test_validation_keeps_study_genes_and_resolving_citations() -> None:
    draft = WriterDraft(
        bottom_line="Glyma.19G194300 is the strongest candidate [E1][E99].",
        key_findings=["**Glyma.19G194300 leads.** It is a curated gene [E1] [source:lis] [source:nope]."],
        candidates=[
            CandidateDraft(gene_id="Glyma.01G000100", narrative="An invented gene [E1]."),
            CandidateDraft(gene_id="Glyma.19G194300", narrative="It lies 58 kb from the SNP [E1]."),
            CandidateDraft(gene_id="Glyma.19G194300", narrative="A duplicate paragraph."),
            CandidateDraft(gene_id="Glyma.19G196000", narrative="Positional only [E3]."),
        ],
        loci=[LocusDraft(locus_id="L9", narrative="Not a locus."), LocusDraft(locus_id="L1", narrative="GmDT1 leads L1.")],
        caveats=["One atlas [E99]."],
        next_steps=["Test a knockout."],
    )
    written, orphans = summary.validate_draft(draft, _report(), KNOWN, "anthropic:claude-haiku-4-5")
    assert written is not None
    assert [item.gene_id for item in written.candidates] == ["Glyma.19G194300", "Glyma.19G196000"]
    assert [(item.tier, item.confidence) for item in written.candidates] == [("T1", "strong"), ("T4", "positional only")]
    assert written.candidates[0].symbol == "GmDT1" and written.candidates[0].locus_id == "L1"
    assert written.bottom_line == "Glyma.19G194300 is the strongest candidate [E1]."
    assert written.key_findings[0].citations == ["E1", "source:lis"]
    assert [item.locus_id for item in written.loci] == ["L1"]
    assert written.caveats == ["One atlas."]
    assert orphans == ["E99", "source:nope"]
    assert written.written_by == "anthropic:claude-haiku-4-5"


def test_validation_rejects_a_draft_without_a_study_candidate() -> None:
    draft = WriterDraft(bottom_line="Something [E1].", candidates=[CandidateDraft(gene_id="Glyma.01G000100", narrative="Invented.")])
    written, _ = summary.validate_draft(draft, _report(), KNOWN, "model")
    assert written is None


def test_template_cites_only_the_facts_pack() -> None:
    report = _report()
    facts = summary.build_facts(report, [], ALIAS_OF)
    written, orphans = summary.validate_draft(summary.template_draft(facts), report, KNOWN, "template")
    assert written is not None and orphans == []
    assert "Glyma.19G194300 (GmDT1; T1, strong)" in written.bottom_line
    assert [item.gene_id for item in written.candidates] == ["Glyma.19G194300", "Glyma.19G196000"]
    assert "ranks first in its locus with a 77% share" in written.candidates[0].narrative
    allowed = set(facts["allowed_citations"])
    texts = [written.bottom_line, *(item.statement for item in written.key_findings), *(item.narrative for item in written.candidates)]
    assert all(set(citations.citation_tokens(text)) <= allowed for text in texts)
    markdown = summary.render_markdown(written, report)
    for heading in ("## Bottom line", "## Key findings at a glance", "## Top candidates", "## Locus by locus"):
        assert heading in markdown
    assert "### Glyma.19G194300 (GmDT1) · T1, strong" in markdown


def test_scripted_writer_writes_the_summary() -> None:
    report = _write(_report())
    assert report.summary is not None and report.summary.written_by == "agrihub-fake:poster"
    assert not any("deterministic template" in item for item in report.limitations)
    assert report.markdown.startswith("# plant height candidate genes in soybean")


def test_writer_falls_back_to_the_template_when_the_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("no provider key")

    monkeypatch.setattr(models, "tool_model", broken)
    report = _write(_report(), "anthropic:claude-haiku-4-5")
    assert report.summary is not None and report.summary.written_by == "template"
    assert report.limitations[-1] == "The summary was written from the deterministic template because the writer model failed (RuntimeError)."
    assert "## Bottom line" in report.markdown


def test_writer_falls_back_when_the_draft_names_no_study_gene(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = WriterDraft(bottom_line="Invented [E1].", candidates=[CandidateDraft(gene_id="Glyma.01G000100", narrative="Invented.")])
    monkeypatch.setattr(models, "tool_model", lambda *_args, **_kwargs: _Scripted(_draft_call(draft)))
    report = _write(_report(), "anthropic:claude-haiku-4-5")
    assert report.summary is not None and report.summary.written_by == "template"
    assert "named no candidate of this study" in report.limitations[-1]


def test_writer_records_citations_it_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = WriterDraft(
        bottom_line="Glyma.19G194300 leads [E1][E404].",
        candidates=[CandidateDraft(gene_id="Glyma.19G194300", narrative="Curated [E1].")],
    )
    monkeypatch.setattr(models, "tool_model", lambda *_args, **_kwargs: _Scripted(_draft_call(draft)))
    report = _write(_report(), "anthropic:claude-haiku-4-5")
    assert report.summary is not None and report.summary.written_by == "anthropic:claude-haiku-4-5"
    assert report.limitations[-1] == "Citations removed from the written summary because they do not resolve: E404."
    assert "[E404]" not in report.markdown
