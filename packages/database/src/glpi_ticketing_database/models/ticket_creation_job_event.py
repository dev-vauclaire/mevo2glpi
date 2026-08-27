"""Append-only job event model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import ErrorScope, JobEventActor, JobStage, JobStatus, sql_values


class TicketCreationJobEvent(Base):
    """Append-only, sanitized audit event displayed by the dashboard."""

    __tablename__ = "ticket_creation_job_events"

    event_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    job_uuid: Mapped[UUID] = mapped_column(
        Uuid(native_uuid=True),
        ForeignKey("ticket_creation_jobs.uuid", ondelete="RESTRICT"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    error_scope: Mapped[str | None] = mapped_column(String(16))
    error_category: Mapped[str | None] = mapped_column(String(64))
    technical_code: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"status IN ({sql_values(JobStatus)})",
            name="status",
        ),
        CheckConstraint(
            f"stage IS NULL OR stage IN ({sql_values(JobStage)})",
            name="stage",
        ),
        CheckConstraint(
            f"error_scope IS NULL OR error_scope IN ({sql_values(ErrorScope)})",
            name="error_scope",
        ),
        CheckConstraint(
            f"actor IN ({sql_values(JobEventActor)})",
            name="actor",
        ),
        CheckConstraint(
            "(error_scope IS NULL AND error_category IS NULL "
            "AND technical_code IS NULL) OR "
            "(error_scope IS NOT NULL AND error_category IS NOT NULL "
            "AND technical_code IS NOT NULL)",
            name="error_fields",
        ),
        Index(
            "ix_ticket_creation_job_events_history",
            job_uuid,
            occurred_at.desc(),
            event_id.desc(),
        ),
    )


__all__ = ["TicketCreationJobEvent"]
