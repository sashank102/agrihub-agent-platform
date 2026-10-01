"""Query functions and async tool wrappers over the fixture soybean bundle."""

import asyncio
import time
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb
import pytest
from agrihub_fixtures import FixtureBundle

from agrihub.evidence_store import EvidenceStore
from agrihub.state import OrthologRef
from agrihub.tools import bundle_tools
from agrihub_data.bundle import close_bundles, open_bundle
from agrihub_data.query import annotation, ids, loci, orthology, overlap, traits
from agrihub_data.query.common import AssemblyMismatchError, Region

L1 = {"label": "L1", "chrom": "5", "start": 2_649_164, "end": 3_149_164, "snp_pos": 2_899_164}
L2 = {"label": "L2", "chrom": "Chr18", "start": 9_013_941, "end": 9_513_941, "snp_pos": 9_263_941}
INNER = {"label": "W2", "chrom": "Gm18", "start": 9_200_000, "end": 9_300_000, "snp_pos": 9_263_941}


def _region(window: dict[str, Any]) -> Region:
    return Region.model_validate(window)


async def _call(tool: Any, args: dict[str, Any], run_id: str = "run-tools") -> tuple[str, dict[str, Any]]:
    message = await tool.ainvoke(
        {"type": "tool_call", "id": f"call-{tool.name}", "name": tool.name, "args": args},
        {"configurable": {"run_id": run_id}},
    )
    return str(message.content), dict(message.artifact or {})


def call(tool: Any, args: dict[str, Any], run_id: str = "run-tools") -> tuple[str, dict[str, Any]]:
    return asyncio.run(_call(tool, args, run_id))


@pytest.fixture
def bundle(fixture_env: FixtureBundle):
    return open_bundle("soybean")


def test_genes_in_window_reports_distance_overlap_and_defline(bundle):
    genes = loci.genes_in_window(bundle, _region(L2))
    assert [(gene.gene_id, gene.dist_to_snp, gene.overlaps_snp) for gene in genes] == [
        ("Glyma.18G092200", 0, True),
        ("Glyma.18G092300", 14_604, False),
        ("Glyma.18G092000", 22_436, False),
    ]
    assert genes[0].defline == "WRKY family transcription factor; IPR003657 (DNA-binding WRKY)"
    assert {gene.assembly for gene in genes} == {"Wm82.a2.v1"}
    assert {gene.source_version for gene in genes} == {"Wm82.gnm2.ann1.RVB6"}
    on_a4 = loci.genes_in_window(bundle, _region(L2), "Wm82.a4.v1")
    assert [gene.start for gene in on_a4] == [9_274_001, 9_300_001, 9_316_001]
    with pytest.raises(AssemblyMismatchError):
        loci.genes_in_window(bundle, _region({**L2, "assembly": "Wm82.a2.v1"}), "Wm82.a4.v1")


def test_gene_annotation_returns_domains_go_and_best_hit(bundle):
    wrky, missing = annotation.gene_annotation(bundle, ["Glyma.18G092200", "Glyma.99G999999"])
    assert missing.found is False
    assert [term.id for term in wrky.domains["pfam"]] == ["PF03106"]
    assert [term.id for term in wrky.domains["interpro"]] == ["IPR003657"]
    assert wrky.domains["interpro"][0].label == "DNA-binding WRKY"
    assert [(term.id, term.name, term.evidence_code) for term in wrky.go] == [
        ("GO:0003700", "DNA-binding transcription factor activity", "IEA"),
        ("GO:0006355", "regulation of DNA-templated transcription", "IEA"),
        ("GO:0043565", "sequence-specific DNA binding", "IEA"),
    ]
    assert (wrky.arabidopsis_best_hit.id, wrky.arabidopsis_best_hit.label) == (
        "AT1G62300",
        "WRKY6: WRKY family transcription factor",
    )
    on_a6 = annotation.gene_annotation(bundle, ["Glyma.18G092200"], "Wm82.a6.v1")[0]
    assert on_a6.rice_best_hit.id == "LOC_Os01g01010"
    assert [term.id for term in on_a6.domains["ec"]] == ["1.1.1.1"]
    assert on_a6.defline == "WRKY TRANSCRIPTION FACTOR"


