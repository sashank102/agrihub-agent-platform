# AgriHub Agent Platform

A self-hosted, provider-agnostic multi-agent research platform built on
LangGraph, PostgreSQL, and a Next.js chat interface.

## Current architecture

- `src/open_deep_research/` — active coordinator/researcher LangGraph workflow
- `src/agent_platform/` — PostgreSQL persistence, platform metadata models, and repositories
- `alembic/` — migrations for the platform-owned PostgreSQL schema
- `frontend/` — Next.js chat client using the LangGraph SDK
- `compose.yaml` — PostgreSQL, one FastAPI process, and the Next.js UI
- `Dockerfile` and `frontend/Dockerfile` — production images
- `.github/workflows/ci.yml` — lint, tests, browser checks, and image build

`./start-dev.sh` starts PostgreSQL-backed FastAPI on port `8000` and the
Next.js UI on port `3000`. It defaults to `AUTH_MODE=api_key` and requires
`API_KEY_PEPPER` plus a key from the admin CLI. `AUTH_MODE=disabled` is an
explicit development bridge; the UI labels it as such, and production rejects
it. `./start-prod.sh` builds the Compose stack. There is no Redis and only
one API worker. The in-memory LangGraph development server is not part of
normal development.

## Local setup

```bash
./setup.sh
cp .env.example .env
# Add API_KEY_PEPPER (16+ characters) and the selected model-provider key.
docker compose up -d postgres
./.tools/bin/uv run alembic upgrade head
./.tools/bin/uv run python -m agent_platform create-user --display-name "Local"
./.tools/bin/uv run python -m agent_platform issue-key --user-id "<user-id>" --label local
./start-dev.sh
```

Open <http://127.0.0.1:3000> and paste the printed API key.

Production on one machine:

```bash
# Export POSTGRES_PASSWORD and API_KEY_PEPPER first. Do not commit them.
./start-prod.sh
```

Operations: [backup and restore](docs/operations/backup-restore.md),
[migrations](docs/operations/migrations.md),
[API keys](docs/operations/api-keys.md),
[reconciliation](docs/operations/reconciliation.md).

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
