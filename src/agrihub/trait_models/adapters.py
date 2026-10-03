"""Model adapters: decide whether a registered model applies to a trait study and produce ranked SNPs.

Every adapter returns :class:`RankedSNP` rows on the assembly its output
uses, with the model's own ``score_type``. Scores of different types are
never combined or compared.
"""

import csv
import math
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from agrihub.state import SnpInput, TraitStudy
from agrihub.trait_models.registry import (
    CANONICAL,
    ModelSpec,
    ScoreType,
    TraitSpec,
    load_model_registry,
    results_root,
)
from agrihub_data.bundle import BundleMissingError, open_bundle
from agrihub_data.query.traits import map_trait
from agrihub_data.registry import (
    UnknownAssemblyError,
    UnknownSpeciesError,
    load_species,
)

CATALOG_TOP_N = 20
CATALOG_SPACING_BP = 250_000
_WORD = re.compile(r"[a-z0-9]+")


class Applicability(BaseModel):
    """Whether a model applies to a study, the reasons, and the datasets it could read."""

    ok: bool
    reasons: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    """Precomputed result sets for the trait, newest last (``PH_2019``)."""
    trait: str | None = None
    """The registered trait the study matched."""


class RankedSNP(BaseModel):
    """One SNP a model proposes, on the assembly its output uses."""

    marker: str
    chrom: str
    pos: int = Field(ge=1)
    assembly: str
    score: float | None = None
    score_type: ScoreType
    model_id: str
    p_value: float | None = None
    rank: int | None = None

    def as_input(self) -> SnpInput:
        """Return the SNP as a study input that intake places and lifts."""
        return SnpInput(
            raw=self.marker,
            chrom=self.chrom,
            pos=self.pos,
            score=self.score,
            p_value=self.p_value,
            method=self.model_id,
            score_type=self.score_type,
            model_id=self.model_id,
        )


class ModelAdapter(Protocol):
    """Runs one registered model for a trait study."""

    spec: ModelSpec

    def applicable(self, study: TraitStudy) -> Applicability:
        """Return whether the model can propose SNPs for ``study``, and why not."""
        ...

    def run(self, study: TraitStudy, dataset: str | None = None) -> list[RankedSNP]:
        """Return the model's ranked SNPs for ``study``."""
        ...


def output_assembly(spec: ModelSpec, species: str) -> str:
    """Return the assembly the model's positions are on."""
    assembly = spec.assemblies[0] if spec.assemblies else CANONICAL
    return load_species(species).canonical_assembly if assembly == CANONICAL else assembly


def _assembly_reasons(spec: ModelSpec, study: TraitStudy) -> list[str]:
    """Explain why the model's positions cannot reach the study assembly."""
    registry = load_species(study.species)
    source = output_assembly(spec, study.species)
    try:
        lifted = registry.assembly(source).lift_to or source
    except UnknownAssemblyError:
        return [f"its output assembly {source} is not registered for {study.species}"]
    if lifted != study.assembly:
        via = f" (lifted to {lifted})" if lifted != source else ""
        return [f"its positions are on {source}{via}, not the study assembly {study.assembly}"]
    return []


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.casefold()))


def match_trait(traits: Iterable[TraitSpec], study: TraitStudy) -> TraitSpec | None:
    """Return the registered trait the study names by code, name or term."""
    wanted = " ".join([study.trait_text, *study.trait_terms]).casefold().strip()
    tokens = _words(wanted)
    for trait in traits:
        if trait.code.casefold() in tokens:
            return trait
        for phrase in (trait.name, *trait.terms):
            phrase_words = _words(phrase)
            if trait.defined and phrase_words and phrase_words <= tokens:
                return trait
    return None


