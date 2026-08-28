"""Repository dedicated to the ``TicketCreationJobEvent`` model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
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
    validate_uuid,
)


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


__all__ = ["TicketCreationJobEventRepository"]
