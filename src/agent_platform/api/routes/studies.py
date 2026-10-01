"""Study routes used by the study form."""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends

from agent_platform.api.dependencies import get_principal, require_ready
from agent_platform.api.schemas import StudyValidationResponse
from agent_platform.services.accounts import AuthenticatedPrincipal
from agrihub.study_preview import preview_study

router = APIRouter(
    prefix="/studies",
    dependencies=[Depends(require_ready)],
)
PrincipalDependency = Annotated[AuthenticatedPrincipal, Depends(get_principal)]


@router.post("/validate", response_model=StudyValidationResponse)
async def validate_study(
    study: Annotated[dict[str, Any], Body()],
    principal: PrincipalDependency,
) -> StudyValidationResponse:
    """Dry-run intake on a study request without starting a run.

    The body is the ``study`` object a run would receive. The response lists
    the placed SNPs, warnings, located errors and a fixed-window locus and
    gene-count preview. Nothing is persisted.
    """
    result = await asyncio.to_thread(preview_study, study)
    return StudyValidationResponse.model_validate(result)
