"""Validate the study request, place its SNPs, and route by input mode."""

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Overwrite
from pydantic import ValidationError

from agent_platform.services.errors import RunInputError
from agrihub import events
from agrihub.state import (
    LiftedFrom,
    SnpInput,
    SnpStudy,
    StudyInputIssue,
    StudyState,
    StudyWarning,
    TraitStudy,
    parse_study,
)
from agrihub_data.availability import domain_status
from agrihub_data.bundle import Bundle, BundleMissingError, open_bundle
from agrihub_data.query.ids import parse_positional, resolve_marker
from agrihub_data.query.liftover import LiftAnchorsMissingError, liftover_for
from agrihub_data.registry import (
    SpeciesRegistry,
    UnknownAssemblyError,
    UnknownChromosomeError,
    UnknownSpeciesError,
    load_species,
    normalize_chrom,
)

MAX_DETAIL_WARNINGS = 5
OUT_OF_BOUNDS_SHARE = 0.02
"""Above this share of positions past chromosome ends, the input is probably on another assembly."""
_UNION_TAG_ERRORS = frozenset({"union_tag_invalid", "union_tag_not_found"})


class StudyInputError(ValueError, RunInputError):
    """The study request is malformed, names an unknown species or assembly, or has no placeable SNP."""


@dataclass
class StudyCheck:
    """The outcome of validating a study request and placing its SNPs.

    ``study`` is normalized to the registry's species and assembly ids;
    ``placed`` are the deduplicated SNPs on that assembly. Any ``errors``
    mean the study cannot run, and ``detail`` says why in one line.
    """

    study: SnpStudy | TraitStudy | None = None
    placed: list[SnpInput] = field(default_factory=list)
    warnings: list[StudyWarning] = field(default_factory=list)
    errors: list[StudyInputIssue] = field(default_factory=list)
    detail: str = ""


def check_study(raw: Any) -> StudyCheck:
    """Validate a study request and place its SNPs without running anything.

    This is intake's validation, shared with the ``/studies/validate`` dry
    run. It reads the species registry and, for marker ids, the bundle; it
    writes nothing.
    """
    if not raw:
        issue = StudyInputIssue(loc=["study"], message="agrihub_study input needs a 'study' object")
        return StudyCheck(errors=[issue], detail=issue.message)
    try:
        study = parse_study(raw)
    except ValidationError as exc:
        errors = schema_issues(exc, raw)
        reasons = "; ".join(_issue_text(issue) for issue in errors[:MAX_DETAIL_WARNINGS])
        return StudyCheck(errors=errors, detail=f"the study request is invalid: {reasons}")
    try:
        registry = load_species(study.species)
        requested = registry.assembly(study.assembly)
    except (UnknownSpeciesError, UnknownAssemblyError) as exc:
        message = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
        loc: list[str | int] = ["species"] if isinstance(exc, UnknownSpeciesError) else ["assembly"]
        return StudyCheck(errors=[StudyInputIssue(loc=loc, message=message)], detail=message)
    assembly = requested.lift_to or requested.id
    window = study.window
    if "window" not in study.model_fields_set or "flank_bp" not in window.model_fields_set:
        window = window.model_copy(update={"flank_bp": registry.default_window.flank_bp})
    study = study.model_copy(
        update={
            "species": registry.species,
            "assembly": assembly,
            "lifted_from_assembly": requested.id if requested.lift_to else None,
            "window": window,
        }
    )
    warnings = window_warnings(registry, study)
    if not isinstance(study, SnpStudy):
        return StudyCheck(study=study, warnings=warnings, detail="trait mode: SNPs come from the model step")
    placed, snp_warnings = place_and_lift(study, study.snps, requested.id)
    warnings.extend(snp_warnings)
    if not placed:
        reasons = "; ".join(warning.message for warning in snp_warnings[:MAX_DETAIL_WARNINGS])
        detail = f"none of the {len(study.snps)} SNPs could be placed on {assembly}: {reasons}"
        return StudyCheck(
            study=study,
            warnings=warnings,
            errors=[StudyInputIssue(loc=["snps"], message=detail)],
            detail=detail,
        )
    detail = f"{len(placed)} valid SNPs of {len(study.snps)} submitted"
    if warnings:
        detail += f"; {len(warnings)} warnings"
    return StudyCheck(study=study, placed=placed, warnings=warnings, detail=detail)


