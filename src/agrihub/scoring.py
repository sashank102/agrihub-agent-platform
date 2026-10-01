"""Deterministic candidate-gene rubric (master plan section 6).

Weights live in ``scoring_weights.yaml`` next to this module so they can be
tuned without code changes; :func:`load_rubric` reads them once.
"""

from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

WEIGHTS_FILE = "scoring_weights.yaml"


class DistanceBand(BaseModel):
    """A factor for distances up to ``max_bp``; the last band has no limit."""

    max_bp: int | None = None
    factor: float = Field(ge=0.0)
    label: str | None = None


class CountBand(BaseModel):
    """A factor for counts of at least ``at_least``."""

    at_least: int = Field(ge=0)
    factor: float = Field(ge=0.0)


class PValueBand(BaseModel):
    """A factor for p-values up to ``max``; the last band also takes missing p-values."""

    max: float | None = None
    factor: float = Field(ge=0.0)


class QtlWeights(BaseModel):
    """How a prior QTL near a gene counts."""

    base: float = Field(ge=0.0)
    trait: dict[str, float]
    overlap: dict[str, float]
    markers: list[CountBand]
    span: list[DistanceBand]
    single_marker: list[DistanceBand]


class GwasWeights(BaseModel):
    """How a published GWAS hit near a gene counts."""

    base: float = Field(ge=0.0)
    trait: dict[str, float]
    distance: list[DistanceBand]
    p_value: list[PValueBand]


class Rubric(BaseModel):
    """Every tunable weight of the rubric."""

    version: int
    qtl: QtlWeights
    gwas: GwasWeights


@cache
def _packaged_rubric() -> Rubric:
    text = (resources.files("agrihub") / WEIGHTS_FILE).read_text(encoding="utf-8")
    return Rubric.model_validate(yaml.safe_load(text))


def load_rubric(path: Path | str | None = None) -> Rubric:
    """Return the packaged rubric, or the one in ``path``."""
    if path is None:
        return _packaged_rubric()
    return Rubric.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def band(bands: list[DistanceBand], value: int) -> DistanceBand | None:
    """Return the first band whose ``max_bp`` admits ``value``, or ``None`` past the last limit."""
    for item in bands:
        if item.max_bp is None or value <= item.max_bp:
            return item
    return None


def qtl_points(value: dict[str, Any], rubric: Rubric | None = None) -> tuple[float, str]:
    """Return the points of one ``qtl_overlap`` hit and a short reason.

    Interval QTLs weigh trait match, overlap type, markers placed and span;
    wide QTLs count little. Single-marker QTLs count as marker proximity.
    """
    weights = (rubric or load_rubric()).qtl
    trait = weights.trait.get(str(value.get("trait_match")), 0.0)
    if value.get("kind") == "marker":
        proximity = band(weights.single_marker, int(value.get("distance_to_core") or 0))
        if proximity is None:
            return 0.0, "single-marker QTL too far from the gene"
        factor = proximity.factor
        reason = f"single-marker QTL {value.get('distance_to_core') or 0} bp from the gene"
    else:
        placed = int(value.get("n_markers_placed") or 0)
        markers = next((item.factor for item in weights.markers if placed >= item.at_least), 0.0)
        span = band(weights.span, int(value.get("span_bp") or 0))
        overlap = weights.overlap.get(str(value.get("overlap_type")), 0.0)
        factor = markers * (span.factor if span else 0.0) * overlap
        reason = (
            f"{placed}-marker QTL, {int(value.get('span_bp') or 0) / 1e6:.2f} Mb"
            + (" (wide)" if value.get("wide") else "")
            + f", {value.get('overlap_type')}"
        )
    return round(weights.base * trait * factor, 3), f"{reason}, trait match {value.get('trait_match')}"


def gwas_points(value: dict[str, Any], rubric: Rubric | None = None) -> tuple[float, str]:
    """Return the points of one ``gwas_catalog_overlap`` hit and a short reason.

    Distance from the hit to the gene body, trait match (ontology over
    keyword) and p-value multiply the base weight.
    """
    weights = (rubric or load_rubric()).gwas
    trait = weights.trait.get(str(value.get("trait_match")), 0.0)
    distance = int(value.get("distance_to_core") or 0)
    proximity = band(weights.distance, distance) or weights.distance[-1]
    p_value = value.get("p_value")
    p_factor = weights.p_value[-1].factor
    if p_value is not None:
        p_factor = next(
            (item.factor for item in weights.p_value if item.max is None or float(p_value) <= item.max),
            p_factor,
        )
    points = weights.base * trait * proximity.factor * p_factor
    p_text = f"p={float(p_value):.1e}" if p_value is not None else "no p-value"
    return round(points, 3), f"GWAS hit {proximity.label or distance}, {p_text}, trait match {value.get('trait_match')}"
