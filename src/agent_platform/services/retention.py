"""Operator retention for events, keys, audit rows, and checkpoints.

Checkpoint pruning always deletes ``checkpoint_writes``, ``checkpoint_blobs``,
and ``checkpoints`` for the same thread ids in one transaction. It never
removes one of those tables on its own. Store rows are removed only by whole
prefix, together with that checkpoint family.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agent_platform.db.session import AsyncSessionFactory, session_scope

_TERMINAL_SQL = (
    "'completed', 'failed', 'cancelled', 'interrupted'"
)
_SECURITY_FILTER = """
    action NOT LIKE 'auth.%%'
    AND action NOT LIKE 'api_key.%%'
    AND action NOT LIKE 'user.%%'
    AND action <> 'access.denied'
"""


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """Counts an operator can review before any delete."""

    dry_run: bool
    run_events: int = 0
    terminal_runs: int = 0
    audit_rows: int = 0
    api_keys: int = 0
    checkpoint_threads: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-ready summary with no row contents."""
        return {
            "dry_run": self.dry_run,
            "run_events": self.run_events,
            "terminal_runs": self.terminal_runs,
            "audit_rows": self.audit_rows,
            "api_keys": self.api_keys,
            "checkpoint_threads": self.checkpoint_threads,
        }


def _cutoff(days: int | None, now: datetime) -> datetime | None:
    if days is None:
        return None
    if days < 1:
        raise ValueError("retention days must be at least 1")
    return now - timedelta(days=days)


async def apply_retention(
    session_factory: AsyncSessionFactory,
    *,
    apply: bool,
    now: datetime | None = None,
    run_events_days: int | None = None,
    terminal_runs_days: int | None = None,
    audit_days: int | None = None,
    expired_keys_days: int | None = None,
    checkpoint_days: int | None = None,
    include_security_audit: bool = False,
) -> RetentionReport:
    """Count matching rows, and delete them only when ``apply`` is true."""
    moment = now or datetime.now(UTC)
    if terminal_runs_days is not None and checkpoint_days is None:
        raise ValueError(
            "terminal run deletion requires --checkpoint-days so checkpoints, "
            "blobs, and writes are removed together"
        )
    async with session_scope(session_factory) as session:
        report = RetentionReport(
            dry_run=not apply,
            run_events=await _count_events(session, _cutoff(run_events_days, moment)),
            terminal_runs=await _count_runs(session, _cutoff(terminal_runs_days, moment)),
            audit_rows=await _count_audit(
                session,
                _cutoff(audit_days, moment),
                include_security_audit=include_security_audit,
            ),
            api_keys=await _count_keys(session, _cutoff(expired_keys_days, moment)),
            checkpoint_threads=await _count_checkpoint_threads(
                session,
                _cutoff(checkpoint_days, moment),
            ),
        )
        if not apply:
            return report
        if run_events_days is not None:
            await _delete_events(session, _cutoff(run_events_days, moment))
        if checkpoint_days is not None:
            await _delete_checkpoint_family(session, _cutoff(checkpoint_days, moment))
        if terminal_runs_days is not None:
            await _delete_runs(session, _cutoff(terminal_runs_days, moment))
        if audit_days is not None:
            await _delete_audit(
                session,
                _cutoff(audit_days, moment),
                include_security_audit=include_security_audit,
            )
        if expired_keys_days is not None:
            await _delete_keys(session, _cutoff(expired_keys_days, moment))
        return report


async def _count_events(session: AsyncSession, cutoff: datetime | None) -> int:
    if cutoff is None:
        return 0
    value = await session.scalar(
        text(
            f"""
            SELECT count(*)
            FROM platform.run_events AS event
            JOIN platform.runs AS run ON run.id = event.run_id
            WHERE run.status IN ({_TERMINAL_SQL})
              AND run.finished_at IS NOT NULL
              AND run.finished_at < :cutoff
              AND event.created_at < :cutoff
            """
        ),
        {"cutoff": cutoff},
    )
    return int(value or 0)


async def _delete_events(session: AsyncSession, cutoff: datetime | None) -> None:
    if cutoff is None:
        return
    await session.execute(
        text(
            f"""
            DELETE FROM platform.run_events AS event
            USING platform.runs AS run
            WHERE run.id = event.run_id
              AND run.status IN ({_TERMINAL_SQL})
              AND run.finished_at IS NOT NULL
              AND run.finished_at < :cutoff
              AND event.created_at < :cutoff
            """
        ),
        {"cutoff": cutoff},
    )


async def _eligible_threads_sql() -> str:
    return f"""
        SELECT thread.id::text
        FROM platform.threads AS thread
        WHERE NOT EXISTS (
            SELECT 1 FROM platform.runs AS active
            WHERE active.thread_id = thread.id
              AND active.status IN ('pending', 'running')
        )
        AND EXISTS (
            SELECT 1 FROM platform.runs AS done
            WHERE done.thread_id = thread.id
              AND done.status IN ({_TERMINAL_SQL})
              AND done.finished_at IS NOT NULL
              AND done.finished_at < :cutoff
        )
        AND NOT EXISTS (
            SELECT 1 FROM platform.runs AS recent
            WHERE recent.thread_id = thread.id
              AND (recent.finished_at IS NULL OR recent.finished_at >= :cutoff)
        )
    """


