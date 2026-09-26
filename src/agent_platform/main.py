"""FastAPI server for the single-process chat protocol."""

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from agent_platform.api.middleware import RequestContextMiddleware, install_redaction
from agent_platform.api.request_limit import RequestSizeLimitMiddleware
from agent_platform.api.routes import health, info, runs, threads
from agent_platform.core.settings import Settings, get_settings
from agent_platform.db.models import Agent
from agent_platform.db.repositories import AgentRepository, UserRepository
from agent_platform.db.session import (
    create_platform_engine,
    create_session_factory,
    session_scope,
)
from agent_platform.fixture_graph import build_fixture_graph
from agent_platform.persistence import open_postgres_persistence
from agent_platform.process_lock import ApiProcessLock
from agent_platform.services.accounts import AccountService
from agent_platform.services.graph_registry import GraphRegistry
from agent_platform.services.run_manager import RunManager
from agent_platform.services.tenant_store import TenantStore
from open_deep_research.deep_researcher import build_graph

logger = logging.getLogger(__name__)

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
                    Agent.owner_user_id.is_(None),
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
                owner_user_id=None,
                graph_id=settings.DEVELOPMENT_GRAPH_ID,
                name="AgriHub Research Agent",
                metadata={"development_seed": True, "global": True},
            )
        if agent.owner_user_id not in {None, user.id}:
            raise RuntimeError("configured development agent has an unexpected owner")
        if agent.graph_id != settings.DEVELOPMENT_GRAPH_ID:
            raise RuntimeError("configured development agent has an unexpected graph ID")


def create_app(
    *,
    settings: Settings | None = None,
    graph_builder: GraphBuilder | None = None,
    persistence_factory: Callable[..., Any] = open_postgres_persistence,
    engine_factory: Callable[..., Any] = create_platform_engine,
) -> FastAPI:
    """Create the local-only API with injectable lifecycle dependencies."""
    active_settings = settings or get_settings()
    if graph_builder is None:
        graph_builder = (
            build_fixture_graph if active_settings.GRAPH_FIXTURE else build_graph
        )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if active_settings.AUTH_MODE == "disabled":
            logger.warning(
                "AUTH_MODE=disabled; every request uses development user %s. "
                "This mode is refused when ENVIRONMENT=production.",
                active_settings.DEVELOPMENT_USER_ID,
            )
        if active_settings.DATABASE_URI is None:
            raise RuntimeError("DATABASE_URI is not configured")
        process_lock = ApiProcessLock(active_settings.DATABASE_URI)
        await process_lock.acquire()
        engine = engine_factory(active_settings.DATABASE_URI)
        application.state.settings = active_settings
        application.state.engine = engine
        application.state.session_factory = create_session_factory(engine)
        application.state.accounts = AccountService(
            application.state.session_factory,
            active_settings,
        )
        application.state.ready = False
        application.state.run_manager = None
        manager: RunManager | None = None
        try:
            async with persistence_factory(
                active_settings.DATABASE_URI
            ) as persistence:
                application.state.persistence = persistence
                await _seed_development_principal(application)
                graph = graph_builder(
                    checkpointer=persistence.checkpointer,
                    store=TenantStore(persistence.store),
                )
                registry = GraphRegistry()
                registry.register(
                    active_settings.DEVELOPMENT_GRAPH_ID,
                    active_settings.DEVELOPMENT_AGENT_ID,
                    graph,
                )
                application.state.graph_registry = registry
                manager = RunManager(
                    application.state.session_factory,
                    registry,
                    max_concurrent_runs=active_settings.API_MAX_CONCURRENT_RUNS,
                    subscriber_queue_size=(
                        active_settings.API_STREAM_SUBSCRIBER_QUEUE_SIZE
                    ),
                )
                await manager.reconcile_orphaned_runs(reason="process_restart")
                await manager.repair_terminal_events()
                await manager.reconcile_pending()
                application.state.run_manager = manager
                application.state.ready = True
                try:
                    yield
                finally:
                    application.state.ready = False
                    await manager.shutdown()
        finally:
            application.state.ready = False
            await process_lock.release()
            await engine.dispose()

    application = FastAPI(
        title="AgriHub Local Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.ready = False
    install_redaction(active_settings.API_KEY_PREFIX)
    application.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=active_settings.API_MAX_REQUEST_BODY_BYTES,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.API_ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=[
            "Content-Location",
            "X-Run-ID",
            "X-Thread-ID",
            "X-Request-ID",
        ],
    )
    application.add_middleware(RequestContextMiddleware)

    application.include_router(health.router)
    application.include_router(info.router)
    application.include_router(threads.router)
    application.include_router(runs.router)
    return application


def run() -> None:
    """Run the API on one Uvicorn worker.

    Run tasks and subscriber queues live in this process. ``WEB_CONCURRENCY``
    greater than 1 is rejected here. Lifespan also takes a PostgreSQL advisory
    lock, so ``uvicorn agent_platform.main:app --workers 2`` cannot start a
    second independent RunManager.
    """
    import uvicorn

    workers = os.environ.get("WEB_CONCURRENCY")
    if workers not in {None, "", "1"}:
        raise RuntimeError(
            "AgriHub requires exactly one Uvicorn worker; "
            f"WEB_CONCURRENCY={workers} is not supported"
        )
    settings = get_settings()
    uvicorn.run(
        "agent_platform.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        workers=1,
    )


app = create_app()
