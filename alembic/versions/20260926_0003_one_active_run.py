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
        CREATE UNIQUE INDEX "{INDEX}"
        ON "{SCHEMA}"."runs" (thread_id)
        WHERE status IN ('pending', 'running')
        """
    )


def downgrade() -> None:
    """Remove the one-active-run invariant."""
    op.execute(f'DROP INDEX IF EXISTS "{SCHEMA}"."{INDEX}"')