class PrecomputedResultsAdapter:
    """Reads a team's precomputed top-SNP files (``top50_..._{trait}_{year}_lee.csv``)."""

    def __init__(self, spec: ModelSpec) -> None:
        """Bind the adapter to its registry entry."""
        self.spec = spec

    def applicable(self, study: TraitStudy) -> Applicability:
        """Match species, trait and assembly, then look for the trait's result files."""
        if not self.spec.covers_species(study.species):
            return Applicability(ok=False, reasons=[f"registered for {', '.join(self.spec.species)}, not {study.species}"])
        trait = match_trait(self.spec.traits, study)
        if trait is None:
            names = ", ".join(f"{item.code} ({item.name})" for item in self.spec.traits)
            return Applicability(ok=False, reasons=[f"no result set for {study.trait_text!r}; it covers {names}"])
        reasons = _assembly_reasons(self.spec, study)
        datasets = self._datasets(trait.code)
        if not datasets:
            reasons.append(f"no {trait.code} result files under {results_root()}")
        return Applicability(ok=not reasons, reasons=reasons, datasets=datasets, trait=trait.code)

    def run(self, study: TraitStudy, dataset: str | None = None) -> list[RankedSNP]:
        """Read one result file (the requested dataset, or the newest year)."""
        applicability = self.applicable(study)
        if not applicability.ok:
            raise ValueError("; ".join(applicability.reasons))
        chosen = dataset if dataset in applicability.datasets else applicability.datasets[-1]
        code, _, year = str(chosen).partition("_")
        assembly = output_assembly(self.spec, study.species)
        snps: list[RankedSNP] = []
        with self._path(code, year).open(encoding="utf-8", newline="") as handle:
            for index, row in enumerate(csv.DictReader(handle), start=1):
                if not row.get("chrom") or not row.get("pos"):
                    continue
                snps.append(
                    RankedSNP(
                        marker=(row.get("rs") or f"S{row['chrom']}_{row['pos']}").strip(),
                        chrom=row["chrom"].strip(),
                        pos=int(row["pos"]),
                        assembly=assembly,
                        score=float(row["score"]) if row.get("score") else None,
                        score_type=self.spec.score_type,
                        model_id=self.spec.id,
                        rank=int(row["rank"]) if (row.get("rank") or "").isdigit() else index,
                    )
                )
        return snps

    def path_for(self, dataset: str) -> str:
        """Return the file a dataset is read from."""
        code, _, year = dataset.partition("_")
        return str(self._path(code, year))

    def _path(self, code: str, year: str) -> Path:
        return results_root() / str(self.spec.path).format(trait=code, year=year)

    def _datasets(self, code: str) -> list[str]:
        if not self.spec.path:
            return []
        pattern = str(self.spec.path).format(trait=code, year="*")
        years = []
        for path in sorted(results_root().glob(pattern)):
            match = re.search(rf"_{re.escape(code)}_(\d{{4}})_", path.name)
            if match:
                years.append(match.group(1))
        return [f"{code}_{year}" for year in sorted(set(years))]


