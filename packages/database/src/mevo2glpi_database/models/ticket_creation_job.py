"""Persistent audio-to-ticket job model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import AudioExtension, ErrorScope, JobStage, JobStatus, sql_values


class TicketCreationJob(Base):
    """Persistent source of truth for one audio-to-GLPI-ticket workflow."""

    __tablename__ = "ticket_creation_jobs"

    uuid: Mapped[UUID] = mapped_column(Uuid(native_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    stage: Mapped[str | None] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(41), nullable=False, unique=True)
    audio_extension: Mapped[str] = mapped_column(String(4), nullable=False)
    audio_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    glpi_ticket_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    attempt_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    consecutive_retry_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_scope: Mapped[str | None] = mapped_column(String(16))
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
            "(status = 'pending' AND stage IS NULL) OR "
            "(status <> 'pending' AND stage IS NOT NULL)",
            name="stage_for_status",
        ),
        CheckConstraint(
            f"audio_extension IN ({sql_values(AudioExtension)})",
            name="audio_extension",
        ),
        CheckConstraint(
            "audio_size_bytes > 0",
            name="audio_size_bytes_positive",
        ),
        CheckConstraint(
            "glpi_ticket_id IS NULL OR glpi_ticket_id > 0",
            name="glpi_ticket_id_positive",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_non_negative",
        ),
        CheckConstraint(
            "consecutive_retry_count >= 0",
            name="consecutive_retry_count_non_negative",
        ),
        CheckConstraint(
            "storage_key = (uuid::text || '.' || audio_extension)",
            name="storage_key_matches_uuid_and_extension",
        ),
        CheckConstraint(
            "(status = 'retry_scheduled' AND next_attempt_at IS NOT NULL) OR "
            "(status <> 'retry_scheduled' AND next_attempt_at IS NULL)",
            name="retry_schedule",
        ),
        CheckConstraint(
            "(status IN ('completed', 'failed', 'blocked') "
            "AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('completed', 'failed', 'blocked') "
            "AND finished_at IS NULL)",
            name="finished_at",
        ),
        CheckConstraint(
            "status <> 'completed' OR "
            "(glpi_ticket_id IS NOT NULL AND stage = 'finalization')",
            name="completed",
        ),
        CheckConstraint(
            f"last_error_scope IS NULL OR "
            f"last_error_scope IN ({sql_values(ErrorScope)})",
            name="last_error_scope",
        ),
        CheckConstraint(
            "(last_error_scope IS NULL AND last_error_category IS NULL "
            "AND last_error_code IS NULL) OR "
            "(last_error_scope IS NOT NULL AND last_error_category IS NOT NULL "
            "AND last_error_code IS NOT NULL)",
            name="error_fields",
        ),
        CheckConstraint(
            "(status IN ('retry_scheduled', 'failed', 'blocked') "
            "AND last_error_scope IS NOT NULL) OR "
            "(status NOT IN ('retry_scheduled', 'failed', 'blocked') "
            "AND last_error_scope IS NULL)",
            name="error_for_status",
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error_scope = 'job'",
            name="failed_error_scope",
        ),
        Index(
            "ix_ticket_creation_jobs_pending",
            created_at,
            uuid,
            postgresql_where=status == JobStatus.PENDING.value,
        ),
        Index(
            "ix_ticket_creation_jobs_retry_scheduled",
            next_attempt_at,
            created_at,
            uuid,
            postgresql_where=status == JobStatus.RETRY_SCHEDULED.value,
        ),
        Index(
            "ix_ticket_creation_jobs_dashboard",
            updated_at.desc(),
            uuid.desc(),
        ),
        Index(
            "ix_ticket_creation_jobs_status_dashboard",
            status,
            updated_at.desc(),
            uuid.desc(),
        ),
        Index(
            "uq_ticket_creation_jobs_single_processing",
            status,
            unique=True,
            postgresql_where=status == JobStatus.PROCESSING.value,
        ),
    )


__all__ = ["TicketCreationJob"]
