"""The model registry: which models or precomputed results can propose SNPs for a species and trait."""

import os
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ScoreType = Literal["p_value", "integrated_gradients", "gnnexplainer", "weight"]
AdapterName = Literal["precomputed_results", "catalog_top_hits", "gwas_runner", "gnn_runner"]
ANY_SPECIES = "*"
CANONICAL = "canonical"
"""An ``assemblies`` entry meaning the species' canonical assembly."""


class TraitSpec(BaseModel):
    """A trait a model was run for, with the words a study may use for it."""

    code: str
    name: str
    terms: list[str] = Field(default_factory=list)
    defined: bool = True
    """False when the source never says what the code means."""


class Benchmark(BaseModel):
    """A published or recorded evaluation of the model."""

    name: str
    reference: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)


class ModelSpec(BaseModel):
    """One registered model or precomputed result set."""

    id: str
    name: str
    adapter: AdapterName
    status: Literal["active", "planned"] = "active"
    species: list[str]
    assemblies: list[str]
    populations: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    score_type: ScoreType
    label: str
    description: str = ""
    path: str | None = None
    notes: str | None = None
    traits: list[TraitSpec] = Field(default_factory=list)
    """Empty means any trait the adapter can match."""
    benchmarks: list[Benchmark] = Field(default_factory=list)

    def covers_species(self, species: str) -> bool:
        """Return whether the model is registered for ``species``."""
        return ANY_SPECIES in self.species or species in self.species


class ModelRegistry(BaseModel):
    """Every registered model."""

    models: list[ModelSpec]

    def get(self, model_id: str) -> ModelSpec:
        """Return one model by id."""
        for model in self.models:
            if model.id == model_id:
                return model
        raise KeyError(f"unknown model {model_id!r}")


@cache
def load_model_registry() -> ModelRegistry:
    """Return the packaged model registry."""
    text = (resources.files("agrihub.trait_models") / "registry.yaml").read_text(encoding="utf-8")
    return ModelRegistry.model_validate(yaml.safe_load(text))


def results_root() -> Path:
    """Return ``AGRIHUB_MODEL_RESULTS_DIR``, or the ``Results`` folder beside a source checkout."""
    configured = os.environ.get("AGRIHUB_MODEL_RESULTS_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[4] / "Results"
