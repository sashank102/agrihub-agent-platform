"""Permit at most one pending or running run per thread.

Revision ID: 20260926_0003
Revises: 20260920_0002
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260926_0003"
down_revision: str | Sequence[str] | None = "20260920_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "platform"
INDEX = "uq_runs_one_active_per_thread"


def upgrade() -> None:
    """Reject a second active run at the database, including admission races."""
    op.execute(
        f"""
        DO $$
        DECLARE
            duplicate_threads text;
        BEGIN
            SELECT string_agg(thread_id::text, ', ' ORDER BY thread_id::text)
            INTO duplicate_threads
            FROM (
                SELECT thread_id
                FROM "{SCHEMA}"."runs"
                WHERE status IN ('pending', 'running')
                GROUP BY thread_id
                HAVING count(*) > 1
                LIMIT 20
            ) AS duplicates;

            IF duplicate_threads IS NOT NULL THEN
                RAISE EXCEPTION
                    'cannot create {INDEX}: multiple active runs exist for thread(s): %',
                    duplicate_threads
                    USING HINT =
                        'Reconcile or mark stale pending/running rows terminal before retrying the migration.';
            END IF;
        END;
        $$;
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX "{INDEX}"
        ON "{SCHEMA}"."runs" (thread_id)
        WHERE status IN ('pending', 'running')
        """
    )


def downgrade() -> None:
    """Remove the one-active-run invariant."""
    op.execute(f'DROP INDEX IF EXISTS "{SCHEMA}"."{INDEX}"')
