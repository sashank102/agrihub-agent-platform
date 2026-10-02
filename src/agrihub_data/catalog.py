"""What the study form needs to know about each registered species."""

from pathlib import Path
from typing import Any

import duckdb

from agrihub_data.availability import LD_PANEL_KIND, domain_status, resource_paths
from agrihub_data.paths import species_paths
from agrihub_data.registry import TIERS, SpeciesRegistry, load_all


def bundle_status(species: str, data_dir: Path | str | None = None) -> dict[str, Any] | None:
    """Return build metadata of a species bundle, or ``None`` when not built."""
    path = species_paths(species, data_dir).bundle
    if not path.exists():
        return None
    connection = duckdb.connect(str(path), read_only=True)
    try:
        info = dict(connection.execute("SELECT key, value FROM bundle_info").fetchall())
    finally:
        connection.close()
    return {
        "tier": info.get("tier"),
        "built_at": info.get("built_at"),
        "schema_version": info.get("schema_version"),
        "bytes": path.stat().st_size,
    }


def species_summary(registry: SpeciesRegistry, data_dir: Path | str | None = None) -> dict[str, Any]:
    """Return assemblies, chromosome names and lengths, windows, sources, bundle state and evidence domains.

    ``ld`` says whether LD windows can be computed (PLINK2 and a built
    panel) and lists the panels the form may offer.
    """
    bundle = bundle_status(registry.species, data_dir)
    domains = domain_status(registry.species, data_dir)
    ld = domains["ld"]
    panels = [path.name.split(".", 1)[0] for path in resource_paths(registry.species, LD_PANEL_KIND, data_dir)]
    return {
        "species": registry.species,
        "scientific_name": registry.scientific_name,
        "common_name": registry.common_name,
        "taxon_id": registry.taxon_id,
        "canonical_assembly": registry.canonical_assembly,
        "default_window": registry.default_window.model_dump(),
        "typical_ld_kb": registry.typical_ld_kb,
        "ld_note": registry.ld_note,
        "assemblies": [
            {
                "id": assembly.id,
                "aliases": assembly.aliases,
                "description": assembly.description,
                "canonical": assembly.id == registry.canonical_assembly,
                "chromosomes": [
                    {"name": chromosome.name, "length": chromosome.length, "aliases": chromosome.aliases}
                    for chromosome in assembly.chromosomes
                ],
            }
            for assembly in registry.assemblies
        ],
        "chromosome_prefixes": registry.naming.prefixes,
        "linkouts": [linkout.model_dump() for linkout in registry.linkouts],
        "tiers": {
            tier: {
                "sources": len([source for source in registry.sources_for(tier) if source.tier == tier]),
                "built": bool(bundle and bundle.get("tier") == tier),
            }
            for tier in TIERS
        },
        "bundle": bundle,
        "ld": {"available": ld.available, "panels": panels if ld.available else [], "reason": ld.reason},
        "domains": {key: {"available": status.available, "reason": status.reason} for key, status in domains.items()},
        "sources": [
            {
                "id": source.id,
                "name": source.name,
                "tier": source.tier,
                "status": source.status,
                "version": source.version,
                "license": source.license,
                "academic_only": source.academic_only,
                "homepage": source.homepage,
            }
            for source in registry.sources
        ],
    }


def catalog(data_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Return the summary of every registered species."""
    return [species_summary(registry, data_dir) for registry in load_all()]