def test_orthologs_are_a_method_consensus_with_confidence(bundle):
    calls = {
        (call.gene_id, call.target_gene_id): call
        for call in orthology.get_orthologs(
            bundle, ["Glyma.19G194300", "Glyma.18G092200", "Glyma.18G273600", "Glyma.05G032200"]
        )
    }
    dt1 = calls[("Glyma.19G194300", "AT5G03840")]
    assert dt1.methods == ["blast_best_hit", "compara", "plaza_BHIF", "plaza_ORTHO", "plaza_TROG"]
    assert (dt1.relation, dt1.confidence, dt1.identity, dt1.target_symbol) == ("many2one", "high", 77.0, "TFL-1")
    assert dt1.ref() == OrthologRef(
        species="arabidopsis", gene_id="AT5G03840", relation="many2one", n_methods=5, confidence="high"
    )
    assert (calls[("Glyma.18G273600", "AT5G60910")].relation, calls[("Glyma.18G273600", "AT5G60910")].n_methods) == (
        "one2one",
        6,
    )
    assert calls[("Glyma.18G092200", "AT1G62300")].relation == "many2many"
    assert calls[("Glyma.18G092200", "AT1G18860")].confidence == "low"
    via_pangene = calls[("Glyma.05G032200", "AT1G14420")]
    assert via_pangene.confidence == "medium"
    assert "plaza_anchor_point" in via_pangene.methods and "compara" not in via_pangene.methods


def test_arabidopsis_knowledge_follows_the_ortholog_and_structures_via(bundle):
    through, direct = orthology.arabidopsis_knowledge(bundle, ["Glyma.19G194300", "AT1G62300"])
    assert (through.gene_id, through.agi, through.symbols, through.full_name) == (
        "Glyma.19G194300",
        "AT5G03840",
        ["TFL-1", "TFL1"],
        "TERMINAL FLOWER 1",
    )
    assert through.curator_summary == "Controls inflorescence meristem identity and determinacy."
    assert [phenotype.germplasm for phenotype in through.phenotypes] == ["CS6167", "CS6168"]
    assert [(term.go_id, term.evidence_code) for term in through.go] == [("GO:0010022", "IMP")]
    assert through.n_publications == 2
    items = through.evidence()
    assert len(items) == 4
    assert all(item.via_ortholog and item.via_ortholog.gene_id == "AT5G03840" for item in items)
    assert all(item.via_ortholog.confidence == "high" for item in items)
    assert direct.via is None and [term.evidence_code for term in direct.go] == ["IDA"]
    assert orthology.arabidopsis_knowledge(bundle, ["Glyma.18G092300"]) == []


def test_map_trait_prefers_curated_profiles_then_tfidf(bundle):
    height = traits.map_trait("Plant height", "soybean", bundle)
    assert height.key == "plant_height"
    assert {term.origin for term in height.terms} == {"curated"}
    assert "TO:0000207" in height.to and "CO_336:0000027" in height.crop_co
    assert {"TO:0001034", "GO:0010023"} <= set(height.expanded_ids)
    assert "determinacy" in height.keywords
    lodging = traits.map_trait("lodging", "soybean", bundle)
    assert lodging.key == "lodging"
    assert lodging.terms[0].term_id == "TO:0000068" and lodging.terms[0].origin == "tfidf"
    assert lodging.terms[0].score == 1.0
    assert traits.map_trait("zzzz unmatched", "soybean", bundle).terms == []


def test_annotation_relevance_weights_go_codes_families_and_keywords(bundle):
    profile = traits.map_trait("plant height", "soybean", bundle)
    scored = {row.gene_id: row for row in annotation.annotation_relevance(bundle, ["Glyma.19G194300", "Glyma.18G092300"], profile)}
    dt1 = scored["Glyma.19G194300"]
    assert [(match.kind, match.term, match.field, match.weight) for match in dt1.matches] == [
        ("go", "GO:0010022", "go", 0.3),
        ("family", "TFL1", "arabidopsis_symbol", 1.0),
        ("keyword", "determinacy", "tair_curator_summary", 0.5),
        ("keyword", "inflorescence meristem", "tair_curator_summary", 0.5),
    ]
    assert dt1.score == 2.3 and scored["Glyma.18G092300"].score == 0


def test_seed_families_match_whole_symbol_tokens_with_paralog_suffixes():
    assert annotation.matched_families(["GA20ox", "FT", "E1", "GID1"], "GA20OX1 FT2a GID1B", symbols=True) == [
        "GA20ox",
        "FT",
        "GID1",
    ]
    assert annotation.matched_families(["E1", "TFL1"], "ubiquitin-activating enzyme E1; TFL1-like", symbols=False) == [
        "TFL1"
    ]
    assert annotation.matched_families(["TFL1"], "TFL12 ATFL1", symbols=True) == []
    assert annotation.matched_families(["CO"], "COL1 CONSTANS", symbols=True) == []


