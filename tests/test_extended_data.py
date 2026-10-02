"""Extended and heavy tiers on fixture data: parsers, query functions and tools."""

import asyncio
from typing import Any

import pytest
from agrihub_fixtures import FixtureBundle

from agrihub.evidence_store import evidence_id_for
from agrihub.tools import extended_tools
from agrihub_data.bundle import open_bundle
from agrihub_data.query import expression
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


def test_specificity_of_flat_and_single_tissue_profiles():
    assert expression.specificity({"a": 2.0, "b": 2.0, "c": 2.0}) == (0.0, {"a": 0.0, "b": 0.0, "c": 0.0})
    tau, z = expression.specificity({"a": 4.0, "b": 0.0, "c": 0.0})
    assert tau == 1.0 and z["a"] > 1
    assert expression.specificity({"a": 0.0, "b": 0.0})[0] is None
