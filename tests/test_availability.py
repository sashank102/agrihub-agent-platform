"""Evidence domains are derived from what a bundle holds and which binaries are installed."""

import json
import stat
from pathlib import Path

import duckdb
import pytest

from agrihub import planning
from agrihub.nodes.harvest import brief_domains
from agrihub.nodes.specialist import SPECS
from agrihub.prompts import PromptContext, render
from agrihub_data import availability


def _bundle(data_dir: Path, tier: str, counts: dict[str, int], networks: dict[str, int] | None = None, resources: dict[str, str] | None = None) -> None:
    root = data_dir / "soybean"
    root.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(root / "bundle.duckdb"))
    connection.execute("CREATE TABLE bundle_info (key VARCHAR, value VARCHAR)")
    connection.executemany("INSERT INTO bundle_info VALUES (?, ?)", [("tier", tier), ("table_counts", json.dumps(counts))])
    if networks is not None:
        connection.execute("CREATE TABLE edges (network VARCHAR)")
        for network, count in networks.items():
            connection.executemany("INSERT INTO edges VALUES (?)", [(network,)] * count)
    if resources is not None:
        connection.execute("CREATE TABLE resources (resource_id VARCHAR, kind VARCHAR, path VARCHAR)")
        for kind, relative in resources.items():
            connection.execute("INSERT INTO resources VALUES (?, ?, ?)", [kind, kind, relative])
            (root / relative).mkdir(parents=True, exist_ok=True)
    connection.close()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGRIHUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGRIHUB_PLINK2", str(tmp_path / "missing-plink2"))
    monkeypatch.setenv("AGRIHUB_VEP_IMAGE", "agrihub-test/no-such-image:0")
    monkeypatch.setenv("PATH", "/nonexistent")
    availability.clear_cache()
    yield tmp_path
    availability.clear_cache()


CORE = {"genes": 10, "gene_parts": 40, "annotation": 5}
EXTENDED = {**CORE, "expression": 100, "samples": 4, "edges": 6, "tf": 2, "regulation": 3, "regulatory_regions": 3, "pathways": 5}


def _brief(statuses: dict[str, availability.DomainStatus]) -> dict:
    return {
        "domains": brief_domains(statuses),
        "loci": [
            {
                "locus_id": "L1",
                "top": [{"gene_id": "Glyma.18G092200", "dist": 0, "why": ["D 3: keyword"]}, {"gene_id": "Glyma.18G092300", "dist": 900, "why": []}],
                "known_genes": [],
            }
        ],
    }


def test_core_only_bundle_declines_expression_network_with_the_bundle_reason(data_dir: Path):
    _bundle(data_dir, "core", CORE)
    statuses = availability.domain_status("soybean")
    assert statuses["variant_location"].available
    for key in ("expression", "coexpression", "network", "regulation", "pathways"):
        assert not statuses[key].available
    assert statuses["expression"].reason == "no expression, samples rows in the soybean bundle (tier core); build the extended tier"
    assert statuses["cross_species_convergence"].reason == "no tool for this domain in this build"

    _, dispatches, skipped = planning.plan_from_brief(_brief(statuses), ["locus_variant", "expression_network"], 12, "plant height")
    assert [item["specialist"] for item in dispatches] == ["locus_variant"]
    reason = next(item["reason"] for item in skipped if item["specialist"] == "expression_network")
    assert "no expression, samples rows in the soybean bundle (tier core); build the extended tier" in reason


def test_extended_bundle_dispatches_expression_network_and_its_prompt_names_no_expression_gap(data_dir: Path):
    _bundle(data_dir, "extended", EXTENDED, networks={"string": 4, "atted": 2})
    statuses = availability.domain_status("soybean")
    assert {"expression", "coexpression", "network", "regulation", "tfbs_cns", "pathways"} <= availability.available_domains("soybean")
    assert not statuses["ld"].available and "build the heavy tier" in str(statuses["ld"].reason)

    _, dispatches, skipped = planning.plan_from_brief(_brief(statuses), ["locus_variant", "expression_network"], 12, "plant height")
    expression = next(item for item in dispatches if item["specialist"] == "expression_network")
    assert expression["focus_gene_ids"] == ["Glyma.18G092200", "Glyma.18G092300"]
    assert not any(item["specialist"] == "expression_network" for item in skipped)

    spec = SPECS["expression_network"]
    lines = availability.unavailable_lines(statuses, spec.prompt.domains)
    assert lines == ()
    text = render(spec.prompt, PromptContext(species="soybean", assembly="Wm82.a2.v1", trait="plant height", max_steps=6, unavailable=lines))
    assert "Unavailable domains" not in text and "expression in trait-relevant tissues" not in text


def test_a_network_missing_from_edges_keeps_its_domain_unavailable(data_dir: Path):
    _bundle(data_dir, "extended", EXTENDED, networks={"string": 4})
    statuses = availability.domain_status("soybean")
    assert statuses["network"].available
    assert statuses["coexpression"].reason == "no atted network in the soybean bundle (tier extended); rebuild the bundle"


def test_heavy_domains_need_their_resources_and_binaries(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    heavy = {**EXTENDED, "homeologs": 2, "gene_haplotypes": 3}
    _bundle(data_dir, "heavy", heavy, networks={"string": 1, "atted": 1}, resources={"ld_panel": "ld/panel", "vep_cache": "vep"})
    statuses = availability.domain_status("soybean")
    assert statuses["homeologs"].available and statuses["haplotypes"].available
    assert statuses["ld"].reason == availability.BINARY_HINTS["plink2"]
    assert statuses["variant_consequence"].reason == availability.BINARY_HINTS["vep"]
    lines = availability.unavailable_lines(statuses, SPECS["locus_variant"].prompt.domains)
    assert any(line.startswith("LD with the lead SNP") and "PLINK2 is not installed" in line for line in lines)

    plink2 = data_dir / "plink2"
    plink2.write_text("#!/bin/sh\n")
    plink2.chmod(plink2.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("AGRIHUB_PLINK2", str(plink2))
    availability.clear_cache()
    assert availability.domain_status("soybean")["ld"].available


def test_tools_of_unavailable_domains_are_not_bound(data_dir: Path):
    _bundle(data_dir, "core", CORE)
    available = availability.available_domains("soybean")
    assert availability.tool_is_served("annotate_variants", available)
    assert not availability.tool_is_served("ld_with_lead", available)
    assert availability.tool_is_served("genes_in_window", available)


def test_no_bundle_makes_every_domain_unavailable(data_dir: Path):
    statuses = availability.domain_status("soybean")
    assert not any(status.available for status in statuses.values())
    assert statuses["expression"].reason == "no soybean bundle is built"