def test_qtl_overlap_types_placement_and_trait_match(bundle):
    profile = traits.map_trait("plant height", "soybean", bundle)
    hits = {hit.qtl_name: hit for hit in overlap.qtl_overlap(bundle, _region(L2), profile)}
    assert set(hits) == {"Plant height 1-1", "Plant height 2-1", "Seed oil 1-1"}
    assert (hits["Plant height 1-1"].overlap_type, hits["Plant height 1-1"].trait_match) == ("partial", "ontology")
    assert hits["Plant height 2-1"].trait_match == "keyword"
    assert (hits["Seed oil 1-1"].overlap_type, hits["Seed oil 1-1"].trait_match) == ("marker_within", "none")
    assert hits["Seed oil 1-1"].kind == "marker" and hits["Plant height 1-1"].kind == "interval"
    assert hits["Plant height 1-1"].n_markers_placed == 2 and not hits["Plant height 1-1"].wide
    inner = overlap.qtl_overlap(bundle, _region(INNER), profile, trait_only=True)
    assert [(hit.qtl_name, hit.overlap_type) for hit in inner] == [
        ("Plant height 2-1", "partial"),
        ("Plant height 1-1", "qtl_contains_window"),
    ]
    assert [hit.qtl_name for hit in overlap.qtl_overlap(bundle, _region(L1), profile)] == ["Plant height 1-2"]
    with pytest.raises(AssemblyMismatchError, match="Lift the region over first"):
        overlap.qtl_overlap(bundle, _region({**L2, "assembly": "Wm82.a4.v1"}), profile)


def test_gene_mode_keys_overlaps_to_genes_with_distances_to_the_gene_body(bundle):
    profile = traits.map_trait("plant height", "soybean", bundle)
    wrky, sec13 = loci.gene_regions(bundle, ["Glyma.18G092200", "Glyma.18G092300"], flank_bp=overlap.GWAS_GENE_FLANK_BP)
    assert (wrky.label, wrky.core, wrky.start, wrky.end) == (
        "Glyma.18G092200",
        (9_262_392, 9_267_008),
        9_212_392,
        9_317_008,
    )
    by_gene = {
        region.label: overlap.gwas_catalog_overlap(bundle, region, profile, trait_only=True) for region in (wrky, sec13)
    }
    assert {(hit.source_db, hit.distance_to_core) for hit in by_gene["Glyma.18G092200"]} == {
        ("GWAS Atlas", 0),
        ("LIS/SoyBase GWAS", 12_391),
        ("SoyBase GWAS", 0),
    }
    assert {item.gene_id for hit in by_gene["Glyma.18G092300"] for item in hit.evidence()} == {"Glyma.18G092300"}
    gene = loci.gene_regions(bundle, ["Glyma.18G092000"])[0]
    near = overlap.qtl_overlap(bundle, gene, profile, marker_flank_bp=overlap.QTL_MARKER_FLANK_BP)
    assert [(hit.qtl_name, hit.kind, hit.overlap_type) for hit in near] == [
        ("Plant height 2-1", "interval", "qtl_contains_window"),
        ("Plant height 1-1", "interval", "qtl_contains_window"),
    ]
    wider = overlap.qtl_overlap(bundle, gene, profile, marker_flank_bp=200_000)
    assert [(hit.qtl_name, hit.overlap_type, hit.distance_to_core) for hit in wider if hit.kind == "marker"] == [
        ("Seed oil 1-1", "marker_within", 136_499)
    ]
    with pytest.raises(ValueError, match="not genes on Wm82.a2.v1"):
        loci.gene_regions(bundle, ["L1"])


