"""Spot checks against the real soybean extended and heavy bundle."""

import random
import statistics

import pytest

from agent_platform.core.settings import get_data_paths
from agrihub_data import availability, external
from agrihub_data.bundle import close_bundles, open_bundle
from agrihub_data.catalog import bundle_status
from agrihub_data.query import duplication, expression, ld, network, overlap, variants
from agrihub_data.query.traits import map_trait

BUNDLE = get_data_paths().data_dir / "soybean" / "bundle.duckdb"
STATUS = bundle_status("soybean") if BUNDLE.exists() else None
TIER = (STATUS or {}).get("tier")

pytestmark = [
    pytest.mark.bundle,
    pytest.mark.skipif(TIER not in {"extended", "heavy"}, reason=f"no extended soybean bundle at {BUNDLE}"),
]
heavy = pytest.mark.skipif(TIER != "heavy", reason="needs the heavy tier")


@pytest.fixture(scope="module")
def bundle():
    availability.clear_cache()
    opened = open_bundle("soybean")
    yield opened
    close_bundles()


def test_extended_domains_are_available(bundle):
    available = availability.available_domains("soybean")
    assert {"expression", "coexpression", "network", "regulation", "tfbs_cns", "pathways", "variant_location"} <= available


def test_fad2_1a_is_highly_and_specifically_expressed_in_seed(bundle):
    oil = map_trait("seed oil", "soybean", bundle)
    selection = expression.trait_relevant_tissues("soybean", oil, bundle)
    profile = next(row for row in expression.expression_profile(bundle, ["Glyma.10G278000"], selection) if row.dataset == "Sreedasyam_Plott_2023")
    assert profile.in_trait_tissue and profile.trait_max_value is not None and profile.trait_max_value > 300
    assert profile.max_tissue == "seed" and profile.trait_max_sample is not None and profile.trait_max_sample.startswith("seed.")
    specificity = next(row for row in expression.tissue_specificity(bundle, ["Glyma.10G278000"], selection) if row.dataset == "Sreedasyam_Plott_2023")
    assert specificity.top_tissue == "seed" and specificity.trait_tissue_top and specificity.tau is not None and specificity.tau > 0.8


def test_e1_is_a_curated_flowering_gene_and_a_seed(bundle):
    flowering = map_trait("flowering time", "soybean", bundle)
    known = {hit.gene_id: hit for hit in overlap.known_trait_genes(bundle, flowering)}
    assert known["Glyma.06G207800"].trait_match == "ontology" and "GmE1" in known["Glyma.06G207800"].symbols
    assert "Glyma.06G207800" in {seed.gene_id for seed in network.trait_seeds(bundle, flowering)}


def test_flowering_seed_propagation_ranks_seed_neighbours_above_random(bundle):
    flowering = map_trait("flowering time", "soybean", bundle)
    seeds = [seed.gene_id for seed in network.trait_seeds(bundle, flowering)]
    graph = network.network_graph(bundle, "string", network.DEFAULT_MIN_SCORE["string"])
    held_out = [seed for seed in seeds if seed in graph.index][:30]
    rng = random.Random(7)
    background = rng.sample([gene for gene in graph.genes if gene not in set(seeds)], 30)
    scores = {row.gene_id: row for row in network.seed_propagation(bundle, held_out + background, seeds, "string", trait_key=flowering.key)}
    seed_p = [scores[gene].empirical_p for gene in held_out if scores[gene].empirical_p is not None]
    random_p = [scores[gene].empirical_p for gene in background if scores[gene].empirical_p is not None]
    assert statistics.median(seed_p) < 0.2 < statistics.median(random_p)
    assert statistics.median(seed_p) < statistics.median(random_p) / 3
    best = min(held_out, key=lambda gene: scores[gene].rank or 10**6)
    assert scores[best].rank == 1 and scores[best].top_seeds


def test_lead_snp_location_class_in_the_wrky_gene(bundle):
    located = variants.location_classes(bundle, [variants.VariantInput(id="S18_9263941", chrom="18", pos=9_263_941)])
    wrky = next(row for row in located if row.gene_id == "Glyma.18G092200")
    assert wrky.location_class == "intron" and wrky.transcripts


@heavy
def test_vep_agrees_with_the_location_class(bundle):
    if external.vep_runner() is None:
        pytest.skip("Ensembl VEP is not installed (scripts/setup_heavy_tools.sh)")
    rows, notes = variants.annotate_variants(bundle, [variants.VariantInput(id="S18_9263941", chrom="18", pos=9_263_941, ref="A", alt="G")])
    wrky = next(row for row in rows if row.gene_id == "Glyma.18G092200")
    assert wrky.method == "vep" and notes == []
    assert any("intron_variant" in consequence.terms for consequence in wrky.consequences)


@heavy
def test_ld_window_of_the_s18_lead_is_finite_and_plausible(bundle):
    if external.plink2_path() is None:
        pytest.skip("PLINK2 is not installed (scripts/setup_heavy_tools.sh)")
    window = ld.ld_window(bundle, "S18_9263941", "18", 9_263_941)
    assert window.genotypes == "Song_Hyten_2015" and window.n_partners > 0
    assert window.start <= 9_263_941 <= window.end
    assert 50_000 <= window.span_bp <= 1_500_000
    assert window.proxy_distance_bp <= ld.PROXY_MAX_BP


@heavy
def test_known_duplicated_pairs_are_homeologs(bundle):
    pairs = {(pair.gene_id, pair.homeolog_id) for pair in duplication.homeologs(bundle, ["Glyma.10G278000", "Glyma.19G194300"])}
    assert ("Glyma.10G278000", "Glyma.20G111000") in pairs
    assert ("Glyma.19G194300", "Glyma.03G194700") in pairs
