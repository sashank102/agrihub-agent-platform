"""Species registry routes used by the study form."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends

from agent_platform.api.dependencies import get_principal, require_ready
from agent_platform.api.schemas import SpeciesResponse
from agent_platform.services.accounts import AuthenticatedPrincipal
from agrihub_data.catalog import catalog

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
