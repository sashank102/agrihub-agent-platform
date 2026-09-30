"""Species registry: assemblies, chromosome names, ID namespaces and sources.

One YAML file per species lives in ``agrihub_data/registry/``. Chromosome
names from any source are normalized only through :func:`normalize_chrom`,
which reads the aliases declared there.
"""

import re
from functools import cache
from importlib import resources
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Tier = Literal["core", "extended", "heavy"]
TIERS: tuple[Tier, ...] = ("core", "extended", "heavy")
NON_GENOMIC_ASSEMBLY = "none"
"""The ``assembly`` value of rows that are not tied to a genome (ontology terms)."""


class UnknownSpeciesError(KeyError):
    """The species has no registry file."""


class UnknownAssemblyError(ValueError):
    """The assembly is not registered for the species."""


class UnknownChromosomeError(ValueError):
    """The chromosome name does not match any registered alias."""


class Chromosome(BaseModel):
    """One assembled molecule with its canonical name and length in bp."""

    name: str
    number: int | None = None
    length: int = Field(gt=0)
    aliases: list[str] = Field(default_factory=list)


class Assembly(BaseModel):
    """A reference assembly and the names sources use for it."""

    id: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    ncbi_accession: str | None = None
    annotation: str | None = None
    chrom_namespace: str | None = None
    """Source prefix stripped before matching, e.g. ``glyma.Wm82.gnm2.``."""
    scaffold_pattern: str | None = None
    """Regex for unplaced scaffolds kept verbatim after the namespace."""
    embedded_in_marker_names: bool = False
    """Marker names such as ``BARC_1.01_Gm01_24939_A_G`` carry positions on this assembly."""
    chromosomes: list[Chromosome]

    def chromosome(self, name: str) -> Chromosome | None:
        """Return the chromosome with this canonical name."""
        for chromosome in self.chromosomes:
            if chromosome.name == name:
                return chromosome
        return None


class ChromosomeNaming(BaseModel):
    """How numbered chromosome names are spelled across sources."""

    canonical: str
    """``str.format`` template for the canonical name, e.g. ``Gm{number:02d}``."""
    prefixes: list[str] = Field(default_factory=list)
    """Case-insensitive prefixes accepted before the number (``Gm``, ``Chr``)."""


class IdNamespace(BaseModel):
    """A gene or marker identifier family."""

    id: str
    pattern: str
    assembly: str | None = None
    description: str = ""
    example: str | None = None


class Window(BaseModel):
    """The species default window around a SNP."""

    flank_bp: int = Field(gt=0)
    warn_above_bp: int | None = None


class Linkout(BaseModel):
    """A URL template for a gene or region page."""

    name: str
    kind: Literal["gene", "region", "publication"] = "gene"
    template: str
    id_namespace: str | None = None


class SourceFile(BaseModel):
    """One file of a source, saved under ``raw/<source_id>/<path>``."""

    url: str
    path: str
    sha256: str | None = None
    """Pinned checksum; a fetch that downloads different bytes fails."""
    size: int | None = None
    optional: bool = False


class Collection(BaseModel):
    """A directory listing whose entries each contribute the same files.

    ``files`` are templates with ``{entry}``; the listing is read at fetch
    time and every resolved URL is recorded in the manifest.
    """

    listing_url: str
    entry_pattern: str
    files: list[SourceFile]


class Source(BaseModel):
    """A dataset with its version, license and download locations."""

    id: str
    name: str
    tier: Tier
    version: str
    license: str
    academic_only: bool = False
    rolling: bool = False
    """The upstream file changes in place; no checksum is pinned."""
    homepage: str | None = None
    citation: str | None = None
    assemblies: list[str] = Field(default_factory=list)
    parser: str | None = None
    status: Literal["active", "planned"] = "active"
    notes: str | None = None
    files: list[SourceFile] = Field(default_factory=list)
    collection: Collection | None = None


class ReferenceAssembly(BaseModel):
    """An assembly of another species whose ids appear in this bundle."""

    species: str
    assembly: str


