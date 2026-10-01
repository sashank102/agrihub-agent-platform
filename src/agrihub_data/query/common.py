"""Types and guards shared by the query functions."""

from pydantic import BaseModel, Field, model_validator

from agrihub_data.bundle import Bundle
from agrihub_data.registry import SpeciesRegistry, load_species, normalize_chrom


class AssemblyMismatchError(ValueError):
    """Coordinates on one assembly were compared with data on another."""


class Region(BaseModel):
    """A labeled interval, 1-based and inclusive, on one assembly.

    A region built around a gene keeps the gene body as ``core_start`` and
    ``core_end``; hits report their distance to the core, not to the flank.
    """

    label: str | None = None
    chrom: str
    start: int = Field(ge=1)
    end: int = Field(ge=1)
    assembly: str | None = None
    snp_pos: int | None = Field(default=None, ge=1)
    core_start: int | None = Field(default=None, ge=1)
    core_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_order(self) -> "Region":
        """Reject intervals whose end precedes their start."""
        if self.end < self.start:
            raise ValueError("region end must not precede start")
        if (self.core_start is None) != (self.core_end is None):
            raise ValueError("core_start and core_end must be given together")
        if self.core_start is not None and self.core_end is not None and self.core_end < self.core_start:
            raise ValueError("core end must not precede core start")
        return self

    @property
    def name(self) -> str:
        """Return the label, or ``chrom:start-end``."""
        return self.label or f"{self.chrom}:{self.start}-{self.end}"

    @property
    def core(self) -> tuple[int, int]:
        """Return the gene body, or the whole region when there is none."""
        if self.core_start is not None and self.core_end is not None:
            return self.core_start, self.core_end
        return self.start, self.end

    def distance_to_core(self, start: int, end: int | None = None) -> int:
        """Return the gap in bp between ``start..end`` and the core; 0 when they overlap."""
        stop = start if end is None else end
        core_start, core_end = self.core
        if stop < core_start:
            return core_start - stop
        if start > core_end:
            return start - core_end
        return 0


def registry_of(bundle: Bundle) -> SpeciesRegistry:
    """Return the registry of a bundle's species."""
    return load_species(bundle.species)


def resolve_region(registry: SpeciesRegistry, region: Region, assembly: str | None = None) -> Region:
    """Return the region with a registered assembly id and canonical chromosome.

    Raises:
        AssemblyMismatchError: when the region and the call name different assemblies.
    """
    stated = region.assembly or assembly
    resolved = registry.assembly(stated).id
    if region.assembly and assembly and registry.assembly(assembly).id != resolved:
        raise AssemblyMismatchError(
            f"region {region.name} is on {resolved} but the query is on {registry.assembly(assembly).id}"
        )
    chrom = normalize_chrom(registry.species, region.chrom, resolved)
    return region.model_copy(update={"assembly": resolved, "chrom": chrom})


def require_same_assembly(region: Region, data_assembly: str, what: str) -> None:
    """Refuse an overlap between a region and data on another assembly."""
    if region.assembly != data_assembly:
        raise AssemblyMismatchError(
            f"{what} are on {data_assembly}; region {region.name} is on {region.assembly}. "
            "Lift the region over first; coordinates are never compared across assemblies."
        )


def source_version(bundle: Bundle, source_id: str, default: str = "unknown") -> str:
    """Return a source's version as stamped in the bundle."""
    return bundle.source_versions().get(source_id, default)
