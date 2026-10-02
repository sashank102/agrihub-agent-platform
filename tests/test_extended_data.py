"""Extended and heavy tiers on fixture data: parsers, query functions and tools."""

import asyncio
from typing import Any

import numpy as np
import pytest
from agrihub_fixtures import FixtureBundle

from agrihub.evidence_store import evidence_id_for
from agrihub.tools import extended_tools
from agrihub_data.bundle import open_bundle
from agrihub_data.query import expression, network, pathways, regulation
from agrihub_data.query.common import Region
from agrihub_data.query.traits import map_trait


@pytest.fixture
def bundle(heavy_env: FixtureBundle):
    return open_bundle("soybean")


def _tool(tool: Any, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    config = {"configurable": {"run_id": "run-extended"}, "metadata": {"agrihub_agent_id": "call_test"}}
    message = asyncio.run(tool.ainvoke({"name": tool.name, "args": args, "id": "call-1", "type": "tool_call"}, config))
    return str(message.content), dict(message.artifact or {})


def _unique_ids(rows: list[Any]) -> None:
    items = [item for row in rows for item in row.evidence()]
    assert len({evidence_id_for(item) for item in items}) == len(items)


def test_expression_build_averages_replicates_and_maps_a4_to_a2(heavy_bundle: FixtureBundle):
    stats = heavy_bundle.build_report.stats
    atlas = stats["lis_expression_atlas"]
    assert atlas["samples"] == 5
    assert atlas["unmapped_source_genes"] == 1
    assert atlas["value_columns_without_sample"] == 1
    assert stats["lis_expression_libault"]["unmapped_source_genes"] == 1
    bundle = open_bundle("soybean", heavy_bundle.data_dir)
    rows = bundle.rows(
        "SELECT gene_id, source_gene_id, source_assembly, sample, tissue, stage, value FROM expression "
        "WHERE dataset = 'Sreedasyam_Plott_2023' ORDER BY gene_id, sample"
    )
    by_key = {(row["gene_id"], row["sample"]): row for row in rows}
    assert by_key[("Glyma.18G092200", "shoot_tip.standard")]["value"] == 42.0
    seed = by_key[("Glyma.05G032200", "seed.seed_s5")]
    assert (seed["source_gene_id"], seed["source_assembly"], seed["value"], seed["stage"]) == ("Glyma.05G032300", "Wm82.a4.v1", 85.0, "seed_s5")
    assert by_key[("Glyma.18G092200", "nodules.standard")]["tissue"] == "nodule"
    assert {row["assembly"] for row in bundle.rows("SELECT DISTINCT assembly FROM expression")} == {"Wm82.a2.v1"}
    libault = {row["sample"]: row["tissue"] for row in bundle.rows("SELECT sample, tissue FROM samples WHERE dataset = 'Libault_Farmer_2010'")}
    assert libault == {"Stacey_Flower": "flower", "Stacey_Root": "root", "12HA1_IN_RH": "root_hair", "Stacey_Apical_Meristem": "shoot_apical_meristem"}
    replicates = {row["sample"]: row["n_replicates"] for row in bundle.rows("SELECT sample, n_replicates FROM samples WHERE dataset = 'Sreedasyam_Plott_2023'")}
    assert replicates["shoot_tip.standard"] == 2 and replicates["stem.standard"] == 1


def test_trait_relevant_tissues_use_the_curated_profile_or_a_keyword_rule(bundle):
    height = expression.trait_relevant_tissues("soybean", map_trait("plant height", "soybean", bundle), bundle)
    assert height.origin == "curated" and height.tissues == ["shoot_tip", "shoot_apical_meristem", "stem"]
    assert height.in_bundle == {"Libault_Farmer_2010": ["shoot_apical_meristem"], "Sreedasyam_Plott_2023": ["shoot_tip", "stem"]}
    oil = expression.trait_relevant_tissues("soybean", map_trait("seed oil", "soybean", bundle), bundle)
    assert oil.tissues == ["seed"] and "seed_s5" in oil.stages
    scn = expression.trait_relevant_tissues("soybean", map_trait("soybean cyst nematode resistance", "soybean", bundle), bundle)
    assert scn.origin == "rule" and scn.tissues[0] == "root"
    assert expression.trait_relevant_tissues("soybean", map_trait("zzz", "soybean", bundle)).origin == "none"


def test_expression_profile_and_tissue_specificity(bundle):
    selection = expression.trait_relevant_tissues("soybean", map_trait("plant height", "soybean", bundle), bundle)
    profiles = expression.expression_profile(bundle, ["Glyma.18G092200", "Glyma.05G032200", "Glyma.18G092300"], selection, "Sreedasyam_Plott_2023")
    by_gene = {profile.gene_id: profile for profile in profiles}
    assert by_gene["Glyma.18G092200"].in_trait_tissue and by_gene["Glyma.18G092200"].trait_max_sample == "shoot_tip.standard"
    assert not by_gene["Glyma.05G032200"].in_trait_tissue and by_gene["Glyma.05G032200"].max_tissue == "seed"
    specific = {row.gene_id: row for row in expression.tissue_specificity(bundle, list(by_gene), selection, "Sreedasyam_Plott_2023")}
    seed = specific["Glyma.05G032200"]
    assert seed.top_tissue == "seed" and seed.tau == 1.0 and not seed.trait_tissue_top
    assert specific["Glyma.18G092300"].tau == 0.0
    shoot = specific["Glyma.18G092200"]
    assert shoot.trait_tissue_top and shoot.trait_z is not None and shoot.trait_z > 1
    _unique_ids(profiles)
    _unique_ids(list(specific.values()))


def test_expression_tools_store_evidence_and_render_aliases(bundle):
    content, artifact = _tool(extended_tools.trait_relevant_tissues, {"trait": "plant height"})
    assert "shoot_tip, shoot_apical_meristem, stem" in content and artifact["evidence_ids"] == []
    content, artifact = _tool(extended_tools.expression_profile, {"gene_ids": ["Glyma.18G092200", "Glyma.99G000001"], "trait": "plant height"})
    assert "EXPRESSED-IN-TRAIT-TISSUE" in content and "not found in the expression atlases: Glyma.99G000001" in content
    assert len(artifact["evidence_ids"]) == 2 and content.splitlines()[2].startswith("E")
    content, artifact = _tool(extended_tools.tissue_specificity, {"gene_ids": ["Glyma.05G032200"], "tissues": ["seed"]})
    assert "tau=1.0 top=seed" in content and "TRAIT-TISSUE-TOP" in content and len(artifact["aliases"]) == 1


def test_string_drops_text_mining_and_atted_keeps_top_partners(heavy_bundle: FixtureBundle):
    stats = heavy_bundle.build_report.stats
    assert stats["string_soybean"] == {"edges": 3, "genes_mapped": 5, "proteins_mapped": 5}
    assert stats["atted_soybean"] == {"edges": 5, "genes_mapped": 4, "unmapped_source_genes": 1, "xref_without_locus_tag": 1}
    bundle = open_bundle("soybean", heavy_bundle.data_dir)
    string = {(row["gene_a"], row["gene_b"]): row["score"] for row in bundle.rows("SELECT gene_a, gene_b, score FROM edges WHERE network = 'string'")}
    assert string[("Glyma.18G092200", "Glyma.19G194300")] == pytest.approx(0.8, abs=1e-3)
    assert string[("Glyma.05G032200", "Glyma.18G273600")] == pytest.approx(0.45, abs=1e-3)
    assert ("Glyma.18G092200", "Glyma.18G092300") not in string
    atted = bundle.rows("SELECT gene_a, gene_b, score, rank FROM edges WHERE network = 'atted' ORDER BY gene_a, rank")
    assert [(row["gene_a"], row["gene_b"], row["rank"]) for row in atted if row["gene_a"] == "Glyma.18G092200"] == [
        ("Glyma.18G092200", "Glyma.19G194300", 1),
        ("Glyma.18G092200", "Glyma.18G092300", 2),
    ]


def test_network_and_coexpression_neighbors(bundle):
    strong = network.network_neighbors(bundle, ["Glyma.18G092200", "Glyma.18G273600"], "string", seeds={"Glyma.19G194300"})
    assert [(row.gene_id, row.neighbor_id, row.neighbor_is_seed) for row in strong] == [
        ("Glyma.18G092200", "Glyma.19G194300", True),
        ("Glyma.18G273600", "Glyma.19G194300", True),
    ]
    assert strong[0].channels == {"experimental": 0.8}
    medium = network.network_neighbors(bundle, ["Glyma.18G273600"], "string", min_score=0.4)
    assert [row.neighbor_id for row in medium] == ["Glyma.19G194300", "Glyma.05G032200"]
    coex = network.network_neighbors(bundle, ["Glyma.18G092200"], "atted")
    assert [(row.neighbor_id, row.score, row.rank) for row in coex] == [("Glyma.19G194300", 6.5, 1), ("Glyma.18G092300", 3.2, 2)]
    for rows in (strong, medium, coex):
        _unique_ids(rows)


def test_rwr_rows_match_the_closed_form():
    graph = network.build_graph([("a", "b", 1.0), ("b", "c", 0.5), ("c", "d", 1.0), ("a", "c", 0.2), ("d", "e", 1.0)])
    restart = 0.5
    rows = graph.rwr_rows(np.arange(len(graph.genes)), restart)
    transition = graph.transition.T.toarray()
    closed = restart * np.linalg.inv(np.eye(len(graph.genes)) - (1 - restart) * transition)
    assert np.allclose(rows, closed.T, atol=1e-6)


def test_seed_propagation_ranks_seed_neighbours_above_distant_genes():
    ring = [(f"g{index}", f"g{(index + 1) % 60}", 1.0) for index in range(60)]
    chords = [(f"g{index}", f"g{(index + 30) % 60}", 1.0) for index in range(0, 60, 7)]
    graph = network.build_graph(ring + chords)
    scores = network.propagate(graph, ["g2", "g31", "g0", "missing"], ["g0", "g1", "g3", "g4"], network="string", trait_key="t", min_score=0.7, n_perm=500)
    by_gene = {score.gene_id: score for score in scores}
    assert by_gene["g2"].empirical_p is not None and by_gene["g2"].empirical_p <= 0.05
    assert by_gene["g31"].empirical_p is not None and by_gene["g31"].empirical_p > 0.2
    assert by_gene["g2"].rank == 1 and by_gene["g0"].is_seed and by_gene["g0"].n_seeds_in_network == 4
    assert not by_gene["missing"].in_network and by_gene["missing"].evidence() == []
    assert by_gene["g2"].top_seeds[0].seed in {"g1", "g3"}
    repeat = network.propagate(graph, ["g2"], ["g0", "g1", "g3", "g4"], network="string", trait_key="t", min_score=0.7, n_perm=500)
    assert repeat[0].empirical_p == by_gene["g2"].empirical_p


def test_seed_propagation_tool_on_the_fixture_network(bundle):
    content, artifact = _tool(
        extended_tools.seed_propagation,
        {"gene_ids": ["Glyma.18G092200", "Glyma.18G273600", "Glyma.05G032200"], "seeds": ["Glyma.19G194300"]},
    )
    assert "from 1 custom seeds (1 in the network): 2 of 3 candidates scored" in content
    assert "not in the string network: Glyma.05G032200" in content and len(artifact["evidence_ids"]) == 2
    content, artifact = _tool(extended_tools.coexpression_neighbors, {"gene_ids": ["Glyma.19G194300"], "min_z": 2.0})
    assert "Glyma.19G194300 -> Glyma.18G092200 z=6.5 rank=1" in content and len(artifact["aliases"]) == 2


def test_regulation_and_pathway_builds_drop_unmapped_ids(heavy_bundle: FixtureBundle):
    stats = heavy_bundle.build_report.stats
    assert stats["planttfdb_soybean"] == {"tf": 2, "tf_with_motifs": 1, "unmapped_genes": 1}
    assert stats["plantregmap_soybean"] == {"cns": 2, "links_with_unmapped_genes": 1, "regulation": 3, "tfbs": 2, "tfbs_unplaced": 1}
    assert stats["pmn_soycyc"] == {"pathway_genes": 2, "unmapped_genes": 1}
    assert stats["plant_reactome"] == {"pathway_genes": 2, "unmapped_genes": 1}


def test_get_regulation_reports_tf_family_targets_and_regulators(bundle):
    records = {row.gene_id: row for row in regulation.get_regulation(bundle, ["Glyma.18G092200", "Glyma.19G194300", "Glyma.05G032200"])}
    wrky = records["Glyma.18G092200"]
    assert (wrky.is_tf, wrky.family, wrky.motif_ids, wrky.n_targets) == (True, "WRKY", ["MP00117"], 1)
    assert wrky.targets[0].evidence == ["FunTFBS", "motif"] and wrky.focus_targets == ["Glyma.19G194300"]
    assert [(partner.gene_id, partner.family) for partner in wrky.regulators] == [("Glyma.18G273600", "MIKC_MADS")]
    target = records["Glyma.19G194300"]
    assert not target.is_tf and target.n_regulators == 1 and target.evidence()[0].subtype == "regulation:target"
    assert records["Glyma.05G032200"].evidence() == []


def test_snps_in_promoter_tfbs_and_conserved_elements(bundle):
    snps = [
        Region(label="S18_9267505", chrom="Gm18", start=9_267_505, end=9_267_505),
        Region(label="S18_9263941", chrom="Gm18", start=9_263_941, end=9_263_941),
        Region(label="S19_45099505", chrom="19", start=45_099_505, end=45_099_505),
        Region(label="S5_1", chrom="5", start=1_000, end=1_000),
    ]
    hits = regulation.snp_in_tfbs_or_cns(bundle, snps)
    found = {(hit.snp, hit.kind, hit.gene_id, hit.relation, hit.tf_gene_id) for hit in hits}
    assert found == {
        ("S18_9267505", "tfbs", "Glyma.18G092200", "promoter", "Glyma.18G273600"),
        ("S18_9263941", "cns", "Glyma.18G092200", "in_gene", None),
        ("S19_45099505", "tfbs", "Glyma.19G194300", "promoter", "Glyma.18G092200"),
    }
    _unique_ids(hits)
    content, artifact = _tool(extended_tools.snp_in_tfbs_or_cns, {"snps": [{"chrom": "18", "pos": 9_267_505, "label": "S18_9267505"}, {"chrom": "5", "pos": 1000, "label": "S5_1"}]})
    assert "in TFBS Glyma.18G273600_1" in content and "-> promoter of Glyma.18G092200" in content
    assert "in no TFBS or conserved element: S5_1" in content and len(artifact["evidence_ids"]) == 1


def test_get_pathways_marks_trait_matching_pathways(bundle):
    rows = pathways.get_pathways(bundle, ["Glyma.18G092300", "Glyma.19G194300", "Glyma.05G032200"], map_trait("plant height", "soybean", bundle))
    by_gene = {row.gene_id: row for row in rows}
    gibberellin = by_gene["Glyma.18G092300"]
    assert (gibberellin.database, gibberellin.pathway_id, gibberellin.reactions, gibberellin.matched) == ("PMN SoyCyc", "PWY-5070", ["RXN-1", "RXN-2"], ["gibberellin"])
    assert by_gene["Glyma.19G194300"].database == "Plant Reactome" and by_gene["Glyma.05G032200"].matched == []
    _unique_ids(rows)
    content, artifact = _tool(extended_tools.get_pathways, {"gene_ids": ["Glyma.18G092300", "Glyma.18G092200"], "trait": "plant height"})
    assert "match=gibberellin" in content and "in no bundled pathway: Glyma.18G092200" in content and len(artifact["evidence_ids"]) == 1


def test_specificity_of_flat_and_single_tissue_profiles():
    assert expression.specificity({"a": 2.0, "b": 2.0, "c": 2.0}) == (0.0, {"a": 0.0, "b": 0.0, "c": 0.0})
    tau, z = expression.specificity({"a": 4.0, "b": 0.0, "c": 0.0})
    assert tau == 1.0 and z["a"] > 1
    assert expression.specificity({"a": 0.0, "b": 0.0})[0] is None