class SpeciesRegistry(BaseModel):
    """Everything the tools need to know about one species."""

    species: str
    scientific_name: str
    common_name: str
    taxon_id: int
    abbrev: str
    canonical_assembly: str
    default_window: Window
    typical_ld_kb: float = Field(gt=0)
    ld_note: str = ""
    naming: ChromosomeNaming
    assemblies: list[Assembly]
    reference_assemblies: list[ReferenceAssembly] = Field(default_factory=list)
    id_namespaces: list[IdNamespace] = Field(default_factory=list)
    linkage_groups: dict[str, int] = Field(default_factory=dict)
    linkouts: list[Linkout] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_references(self) -> "SpeciesRegistry":
        """Reject a registry whose assemblies or sources contradict each other."""
        assembly_ids = {assembly.id for assembly in self.assemblies}
        if self.canonical_assembly not in assembly_ids:
            raise ValueError(f"canonical assembly {self.canonical_assembly} is not listed")
        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        known = assembly_ids | {ref.assembly for ref in self.reference_assemblies}
        for source in self.sources:
            unknown = set(source.assemblies) - known - {NON_GENOMIC_ASSEMBLY}
            if unknown:
                raise ValueError(f"source {source.id} names unregistered assemblies {sorted(unknown)}")
        return self

    def assembly(self, assembly_id: str | None = None) -> Assembly:
        """Return an assembly by id or alias; the canonical one when ``None``."""
        wanted = assembly_id or self.canonical_assembly
        folded = wanted.casefold()
        for assembly in self.assemblies:
            if assembly.id.casefold() == folded or folded in {
                alias.casefold() for alias in assembly.aliases
            }:
                return assembly
        raise UnknownAssemblyError(
            f"{wanted!r} is not a registered {self.species} assembly; "
            f"known: {', '.join(assembly.id for assembly in self.assemblies)}"
        )

    def registered_assemblies(self) -> set[str]:
        """Return every assembly id a bundle row of this species may carry."""
        return {assembly.id for assembly in self.assemblies} | {
            ref.assembly for ref in self.reference_assemblies
        }

    def source(self, source_id: str) -> Source:
        """Return one source by id."""
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(f"unknown {self.species} source {source_id!r}")

    def sources_for(self, tier: Tier) -> list[Source]:
        """Return active sources up to and including ``tier``."""
        allowed = TIERS[: TIERS.index(tier) + 1]
        return [
            source
            for source in self.sources
            if source.tier in allowed and source.status == "active"
        ]

    def chromosome_for_linkage_group(self, group: str) -> str | None:
        """Return the canonical chromosome for a classical linkage group name."""
        for name, number in self.linkage_groups.items():
            if name.casefold() == group.strip().casefold():
                return self.naming.canonical.format(number=number)
        return None


_registered: dict[str, SpeciesRegistry] = {}


def register_species(registry: SpeciesRegistry) -> None:
    """Let an in-memory registry take precedence over the packaged file (test fixtures)."""
    _registered[registry.species] = registry


def unregister_species(species: str) -> None:
    """Drop an in-memory registry registered with :func:`register_species`."""
    _registered.pop(species, None)


def species_names() -> list[str]:
    """Return every species with a registry file or an in-memory registry, sorted."""
    folder = resources.files("agrihub_data") / "registry"
    packaged = {
        entry.name.removesuffix(".yaml")
        for entry in folder.iterdir()
        if entry.name.endswith(".yaml")
    }
    return sorted(packaged | set(_registered))


def load_species(species: str) -> SpeciesRegistry:
    """Return one validated species registry."""
    name = species.strip().lower()
    return _registered.get(name) or _load_packaged(name)


@cache
def _load_packaged(name: str) -> SpeciesRegistry:
    path = resources.files("agrihub_data") / "registry" / f"{name}.yaml"
    if not path.is_file():
        raise UnknownSpeciesError(
            f"no registry for species {name!r}; known: {', '.join(species_names())}"
        )
    return SpeciesRegistry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_all() -> list[SpeciesRegistry]:
    """Load every species registry."""
    return [load_species(name) for name in species_names()]


def normalize_chrom(species: str, chrom: str | int, assembly: str | None = None) -> str:
    """Return the canonical chromosome name for any registered spelling.

    Accepts bare numbers (``18``, ``01``), prefixed names in any case
    (``Gm18``, ``Chr18``, ``chr01``, ``Chr1``), source-namespaced names
    (``glyma.Wm82.gnm2.Gm18``), explicit aliases and unplaced scaffolds.
    ``assembly`` defaults to the canonical assembly.

    Raises:
        UnknownChromosomeError: when nothing in the registry matches, when the
            chromosome does not exist in the assembly, or when the name is
            namespaced for a different assembly.
    """
    registry = load_species(species)
    target = registry.assembly(assembly)
    raw = str(chrom).strip()
    for candidate in registry.assemblies:
        if candidate.chrom_namespace and raw.startswith(candidate.chrom_namespace):
            if candidate is not target:
                raise UnknownChromosomeError(
                    f"{raw!r} is named for {candidate.id}, not {target.id}; "
                    "lift over coordinates before comparing assemblies"
                )
            raw = raw[len(candidate.chrom_namespace) :]
            break
    name = _match_chromosome(registry, target, raw)
    if name is None:
        raise UnknownChromosomeError(
            f"{chrom!r} is not a {registry.species} chromosome in {target.id}"
        )
    return name


def _match_chromosome(
    registry: SpeciesRegistry,
    assembly: Assembly,
    raw: str,
) -> str | None:
    folded = raw.casefold()
    for chromosome in assembly.chromosomes:
        if folded == chromosome.name.casefold() or folded in {
            alias.casefold() for alias in chromosome.aliases
        }:
            return chromosome.name
    if assembly.scaffold_pattern and re.fullmatch(assembly.scaffold_pattern, raw):
        return raw
    prefixes = sorted(registry.naming.prefixes, key=len, reverse=True)
    pattern = rf"(?:{'|'.join(re.escape(prefix) for prefix in prefixes)})?[_ ]?0*(\d+)"
    match = re.fullmatch(pattern, raw, flags=re.IGNORECASE)
    if match is None:
        return None
    number = int(match.group(1))
    for chromosome in assembly.chromosomes:
        if chromosome.number == number:
            return chromosome.name
    return None
