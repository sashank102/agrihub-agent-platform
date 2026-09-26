"""FastAPI dependencies for application-owned resources."""

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.api.request_context import user_id_var
from agent_platform.core.settings import Settings
from agent_platform.db.session import session_scope
from agent_platform.services.accounts import (
    AUTH_FAILURE_DETAIL,
    AccountService,
    AuthenticatedPrincipal,
    AuthenticationFailure,
)

_READY_ATTRIBUTES = (
    "session_factory",
    "graph_registry",
    "persistence",
    "run_manager",
)


async def require_ready(request: Request) -> None:
    """Reject business routes until lifespan initialization has finished."""
    state = request.app.state
    if not getattr(state, "ready", False):
        raise HTTPException(status_code=503, detail="service is not ready")
    if any(getattr(state, name, None) is None for name in _READY_ATTRIBUTES):
        raise HTTPException(status_code=503, detail="service is not ready")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Provide one transaction-scoped SQLAlchemy session."""
    async with session_scope(request.app.state.session_factory) as session:
        yield session


def get_runtime_settings(request: Request) -> Settings:
    """Return the settings captured when this app was created."""
    return request.app.state.settings


async def get_principal(request: Request) -> AuthenticatedPrincipal:
    """Authenticate the caller or, only in local development, use the seeded user."""
    settings = request.app.state.settings
    if settings.AUTH_MODE == "disabled":
        if settings.ENVIRONMENT == "production":
            raise HTTPException(status_code=503, detail="service is not ready")
        principal = AuthenticatedPrincipal(
            user_id=settings.DEVELOPMENT_USER_ID,
            api_key_id=None,
            auth_mode="disabled",
        )
        request.state.principal = principal
        user_id_var.set(str(principal.user_id))
        return principal

    accounts: AccountService = request.app.state.accounts
    presented = _presented_credential(request)
    if isinstance(presented, AuthenticationFailure):
        await accounts.record_auth_failure(presented.reason)
        raise _unauthorized()
    result = await accounts.authenticate(presented)
    if isinstance(result, AuthenticationFailure):
        raise _unauthorized()
    request.state.principal = result
    user_id_var.set(str(result.user_id))
    return result


def _presented_credential(request: Request) -> str | AuthenticationFailure:
    header_key = request.headers.get("x-api-key")
    authorization = request.headers.get("authorization")
    bearer: str | None = None
    if authorization is not None:
        scheme, _, remainder = authorization.partition(" ")
        if scheme.lower() != "bearer" or not remainder.strip():
            return AuthenticationFailure("malformed")
        bearer = remainder.strip()
    if header_key is not None and bearer is not None and header_key != bearer:
        return AuthenticationFailure("conflicting")
    token = header_key or bearer
    if token is None or not token.strip():
        return AuthenticationFailure("missing")
    return token.strip()


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=AUTH_FAILURE_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )
