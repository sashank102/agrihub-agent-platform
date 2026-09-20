# Platform metadata schema

The application owns only the `platform` PostgreSQL schema. Alembic revision
`20260920_0001` creates these tables, and revision `20260920_0002` aligns the
initial check-constraint names with SQLAlchemy's naming convention:

- `users`: identity profile and soft-disable/soft-delete timestamps
- `api_keys`: visible prefixes and one-way secret hashes; never plaintext keys
- `agents`: owner-scoped or global versioned graph configurations
- `threads`: ownership and lifecycle metadata for a LangGraph thread
- `runs`: execution status, generic input/configuration, summaries, and errors
- `run_events`: ordered, append-only events for replay
- `artifacts`: reports, small generic values, or references to external content
- `audit_log`: immutable action records

The repositories under `agent_platform.db.repositories` accept an existing
`AsyncSession`, flush changes, and never commit. Callers own transaction
boundaries. `session_scope()` is available to scripts and other
dependency-free callers.

Ownership checks for runs and artifacts are resolved through their referenced
thread. Repository write paths also require a run's agent to match its thread,
an artifact owner to match its thread owner, and an artifact's optional run to
belong to that same thread. Global agents are readable by development users,
but only the explicit system/admin update path can mutate them.

Disabled and deleted users are retained in this schema, but requests are not
yet rejected based on those states. That enforcement belongs to Plan 06
authentication, where the authenticated principal is established.

## LangGraph ownership

LangGraph continues to own the public-schema tables `checkpoint_migrations`,
`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `store_migrations`, and
`store`. Its setup lifecycle and migration tracking are independent of
Alembic. Alembic autogeneration is filtered to `platform`; platform migrations
must never create, alter, or drop LangGraph tables.

There is deliberately no relational messages table. Complete conversation
state—including messages, tool calls, tool results, graph state, notes, and
reports—already lives in LangGraph checkpoints. Duplicating messages would
introduce two sources of truth with different transaction and retention
semantics.

## Migrations

The files under `alembic/` are repository deployment assets. They must be
shipped and applied in revision order; metadata declarations alone do not
upgrade an existing database.

Set the validated `DATABASE_URI`, then run:

```bash
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic upgrade head
```

The Alembic version table is migration bookkeeping in `public`; all application
objects created by the revision are in `platform`. A full downgrade removes
the `platform` schema and leaves public-schema LangGraph tables unchanged.

## Integration tests

`TEST_DATABASE_URI` must point to a PostgreSQL database whose role can create
and drop test databases. Every test uses a fresh database and makes no model
provider calls.

```bash
TEST_DATABASE_URI="$DATABASE_URI" uv run pytest -m postgres \
  tests/test_platform_schema.py
```

The suite covers empty-database upgrade, full downgrade, re-upgrade, exact
tables/indexes/constraints, LangGraph isolation, repositories, ownership,
foreign keys, deletion behavior, idempotency, concurrent event ordering, and
transaction rollback.

## Deletion and retention

User deletion is normally soft deletion through `users.status` and
`deleted_at`. A deliberate hard delete cascades credentials, threads, their
runs/events, and owned artifacts. Owned agent definitions survive with a null
owner. Agents referenced by threads or runs cannot be hard-deleted. Deleting a
thread cascades its runs, run events, and artifacts. Deleting a run cascades
events but preserves artifacts by clearing `artifacts.run_id`.

Audit rows are append-only: a database trigger rejects updates and deletes, and
their actor foreign key restricts hard deletion. An operator applying a legal
retention policy must use a privileged, audited maintenance procedure to
temporarily manage that protection. API-key expiry/revocation timestamps,
user deletion timestamps, run finish times, event timestamps, and audit
timestamps are indexed for retention scans.

The schema does not itself schedule retention. Operators must define and test
policy-specific pruning for platform rows, LangGraph checkpoint history,
LangGraph store namespaces, external artifact targets, database backups, and
replicas. Deleting platform thread metadata does not automatically identify or
delete serialized LangGraph checkpoints; coordinated cleanup is required.

`updated_at` is maintained by SQLAlchemy's ORM/Core update behavior. SQL
executed directly outside SQLAlchemy does not trigger that client-side
`onupdate` behavior; direct writers must set the column themselves when
appropriate.
