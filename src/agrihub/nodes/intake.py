"""Validate the study request, place its SNPs, and route by input mode."""

import asyncio
from collections import defaultdict
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Overwrite

from agrihub import events
from agrihub.state import (
    SnpInput,
    SnpStudy,
    StudyState,
    StudyWarning,
    TraitStudy,
    parse_study,
)
from agrihub_data.bundle import Bundle, BundleMissingError, open_bundle
from agrihub_data.query.ids import parse_positional, resolve_marker
from agrihub_data.registry import (
    SpeciesRegistry,
    UnknownAssemblyError,
    UnknownChromosomeError,
    UnknownSpeciesError,
    load_species,
    normalize_chrom,
)

MAX_DETAIL_WARNINGS = 5


class StudyInputError(ValueError):
    """Nothing in the study can be placed on its species and assembly."""


async def intake(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Validate the study, place and dedupe SNPs, and reset per-study state.

    The form guarantees completeness, so there is no clarification step.
    Dropped or doubtful inputs become warnings in state and in the
    ``run.phase(intake)`` event. When no SNP is left the phase fails with a
    readable event and the run stops.

    Raises:
        StudyInputError: when the species or assembly is unknown, or no SNP is valid.
    """
    events.phase("intake")
    raw = state.get("study")
    if not raw:
        raise ValueError("agrihub_study input needs a 'study' object")
    study = parse_study(raw)
    try:
        registry = load_species(study.species)
        assembly = registry.assembly(study.assembly).id
    except (UnknownSpeciesError, UnknownAssemblyError) as exc:
        message = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        events.phase("intake", "failed", detail=message)
        raise StudyInputError(message) from exc
    study = study.model_copy(update={"species": registry.species, "assembly": assembly})
    warnings = window_warnings(registry, study)
    snps: list[dict[str, Any]] = []
    detail = "trait mode: SNPs come from the model step"
    if isinstance(study, SnpStudy):
        placed, snp_warnings = await asyncio.to_thread(place_snps, study, study.snps)
        warnings.extend(snp_warnings)
        snps = [snp.model_dump(mode="json") for snp in placed]
        if not placed:
            reasons = "; ".join(warning.message for warning in snp_warnings[:MAX_DETAIL_WARNINGS])
            detail = f"none of the {len(study.snps)} SNPs could be placed on {assembly}: {reasons}"
            events.phase("intake", "failed", detail=detail, warnings=_dump(warnings))
            raise StudyInputError(detail)
        detail = f"{len(placed)} valid SNPs of {len(study.snps)} submitted"
        if warnings:
            detail += f"; {len(warnings)} warnings"
    events.phase("intake", "completed", detail=detail, warnings=_dump(warnings))
    return {
        "study": study.model_dump(mode="json"),
        "snps": snps,
        "warnings": _dump(warnings),
        "model_result": {},
        "loci": [],
        "candidates": [],
        "triage_brief": {},
        "dispatches": [],
        "findings": Overwrite([]),
        "specialist_results": Overwrite([]),
        "round": 0,
        "ranking": [],
        "scoring": {},
        "report": None,
        "run_status": "running",
    }


def route_after_intake(state: StudyState) -> Literal["model_agent", "locus_builder"]:
    """Send trait studies through the model agent and SNP studies to loci."""
    if (state.get("study") or {}).get("mode") == "trait":
        return "model_agent"
    return "locus_builder"


def window_warnings(registry: SpeciesRegistry, study: SnpStudy | TraitStudy) -> list[StudyWarning]:
    """Warn when the flank is more than twice the species' typical LD distance."""
    limit_bp = int(2 * registry.typical_ld_kb * 1_000)
    if study.window.mode == "fixed" and study.window.flank_bp > limit_bp:
        return [
            StudyWarning(
                code="window_exceeds_ld",
                message=(
                    f"±{study.window.flank_bp // 1_000} kb is more than twice the typical "
                    f"{registry.species} LD of {registry.typical_ld_kb:g} kb; loci will hold many "
                    "genes unrelated to the SNP"
                ),
            )
        ]
    return []


def place_snps(study: SnpStudy | TraitStudy, snps: list[SnpInput]) -> tuple[list[SnpInput], list[StudyWarning]]:
    """Normalize chromosomes, resolve marker ids, check bounds and dedupe.

    Every returned SNP has a canonical chromosome and a position on the
    study assembly. Positions are never taken from another assembly: a BARC
    name's embedded Wm82.a1 position is ignored in favour of the marker-set
    placement on the study assembly, and flagged.
    """
    registry = load_species(study.species)
    target = registry.assembly(study.assembly)
    warnings: list[StudyWarning] = []
    bundle: Bundle | None = None
    bundle_checked = False
    spellings: dict[str, set[str]] = defaultdict(set)
    placed: list[SnpInput] = []
    for snp in snps:
        spelling = None
        if snp.chrom is not None and snp.pos is not None:
            try:
                chrom = normalize_chrom(registry.species, snp.chrom, target.id)
            except UnknownChromosomeError as exc:
                warnings.append(StudyWarning(code="unknown_chromosome", message=str(exc), snp=snp.raw))
                continue
            spelling = snp.chrom.strip()
            pos = snp.pos
            named = parse_positional(registry.species, snp.raw, target.id)
            if named is not None and named != (chrom, pos):
                warnings.append(
                    StudyWarning(
                        code="position_mismatch",
                        message=f"{snp.raw} names {named[0]}:{named[1]} but was given as {chrom}:{pos}; using {chrom}:{pos}",
                        snp=snp.raw,
                    )
                )
        else:
            if not bundle_checked:
                bundle_checked = True
                try:
                    bundle = open_bundle(registry.species)
                except BundleMissingError:
                    bundle = None
            located = _resolve(registry, target.id, snp, bundle, warnings)
            if located is None:
                continue
            chrom, pos = located
        chromosome = target.chromosome(chrom)
        if chromosome is not None and pos > chromosome.length:
            warnings.append(
                StudyWarning(
                    code="out_of_bounds",
                    message=f"{snp.raw}: {chrom}:{pos} is beyond the end of {chrom} ({chromosome.length} bp on {target.id})",
                    snp=snp.raw,
                )
            )
            continue
        if spelling is not None:
            spellings[chrom].add(spelling)
        placed.append(snp.model_copy(update={"chrom": chrom, "pos": pos}))
    for chrom, given in sorted(spellings.items()):
        if len(given) > 1:
            warnings.append(
                StudyWarning(
                    code="chromosome_aliases",
                    message=f"{chrom} was given as {', '.join(sorted(given))}; all were normalized to {chrom}",
                )
            )
    unique: list[SnpInput] = []
    first: dict[tuple[str, int], SnpInput] = {}
    for snp in placed:
        key = (str(snp.chrom), int(snp.pos or 0))
        if key in first:
            warnings.append(
                StudyWarning(
                    code="duplicate",
                    message=f"{snp.raw} is the same position as {first[key].raw} ({key[0]}:{key[1]}); kept once",
                    snp=snp.raw,
                )
            )
            continue
        first[key] = snp
        unique.append(snp)
    return unique, warnings


def _resolve(
    registry: SpeciesRegistry,
    assembly: str,
    snp: SnpInput,
    bundle: Bundle | None,
    warnings: list[StudyWarning],
) -> tuple[str, int] | None:
    marker = str(snp.marker_id)
    hits = resolve_marker(registry.species, marker, assembly, bundle)
    on_target = [hit for hit in hits if hit.assembly == assembly]
    if any("a1_embedded_position" in hit.flags for hit in hits):
        outcome = (
            f"using its {assembly} marker-set placement {on_target[0].chrom}:{on_target[0].pos}"
            if on_target
            else f"it has no {assembly} placement and was dropped"
        )
        warnings.append(
            StudyWarning(
                code="a1_position",
                message=f"{marker} embeds a Wm82.a1 position that is not comparable with {assembly}; {outcome}",
                snp=snp.raw,
            )
        )
    if not on_target:
        if not any("a1_embedded_position" in hit.flags for hit in hits):
            reason = "the soybean bundle is not built" if bundle is None and not hits else f"not found on {assembly}"
            warnings.append(
                StudyWarning(code="unresolved_marker", message=f"{marker} could not be placed: {reason}", snp=snp.raw)
            )
        return None
    positions = sorted({(hit.chrom, hit.pos) for hit in on_target})
    if len(positions) > 1:
        warnings.append(
            StudyWarning(
                code="ambiguous_marker",
                message=f"{marker} has {len(positions)} placements on {assembly} "
                f"({', '.join(f'{chrom}:{pos}' for chrom, pos in positions[:3])}); dropped",
                snp=snp.raw,
            )
        )
        return None
    return positions[0]


def _dump(warnings: list[StudyWarning]) -> list[dict[str, Any]]:
    return [warning.model_dump(exclude_none=True) for warning in warnings]
