"""SQLAlchemy ORM models — compatible with both SQLite and AlloyDB."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def utc_now() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)


class AgentSession(Base):
    """Tracks an agent conversation session."""

    __tablename__ = "agent_sessions"
    __table_args__ = (
        Index("ix_agent_sessions_user_id", "user_id"),
        Index("ix_agent_sessions_status", "status"),
        Index("ix_agent_sessions_last_active_at", "last_active_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    workflow_logs: Mapped[list["WorkflowLog"]] = relationship(
        "WorkflowLog",
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WorkflowLog(Base):
    """Audit log for every action an agent takes."""

    __tablename__ = "workflow_logs"
    __table_args__ = (
        Index("ix_workflow_logs_session_id", "session_id"),
        Index("ix_workflow_logs_agent_name", "agent_name"),
        Index("ix_workflow_logs_status", "status"),
        Index("ix_workflow_logs_started_at", "started_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    workflow_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    input_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    session: Mapped["AgentSession | None"] = relationship(
        "AgentSession",
        back_populates="workflow_logs",
    )


class UserPreference(Base):
    """Per-user preferences and settings."""

    __tablename__ = "user_preferences"
    __table_args__ = (
        Index("ix_user_preferences_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="Asia/Kolkata")
    working_hours: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {"start": "09:00", "end": "17:00"},
    )
    default_calendar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferences: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )