"""Append-only Exchange-message ingestion event model."""

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
from .enums import (
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEventType,
    MailIngestionStage,
    MailIngestionStatus,
    sql_values,
)


class MailIngestionEvent(Base):
    """Append-only, sanitized audit event for one mail ingestion."""

    __tablename__ = "mail_ingestion_events"

    event_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        primary_key=True,
    )
    ingestion_uuid: Mapped[UUID] = mapped_column(
        Uuid(native_uuid=True),
        ForeignKey("mail_ingestions.uuid", ondelete="RESTRICT"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    disposition: Mapped[str | None] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    error_scope: Mapped[str | None] = mapped_column(String(16))
    error_category: Mapped[str | None] = mapped_column(String(64))
    technical_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
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
            f"event_type IN ({sql_values(MailIngestionEventType)})",
            name="event_type",
        ),
        CheckConstraint(
            f"error_scope IS NULL OR "
            f"error_scope IN ({sql_values(MailIngestionErrorScope)})",
            name="error_scope",
        ),
        CheckConstraint(
            "(stage = 'acknowledgement' AND disposition IS NOT NULL) OR "
            "(stage <> 'acknowledgement' AND disposition IS NULL)",
            name="disposition_at_acknowledgement",
        ),
        CheckConstraint(
            "(error_scope IS NULL AND error_category IS NULL "
            "AND technical_code IS NULL) OR "
            "(error_scope IS NOT NULL AND error_category IS NOT NULL "
            "AND technical_code IS NOT NULL)",
            name="error_fields",
        ),
        Index(
            "ix_mail_ingestion_events_history",
            ingestion_uuid,
            occurred_at.desc(),
            event_id.desc(),
        ),
    )


__all__ = ["MailIngestionEvent"]
