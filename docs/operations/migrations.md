# Migrations

Platform tables live in the `platform` schema and are owned by Alembic.
LangGraph owns `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`,
`checkpoint_migrations`, `store`, and `store_migrations` in `public`. Do not
edit those with Alembic, and do not drop one checkpoint table without the
others.

## Inspect

```bash
./.tools/bin/uv run alembic current
./.tools/bin/uv run alembic heads
```

Inside Compose:

```bash
docker compose exec api python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config('alembic.ini')).get_current_head())"
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version;"
```

`/ready` returns 503 when the database revision does not match the build.

## Upgrade

Take a backup first. See [backup and restore](backup-restore.md).

```bash
./.tools/bin/uv run alembic upgrade head
```

The production entrypoint takes a migration advisory lock
(`classid=1095914057`, `objid=1214579202`), runs `alembic upgrade head`,
initializes LangGraph tables, releases that lock, and only then starts the
one API worker. The worker takes a different advisory lock and refuses a
second process.

## Downgrade

```bash
./.tools/bin/uv run alembic downgrade -1
./.tools/bin/uv run alembic upgrade head
```

Downgrade only a revision whose `downgrade()` restores the previous schema.
`20260926_0003` drops `uq_runs_one_active_per_thread` and is reversible.
If a downgrade function cannot restore data, stop, restore the backup, and
deploy the previous build instead of inventing a reverse migration.

Alembic does not migrate LangGraph tables. LangGraph setup runs on API
startup and is forward-only.
