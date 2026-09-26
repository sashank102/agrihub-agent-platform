# AgriHub Agent Platform

A self-hosted, provider-agnostic multi-agent research platform built on
LangGraph, PostgreSQL, and a Next.js chat interface.

## Current architecture

- `src/open_deep_research/` — active coordinator/researcher LangGraph workflow
- `src/agent_platform/` — PostgreSQL persistence, platform metadata models, and repositories
- `alembic/` — migrations for the platform-owned PostgreSQL schema
- `frontend/` — Next.js chat client using the LangGraph SDK
- `compose.yaml` — local PostgreSQL service

`start-dev.sh` starts PostgreSQL-backed FastAPI on port `8000` and the Next.js
UI on port `3000`. The UI sends a platform API key as `X-Api-Key`. Local
development can set `AUTH_MODE=disabled`; production requires API keys. The
in-memory LangGraph development server is not part of normal development.

## Local setup

```bash
./setup.sh
cp .env.example .env
# Add the selected model-provider key to .env
./start-dev.sh
```

Open <http://127.0.0.1:3000>.

For free-tier Groq testing:

```bash
cp .env.groq.example .env
# Add GROQ_API_KEY to .env
./start-dev.sh
```

## PostgreSQL

```bash
docker compose up -d postgres
docker compose ps postgres
```

See:

- [PostgreSQL graph persistence](docs/postgres-persistence.md)
- [Platform metadata schema](docs/platform-schema.md)
- [Local FastAPI chat server](docs/local-fastapi.md)
- [Project-specific setup and extension points](AGRIHUB.md)

## Verification

```bash
./.tools/bin/uv run pytest -m "not postgres"
cd frontend && pnpm build
```

PostgreSQL integration tests require `TEST_DATABASE_URI`; the persistence docs
contain the complete command.

## License and attribution

This project contains code derived from LangChain's Open Deep Research and
Agent Chat UI projects. Their MIT license notices remain in this repository.
