"""Species registry, chromosome aliases, positional SNP ids and the registry route."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_platform.api.dependencies import require_ready
from agent_platform.api.routes import registry as registry_routes
from agent_platform.core.settings import Settings
from agrihub_data.query.ids import parse_positional, resolve_marker
from agrihub_data.query.loci import define_locus
from agrihub_data.registry import (
    UnknownAssemblyError,
    UnknownChromosomeError,
    UnknownSpeciesError,
    load_all,
    load_species,
    normalize_chrom,
    species_names,
)

CANONICAL = {
    "soybean": ("Wm82.a2.v1", 250_000, 20),
    "rice": ("IRGSP-1.0", 150_000, 14),
    "maize": ("Zm-B73-REFERENCE-NAM-5.0", 50_000, 10),
    "sorghum": ("Sorghum_bicolor_NCBIv3", 100_000, 10),
}


def test_registry_covers_four_species_with_defaults_ld_and_linkouts():
    assert species_names() == ["maize", "rice", "sorghum", "soybean"]
    for registry in load_all():
        assembly, flank, chromosomes = CANONICAL[registry.species]
        assert registry.canonical_assembly == assembly
        assert registry.default_window.flank_bp == flank
        assert len(registry.assembly().chromosomes) == chromosomes
        assert registry.typical_ld_kb > 0 and registry.ld_note
        assert registry.linkouts and all("{" in linkout.template for linkout in registry.linkouts)
        assert all(source.license for source in registry.sources)
    soybean = load_species("soybean")
    assert {assembly.id for assembly in soybean.assemblies} == {
        "Wm82.a1.v1",
        "Wm82.a2.v1",
        "Wm82.a4.v1",
        "Wm82.a6.v1",
    }
    assert soybean.assembly("Gmax_275_Wm82.a2.v1").id == "Wm82.a2.v1"
    assert soybean.assembly().chromosome("Gm18").length == 58_018_742
    assert soybean.source("gwas_atlas").academic_only
    assert not soybean.source("lis_wm82_a2").academic_only
    assert soybean.chromosome_for_linkage_group("A1") == "Gm05"
    assert load_species("maize").default_window.warn_above_bp == 50_000
    with pytest.raises(UnknownSpeciesError):
        load_species("wheat")
    with pytest.raises(UnknownAssemblyError):
        soybean.assembly("Wm83.a2.v1")


@pytest.mark.parametrize(
    ("species", "raw", "assembly", "expected"),
    [
        ("soybean", "Gm18", None, "Gm18"),
        ("soybean", "Chr18", None, "Gm18"),
        ("soybean", "chr18", None, "Gm18"),
        ("soybean", "18", None, "Gm18"),
        ("soybean", 18, None, "Gm18"),
        ("soybean", "glyma.Wm82.gnm2.Gm18", None, "Gm18"),
        ("soybean", "glyma.Wm82.gnm4.Gm18", "Wm82.a4.v1", "Gm18"),
        ("soybean", "Gm05", "a4", "Gm05"),
        ("soybean", "glyma.Wm82.gnm2.scaffold_298", None, "scaffold_298"),
        ("rice", "chr01", None, "chr01"),
        ("rice", "Chr1", None, "chr01"),
        ("rice", "Mt", None, "chrMt"),
        ("maize", "Chr1", None, "chr1"),
        ("maize", "chr01", None, "chr1"),
        ("sorghum", "Chr1", None, "Chr01"),
        ("sorghum", "chr01", None, "Chr01"),
    ],
)
def test_normalize_chrom_accepts_registered_spellings(species, raw, assembly, expected):
    assert normalize_chrom(species, raw, assembly) == expected


@pytest.mark.parametrize(
    ("raw", "assembly", "message"),
    [
        ("21", None, "not a soybean chromosome"),
        ("A1", None, "not a soybean chromosome"),
        ("glyma.Wm82.gnm4.Gm18", None, "named for Wm82.a4.v1, not Wm82.a2.v1"),
        ("glyma.Wm82.gnm2.Gm18", "Wm82.a6.v1", "named for Wm82.a2.v1, not Wm82.a6.v1"),
    ],
)
def test_normalize_chrom_refuses_unknown_names_and_other_assemblies(raw, assembly, message):
    with pytest.raises(UnknownChromosomeError, match=message):
        normalize_chrom("soybean", raw, assembly)


@pytest.mark.parametrize(
    "raw",
    ["S18_9263941", "Chr18:9263941", "chr18_9263941", "18 9263941", "Gm18:9263941", "18:9263941"],
)
def test_positional_snp_ids_parse_without_a_bundle(raw):
    assert parse_positional("soybean", raw) == ("Gm18", 9_263_941)
    hits = resolve_marker("soybean", raw)
    assert [(hit.assembly, hit.chrom, hit.pos) for hit in hits] == [("Wm82.a2.v1", "Gm18", 9_263_941)]
    assert hits[0].flags == ["positional_id", "assembly_assumed"]
    stated = resolve_marker("soybean", raw, "Wm82.a4.v1")
    assert stated[0].assembly == "Wm82.a4.v1" and stated[0].flags == ["positional_id"]


def test_positional_ids_beyond_the_chromosome_do_not_resolve():
    assert resolve_marker("soybean", "S18_99999999") == []
    assert parse_positional("soybean", "ss715631025") is None


def test_barc_names_are_flagged_as_a1_positions_without_a_bundle():
    hits = resolve_marker("soybean", "BARC_1.01_Gm01_24939_A_G")
    assert [(hit.assembly, hit.chrom, hit.pos) for hit in hits] == [("Wm82.a1.v1", "Gm01", 24_939)]
    assert "a1_embedded_position" in hits[0].flags and "other_assembly" in hits[0].flags


def test_define_locus_uses_species_defaults_and_clamps():
    window = define_locus("soybean", "18", 9_263_941)
    assert (window.chrom, window.start, window.end, window.flank_bp, window.clamped) == (
        "Gm18",
        9_013_941,
        9_513_941,
        250_000,
        False,
    )
    edge = define_locus("soybean", "Gm01", 100_000)
    assert edge.start == 1 and edge.clamped
    tail = define_locus("soybean", "Gm18", 58_000_000)
    assert tail.end == 58_018_742
    wide = define_locus("maize", "chr1", 1_000_000, flank_bp=250_000)
    assert wide.warning and "typical LD" in wide.warning
    assert define_locus("maize", "chr1", 1_000_000).warning is None
    with pytest.raises(ValueError, match="mode='fixed'"):
        define_locus("soybean", "18", 9_263_941, mode="ld")
    with pytest.raises(ValueError, match="outside"):
        define_locus("soybean", "18", 60_000_000)


def _registry_app(auth_mode: str) -> FastAPI:
    app = FastAPI()
    app.include_router(registry_routes.router)
    app.state.settings = Settings(
        ENVIRONMENT="test",
        AUTH_MODE=auth_mode,
        API_KEY_PEPPER="test-pepper-0123456789",
        DATABASE_URI="postgresql://agent_platform:agent_platform@localhost:5432/agent_platform",
        _env_file=None,
    )

    async def record_auth_failure(reason: str) -> None:
        return None

    app.state.accounts = SimpleNamespace(record_auth_failure=record_auth_failure)
    app.dependency_overrides[require_ready] = lambda: None
    return app


def test_registry_species_route_lists_four_species(tmp_path, monkeypatch):
    monkeypatch.setenv("AGRIHUB_DATA_DIR", str(tmp_path))

    async def scenario() -> tuple[int, list[dict], int]:
        async with AsyncClient(transport=ASGITransport(app=_registry_app("disabled")), base_url="http://test") as client:
            listed = await client.get("/registry/species")
        async with AsyncClient(transport=ASGITransport(app=_registry_app("api_key")), base_url="http://test") as client:
            refused = await client.get("/registry/species")
        return listed.status_code, listed.json(), refused.status_code

    status, body, refused = asyncio.run(scenario())
    assert status == 200
    assert [item["species"] for item in body] == ["maize", "rice", "sorghum", "soybean"]
    soybean = body[-1]
    assert soybean["canonical_assembly"] == "Wm82.a2.v1"
    assert soybean["default_window"]["flank_bp"] == 250_000
    canonical = next(assembly for assembly in soybean["assemblies"] if assembly["canonical"])
    assert canonical["chromosomes"][17] == {"name": "Gm18", "length": 58_018_742, "aliases": []}
    assert soybean["bundle"] is None and soybean["tiers"]["core"]["built"] is False
    assert any(source["id"] == "gwas_atlas" and source["academic_only"] for source in soybean["sources"])
    assert refused == 401
