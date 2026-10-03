"""Which evidence domains a species can serve, derived from what was built and installed.

A domain is available when the built bundle has rows in every table it reads
(the counts ``build`` recorded in ``bundle_info``), the networks it walks are
loaded, the heavy resources it needs are unpacked on disk, and the external
binary it runs is installed. Nothing is configured by hand, so a core-only
bundle and a heavy one report themselves correctly. Domains no tier provides
yet are always unavailable.

Results are cached per bundle file and modification time; binary probes are
cached by :mod:`agrihub_data.external`.
"""

import json
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import duckdb
from pydantic import BaseModel

from agrihub_data import external
from agrihub_data.paths import species_paths
from agrihub_data.registry import TIERS, Tier, load_species

LD_PANEL_KIND = "ld_panel"
VEP_CACHE_KIND = "vep_cache"


@dataclass(frozen=True)
class Domain:
    """An evidence domain, the tools that serve it and what they need."""

    key: str
    label: str
    tools: tuple[str, ...]
    tier: Tier | None
    """The tier that provides the domain; ``None`` when no tier does yet."""
    tables: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    binaries: tuple[str, ...] = ()


DOMAINS: tuple[Domain, ...] = (
    Domain(
        "qtl",
        "QTL intervals overlapping the locus (qtl_overlap)",
        ("qtl_overlap",),
        "core",
        tables=("qtl",),
    ),
    Domain(
        "expression",
        "expression in trait-relevant tissues and tissue specificity",
        ("trait_relevant_tissues", "expression_profile", "tissue_specificity"),
        "extended",
        tables=("expression", "samples"),
    ),
    Domain(
        "coexpression",
        "co-expression neighbours",
        ("coexpression_neighbors",),
        "extended",
        tables=("edges",),
        networks=("atted",),
    ),
    Domain(
        "network",
        "network neighbours and seed propagation from known trait genes",
        ("network_neighbors", "seed_propagation"),
        "extended",
        tables=("edges",),
        networks=("string",),
    ),
    Domain(
        "regulation",
        "transcription-factor status, regulators and targets",
        ("get_regulation",),
        "extended",
        tables=("tf", "regulation"),
    ),
    Domain(
        "tfbs_cns",
        "SNPs in promoter TF binding sites or conserved non-coding elements",
        ("snp_in_tfbs_or_cns",),
        "extended",
        tables=("regulatory_regions",),
    ),
    Domain(
        "pathways",
        "metabolic and signalling pathways",
        ("get_pathways",),
        "extended",
        tables=("pathways",),
    ),
    Domain(
        "variant_location",
        "variant location class: CDS, UTR, splice, intron, upstream (gene models)",
        ("annotate_variants",),
        "core",
        tables=("gene_parts",),
    ),
    Domain(
        "variant_consequence",
        "variant consequences with alleles (Ensembl VEP cache)",
        ("annotate_variants",),
        "heavy",
        resources=(VEP_CACHE_KIND,),
        binaries=("vep",),
    ),
    Domain(
        "ld",
        "LD with the lead SNP and LD-based locus windows, define_locus(mode=ld) (PLINK2 on a reference panel or a study VCF)",
        ("ld_with_lead",),
        "heavy",
        resources=(LD_PANEL_KIND,),
        binaries=("plink2",),
    ),
    Domain(
        "homeologs",
        "homeolog pairs from recent-duplication synteny",
        ("homeologs",),
        "heavy",
        tables=("homeologs",),
    ),
    Domain(
        "haplotypes",
        "gene haplotypes and non-synonymous SNPs",
        ("gene_haplotypes",),
        "heavy",
        tables=("gene_haplotypes",),
    ),
    Domain(
        "cross_species_convergence",
        "cross-species convergence: orthologs near same-trait hits in rice, maize or sorghum (cross_species_convergence)",
        ("cross_species_convergence",),
        None,
    ),
    Domain("protein_records", "UniProt protein records (get_protein)", ("get_protein",), None),
    Domain("gene_family", "gene-family trees beyond PANTHER/Pfam labels (gene_family)", ("gene_family",), None),
    Domain("rice_orthologs", "rice orthologs (no bundle loads cross-crop orthologs yet)", (), None),
)
DOMAIN_KEYS = tuple(domain.key for domain in DOMAINS)
SPECIALIST_DOMAINS: dict[str, tuple[str, ...]] = {
    "locus_variant": ("variant_location", "variant_consequence", "tfbs_cns", "ld", "homeologs", "haplotypes"),
    "qtl_gwas": ("qtl", "cross_species_convergence"),
    "function_orthology": ("pathways", "protein_records", "gene_family", "rice_orthologs"),
    "expression_network": ("expression", "coexpression", "network", "regulation"),
    "literature": (),
}
"""The domains each specialist's prompt and tools depend on."""
SPECIALIST_REQUIRES: dict[str, tuple[str, ...]] = {
    "expression_network": ("expression", "coexpression", "network", "regulation"),
}
"""Specialists with no core-tier tools: they need at least one of these domains to run."""
BINARY_HINTS = {
    "plink2": "PLINK2 is not installed (set AGRIHUB_PLINK2 or run scripts/setup_heavy_tools.sh)",
    "vep": "Ensembl VEP is not installed (pull the Docker image with scripts/setup_heavy_tools.sh or set AGRIHUB_VEP)",
}


