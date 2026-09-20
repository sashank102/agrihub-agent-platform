"""Execution metadata for agent runs."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_platform.db.base import Base
from agent_platform.db.models.common import CreatedAtMixin

RUN_STATUSES = (
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
)


class Run(CreatedAtMixin, Base):
    """One execution attempt for a thread and agent."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN "
            "('pending', 'running', 'completed', 'failed', "
            "'cancelled', 'interrupted')",
            name="run_status",
        ),
        Index("ix_runs_thread_created_at", "thread_id", "created_at"),
        Index("ix_runs_agent_id", "agent_id"),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index(
            "ix_runs_active_thread",
            "thread_id",
            "created_at",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
        Index(
            "ix_runs_finished_at",
            "finished_at",
            postgresql_where=text("finished_at IS NOT NULL"),
        ),
        Index(
            "uq_runs_thread_idempotency_key",
            "thread_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform.threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform.agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cancellation_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
