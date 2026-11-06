"""Repository dedicated to the ``TicketCreationJobEvent`` model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import (
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    TicketCreationJobEvent,
)
from ._validation import (
    validate_aware_datetime,
    validate_optional_error,
    validate_page_limit,
    validate_positive_integer,
    validate_uuid,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class JobEventSnapshot:
    """Immutable, typed view of one sanitized lifecycle event."""

    event_id: int
    job_uuid: UUID
    occurred_at: datetime
    status: JobStatus
    stage: JobStage | None
    event_type: JobEventType
    error_scope: ErrorScope | None
    error_category: str | None
    technical_code: str | None
    actor: JobEventActor


@dataclass(frozen=True, slots=True, kw_only=True)
class JobEventCursor:
    """Stable keyset cursor for one job's descending event history."""

    occurred_at: datetime
    event_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class JobEventPage:
    """One bounded event page and the cursor for the following page."""

    items: tuple[JobEventSnapshot, ...]
    next_cursor: JobEventCursor | None
    has_more: bool


class TicketCreationJobEventRepository:
    """Append sanitized job events using a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def append(
        self,
        job_uuid: UUID,
        *,
        occurred_at: datetime,
        status: JobStatus,
        stage: JobStage | None,
        event_type: JobEventType,
        actor: JobEventActor,
        error_scope: ErrorScope | None = None,
        error_category: str | None = None,
        technical_code: str | None = None,
    ) -> None:
        """Append one event without reading or mutating its related job."""
        validate_uuid(job_uuid, "job_uuid")
        occurred_at = validate_aware_datetime(occurred_at, "occurred_at")
        if not isinstance(status, JobStatus):
            raise TypeError("status must be a JobStatus")
        if stage is not None and not isinstance(stage, JobStage):
            raise TypeError("stage must be a JobStage or None")
        if not isinstance(event_type, JobEventType):
            raise TypeError("event_type must be a JobEventType")
        if not isinstance(actor, JobEventActor):
            raise TypeError("actor must be a JobEventActor")
        if error_scope is not None and not isinstance(error_scope, ErrorScope):
            raise TypeError("error_scope must be an ErrorScope or None")
        error_category, technical_code = validate_optional_error(
            error_category,
            technical_code,
        )
        if (error_scope is None) != (error_category is None):
            raise ValueError(
                "error_scope, error_category and technical_code must be provided together"
            )

        event = TicketCreationJobEvent(
            job_uuid=job_uuid,
            occurred_at=occurred_at,
            status=status.value,
            stage=stage.value if stage is not None else None,
            event_type=event_type.value,
            error_scope=error_scope.value if error_scope is not None else None,
            error_category=error_category,
            technical_code=technical_code,
            actor=actor.value,
        )
        self._session.add(event)
        self._session.flush()

    def exists(
        self,
        job_uuid: UUID,
        *,
        event_type: JobEventType,
        actor: JobEventActor,
    ) -> bool:
        """Return whether the exact application event exists for one job."""
        validate_uuid(job_uuid, "job_uuid")
        if not isinstance(event_type, JobEventType):
            raise TypeError("event_type must be a JobEventType")
        if not isinstance(actor, JobEventActor):
            raise TypeError("actor must be a JobEventActor")
        statement = (
            select(TicketCreationJobEvent.event_id)
            .where(
                TicketCreationJobEvent.job_uuid == job_uuid,
                TicketCreationJobEvent.event_type == event_type.value,
                TicketCreationJobEvent.actor == actor.value,
            )
            .limit(1)
        )
        return self._session.scalar(statement) is not None

    def list_page(
        self,
        job_uuid: UUID,
        *,
        limit: int,
        cursor: JobEventCursor | None = None,
    ) -> JobEventPage:
        """List one job's event history with descending keyset pagination."""
        validate_uuid(job_uuid, "job_uuid")
        limit = validate_page_limit(limit)
        if cursor is not None:
            if not isinstance(cursor, JobEventCursor):
                raise TypeError("cursor must be a JobEventCursor or None")
            validate_aware_datetime(cursor.occurred_at, "cursor.occurred_at")
            validate_positive_integer(cursor.event_id, "cursor.event_id")

        statement = select(TicketCreationJobEvent).where(
            TicketCreationJobEvent.job_uuid == job_uuid
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    TicketCreationJobEvent.occurred_at < cursor.occurred_at,
                    and_(
                        TicketCreationJobEvent.occurred_at == cursor.occurred_at,
                        TicketCreationJobEvent.event_id < cursor.event_id,
                    ),
                )
            )
        statement = statement.order_by(
            TicketCreationJobEvent.occurred_at.desc(),
            TicketCreationJobEvent.event_id.desc(),
        ).limit(limit + 1)

        events = tuple(self._session.scalars(statement).all())
        has_more = len(events) > limit
        items = tuple(_to_snapshot(event) for event in events[:limit])
        next_cursor = None
        if has_more:
            last_item = items[-1]
            next_cursor = JobEventCursor(
                occurred_at=last_item.occurred_at,
                event_id=last_item.event_id,
            )
        return JobEventPage(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )


def _to_snapshot(event: TicketCreationJobEvent) -> JobEventSnapshot:
    return JobEventSnapshot(
        event_id=event.event_id,
        job_uuid=event.job_uuid,
        occurred_at=event.occurred_at,
        status=JobStatus(event.status),
        stage=JobStage(event.stage) if event.stage is not None else None,
        event_type=JobEventType(event.event_type),
        error_scope=(
            ErrorScope(event.error_scope) if event.error_scope is not None else None
        ),
        error_category=event.error_category,
        technical_code=event.technical_code,
        actor=JobEventActor(event.actor),
    )


__all__ = [
    "JobEventCursor",
    "JobEventPage",
    "JobEventSnapshot",
    "TicketCreationJobEventRepository",
]
