"""Align platform check-constraint names with SQLAlchemy metadata.

Revision ID: 20260920_0002
Revises: 20260920_0001
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260920_0002"
down_revision: str | Sequence[str] | None = "20260920_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "platform"
CONSTRAINT_RENAMES = (
    ("users", "user_status", "ck_users_user_status"),
    ("agents", "agent_version_positive", "ck_agents_agent_version_positive"),
    ("threads", "thread_status", "ck_threads_thread_status"),
    ("runs", "run_status", "ck_runs_run_status"),
    (
        "run_events",
        "run_event_sequence_positive",
        "ck_run_events_run_event_sequence_positive",
    ),
)


def _rename_constraint(table: str, old_name: str, new_name: str) -> None:
    """Rename one known constraint when the source name exists.

    Alembic may apply the metadata naming convention while executing the
    original revision on a fresh database. The guard keeps this remediation
    compatible with both those databases and already-deployed databases whose
    initial revision retained the short names.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conrelid = '"{SCHEMA}"."{table}"'::regclass
                  AND conname = '{old_name}'
            ) THEN
                ALTER TABLE "{SCHEMA}"."{table}"
                RENAME CONSTRAINT "{old_name}" TO "{new_name}";
            END IF;
        END;
        $$
        """
    )


def upgrade() -> None:
    """Apply SQLAlchemy naming-convention names."""
    for table, old_name, new_name in CONSTRAINT_RENAMES:
        _rename_constraint(table, old_name, new_name)


def downgrade() -> None:
    """Restore the names created by the initial revision."""
    for table, old_name, new_name in reversed(CONSTRAINT_RENAMES):
        _rename_constraint(table, new_name, old_name)