class CatalogTopHitsAdapter:
    """The bundle's catalog GWAS hits for the trait: previously published associations, not a new analysis."""

    def __init__(self, spec: ModelSpec) -> None:
        """Bind the adapter to its registry entry."""
        self.spec = spec

    def applicable(self, study: TraitStudy) -> Applicability:
        """Need a bundle whose catalog has hits matching the trait on the study assembly."""
        reasons = _assembly_reasons(self.spec, study)
        if reasons:
            return Applicability(ok=False, reasons=reasons)
        try:
            hits = self._hits(study)
        except BundleMissingError:
            return Applicability(ok=False, reasons=[f"no {study.species} data bundle is built"])
        if not hits:
            return Applicability(ok=False, reasons=[f"the {study.species} catalog has no GWAS hits matching {study.trait_text!r}"])
        return Applicability(ok=True, reasons=[f"{len(hits)} catalog hits match the trait"])

    def run(self, study: TraitStudy, dataset: str | None = None) -> list[RankedSNP]:
        """Return up to ``CATALOG_TOP_N`` hits, the most significant first, one per ``CATALOG_SPACING_BP``."""
        kept: list[RankedSNP] = []
        for row in self._hits(study):
            chrom, pos = str(row["chrom"]), int(row["pos"])
            if any(item.chrom == chrom and abs(item.pos - pos) < CATALOG_SPACING_BP for item in kept):
                continue
            p_value = float(row["p_value"]) if row["p_value"] is not None else None
            kept.append(
                RankedSNP(
                    marker=str(row["marker"] or f"{row['source_db']}:{row['hit_id']}"),
                    chrom=chrom,
                    pos=pos,
                    assembly=study.assembly,
                    score=-math.log10(p_value) if p_value else None,
                    score_type=self.spec.score_type,
                    model_id=self.spec.id,
                    p_value=p_value if p_value is not None and 0 <= p_value <= 1 else None,
                    rank=len(kept) + 1,
                )
            )
            if len(kept) >= CATALOG_TOP_N:
                break
        return kept

    def _hits(self, study: TraitStudy) -> list[dict[str, Any]]:
        bundle = open_bundle(study.species)
        profile = map_trait(study.trait_text, study.species, bundle)
        wanted = set(profile.expanded_ids) | set(profile.ids())
        rows = bundle.rows(
            "SELECT hit_id, source_db, marker, chrom, pos, p_value, trait_name, trait_terms FROM gwas_hits "
            "WHERE assembly = ? AND chrom IS NOT NULL AND pos IS NOT NULL ORDER BY p_value NULLS LAST, hit_id",
            [study.assembly],
        )
        return [
            row
            for row in rows
            if wanted & set(row["trait_terms"] or []) or profile.matched_keywords(str(row["trait_name"] or ""))
        ]


class PlannedAdapter:
    """A model that is registered but not runnable yet (GAPIT/rMVP scans, live GNN runs)."""

    def __init__(self, spec: ModelSpec) -> None:
        """Bind the adapter to its registry entry."""
        self.spec = spec

    def applicable(self, study: TraitStudy) -> Applicability:
        """Never applicable; say what it would need."""
        needs = ", ".join(self.spec.required_inputs) or "a runner"
        return Applicability(ok=False, reasons=[f"planned, not yet runnable; it will need {needs}"])

    def run(self, study: TraitStudy, dataset: str | None = None) -> list[RankedSNP]:
        """Refuse to run."""
        raise NotImplementedError(f"{self.spec.id} is planned and has no runner yet")


def adapter_for(spec: ModelSpec) -> ModelAdapter:
    """Return the adapter that runs ``spec``."""
    if spec.status == "planned" or spec.adapter in {"gwas_runner", "gnn_runner"}:
        return PlannedAdapter(spec)
    if spec.adapter == "precomputed_results":
        return PrecomputedResultsAdapter(spec)
    return CatalogTopHitsAdapter(spec)


class Candidate(BaseModel):
    """A registered model with its applicability to one study."""

    model_id: str
    name: str
    adapter: str
    status: str
    score_type: ScoreType
    label: str
    assembly: str
    applicability: Applicability
    benchmarks: list[dict[str, object]] = Field(default_factory=list)


def evaluate_models(study: TraitStudy) -> list[Candidate]:
    """Return every model registered for the study's species, applicable ones first."""
    try:
        load_species(study.species)
    except UnknownSpeciesError:
        return []
    candidates = []
    for spec in load_model_registry().models:
        if not spec.covers_species(study.species):
            continue
        adapter = adapter_for(spec)
        candidates.append(
            Candidate(
                model_id=spec.id,
                name=spec.name,
                adapter=spec.adapter,
                status=spec.status,
                score_type=spec.score_type,
                label=spec.label,
                assembly=output_assembly(spec, study.species),
                applicability=adapter.applicable(study),
                benchmarks=[benchmark.model_dump() for benchmark in spec.benchmarks],
            )
        )
    candidates.sort(key=lambda item: not item.applicability.ok)
    return candidates