class DomainStatus(BaseModel):
    """Whether one domain can be served now, and why not."""

    key: str
    label: str
    available: bool
    reason: str | None = None
    tools: list[str]


class BundleFacts(BaseModel):
    """What a built bundle holds, read once per bundle file."""

    built: bool
    tier: str | None = None
    table_counts: dict[str, int] = {}
    networks: dict[str, int] = {}
    resources: dict[str, list[str]] = {}
    """Resource kind -> paths under the species directory."""


_cache: dict[str, tuple[int, BundleFacts]] = {}
_cache_lock = threading.Lock()
_heavy_pin: bool | None = None
"""When set, LD and VEP ignore the host binaries. ``False`` is the fake-LLM recording profile."""


def bundle_facts(species: str, data_dir: Path | str | None = None) -> BundleFacts:
    """Return table counts, loaded networks and heavy resources of a species bundle."""
    path = species_paths(species, data_dir).bundle
    if not path.exists():
        return BundleFacts(built=False)
    key, mtime = str(path.resolve()), path.stat().st_mtime_ns
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    facts = _read_facts(path)
    with _cache_lock:
        _cache[key] = (mtime, facts)
    return facts


def domain_status(species: str, data_dir: Path | str | None = None) -> dict[str, DomainStatus]:
    """Return the status of every domain for a species, keyed by domain."""
    registry = load_species(species)
    root = species_paths(registry.species, data_dir).root
    facts = bundle_facts(registry.species, data_dir)
    return {domain.key: _status(domain, registry.species, facts, root) for domain in DOMAINS}


def available_domains(species: str, data_dir: Path | str | None = None) -> set[str]:
    """Return the keys of the domains that can be served now."""
    return {key for key, status in domain_status(species, data_dir).items() if status.available}


def unavailable_lines(statuses: dict[str, DomainStatus], keys: Iterable[str]) -> tuple[str, ...]:
    """Return prompt lines for the unavailable domains among ``keys``: label and reason."""
    lines = []
    for key in keys:
        status = statuses.get(key)
        if status is not None and not status.available:
            lines.append(f"{status.label}: {status.reason}")
    return tuple(lines)


def tool_is_served(tool: str, available: set[str]) -> bool:
    """Return whether a tool can return data: it belongs to no domain, or to an available one."""
    owners = [domain.key for domain in DOMAINS if tool in domain.tools]
    return not owners or any(key in available for key in owners)


def resource_paths(species: str, kind: str, data_dir: Path | str | None = None) -> list[Path]:
    """Return the on-disk paths of one kind of heavy resource that still exist."""
    root = species_paths(species, data_dir).root
    return [root / relative for relative in bundle_facts(species, data_dir).resources.get(kind, []) if (root / relative).exists()]


def pin_heavy_domains(available: bool | None) -> None:
    """Force LD and variant-consequence availability, ignoring host binaries.

    ``True`` reports both domains available, ``False`` reports them
    unavailable, and ``None`` probes the host again. Fake-LLM recordings pin
    ``False`` so the golden event fixture does not depend on PLINK2 or VEP.
    """
    global _heavy_pin
    _heavy_pin = available