def test_gwas_catalog_overlap_never_mixes_assemblies(bundle):
    profile = traits.map_trait("plant height", "soybean", bundle)
    hits = overlap.gwas_catalog_overlap(bundle, _region(L2), profile)
    assert [(hit.source_db, hit.trait_name, hit.trait_match) for hit in hits] == [
        ("GWAS Atlas", "plant height", "ontology"),
        ("LIS/SoyBase GWAS", "Plant height", "ontology"),
        ("SoyBase GWAS", "Plant height", "keyword"),
        ("LIS/SoyBase GWAS", "Seed oil", "none"),
    ]
    assert {hit.assembly for hit in hits} == {"Wm82.a2.v1"}
    assert hits[0].reported_genes == ["Glyma.18G092200"] and hits[0].pmid == "12345678"
    strict = overlap.gwas_catalog_overlap(bundle, _region(L2), profile, max_p=1e-9)
    assert [(hit.source_db, hit.p_value) for hit in strict] == [("GWAS Atlas", 1e-10), ("SoyBase GWAS", None)]
    a1 = overlap.gwas_catalog_overlap(bundle, _region({**L2, "assembly": "Wm82.a1.v1"}))
    assert [(hit.study_id, hit.assembly) for hit in a1] == [("Old study 1", "Wm82.a1.v1")]
    with pytest.raises(AssemblyMismatchError):
        overlap.gwas_catalog_overlap(bundle, _region({**L2, "assembly": "Wm82.a4.v1"}))


def test_known_trait_genes_include_dt1_and_dt2(bundle):
    height = traits.map_trait("plant height", "soybean", bundle)
    hits = {hit.gene_id: hit for hit in overlap.known_trait_genes(bundle, height)}
    assert set(hits) == {"Glyma.19G194300", "Glyma.18G273600"}
    assert (hits["Glyma.19G194300"].trait_match, hits["Glyma.19G194300"].matched) == ("ontology", ["CO_336:0000027"])
    assert (hits["Glyma.18G273600"].trait_match, hits["Glyma.18G273600"].matched) == ("keyword", ["determinacy"])
    near = overlap.known_trait_genes(
        bundle, height, region=Region(chrom="Gm18", start=55_500_000, end=55_700_000, snp_pos=55_650_000)
    )
    assert [(hit.gene_id, hit.distance_to_snp) for hit in near] == [("Glyma.18G273600", 47_000)]
    flowering = traits.map_trait("flowering time", "soybean", bundle)
    unmapped = [hit for hit in overlap.known_trait_genes(bundle, flowering) if hit.mapping == "unmapped"]
    assert [(hit.gene_id, hit.assembly, hit.chrom) for hit in unmapped] == [("Glyma.11G148362", "Wm82.a4.v1", "Gm11")]
    text, _ = call(bundle_tools.known_trait_genes, {"trait": "flowering time"})
    assert "Glyma.11G148362 GmTof11 Gm11:11236816-11240000 (only on Wm82.a4.v1; no Wm82.a2.v1 model)" in text


def test_map_gene_ids_across_namespaces_and_assemblies(bundle):
    mapped = {
        row.query: (row.to_id, row.relation)
        for row in ids.map_gene_ids(
            bundle,
            ["GLYMA_18G092200", "glyma.Wm82.gnm4.ann1.Glyma.05G032300", "Glyma18g11630", "Glyma.18G092200.1", "nope"],
        )
    }
    assert mapped == {
        "GLYMA_18G092200": ("Glyma.18G092200", "identical"),
        "glyma.Wm82.gnm4.ann1.Glyma.05G032300": ("Glyma.05G032200", "pangene"),
        "Glyma18g11630": ("Glyma.18G092200", "synonym"),
        "Glyma.18G092200.1": ("Glyma.18G092200", "identical"),
        "nope": ("nope", "identical"),
    }
    assert [row.exists for row in ids.map_gene_ids(bundle, ["nope"])] == [False]
    forward = ids.map_gene_ids(bundle, ["Glyma.18G092200"], "Wm82.a6.v1")
    assert [(row.to_id, row.relation) for row in forward] == [("Glyma.18G092200", "ancestor")]


def test_liftover_uses_gene_anchors_then_marker_anchors(bundle):
    to_a4 = ids.liftover(bundle, [_region(L2)], "Wm82.a2.v1", "Wm82.a4.v1")[0]
    assert (to_a4.method, to_a4.n_anchors, to_a4.chrom) == ("gene_anchor", 3, "Gm18")
    assert abs(to_a4.start - 9_050_000) < 20_000 and abs(to_a4.end - 9_549_000) < 20_000
    assert to_a4.genes == ["Glyma.18G092000", "Glyma.18G092200", "Glyma.18G092300"]
    to_a1 = ids.liftover(bundle, [_region(L2)], "Wm82.a2.v1", "Wm82.a1.v1")[0]
    assert (to_a1.method, to_a1.n_anchors) == ("marker_anchor", 2)
    assert abs(to_a1.start - 8_963_927) < 1_000
    nowhere = ids.liftover(bundle, [Region(chrom="Gm10", start=1, end=100)], "Wm82.a2.v1", "Wm82.a4.v1")[0]
    assert nowhere.method == "none" and nowhere.chrom is None


