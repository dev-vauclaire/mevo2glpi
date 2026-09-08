"""Repository dedicated to the ``MailIngestionEvent`` model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import (
    MailIngestionDisposition,
    MailIngestionErrorScope,
    MailIngestionEvent,
    MailIngestionEventType,
    MailIngestionStage,
    MailIngestionStatus,
)
from ._validation import (
    validate_optional_error,
    validate_page_limit,
    validate_positive_integer,
    validate_utc_datetime,
    validate_uuid,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class MailIngestionEventSnapshot:
    """Immutable typed view of one sanitized publisher event."""

    event_id: int
    ingestion_uuid: UUID
    occurred_at: datetime
    status: MailIngestionStatus
    stage: MailIngestionStage
    disposition: MailIngestionDisposition | None
    event_type: MailIngestionEventType
    error_scope: MailIngestionErrorScope | None
    error_category: str | None
    technical_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class MailIngestionEventCursor:
    """Stable keyset cursor for one ingestion's descending event history."""

    occurred_at: datetime
    event_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MailIngestionEventPage:
    """One bounded event page and the cursor for the following page."""

    items: tuple[MailIngestionEventSnapshot, ...]
    next_cursor: MailIngestionEventCursor | None
    has_more: bool


class MailIngestionEventRepository:
    """Append sanitized ingestion events in a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def append(
        self,
        ingestion_uuid: UUID,
        *,
        occurred_at: datetime,
        status: MailIngestionStatus,
        stage: MailIngestionStage,
        disposition: MailIngestionDisposition | None,
        event_type: MailIngestionEventType,
        error_scope: MailIngestionErrorScope | None = None,
        error_category: str | None = None,
        technical_code: str | None = None,
    ) -> None:
        """Append one event without reading or mutating its ingestion."""
        validate_uuid(ingestion_uuid, "ingestion_uuid")
        occurred_at = validate_utc_datetime(occurred_at, "occurred_at")
        if not isinstance(status, MailIngestionStatus):
            raise TypeError("status must be a MailIngestionStatus")
        if not isinstance(stage, MailIngestionStage):
            raise TypeError("stage must be a MailIngestionStage")
        if disposition is not None and not isinstance(
            disposition,
            MailIngestionDisposition,
        ):
            raise TypeError("disposition must be a MailIngestionDisposition or None")
        if not isinstance(event_type, MailIngestionEventType):
            raise TypeError("event_type must be a MailIngestionEventType")
        if (stage is MailIngestionStage.ACKNOWLEDGEMENT) != (disposition is not None):
            raise ValueError(
                "disposition must be provided exactly at acknowledgement stage"
            )
        if error_scope is not None and not isinstance(
            error_scope,
            MailIngestionErrorScope,
        ):
            raise TypeError("error_scope must be a MailIngestionErrorScope or None")
        error_category, technical_code = validate_optional_error(
            error_category,
            technical_code,
        )
        if (error_scope is None) != (error_category is None):
            raise ValueError(
                "error_scope, error_category and technical_code must be provided together"
            )

        event = MailIngestionEvent(
            ingestion_uuid=ingestion_uuid,
            occurred_at=occurred_at,
            status=status.value,
            stage=stage.value,
            disposition=(disposition.value if disposition is not None else None),
            event_type=event_type.value,
            error_scope=error_scope.value if error_scope is not None else None,
            error_category=error_category,
            technical_code=technical_code,
        )
        self._session.add(event)
        self._session.flush()

    def list_page(
        self,
        ingestion_uuid: UUID,
        *,
        limit: int,
        cursor: MailIngestionEventCursor | None = None,
    ) -> MailIngestionEventPage:
        """List one ingestion's events with descending keyset pagination."""
        validate_uuid(ingestion_uuid, "ingestion_uuid")
        limit = validate_page_limit(limit)
        if cursor is not None:
            if not isinstance(cursor, MailIngestionEventCursor):
                raise TypeError("cursor must be a MailIngestionEventCursor or None")
            validate_utc_datetime(cursor.occurred_at, "cursor.occurred_at")
            validate_positive_integer(cursor.event_id, "cursor.event_id")

        statement = select(MailIngestionEvent).where(
            MailIngestionEvent.ingestion_uuid == ingestion_uuid
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    MailIngestionEvent.occurred_at < cursor.occurred_at,
                    and_(
                        MailIngestionEvent.occurred_at == cursor.occurred_at,
                        MailIngestionEvent.event_id < cursor.event_id,
                    ),
                )
            )
        statement = statement.order_by(
            MailIngestionEvent.occurred_at.desc(),
            MailIngestionEvent.event_id.desc(),
        ).limit(limit + 1)

        events = tuple(self._session.scalars(statement).all())
        has_more = len(events) > limit
        items = tuple(_to_snapshot(event) for event in events[:limit])
        next_cursor = None
        if has_more:
            last_item = items[-1]
            next_cursor = MailIngestionEventCursor(
                occurred_at=last_item.occurred_at,
                event_id=last_item.event_id,
            )
        return MailIngestionEventPage(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )


def _to_snapshot(event: MailIngestionEvent) -> MailIngestionEventSnapshot:
    return MailIngestionEventSnapshot(
        event_id=event.event_id,
        ingestion_uuid=event.ingestion_uuid,
        occurred_at=event.occurred_at,
        status=MailIngestionStatus(event.status),
        stage=MailIngestionStage(event.stage),
        disposition=(
            MailIngestionDisposition(event.disposition)
            if event.disposition is not None
            else None
        ),
        event_type=MailIngestionEventType(event.event_type),
        error_scope=(
            MailIngestionErrorScope(event.error_scope)
            if event.error_scope is not None
            else None
        ),
        error_category=event.error_category,
        technical_code=event.technical_code,
    )


__all__ = [
    "MailIngestionEventCursor",
    "MailIngestionEventPage",
    "MailIngestionEventRepository",
    "MailIngestionEventSnapshot",
]
