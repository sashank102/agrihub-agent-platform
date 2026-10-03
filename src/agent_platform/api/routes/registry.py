"""Species and model registry routes used by the study form."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from agent_platform.api.dependencies import get_principal, require_ready
from agent_platform.api.schemas import SpeciesResponse
from agent_platform.services.accounts import AuthenticatedPrincipal
from agrihub.state import TraitStudy
from agrihub.trait_models.adapters import evaluate_models
from agrihub_data.catalog import catalog
from agrihub_data.registry import (
    UnknownAssemblyError,
    UnknownSpeciesError,
    load_species,
)

router = APIRouter(
    prefix="/registry",
    dependencies=[Depends(require_ready)],
)
PrincipalDependency = Annotated[AuthenticatedPrincipal, Depends(get_principal)]


@router.get("/species", response_model=list[SpeciesResponse])
async def list_species(principal: PrincipalDependency) -> list[SpeciesResponse]:
    """Return assemblies, chromosome lengths, default windows and bundle state per species."""
    summaries = await asyncio.to_thread(catalog)
    return [SpeciesResponse.model_validate(summary) for summary in summaries]


@router.get("/models")
async def list_models(
    principal: PrincipalDependency,
    species: Annotated[str, Query(min_length=1)],
    trait: Annotated[str, Query(min_length=1)],
    assembly: str | None = None,
) -> dict[str, Any]:
    """Return the registered models for a species with whether each applies to the trait, and why not."""

    def evaluate() -> dict[str, Any]:
        try:
            registry = load_species(species)
            requested = registry.assembly(assembly)
        except (UnknownSpeciesError, UnknownAssemblyError) as exc:
            return {"models": [], "applicable": 0, "detail": str(exc.args[0]) if exc.args else str(exc)}
        study = TraitStudy(
            mode="trait",
            species=registry.species,
            assembly=requested.lift_to or requested.id,
            trait_text=trait,
        )
        candidates = [item.model_dump(mode="json") for item in evaluate_models(study)]
        applicable = sum(1 for item in candidates if item["applicability"]["ok"])
        detail = "" if applicable else f"No applicable model is registered for {trait} in {registry.species}."
        return {"models": candidates, "applicable": applicable, "detail": detail}

    return await asyncio.to_thread(evaluate)
