# Backup and restore

PostgreSQL is the only durable store. Checkpoints, blobs, writes, the store,
platform tables, and `alembic_version` live in one database. Back them up
together. Do not copy a single LangGraph table by itself.

## Backup

Stop writes or accept a point-in-time dump. From the host that can reach the
database:

```bash
docker compose exec -T postgres \
  pg_dump --format=custom --no-owner --username "$POSTGRES_USER" "$POSTGRES_DB" \
  > agrihub.dump
```

Encrypt the dump before it leaves the machine. The example uses age; any
tool that keeps the ciphertext and the key apart is fine. Do not commit the
dump or the key.

```bash
age -r "$AGE_RECIPIENT" -o agrihub.dump.age agrihub.dump
rm agrihub.dump
```

## Restore into an empty deployment

1. Start only PostgreSQL with an empty volume.
2. Create the empty database, then restore before starting the API.

```bash
docker compose up -d postgres
docker compose exec -T postgres \
  pg_restore --clean --if-exists --no-owner --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" < agrihub.dump
```

3. Start the API. Startup runs `alembic upgrade head` and LangGraph setup.
   Those steps are additive when the dump is already at head.
4. Confirm threads, checkpoints, run events, and audit rows:

```bash
docker compose exec -T postgres psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "SELECT version_num FROM alembic_version;" \
  -c "SELECT count(*) FROM platform.threads;" \
  -c "SELECT count(*) FROM platform.runs;" \
  -c "SELECT count(*) FROM platform.run_events;" \
  -c "SELECT count(*) FROM platform.audit_log;" \
  -c "SELECT count(*) FROM checkpoints;"
```

Open a known thread in the UI and confirm its history.

## Retention

Dry-run is the default. Terminal-run deletion also requires checkpoint
pruning so writes, blobs, and checkpoints leave together.

```bash
python -m agent_platform retain --run-events-days 30 --audit-days 365 --expired-keys-days 30
python -m agent_platform retain --apply --run-events-days 30
python -m agent_platform retain --apply --checkpoint-days 90 --terminal-runs-days 90
```

Security audit rows (`auth.*`, `api_key.*`, `user.*`, `access.denied`) stay
unless `--include-security-audit` is set.

The production Compose file also exposes a one-shot maintenance profile:

```bash
docker compose --profile maintenance run --rm retention
```

Schedule that command with the host scheduler rather than keeping a second
long-running worker. For example, a daily systemd timer or cron entry can run
the one-shot container after backups complete. Override
`RETENTION_RUN_EVENTS_DAYS` and `RETENTION_EXPIRED_KEYS_DAYS` in the scheduler
environment. Run the CLI without `--apply` manually before changing retention
windows.

## Limits

- A dump does not include files that were never stored in PostgreSQL. This
  deployment has no object storage.
- Restoring a dump from a newer Alembic revision onto an older build fails
  closed. Upgrade the build first.
- The API pepper is not in the database. A restore without the same
  `API_KEY_PEPPER` cannot verify existing keys.
- One API process holds the advisory lock. Restore does not replay a model
  call that was in memory when the process died.