def schema_issues(exc: ValidationError, raw: Any) -> list[StudyInputIssue]:
    """Turn Pydantic errors into ``{loc, message}`` issues located in the request.

    The ``mode`` discriminator tag that Pydantic prefixes to every location
    is dropped, so ``loc`` reads ``["snps", 0, "pos"]``.
    """
    mode = raw.get("mode") if isinstance(raw, dict) else None
    issues = []
    for error in exc.errors(include_url=False):
        loc: list[str | int] = list(error.get("loc") or ())
        if error.get("type") in _UNION_TAG_ERRORS:
            loc = ["mode"]
        elif loc and mode is not None and loc[0] == mode:
            loc = loc[1:]
        issues.append(StudyInputIssue(loc=loc, message=str(error.get("msg") or "invalid value")))
    return issues


async def intake(state: StudyState, config: RunnableConfig) -> dict[str, Any]:
    """Validate the study, place and dedupe SNPs, and reset per-study state.

    The form guarantees completeness, so there is no clarification step.
    Dropped or doubtful inputs become warnings in state and in the
    ``run.phase(intake)`` event. A malformed request, an unknown species or
    assembly, or a study with no placeable SNP fails the phase with a
    readable event carrying ``errors[]``, and the run stops.

    Raises:
        StudyInputError: when the study cannot run.
    """
    events.phase("intake")
    check = await asyncio.to_thread(check_study, state.get("study"))
    if check.errors or check.study is None:
        events.phase(
            "intake",
            "failed",
            detail=check.detail,
            warnings=_dump(check.warnings),
            errors=[issue.model_dump() for issue in check.errors],
        )
        raise StudyInputError(check.detail)
    study = check.study
    events.phase("intake", "completed", detail=check.detail, warnings=_dump(check.warnings))
    return {
        "study": study.model_dump(mode="json"),
        "snps": [snp.model_dump(mode="json") for snp in check.placed],
        "warnings": _dump(check.warnings),
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
        "study_run_id": "",
        "evidence_snapshot_id": None,
        "run_status": "running",
    }


def route_after_intake(state: StudyState) -> Literal["model_agent", "locus_builder"]:
    """Send trait studies through the model agent and SNP studies to loci."""
    if (state.get("study") or {}).get("mode") == "trait":
        return "model_agent"
    return "locus_builder"


def window_warnings(registry: SpeciesRegistry, study: SnpStudy | TraitStudy) -> list[StudyWarning]:
    """Warn when the flank is more than twice the species' typical LD distance, or LD windows cannot be computed."""
    if study.window.mode == "ld":
        status = domain_status(registry.species).get("ld")
        if status is not None and not status.available and not study.genotype_vcf_ref:
            return [StudyWarning(code="ld_unavailable", message=f"LD windows are unavailable ({status.reason}); fixed ±{study.window.flank_bp // 1_000} kb windows will be used")]
        return []
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


def place_and_lift(
    study: SnpStudy | TraitStudy,
    snps: list[SnpInput],
    given_on: str | None = None,
) -> tuple[list[SnpInput], list[StudyWarning]]:
    """Place SNPs given on ``given_on`` (the study assembly by default) and lift them to the study assembly.

    When at least two SNPs, and more than ``OUT_OF_BOUNDS_SHARE`` of them,
    fall past chromosome ends on ``given_on``, a warning says they are
    probably on another assembly.
    """
    source = load_species(study.species).assembly(given_on or study.assembly)
    placed, warnings = place_snps(study, snps, source.id)
    beyond = sum(1 for warning in warnings if warning.code == "out_of_bounds")
    if beyond >= 2 and beyond / len(snps) > OUT_OF_BOUNDS_SHARE:
        warnings.append(
            StudyWarning(
                code="assembly_mismatch_suspected",
                message=(
                    f"{beyond} of {len(snps)} positions ({beyond / len(snps):.0%}) fall past chromosome ends on "
                    f"{source.id}; the SNPs are probably on another assembly"
                ),
            )
        )
    if source.lift_to is None or not placed:
        return placed, warnings
    lifted, lift_warnings = lift_snps(study.species, source.id, placed)
    return lifted, [*warnings, *lift_warnings]


