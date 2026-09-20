"""Local-development FastAPI server for the minimal chat protocol."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from agent_platform.api.routes import health, info, runs, threads
from agent_platform.core.settings import Settings, get_settings
from agent_platform.db.models import Agent
from agent_platform.db.repositories import AgentRepository, UserRepository
from agent_platform.db.session import (
    create_platform_engine,
    create_session_factory,
    session_scope,
)
from agent_platform.persistence import open_postgres_persistence
from agent_platform.services.graph_registry import GraphRegistry
from open_deep_research.deep_researcher import build_graph

GraphBuilder = Callable[..., Any]


async def _seed_development_principal(app: FastAPI) -> None:
    """Idempotently create the configured local user and sample agent."""
    settings = app.state.settings
    async with session_scope(app.state.session_factory) as session:
        users = UserRepository(session)
        user = await users.get(settings.DEVELOPMENT_USER_ID)
        if user is None:
            email_match = await users.get_by_email(settings.DEVELOPMENT_USER_EMAIL)
            if email_match is not None:
                raise RuntimeError(
                    "development user email belongs to a different configured ID"
                )
            user = await users.create(
                user_id=settings.DEVELOPMENT_USER_ID,
                display_name="AgriHub Local Developer",
                email=settings.DEVELOPMENT_USER_EMAIL,
            )

        agent = await session.get(Agent, settings.DEVELOPMENT_AGENT_ID)
        if agent is None:
            conflicting = await session.scalar(
                select(Agent).where(
                    Agent.owner_user_id == user.id,
                    Agent.graph_id == settings.DEVELOPMENT_GRAPH_ID,
                    Agent.version == 1,
                )
            )
            if conflicting is not None:
                raise RuntimeError(
                    "development graph belongs to a different configured agent ID"
                )
            agent = await AgentRepository(session).create(
                agent_id=settings.DEVELOPMENT_AGENT_ID,
                owner_user_id=user.id,
                graph_id=settings.DEVELOPMENT_GRAPH_ID,
                name="AgriHub Research Agent",
                metadata={"development_seed": True},
            )
        if agent.owner_user_id != user.id:
            raise RuntimeError("configured development agent has an unexpected owner")
        if agent.graph_id != settings.DEVELOPMENT_GRAPH_ID:
            raise RuntimeError("configured development agent has an unexpected graph ID")


def create_app(
    *,
    settings: Settings | None = None,
    graph_builder: GraphBuilder = build_graph,
    persistence_factory: Callable[..., Any] = open_postgres_persistence,
    engine_factory: Callable[..., Any] = create_platform_engine,
) -> FastAPI:
    """Create the local-only API with injectable lifecycle dependencies."""
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if active_settings.ENVIRONMENT == "production":
            raise RuntimeError(
                "the unauthenticated development API cannot run in production"
            )

        engine = engine_factory(active_settings.DATABASE_URI)
        application.state.settings = active_settings
        application.state.engine = engine
        application.state.session_factory = create_session_factory(engine)
        application.state.ready = False
        try:
            async with persistence_factory(
                active_settings.DATABASE_URI
            ) as persistence:
                application.state.persistence = persistence
                await _seed_development_principal(application)
                graph = graph_builder(
                    checkpointer=persistence.checkpointer,
                    store=persistence.store,
                )
                registry = GraphRegistry()
                registry.register(
                    active_settings.DEVELOPMENT_GRAPH_ID,
                    active_settings.DEVELOPMENT_AGENT_ID,
                    graph,
                )
                application.state.graph_registry = registry
                application.state.run_semaphore = asyncio.Semaphore(
                    active_settings.API_MAX_CONCURRENT_RUNS
                )
                application.state.ready = True
                yield
        finally:
            application.state.ready = False
            await engine.dispose()

    application = FastAPI(
        title="AgriHub Local Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.ready = False
    application.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.API_ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Location", "X-Run-ID", "X-Thread-ID"],
    )

    @application.middleware("http")
    async def limit_request_body(request: Request, call_next: Callable[..., Any]):
        content_length = request.headers.get("content-length")
        maximum = active_settings.API_MAX_REQUEST_BODY_BYTES
        if content_length is not None:
            try:
                if int(content_length) > maximum:
                    return JSONResponse(
                        {"detail": "request body too large"},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid content-length"},
                    status_code=400,
                )
        body = await request.body()
        if len(body) > maximum:
            return JSONResponse(
                {"detail": "request body too large"},
                status_code=413,
            )
        return await call_next(request)

    application.include_router(health.router)
    application.include_router(info.router)
    application.include_router(threads.router)
    application.include_router(runs.router)
    return application


def run() -> None:
    """Run the development API with the configured address and one worker."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "agent_platform.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        workers=1,
    )


app = create_app()
