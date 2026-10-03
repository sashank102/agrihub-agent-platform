"""Spot checks on the built rice, maize and sorghum bundles (skipped when a bundle is not built)."""

import gc
from collections.abc import Iterator

import pytest
import yaml

from agent_platform.core.settings import get_data_paths
from agrihub_data.availability import domain_status
from agrihub_data.bundle import close_bundles, open_bundle
from agrihub_data.query import overlap, traits
from agrihub_data.query.common import Region
from agrihub_data.query.ids import map_gene_ids
from agrihub_data.query.traits import map_trait
from agrihub_data.registry import load_species, normalize_chrom
from agrihub_data.verify import verify

DATA = get_data_paths().data_dir
pytestmark = pytest.mark.bundle


@pytest.fixture(autouse=True, scope="module")
def _release_bundles() -> Iterator[None]:
    """Drop the cereal bundles and their ontology indexes so later tests run on a small heap."""
    yield
    close_bundles()
    for cache in (traits._indexes, traits._children, traits._names):
        cache.clear()
    gc.collect()


def _built(species: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(not (DATA / species / "bundle.duckdb").exists(), reason=f"no {species} bundle at {DATA}")


@_built("rice")
def test_rice_sd1_is_a_curated_plant_height_gene_and_msu_ids_map_to_rap():
    bundle = open_bundle("rice")
    assert verify("rice", checksums=False).ok
    profile = map_trait("plant height", "rice", bundle)
    region = Region(label="sd1", chrom=normalize_chrom("rice", "Chr1"), start=38_300_000, end=38_450_000, snp_pos=38_383_500, assembly="IRGSP-1.0")
    sd1 = [hit for hit in overlap.known_trait_genes(bundle, profile, region=region) if hit.gene_id == "Os01g0883800"]
    assert {hit.source_db for hit in sd1 if hit.trait_match == "ontology"} >= {"RAP-DB curated genes", "Oryzabase"}
    assert all("TO:0000207" in hit.matched for hit in sd1 if hit.trait_match == "ontology")
    [mapped] = map_gene_ids(bundle, ["LOC_Os01g66100"])
    assert (mapped.to_id, mapped.relation, mapped.exists) == ("Os01g0883800", "synonym", True)


@_built("maize")
def test_maize_lifted_gwas_hits_land_in_their_reported_v5_genes_and_qtl_is_a_known_gap():
    bundle = open_bundle("maize")
    assert verify("maize", checksums=False).ok
    [row] = bundle.rows(
        """
        SELECT count(*) AS lifted,
               count(*) FILTER (WHERE h.pos BETWEEN g.start AND g."end") AS inside
        FROM gwas_hits AS h JOIN genes AS g ON g.assembly = h.assembly AND list_contains(h.reported_genes, g.gene_id)
        WHERE h.placement LIKE 'lifted:B73_RefGen_v4:%'
        """
    )
    assert row["lifted"] > 1_000 and row["inside"] > 100
    assert not bundle.rows("SELECT 1 FROM gwas_hits WHERE assembly = 'B73_RefGen_v4' LIMIT 1")
    status = domain_status("maize")["qtl"]
    assert not status.available and status.reason and status.reason.startswith("known gap")


@_built("sorghum")
def test_sorghum_dw3_is_a_major_gene_and_sbi14_rows_are_dropped():
    bundle = open_bundle("sorghum")
    assert verify("sorghum", checksums=False).ok
    profile = map_trait("plant height", "sorghum", bundle)
    dw3 = [hit for hit in overlap.known_trait_genes(bundle, profile) if "Dw3" in hit.symbols]
    assert [(hit.gene_id, hit.chrom, hit.start, hit.end, hit.trait_match) for hit in dw3] == [
        ("SORBI_3007G163800", "Chr07", 59_821_905, 59_829_921, "ontology")
    ]
    assert map_gene_ids(bundle, ["Sobic.007G163800"])[0].to_id == "SORBI_3007G163800"
    assert domain_status("sorghum")["expression"].reason.startswith("known gap")  # type: ignore[union-attr]


@pytest.mark.parametrize("species", ["rice", "maize", "sorghum"])
def test_trait_profile_terms_exist_in_the_fetched_ontologies(species: str):
    if not (DATA / species / "bundle.duckdb").exists():
        pytest.skip(f"no {species} bundle")
    from importlib import resources

    profiles = yaml.safe_load((resources.files("agrihub_data.query") / "traits" / f"{species}.yaml").read_text(encoding="utf-8"))["profiles"]
    ids = sorted({term for profile in profiles for term in profile["terms"]})
    assert {profile["key"] for profile in profiles} >= {"plant_height", "flowering_time", "grain_yield"}
    rows = open_bundle(species).rows_raw(
        f"SELECT term_id FROM ontology_terms WHERE NOT is_obsolete AND term_id IN ({', '.join('?' for _ in ids)})", ids
    )
    assert sorted(str(row[0]) for row in rows) == ids
    assert load_species(species).canonical_assembly
