"""Singleton worker heartbeat model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import WorkerRuntimeState, WorkerServiceName, sql_values


class WorkerRuntimeStatus(Base):
    """Latest heartbeat and sanitized health state for a singleton worker."""

    __tablename__ = "worker_runtime_status"

    service_name: Mapped[str] = mapped_column(String(32), primary_key=True)
    instance_id: Mapped[UUID] = mapped_column(Uuid(native_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'starting'"),
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    current_job_uuid: Mapped[UUID | None] = mapped_column(
        Uuid(native_uuid=True),
        ForeignKey("ticket_creation_jobs.uuid", ondelete="SET NULL"),
    )
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            f"service_name IN ({sql_values(WorkerServiceName)})",
            name="service_name",
        ),
        CheckConstraint(
            f"state IN ({sql_values(WorkerRuntimeState)})",
            name="state",
        ),
        CheckConstraint(
            "(last_error_category IS NULL AND last_error_code IS NULL) OR "
            "(last_error_category IS NOT NULL AND last_error_code IS NOT NULL)",
            name="error_fields",
        ),
        CheckConstraint(
            "(state IN ('degraded', 'blocked') "
            "AND last_error_category IS NOT NULL) OR "
            "(state NOT IN ('degraded', 'blocked') "
            "AND last_error_category IS NULL)",
            name="error_for_state",
        ),
        Index("ix_worker_runtime_status_heartbeat", last_heartbeat_at),
    )


__all__ = ["WorkerRuntimeStatus"]