async def _count_runs(session: AsyncSession, cutoff: datetime | None) -> int:
    if cutoff is None:
        return 0
    value = await session.scalar(
        text(
            f"""
            SELECT count(*)
            FROM platform.runs
            WHERE thread_id::text IN ({await _eligible_threads_sql()})
              AND status IN ({_TERMINAL_SQL})
              AND finished_at IS NOT NULL
              AND finished_at < :cutoff
            """
        ),
        {"cutoff": cutoff},
    )
    return int(value or 0)


async def _delete_runs(session: AsyncSession, cutoff: datetime | None) -> None:
    if cutoff is None:
        return
    await session.execute(
        text(
            f"""
            DELETE FROM platform.runs
            WHERE thread_id::text IN ({await _eligible_threads_sql()})
              AND status IN ({_TERMINAL_SQL})
              AND finished_at IS NOT NULL
              AND finished_at < :cutoff
            """
        ),
        {"cutoff": cutoff},
    )


async def _count_checkpoint_threads(session: AsyncSession, cutoff: datetime | None) -> int:
    if cutoff is None:
        return 0
    if not await _checkpoint_tables_present(session):
        raise RuntimeError("LangGraph checkpoint tables are not initialized")
    value = await session.scalar(
        text(
            f"""
            SELECT count(DISTINCT checkpoints.thread_id)
            FROM checkpoints
            WHERE checkpoints.thread_id IN ({await _eligible_threads_sql()})
            """
        ),
        {"cutoff": cutoff},
    )
    return int(value or 0)


async def _delete_checkpoint_family(session: AsyncSession, cutoff: datetime | None) -> None:
    """Delete writes, blobs, checkpoints, and whole store prefixes together."""
    if cutoff is None:
        return
    if not await _checkpoint_tables_present(session):
        raise RuntimeError("LangGraph checkpoint tables are not initialized")
    params = {"cutoff": cutoff}
    threads = await _eligible_threads_sql()
    await session.execute(
        text(
            f"""
            DELETE FROM checkpoint_writes
            WHERE thread_id IN ({threads})
            """
        ),
        params,
    )
    await session.execute(
        text(
            f"""
            DELETE FROM checkpoint_blobs
            WHERE thread_id IN ({threads})
            """
        ),
        params,
    )
    await session.execute(
        text(
            f"""
            DELETE FROM checkpoints
            WHERE thread_id IN ({threads})
            """
        ),
        params,
    )
    if await _table_present(session, "store"):
        await session.execute(
            text(
                f"""
                DELETE FROM store
                WHERE prefix IN (
                    SELECT DISTINCT store.prefix
                    FROM store
                    JOIN ({threads}) AS eligible(thread_id)
                      ON store.prefix LIKE '%%' || eligible.thread_id || '%%'
                )
                """
            ),
            params,
        )


async def _count_audit(
    session: AsyncSession,
    cutoff: datetime | None,
    *,
    include_security_audit: bool,
) -> int:
    if cutoff is None:
        return 0
    security = "" if include_security_audit else f"AND {_SECURITY_FILTER}"
    value = await session.scalar(
        text(
            f"""
            SELECT count(*) FROM platform.audit_log
            WHERE created_at < :cutoff
            {security}
            """
        ),
        {"cutoff": cutoff},
    )
    return int(value or 0)


async def _delete_audit(
    session: AsyncSession,
    cutoff: datetime | None,
    *,
    include_security_audit: bool,
) -> None:
    if cutoff is None:
        return
    security = "" if include_security_audit else f"AND {_SECURITY_FILTER}"
    await session.execute(
        text(
            f"""
            DELETE FROM platform.audit_log
            WHERE created_at < :cutoff
            {security}
            """
        ),
        {"cutoff": cutoff},
    )


async def _count_keys(session: AsyncSession, cutoff: datetime | None) -> int:
    if cutoff is None:
        return 0
    value = await session.scalar(
        text(
            """
            SELECT count(*) FROM platform.api_keys
            WHERE (revoked_at IS NOT NULL AND revoked_at < :cutoff)
               OR (expires_at IS NOT NULL AND expires_at < :cutoff)
            """
        ),
        {"cutoff": cutoff},
    )
    return int(value or 0)


async def _delete_keys(session: AsyncSession, cutoff: datetime | None) -> None:
    if cutoff is None:
        return
    await session.execute(
        text(
            """
            DELETE FROM platform.api_keys
            WHERE (revoked_at IS NOT NULL AND revoked_at < :cutoff)
               OR (expires_at IS NOT NULL AND expires_at < :cutoff)
            """
        ),
        {"cutoff": cutoff},
    )


async def _table_present(session: AsyncSession, name: str) -> bool:
    value = await session.scalar(
        text(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = :name
            )
            """
        ),
        {"name": name},
    )
    return bool(value)


async def _checkpoint_tables_present(session: AsyncSession) -> bool:
    for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        if not await _table_present(session, name):
            return False
    return True