def heavy_domains_pinned() -> bool | None:
    """Return the heavy-domain pin, or ``None`` when the host is probed."""
    return _heavy_pin


def clear_cache() -> None:
    """Forget cached bundle facts (tests)."""
    with _cache_lock:
        _cache.clear()
    external.reset_probes()


def known_gap(species: str, tables: Iterable[str], resources: Iterable[str] = ()) -> str | None:
    """Return why no tier of the species can fill ``tables`` or ``resources``, or ``None`` when a registered source can.

    The registry decides: a table is a gap when no active source's parser
    writes it, a resource kind when no active source uses the parser of that
    name (``ld_panel``, ``vep_cache``). Planned sources that would fill a
    missing table are named with their notes.
    """
    from agrihub_data.build import required_tables

    wanted = list(tables)
    kinds = list(resources)
    if not wanted and not kinds:
        return None
    registry = load_species(species)
    fillable = required_tables(registry, "heavy")
    parsers = {source.parser for source in registry.sources if source.status == "active"}
    missing = [table for table in wanted if table not in fillable] + [kind.replace("_", " ") for kind in kinds if kind not in parsers]
    if not missing:
        return None
    planned = [
        f"{source.name}{f' ({source.notes.strip()})' if source.notes else ''}"
        for source in registry.sources
        if source.status == "planned" and set(source.provides) & set(missing)
    ]
    reason = f"known gap: no registered {species} source provides {', '.join(missing)}"
    return f"{reason}; candidates: {'; '.join(planned)}" if planned else reason


def _read_facts(path: Path) -> BundleFacts:
    connection = duckdb.connect(str(path), read_only=True)
    try:
        info = dict(connection.execute("SELECT key, value FROM bundle_info").fetchall())
        tables = {str(name) for (name,) in connection.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        networks: dict[str, int] = {}
        if "edges" in tables:
            networks = {str(name): int(count) for name, count in connection.execute("SELECT network, count(*) FROM edges GROUP BY 1").fetchall()}
        resources: dict[str, list[str]] = {}
        if "resources" in tables:
            for kind, relative in connection.execute("SELECT kind, path FROM resources ORDER BY resource_id").fetchall():
                resources.setdefault(str(kind), []).append(str(relative))
    finally:
        connection.close()
    counts = {str(name): int(count) for name, count in json.loads(str(info.get("table_counts") or "{}")).items()}
    return BundleFacts(built=True, tier=info.get("tier"), table_counts=counts, networks=networks, resources=resources)


def _status(domain: Domain, species: str, facts: BundleFacts, root: Path) -> DomainStatus:
    def status(reason: str | None) -> DomainStatus:
        return DomainStatus(key=domain.key, label=domain.label, available=reason is None, reason=reason, tools=list(domain.tools))

    if domain.tier is None:
        return status("no tool for this domain in this build")
    gap = known_gap(species, domain.tables, domain.resources)
    if gap is not None:
        return status(gap)
    if not facts.built:
        return status(f"no {species} bundle is built")
    built_tier = facts.tier if facts.tier in TIERS else "core"
    hint = f"build the {domain.tier} tier" if TIERS.index(domain.tier) > TIERS.index(built_tier) else "rebuild the bundle"  # type: ignore[arg-type]
    empty = [table for table in domain.tables if not facts.table_counts.get(table)]
    if empty:
        return status(f"no {', '.join(empty)} rows in the {species} bundle (tier {built_tier}); {hint}")
    missing_networks = [network for network in domain.networks if not facts.networks.get(network)]
    if missing_networks:
        return status(f"no {', '.join(missing_networks)} network in the {species} bundle (tier {built_tier}); {hint}")
    for kind in domain.resources:
        if not any((root / relative).exists() for relative in facts.resources.get(kind, [])):
            return status(f"no {kind.replace('_', ' ')} for {species} (tier {built_tier}); {hint}")
    for binary in domain.binaries:
        if _heavy_pin is False:
            return status(BINARY_HINTS.get(binary, f"{binary} is pinned unavailable"))
        if _heavy_pin is True:
            continue
        if binary == "plink2" and external.plink2_path() is None:
            return status(BINARY_HINTS["plink2"])
        if binary == "vep" and external.vep_runner() is None:
            return status(BINARY_HINTS["vep"])
    return status(None)
