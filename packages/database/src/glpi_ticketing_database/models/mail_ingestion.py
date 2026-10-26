"""Persistent Exchange-message ingestion model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import (
    AudioExtension,
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionStage,
    MailIngestionStatus,
    sql_values,
)


class MailIngestion(Base):
    """Durable, content-free state for one Exchange message ingestion."""

    __tablename__ = "mail_ingestions"

    uuid: Mapped[UUID] = mapped_column(Uuid(native_uuid=True), primary_key=True)
    mailbox_id: Mapped[UUID] = mapped_column(Uuid(native_uuid=True), nullable=False)
    message_id: Mapped[str] = mapped_column(
        String(512, collation="C"),
        nullable=False,
    )
    attachment_id: Mapped[str | None] = mapped_column(
        String(512, collation="C"),
    )
    job_uuid: Mapped[UUID | None] = mapped_column(
        Uuid(native_uuid=True),
        ForeignKey("ticket_creation_jobs.uuid", ondelete="RESTRICT"),
        unique=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'pending'"),
    )
    stage: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'discovery'"),
    )
    disposition: Mapped[str | None] = mapped_column(String(16))
    audio_extension: Mapped[str | None] = mapped_column(String(4))
    attachment_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
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
    rejection_category: Mapped[str | None] = mapped_column(String(64))
    rejection_code: Mapped[str | None] = mapped_column(String(64))
    last_error_scope: Mapped[str | None] = mapped_column(String(16))
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
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
        UniqueConstraint(
            "mailbox_id",
            "message_id",
            name="uq_mail_ingestions_source",
        ),
        CheckConstraint(
            "length(btrim(message_id)) > 0",
            name="message_id_not_empty",
        ),
        CheckConstraint(
            "attachment_id IS NULL OR length(btrim(attachment_id)) > 0",
            name="attachment_id_not_empty",
        ),
        CheckConstraint(
            f"status IN ({sql_values(MailIngestionStatus)})",
            name="status",
        ),
        CheckConstraint(
            f"stage IN ({sql_values(MailIngestionStage)})",
            name="stage",
        ),
        CheckConstraint(
            f"disposition IS NULL OR "
            f"disposition IN ({sql_values(MailIngestionDisposition)})",
            name="disposition",
        ),
        CheckConstraint(
            f"audio_extension IS NULL OR "
            f"audio_extension IN ({sql_values(AudioExtension)})",
            name="audio_extension",
        ),
        CheckConstraint(
            f"last_error_scope IS NULL OR "
            f"last_error_scope IN ({sql_values(MailIngestionErrorScope)})",
            name="last_error_scope",
        ),
        CheckConstraint(
            "attachment_size_bytes IS NULL OR attachment_size_bytes >= 0",
            name="attachment_size_bytes_non_negative",
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
            "status <> 'retry_scheduled' OR consecutive_retry_count > 0",
            name="retry_has_attempt",
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
            "(status = 'pending' AND stage = 'discovery' "
            "AND consecutive_retry_count = 0 "
            "AND ((attempt_count = 0 AND first_started_at IS NULL) "
            "OR (attempt_count > 0 AND first_started_at IS NOT NULL))) OR "
            "(status <> 'pending' AND attempt_count > 0 "
            "AND first_started_at IS NOT NULL)",
            name="attempt_state",
        ),
        CheckConstraint(
            "status <> 'pending' OR "
            "(attachment_id IS NULL AND audio_extension IS NULL "
            "AND attachment_size_bytes IS NULL AND job_uuid IS NULL "
            "AND disposition IS NULL AND rejection_category IS NULL "
            "AND rejection_code IS NULL)",
            name="pending_state",
        ),
        CheckConstraint(
            "audio_extension IS NULL OR "
            "(attachment_id IS NOT NULL AND attachment_size_bytes IS NOT NULL)",
            name="audio_extension_has_attachment",
        ),
        CheckConstraint(
            "stage <> 'publication' OR "
            "(audio_extension IS NOT NULL AND attachment_size_bytes > 0)",
            name="publication_has_audio_extension",
        ),
        CheckConstraint(
            "(stage = 'acknowledgement' AND disposition IS NOT NULL) OR "
            "(stage <> 'acknowledgement' AND disposition IS NULL)",
            name="disposition_at_acknowledgement",
        ),
        CheckConstraint(
            "disposition IS NULL OR disposition <> 'accepted' OR "
            "(job_uuid IS NOT NULL AND attachment_id IS NOT NULL "
            "AND audio_extension IS NOT NULL "
            "AND attachment_size_bytes IS NOT NULL "
            "AND attachment_size_bytes > 0 "
            "AND rejection_category IS NULL AND rejection_code IS NULL)",
            name="accepted_disposition",
        ),
        CheckConstraint(
            "(disposition = 'rejected' AND job_uuid IS NULL "
            "AND rejection_category IS NOT NULL AND rejection_code IS NOT NULL) OR "
            "((disposition IS NULL OR disposition <> 'rejected') "
            "AND rejection_category IS NULL AND rejection_code IS NULL)",
            name="rejected_disposition",
        ),
        CheckConstraint(
            "job_uuid IS NULL OR "
            "(disposition IS NOT NULL AND disposition = 'accepted')",
            name="job_for_accepted_disposition",
        ),
        CheckConstraint(
            "status <> 'completed' OR "
            "(disposition IS NOT NULL AND disposition = 'accepted')",
            name="completed_disposition",
        ),
        CheckConstraint(
            "status <> 'failed' OR "
            "(disposition IS NOT NULL AND disposition = 'rejected')",
            name="failed_disposition",
        ),
        CheckConstraint(
            "(last_error_scope IS NULL AND last_error_category IS NULL "
            "AND last_error_code IS NULL) OR "
            "(last_error_scope IS NOT NULL AND last_error_category IS NOT NULL "
            "AND last_error_code IS NOT NULL)",
            name="error_fields",
        ),
        CheckConstraint(
            "(status IN ('retry_scheduled', 'blocked') "
            "AND last_error_scope IS NOT NULL) OR "
            "(status NOT IN ('retry_scheduled', 'blocked') "
            "AND last_error_scope IS NULL)",
            name="error_for_status",
        ),
        Index(
            "ix_mail_ingestions_pending",
            received_at,
            uuid,
            postgresql_where=status == MailIngestionStatus.PENDING.value,
        ),
        Index(
            "ix_mail_ingestions_retry_scheduled",
            next_attempt_at,
            received_at,
            uuid,
            postgresql_where=status == MailIngestionStatus.RETRY_SCHEDULED.value,
        ),
        Index(
            "ix_mail_ingestions_dashboard",
            updated_at.desc(),
            uuid.desc(),
        ),
        Index(
            "uq_mail_ingestions_single_processing",
            status,
            unique=True,
            postgresql_where=status == MailIngestionStatus.PROCESSING.value,
        ),
    )


__all__ = ["MailIngestion"]
