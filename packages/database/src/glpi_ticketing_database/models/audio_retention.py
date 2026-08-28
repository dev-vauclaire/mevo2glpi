"""Audio retention and deletion audit model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Uuid,
    func,
    literal_column,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import DeletionReason, RetentionPolicy, RetentionStatus, sql_values


class AudioRetention(Base):
    """Retention policy and deletion audit for one job's audio bytes."""

    __tablename__ = "audio_retentions"

    job_uuid: Mapped[UUID] = mapped_column(
        Uuid(native_uuid=True),
        ForeignKey("ticket_creation_jobs.uuid", ondelete="RESTRICT"),
        primary_key=True,
    )
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        server_default=text("'inactive'"),
    )
    hard_delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_glpi_status_id: Mapped[int | None] = mapped_column(SmallInteger)
    check_attempt_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    consecutive_retry_count: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    deletion_reason: Mapped[str | None] = mapped_column(String(32))
    deletion_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    audio_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            f"policy IN ({sql_values(RetentionPolicy)})",
            name="policy",
        ),
        CheckConstraint(
            f"status IN ({sql_values(RetentionStatus)})",
            name="status",
        ),
        CheckConstraint(
            "check_attempt_count >= 0",
            name="check_attempt_count_non_negative",
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
            f"deletion_reason IS NULL OR "
            f"deletion_reason IN ({sql_values(DeletionReason)})",
            name="deletion_reason",
        ),
        CheckConstraint(
            "(status = 'inactive' AND hard_delete_at IS NULL) OR "
            "(status <> 'inactive' AND hard_delete_at IS NOT NULL)",
            name="active_hard_delete_at",
        ),
        CheckConstraint(
            "((status = 'retry_scheduled' OR "
            "(policy = 'ticket_or_deadline' AND status = 'scheduled')) "
            "AND next_check_at IS NOT NULL) OR "
            "((status <> 'retry_scheduled' AND "
            "(policy <> 'ticket_or_deadline' OR status <> 'scheduled')) "
            "AND next_check_at IS NULL)",
            name="next_check_for_policy",
        ),
        CheckConstraint(
            "status <> 'checking' OR policy = 'ticket_or_deadline'",
            name="glpi_check_policy",
        ),
        CheckConstraint(
            "status <> 'checking' OR last_checked_at IS NOT NULL",
            name="checking_has_timestamp",
        ),
        CheckConstraint(
            "last_glpi_status_id IS NULL OR last_glpi_status_id > 0",
            name="last_glpi_status_id_positive",
        ),
        CheckConstraint(
            "policy = 'ticket_or_deadline' OR last_glpi_status_id IS NULL",
            name="last_glpi_status_for_policy",
        ),
        CheckConstraint(
            "(deletion_reason IS NULL AND deletion_started_at IS NULL) OR "
            "(deletion_reason IS NOT NULL AND deletion_started_at IS NOT NULL)",
            name="deletion_fields",
        ),
        CheckConstraint(
            "(status IN ('deleting', 'deleted') "
            "AND deletion_reason IS NOT NULL) OR "
            "status IN ('retry_scheduled', 'blocked') OR "
            "(status IN ('inactive', 'scheduled', 'checking') "
            "AND deletion_reason IS NULL)",
            name="deletion_for_status",
        ),
        CheckConstraint(
            "deletion_reason IS NULL OR "
            "(policy = 'ticket_or_deadline' AND "
            "deletion_reason IN ('ticket_closed', 'deadline_expired')) OR "
            "(policy = 'deadline_only' AND "
            "deletion_reason IN ('failed_ttl', 'blocked_ttl'))",
            name="deletion_reason_for_policy",
        ),
        CheckConstraint(
            "policy <> 'deadline_only' OR "
            "status NOT IN ('retry_scheduled', 'blocked') OR "
            "(deletion_reason IS NOT NULL AND deletion_started_at IS NOT NULL)",
            name="deadline_retry_has_deletion_context",
        ),
        CheckConstraint(
            "(status = 'deleted' AND audio_deleted_at IS NOT NULL) OR "
            "(status <> 'deleted' AND audio_deleted_at IS NULL)",
            name="audio_deleted_status",
        ),
        CheckConstraint(
            "audio_deleted_at IS NULL OR "
            "(deletion_started_at IS NOT NULL AND "
            "audio_deleted_at >= deletion_started_at)",
            name="audio_deleted_after_deletion_started",
        ),
        CheckConstraint(
            "deletion_started_at IS NOT NULL OR next_check_at IS NULL OR "
            "next_check_at <= hard_delete_at",
            name="next_check_before_deadline",
        ),
        CheckConstraint(
            "(last_error_category IS NULL AND last_error_code IS NULL) OR "
            "(last_error_category IS NOT NULL AND last_error_code IS NOT NULL)",
            name="error_fields",
        ),
        CheckConstraint(
            "(status IN ('retry_scheduled', 'blocked') "
            "AND last_error_category IS NOT NULL) OR "
            "(status NOT IN ('retry_scheduled', 'blocked') "
            "AND last_error_category IS NULL)",
            name="error_for_status",
        ),
        Index(
            "ix_audio_retentions_deadline",
            hard_delete_at,
            job_uuid,
            postgresql_where=status.not_in(
                (RetentionStatus.INACTIVE.value, RetentionStatus.DELETED.value)
            ),
        ),
        Index(
            "ix_audio_retentions_due_check",
            next_check_at,
            job_uuid,
            postgresql_where=(
                (
                    (
                        (policy == RetentionPolicy.TICKET_OR_DEADLINE.value)
                        & (status == RetentionStatus.SCHEDULED.value)
                    )
                    | (status == RetentionStatus.RETRY_SCHEDULED.value)
                )
                & next_check_at.is_not(None)
            ),
        ),
        Index(
            "uq_audio_retentions_single_active_operation",
            literal_column("(1)"),
            unique=True,
            postgresql_where=status.in_(
                (RetentionStatus.CHECKING.value, RetentionStatus.DELETING.value)
            ),
        ),
    )


__all__ = ["AudioRetention"]