def test_resolve_marker_uses_bundle_marker_sets(bundle):
    barc = ids.resolve_marker("soybean", "BARC_1.01_Gm18_9199987_A_G", None, bundle)
    assert [(hit.assembly, hit.pos, hit.flags) for hit in barc] == [
        ("Wm82.a2.v1", 9_250_001, []),
        ("Wm82.a1.v1", 9_199_987, ["a1_embedded_position", "other_assembly"]),
    ]
    ss = ids.resolve_marker("soybean", "ss715631025", None, bundle)
    assert [(hit.assembly, hit.pos) for hit in ss] == [
        ("Wm82.a2.v1", 9_250_001),
        ("Wm82.a1.v1", 9_199_987),
        ("Wm82.a4.v1", 9_288_001),
    ]
    assert [(hit.marker_id, hit.pos) for hit in ids.resolve_marker("soybean", "satt324", None, bundle)] == [
        ("Satt324", 9_100_001)
    ]


def test_wrappers_store_evidence_and_render_compact_text(fixture_env: FixtureBundle):
    text, artifact = call(bundle_tools.genes_in_window, {"windows": [L2]})
    lines = text.splitlines()
    assert lines[0] == "genes_in_window 1 windows: 3 genes"
    assert lines[1].startswith("E1 Glyma.18G092200 Gm18:9262392-9267008(-) dist=0 SNP-in-gene | WRKY")
    assert lines[-1] == "truncated: false (showing 3 of 3); output_ref: O1"
    assert artifact["aliases"] == ["E1", "E2", "E3"] and artifact["total"] == 3
    store = EvidenceStore.for_run("run-tools")
    assert store.get(["E1"])[0].value["window"] == "Gm18:9013941-9513941"
    assert store.get_output("O1")["rows"][0]["gene_id"] == "Glyma.18G092200"

    text, artifact = call(bundle_tools.gene_annotation, {"gene_ids": ["Glyma.18G092200", "Glyma.99G999999"]})
    assert "not found: Glyma.99G999999" in text
    assert text.splitlines()[2].startswith("E4..E12 Glyma.18G092200 | WRKY")

    text, _ = call(bundle_tools.qtl_overlap, {"windows": [{**L2, "assembly": "Wm82.a4.v1"}]})
    assert "Lift the region over first" in text

    text, artifact = call(bundle_tools.normalize_chrom, {"chroms": ["chr18", "21"]}, run_id="")
    assert "chr18 -> Gm18" in text and "21 -> ERROR" in text and artifact["output_ref"] is None

    text, _ = call(bundle_tools.known_trait_genes, {"trait": "plant height"})
    assert "Glyma.19G194300 GmDT1/GmTFL1b" in text and "Glyma.18G273600 GmDT2" in text


def _evidence_ids(tool: Any, args: dict[str, Any], run_id: str) -> list[str]:
    _, artifact = call(tool, args, run_id)
    return artifact["evidence_ids"]


@pytest.mark.parametrize(
    ("tool", "first", "second", "expected"),
    [
        (bundle_tools.genes_in_window, {"windows": [L2]}, {"windows": [INNER]}, 3 + 3),
        (bundle_tools.gene_annotation, {"gene_ids": ["Glyma.18G092200"]}, {"gene_ids": ["Glyma.19G194300"]}, 9 + 7),
        (bundle_tools.get_orthologs, {"gene_ids": ["Glyma.18G092200"]}, {"gene_ids": ["Glyma.19G194300"]}, 2 + 1),
        (
            bundle_tools.arabidopsis_knowledge,
            {"ids_or_genes": ["Glyma.19G194300"]},
            {"ids_or_genes": ["AT5G03840"]},
            4 + 4,
        ),
        (
            bundle_tools.annotation_relevance,
            {"gene_ids": ["Glyma.19G194300"], "trait": "plant height"},
            {"gene_ids": ["Glyma.19G194300"], "trait": "flowering time"},
            4 + 0,
        ),
        (bundle_tools.qtl_overlap, {"windows": [L2]}, {"windows": [INNER]}, 3 + 2),
        (bundle_tools.gwas_catalog_overlap, {"windows": [L2]}, {"windows": [INNER]}, 4 + 4),
        (
            bundle_tools.known_trait_genes,
            {"trait": "plant height"},
            {"trait": "maturity", "gene_ids": ["Glyma.19G194300", "Glyma.18G273600"]},
            2 + 2,
        ),
    ],
)
def test_distinct_facts_get_distinct_evidence_ids(fixture_env: FixtureBundle, tool, first, second, expected):
    one = _evidence_ids(tool, first, "run-distinct")
    two = _evidence_ids(tool, second, "run-distinct")
    assert len(one) == len(set(one)) and len(two) == len(set(two))
    assert len(set(one) | set(two)) == expected
    assert _evidence_ids(tool, first, "run-distinct") == one


