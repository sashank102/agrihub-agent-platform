"""Versioned agent definitions."""

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from agent_platform.db.base import Base
from agent_platform.db.models.common import TimestampMixin


class Agent(TimestampMixin, Base):
    """A stable graph identifier plus an owner-scoped configuration version."""

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("version > 0", name="agent_version_positive"),
        Index("ix_agents_owner_user_id", "owner_user_id"),
        Index("ix_agents_graph_id", "graph_id"),
        Index(
            "uq_agents_owner_graph_version",
            "owner_user_id",
            "graph_id",
            "version",
            unique=True,
            postgresql_where=text("owner_user_id IS NOT NULL"),
        ),
        Index(
            "uq_agents_global_graph_version",
            "graph_id",
            "version",
            unique=True,
            postgresql_where=text("owner_user_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform.users.id", ondelete="SET NULL"),
    )
    graph_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
