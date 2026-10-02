"""Expression in trait-relevant tissues and tissue specificity.

Trait-relevant tissues come from the curated ``query/tissues/<species>.yaml``:
the entry for the trait profile's key, else the first keyword rule matching
the trait text. Values are per-sample means (TPM) on the canonical assembly.

Tissue specificity uses tissue means of ``log2(TPM + 1)``: ``tau`` (Yanai et
al. 2005) is ``sum(1 - x_i / x_max) / (n - 1)``, 0 for ubiquitous and 1 for
single-tissue genes, and the z-score of each tissue is taken over the
gene's tissue means.
"""

import math
import re
from collections import defaultdict
from functools import cache
from importlib import resources
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from agrihub.state import EvidenceItem
from agrihub_data.bundle import Bundle
from agrihub_data.query.common import registry_of
from agrihub_data.query.traits import TraitProfile

EXPRESSED_TPM = 1.0
"""A gene counts as expressed in a sample at this TPM or above."""
TOP_SAMPLES = 5
TissueOrigin = Literal["curated", "rule", "requested", "none"]


class TissueSelection(BaseModel):
    """The tissues (and optional stages) where a trait is expected to act."""

    trait_key: str
    tissues: list[str]
    stages: list[str] = Field(default_factory=list)
    origin: TissueOrigin
    note: str | None = None
    in_bundle: dict[str, list[str]] = Field(default_factory=dict)
    """Dataset -> the selected tissues it has samples for."""


class SampleValue(BaseModel):
    """One sample's mean value."""

    sample: str
    tissue: str
    stage: str | None = None
    value: float


class ExpressionProfile(BaseModel):
    """A gene's expression in one dataset, with its trait-tissue summary."""

    gene_id: str
    assembly: str
    dataset: str
    unit: str
    trait_key: str
    trait_tissues: list[str]
    n_samples: int
    max_value: float
    max_sample: str
    max_tissue: str
    trait_max_value: float | None = None
    trait_max_sample: str | None = None
    trait_mean: float | None = None
    expressed: bool
    in_trait_tissue: bool
    top: list[SampleValue]
    source_gene_id: str
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the profile as one expression fact for this trait's tissues."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="expression",
                subtype=f"expression_profile:{self.dataset}",
                value=self.model_dump(exclude={"gene_id", "assembly", "source_version"}),
                source_db=f"LIS expression {self.dataset}",
                db_version=self.source_version,
                source_record=f"{self.dataset}:{self.source_gene_id}|{self.trait_key}:{','.join(self.trait_tissues)}",
            )
        ]


class TissueSpecificity(BaseModel):
    """How specific a gene's expression is across the tissues of one dataset."""

    gene_id: str
    assembly: str
    dataset: str
    unit: str
    trait_key: str
    trait_tissues: list[str]
    n_tissues: int
    tau: float | None
    top_tissue: str | None
    top_value: float
    tissue_means: dict[str, float]
    z: dict[str, float]
    trait_z: float | None = None
    trait_tissue_top: bool
    source_gene_id: str
    source_version: str

    def evidence(self) -> list[EvidenceItem]:
        """Return the specificity as one expression fact for this trait's tissues."""
        return [
            EvidenceItem(
                gene_id=self.gene_id,
                category="expression",
                subtype=f"tissue_specificity:{self.dataset}",
                value=self.model_dump(exclude={"gene_id", "assembly", "source_version"}),
                source_db=f"LIS expression {self.dataset}",
                db_version=self.source_version,
                source_record=f"{self.dataset}:{self.source_gene_id}|{self.trait_key}:{','.join(self.trait_tissues)}",
            )
        ]


def trait_relevant_tissues(
    species: str,
    profile: TraitProfile | None,
    bundle: Bundle | None = None,
    tissues: list[str] | None = None,
) -> TissueSelection:
    """Return the tissues where ``profile``'s trait acts, and which datasets sample them."""
    curated = _curated(species)
    key = profile.key if profile else "custom"
    if tissues:
        selection = TissueSelection(trait_key="custom" if profile is None else key, tissues=list(tissues), origin="requested")
    elif profile is not None and profile.key in curated["profiles"]:
        entry = curated["profiles"][profile.key]
        selection = TissueSelection(
            trait_key=key,
            tissues=list(entry.get("tissues") or []),
            stages=list(entry.get("stages") or []),
            origin="curated",
            note=entry.get("note"),
        )
    else:
        text = " ".join([profile.query, *profile.keywords]) if profile else ""
        rule = next((rule for rule in curated["rules"] if any(_has_phrase(text, keyword) for keyword in rule.get("keywords") or [])), None)
        selection = (
            TissueSelection(trait_key=key, tissues=list(rule.get("tissues") or []), origin="rule", note=rule.get("note"))
            if rule
            else TissueSelection(trait_key=key, tissues=[], origin="none", note="no curated tissues for this trait")
        )
    if bundle is not None and selection.tissues:
        placeholders = ", ".join("?" for _ in selection.tissues)
        for dataset, tissue in bundle.rows_raw(
            f"SELECT DISTINCT dataset, tissue FROM samples WHERE tissue IN ({placeholders}) ORDER BY 1, 2",
            selection.tissues,
        ):
            selection.in_bundle.setdefault(str(dataset), []).append(str(tissue))
    return selection