def test_qtl_and_gwas_ids_follow_the_granularity_rule(bundle):
    profile = traits.map_trait("plant height", "soybean", bundle)
    hits = {hit.qtl_name: hit for hit in overlap.qtl_overlap(bundle, _region(L2), profile)}
    qtl = hits["Plant height 1-1"].evidence()[0]
    assert (qtl.source_record, qtl.subtype, qtl.gene_id) == (
        "Alpha_x_Beta.qtl.Test_2020|Plant height 1-1",
        "qtl:partial",
        "L2",
    )
    lis = [hit for hit in overlap.gwas_catalog_overlap(bundle, _region(L2)) if hit.source_db == "LIS/SoyBase GWAS"]
    records = sorted(hit.evidence()[0].source_record for hit in lis)
    assert records == [
        "mixed.gwas.Test_2019|ss715631025|Plant height",
        "mixed.gwas.Test_2019|ss715631025|Seed oil",
    ]


def test_wrapper_output_truncates_at_forty_rows(fixture_env: FixtureBundle):
    rows = [
        loci.GeneInWindow(
            gene_id=f"Glyma.01G{index:06d}",
            assembly="Wm82.a2.v1",
            chrom="Gm01",
            start=index * 10 + 1,
            end=index * 10 + 5,
            strand="+",
            defline=None,
            window="Gm01:1-1000",
            snp_pos=None,
            dist_to_snp=None,
            overlaps_snp=False,
            source_db="LIS",
            source_version="v",
        )
        for index in range(45)
    ]
    text, artifact = bundle_tools._respond(
        {"configurable": {"run_id": "run-trunc"}},
        "genes_in_window",
        "header",
        rows,
        lambda row: row.gene_id,
        evidence=True,
    )
    lines = text.splitlines()
    assert len(lines) == 1 + 40 + 1
    assert lines[-1] == "truncated: true (showing 40 of 45); output_ref: O1"
    assert artifact["truncated"] and artifact["total"] == 45 and len(artifact["evidence_ids"]) == 45
    assert len(EvidenceStore.for_run("run-trunc").get_output("O1")["rows"]) == 45


def _synthetic_bundle(data_dir: Path, genes: int) -> None:
    folder = data_dir / "soybean"
    folder.mkdir(parents=True)
    connection = duckdb.connect(str(folder / "bundle.duckdb"))
    connection.execute((resources.files("agrihub_data") / "schema.sql").read_text(encoding="utf-8"))
    connection.execute("INSERT INTO bundle_info VALUES ('species', 'soybean')")
    connection.execute(
        "INSERT INTO genes SELECT 'soybean', 'Wm82.a2.v1', 'synthetic', printf('Glyma.01G%06d', i), 'Gm01', "
        "i * 1000 + 1, i * 1000 + 500, '+', 'synthetic gene ' || i, NULL, 'test' FROM range(?) AS t(i)",
        [genes],
    )
    connection.close()


def test_large_bundle_query_does_not_block_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _synthetic_bundle(tmp_path / "data", 12_000)
    monkeypatch.setenv("AGRIHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGRIHUB_RUN_DIR", str(tmp_path / "runs"))

    async def scenario() -> tuple[float, list[float], dict[str, Any]]:
        ticks: list[float] = []
        running = True

        async def heartbeat() -> None:
            while running:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        started = time.monotonic()
        _, artifact = await _call(
            bundle_tools.genes_in_window,
            {"windows": [{"chrom": "Gm01", "start": 1, "end": 56_000_000, "snp_pos": 6_000_000}]},
            "run-heartbeat",
        )
        elapsed = time.monotonic() - started
        running = False
        await beat
        return elapsed, [tick for tick in ticks if tick >= started], artifact

    try:
        elapsed, ticks, artifact = asyncio.run(scenario())
    finally:
        close_bundles()
    assert artifact["total"] == 12_000 and artifact["truncated"]
    assert elapsed > 0.2
    gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:], strict=False)]
    assert len(ticks) >= elapsed / 0.05, (len(ticks), elapsed)
    assert max(gaps) < 0.25, max(gaps)
