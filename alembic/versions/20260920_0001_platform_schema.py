"""Create the platform metadata schema.

Revision ID: 20260920_0001
Revises:
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260920_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "platform"


def upgrade() -> None:
    """Create only platform-owned objects."""
    op.execute(sa.schema.CreateSchema(SCHEMA))

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'deleted')",
            name="user_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_users_deleted_at",
        "users",
        ["deleted_at"],
        unique=False,
        schema=SCHEMA,
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_api_keys_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_api_keys_user_id",
        "api_keys",
        ["user_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_api_keys_expires_at",
        "api_keys",
        ["expires_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_api_keys_revoked_at",
        "api_keys",
        ["revoked_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("graph_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version > 0",
            name="agent_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_agents_owner_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_agents_owner_user_id",
        "agents",
        ["owner_user_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_agents_graph_id",
        "agents",
        ["graph_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "uq_agents_owner_graph_version",
        "agents",
        ["owner_user_id", "graph_id", "version"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_agents_global_graph_version",
        "agents",
        ["graph_id", "version"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("owner_user_id IS NULL"),
    )

    op.create_table(
        "threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'deleted')",
            name="thread_status",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            [f"{SCHEMA}.agents.id"],
            name="fk_threads_agent_id_agents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_threads_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_threads"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_threads_owner_last_activity",
        "threads",
        ["owner_user_id", "last_activity_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_threads_agent_id",
        "threads",
        ["agent_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_threads_status_last_activity",
        "threads",
        ["status", "last_activity_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "input",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "output_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "error_details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "cancellation_requested",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', "
            "'cancelled', 'interrupted')",
            name="run_status",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            [f"{SCHEMA}.agents.id"],
            name="fk_runs_agent_id_agents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            [f"{SCHEMA}.threads.id"],
            name="fk_runs_thread_id_threads",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_runs_thread_created_at",
        "runs",
        ["thread_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_runs_agent_id",
        "runs",
        ["agent_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_runs_status_created_at",
        "runs",
        ["status", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_runs_active_thread",
        "runs",
        ["thread_id", "created_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "ix_runs_finished_at",
        "runs",
        ["finished_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("finished_at IS NOT NULL"),
    )
    op.create_index(
        "uq_runs_thread_idempotency_key",
        "runs",
        ["thread_id", "idempotency_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "run_events",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="run_event_sequence_positive",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{SCHEMA}.runs.id"],
            name="fk_run_events_run_id_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "run_id",
            "sequence",
            name="pk_run_events",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_run_events_created_at",
        "run_events",
        ["created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("thread_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("external_uri", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_artifacts_owner_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            [f"{SCHEMA}.runs.id"],
            name="fk_artifacts_run_id_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            [f"{SCHEMA}.threads.id"],
            name="fk_artifacts_thread_id_threads",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_artifacts_owner_created_at",
        "artifacts",
        ["owner_user_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_artifacts_thread_created_at",
        "artifacts",
        ["thread_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_artifacts_run_id",
        "artifacts",
        ["run_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("resource_type", sa.String(length=100), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_audit_log_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_log_actor_created_at",
        "audit_log",
        ["actor_user_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_log_resource",
        "audit_log",
        ["resource_type", "resource_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_log_request_id",
        "audit_log",
        ["request_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_log_created_at",
        "audit_log",
        ["created_at"],
        schema=SCHEMA,
    )
    op.execute(
        """
        CREATE FUNCTION platform.reject_audit_log_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'platform.audit_log records are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_immutable
        BEFORE UPDATE OR DELETE ON platform.audit_log
        FOR EACH ROW
        EXECUTE FUNCTION platform.reject_audit_log_mutation()
        """
    )


def downgrade() -> None:
    """Remove platform-owned objects without touching public tables."""
    op.execute("DROP TRIGGER IF EXISTS audit_log_immutable ON platform.audit_log")
    op.drop_table("audit_log", schema=SCHEMA)
    op.execute("DROP FUNCTION IF EXISTS platform.reject_audit_log_mutation()")
    op.drop_table("artifacts", schema=SCHEMA)
    op.drop_table("run_events", schema=SCHEMA)
    op.drop_table("runs", schema=SCHEMA)
    op.drop_table("threads", schema=SCHEMA)
    op.drop_table("agents", schema=SCHEMA)
    op.drop_table("api_keys", schema=SCHEMA)
    op.drop_table("users", schema=SCHEMA)
    op.execute(sa.schema.DropSchema(SCHEMA))