def lift_snps(species: str, given_on: str, snps: list[SnpInput]) -> tuple[list[SnpInput], list[StudyWarning]]:
    """Lift placed SNPs from ``given_on`` to its ``lift_to`` assembly through the bundle's gene anchors.

    Each lifted SNP keeps its original position, the method and the
    confidence in ``lifted_from``. SNPs that cannot be lifted are dropped
    with a warning; low-confidence lifts are kept and flagged.
    """
    registry = load_species(species)
    source = registry.assembly(given_on)
    target = registry.assembly(source.lift_to)
    try:
        liftover = liftover_for(
            open_bundle(registry.species),
            source.id,
            target.id,
            {chromosome.name: chromosome.length for chromosome in target.chromosomes},
        )
    except (BundleMissingError, LiftAnchorsMissingError) as exc:
        message = f"SNPs on {source.id} cannot be lifted to {target.id}: {exc}"
        return [], [StudyWarning(code="lift_unavailable", message=message)]
    warnings: list[StudyWarning] = []
    lifted: list[SnpInput] = []
    levels: Counter[str] = Counter()
    for snp in snps:
        chrom, pos = str(snp.chrom), int(snp.pos or 0)
        result = liftover.lift(chrom, pos)
        if not result.ok or result.confidence is None:
            warnings.append(
                StudyWarning(
                    code="unlifted",
                    message=f"{snp.raw}: {source.id} {chrom}:{pos} could not be lifted to {target.id}: {result.detail}",
                    snp=snp.raw,
                )
            )
            continue
        levels[result.confidence] += 1
        origin = LiftedFrom(
            assembly=source.id,
            chrom=chrom,
            pos=pos,
            method=result.method,
            confidence=result.confidence,
            detail=result.detail,
            anchors=list(result.anchors),
        )
        lifted.append(snp.model_copy(update={"chrom": result.chrom, "pos": result.pos, "lifted_from": origin}))
        if result.confidence == "low":
            warnings.append(
                StudyWarning(
                    code="lift_low_confidence",
                    message=f"{snp.raw}: lifted from {source.id} {chrom}:{pos} to {result.chrom}:{result.pos} with low confidence ({result.detail})",
                    snp=snp.raw,
                )
            )
    counts = ", ".join(f"{levels[level]} {level}" for level in ("high", "medium", "low") if levels[level])
    dropped = len(snps) - len(lifted)
    warnings.insert(
        0,
        StudyWarning(
            code="lifted_assembly",
            message=(
                f"{len(lifted)} of {len(snps)} SNPs were lifted from {source.id} to {target.id} through one-to-one "
                f"pangene gene anchors ({counts or 'none'})" + (f"; {dropped} could not be lifted" if dropped else "")
            ),
        ),
    )
    unique, duplicates = _dedupe(lifted)
    return unique, [*warnings, *duplicates]


def place_snps(
    study: SnpStudy | TraitStudy,
    snps: list[SnpInput],
    assembly: str | None = None,
) -> tuple[list[SnpInput], list[StudyWarning]]:
    """Normalize chromosomes, resolve marker ids, check bounds and dedupe.

    Every returned SNP has a canonical chromosome and a position on
    ``assembly`` (the study assembly by default). A SNP given only as
    ``raw`` text is parsed as a positional id first and resolved as a marker
    name otherwise. Positions are never taken from another assembly: a BARC
    name's embedded Wm82.a1 position is ignored in favour of the marker-set
    placement on the study assembly, and flagged.
    """
    registry = load_species(study.species)
    target = registry.assembly(assembly or study.assembly)
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
            located = None if snp.marker_id else parse_positional(registry.species, snp.raw, target.id)
            if located is None:
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
    unique, duplicates = _dedupe(placed)
    return unique, [*warnings, *duplicates]


def _dedupe(snps: list[SnpInput]) -> tuple[list[SnpInput], list[StudyWarning]]:
    warnings: list[StudyWarning] = []
    unique: list[SnpInput] = []
    first: dict[tuple[str, int], SnpInput] = {}
    for snp in snps:
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
    marker = (snp.marker_id or snp.raw).strip()
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


def _issue_text(issue: StudyInputIssue) -> str:
    where = ".".join(str(part) for part in issue.loc)
    return f"{where}: {issue.message}" if where else issue.message
