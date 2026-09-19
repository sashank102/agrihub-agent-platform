# PostgreSQL graph persistence

The platform runtime can compile the existing research graph with
`AsyncPostgresSaver` and `AsyncPostgresStore`. The module-level
`deep_researcher` export remains in memory, so the existing `langgraph dev`
command and Studio workflow are unchanged.

## Start PostgreSQL

Copy the development variables from `.env.example` into your ignored local
`.env`, then start the PostgreSQL-only Compose service:

```bash
docker compose up -d postgres
docker compose ps
```

The image version is pinned, the database has a health check, and its data is
kept in the `postgres_data` volume. The example username and password are only
for local development. Production credentials must come from a secret manager;
`ENVIRONMENT=production` rejects a missing `DATABASE_URI`.

## Initialize LangGraph tables

The persistence lifecycle runs both idempotent setup methods whenever it opens,
so using `open_persistent_graph()` initializes an empty database automatically:

```bash
uv run python - <<'PY'
import asyncio
from agent_platform.runtime import open_persistent_graph

async def initialize():
    async with open_persistent_graph() as graph:
        print(type(graph).__name__)

asyncio.run(initialize())
PY
```

No platform-owned schema or migration framework is used. With vector indexing
disabled, the library setup methods create these tables:

- Checkpointer: `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`,
  and `checkpoint_writes`
- Store: `store_migrations` and `store`

## Run persistence tests

Export the local database URI and run the focused integration suite:

```bash
set -a
source .env
set +a
TEST_DATABASE_URI="$DATABASE_URI" uv run pytest -m postgres \
  tests/test_postgres_persistence.py
```

Each test creates and drops a separate empty PostgreSQL database. The suite
does not invoke a model or make paid API calls. It covers setup, direct
checkpoint and store round trips, graph dependency injection, multi-turn state,
checkpoint history, thread isolation, closed-resource restart, and a
separate-writer/separate-reader process restart.

## Data and operations

Checkpoints are serialized LangGraph state, not normalized message rows. A
checkpoint can contain the full human and assistant conversation, model tool
calls, tool results, supervisor/researcher state, raw notes, final reports, and
other graph channel values. Historical checkpoints retain earlier versions of
that content. Store records are separate durable LangGraph namespace/key/value
items and are available to graph nodes through `get_store()`.

This means the database and its backups may contain prompts, generated content,
third-party tool output, biological research data, and secrets accidentally
included in any of them. Restrict database and backup access, require encrypted
connections outside local development, encrypt backups, rotate credentials,
test restores, and apply the same data-classification and deletion controls to
backups as to the live database. Do not place credentials in graph state.

LangGraph setup creates storage but does not define a retention policy.
Production operation therefore requires an explicit, tested pruning schedule
for checkpoint history and store namespaces, plus corresponding backup
expiration. Pruning must preserve checkpoints still needed for active threads
and must be coordinated across checkpoint metadata, blobs, and writes; avoid
ad hoc deletion of only one table.

Checkpoint persistence is not model prompt or response caching. Continuing a
thread restores graph state, but any node executed again may call its model
again. Model caching, API services, authentication, platform-owned tables,
workers, and frontend integration are outside this persistence layer.