def expression_profile(
    bundle: Bundle,
    gene_ids: list[str],
    selection: TissueSelection,
    dataset: str | None = None,
) -> list[ExpressionProfile]:
    """Return each gene's expression per dataset with its maximum in the trait tissues."""
    profiles = []
    for (gene_id, name), rows in _values(bundle, gene_ids, dataset).items():
        trait = [row for row in rows if _is_trait_sample(row, selection, rows)]
        best = max(rows, key=lambda row: row.value)
        trait_best = max(trait, key=lambda row: row.value) if trait else None
        profiles.append(
            ExpressionProfile(
                gene_id=gene_id,
                assembly=registry_of(bundle).canonical_assembly,
                dataset=name,
                unit=rows[0].unit,
                trait_key=selection.trait_key,
                trait_tissues=selection.tissues,
                n_samples=len(rows),
                max_value=best.value,
                max_sample=best.sample,
                max_tissue=best.tissue,
                trait_max_value=trait_best.value if trait_best else None,
                trait_max_sample=trait_best.sample if trait_best else None,
                trait_mean=round(sum(row.value for row in trait) / len(trait), 3) if trait else None,
                expressed=best.value >= EXPRESSED_TPM,
                in_trait_tissue=bool(trait_best and trait_best.value >= EXPRESSED_TPM),
                top=[SampleValue(sample=row.sample, tissue=row.tissue, stage=row.stage, value=row.value) for row in sorted(rows, key=lambda row: -row.value)[:TOP_SAMPLES]],
                source_gene_id=rows[0].source_gene_id,
                source_version=rows[0].source_version,
            )
        )
    return profiles


def tissue_specificity(
    bundle: Bundle,
    gene_ids: list[str],
    selection: TissueSelection,
    dataset: str | None = None,
) -> list[TissueSpecificity]:
    """Return tau and per-tissue z-scores of each gene per dataset."""
    results = []
    for (gene_id, name), rows in _values(bundle, gene_ids, dataset).items():
        by_tissue: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            if row.tissue != "unspecified":
                by_tissue[row.tissue].append(math.log2(row.value + 1.0))
        means = {tissue: sum(values) / len(values) for tissue, values in sorted(by_tissue.items())}
        tau, z = specificity(means)
        top = max(means, key=lambda tissue: means[tissue]) if means and max(means.values()) > 0 else None
        trait_z = [z[tissue] for tissue in selection.tissues if tissue in z]
        results.append(
            TissueSpecificity(
                gene_id=gene_id,
                assembly=registry_of(bundle).canonical_assembly,
                dataset=name,
                unit=f"log2({rows[0].unit}+1)",
                trait_key=selection.trait_key,
                trait_tissues=selection.tissues,
                n_tissues=len(means),
                tau=tau,
                top_tissue=top,
                top_value=round(means[top], 3) if top else 0.0,
                tissue_means={tissue: round(value, 3) for tissue, value in means.items()},
                z=z,
                trait_z=max(trait_z) if trait_z else None,
                trait_tissue_top=top is not None and top in selection.tissues,
                source_gene_id=rows[0].source_gene_id,
                source_version=rows[0].source_version,
            )
        )
    return results


def specificity(means: dict[str, float]) -> tuple[float | None, dict[str, float]]:
    """Return tau and the per-tissue z-scores of tissue means (``None`` tau when nothing is expressed)."""
    if len(means) < 2:
        return None, {tissue: 0.0 for tissue in means}
    values = list(means.values())
    peak = max(values)
    tau = round(sum(1 - value / peak for value in values) / (len(values) - 1), 3) if peak > 0 else None
    average = sum(values) / len(values)
    spread = math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))
    z = {tissue: round((value - average) / spread, 2) if spread > 0 else 0.0 for tissue, value in means.items()}
    return tau, z


def datasets(bundle: Bundle) -> list[str]:
    """Return the expression datasets in the bundle."""
    return [str(row[0]) for row in bundle.rows_raw("SELECT DISTINCT dataset FROM samples ORDER BY 1")]


class _Row(BaseModel):
    sample: str
    tissue: str
    stage: str | None
    value: float
    unit: str
    source_gene_id: str
    source_version: str


def _values(bundle: Bundle, gene_ids: list[str], dataset: str | None) -> dict[tuple[str, str], list[_Row]]:
    wanted = list(dict.fromkeys(gene for gene in gene_ids if gene))
    if not wanted:
        return {}
    canonical = registry_of(bundle).canonical_assembly
    parameters: list[Any] = [canonical, *wanted]
    clause = ""
    if dataset:
        clause = " AND dataset = ?"
        parameters.append(dataset)
    grouped: dict[tuple[str, str], list[_Row]] = {}
    for row in bundle.rows(
        "SELECT gene_id, dataset, sample, tissue, stage, value, unit, source_gene_id, source_version FROM expression "
        f"WHERE assembly = ? AND gene_id IN ({', '.join('?' for _ in wanted)}){clause} ORDER BY gene_id, dataset, sample",
        parameters,
    ):
        grouped.setdefault((str(row["gene_id"]), str(row["dataset"])), []).append(
            _Row(
                sample=row["sample"],
                tissue=row["tissue"],
                stage=row["stage"],
                value=float(row["value"]),
                unit=row["unit"],
                source_gene_id=row["source_gene_id"],
                source_version=row["source_version"],
            )
        )
    order = {gene: index for index, gene in enumerate(wanted)}
    return dict(sorted(grouped.items(), key=lambda item: (order[item[0][0]], item[0][1])))


def _is_trait_sample(row: _Row, selection: TissueSelection, rows: list[_Row]) -> bool:
    if row.tissue not in selection.tissues:
        return False
    if not selection.stages:
        return True
    staged = {other.stage for other in rows if other.tissue == row.tissue}
    return row.stage in selection.stages or not staged & set(selection.stages)


@cache
def _curated(species: str) -> dict[str, Any]:
    path = resources.files("agrihub_data.query") / "tissues" / f"{species}.yaml"
    if not path.is_file():
        return {"profiles": {}, "rules": []}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"profiles": dict(loaded.get("profiles") or {}), "rules": list(loaded.get("rules") or [])}


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.casefold())}", text.casefold()) is not None
