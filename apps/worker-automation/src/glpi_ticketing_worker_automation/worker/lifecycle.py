"""Application service coordinating persistent automation-job transitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

from glpi_ticketing_database import (
    AudioRetentionRepository,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobSnapshot,
    JobStage,
    JobStatus,
    MailIngestionDisposition,
    MailIngestionRepository,
    MailIngestionSnapshot,
    RetentionPolicy,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class AutomationJobAcquisition:
    """Acquired job enriched with immutable source-message metadata."""

    job: JobSnapshot
    previous_status: JobStatus
    resumed: bool
    source_received_at: datetime | None


class AutomationJobLifecycleService:
    """Own short SQL transactions spanning model-specific repositories."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        ownership_guard: Callable[[], None] | None = None,
        job_repository_factory: Callable[
            [Session], TicketCreationJobRepository
        ] = TicketCreationJobRepository,
        event_repository_factory: Callable[
            [Session], TicketCreationJobEventRepository
        ] = TicketCreationJobEventRepository,
        retention_repository_factory: Callable[
            [Session], AudioRetentionRepository
        ] = AudioRetentionRepository,
        mail_ingestion_repository_factory: Callable[
            [Session], MailIngestionRepository
        ] = MailIngestionRepository,
    ) -> None:
        for value, field_name in (
            (session_factory, "session_factory"),
            (job_repository_factory, "job_repository_factory"),
            (event_repository_factory, "event_repository_factory"),
            (retention_repository_factory, "retention_repository_factory"),
            (
                mail_ingestion_repository_factory,
                "mail_ingestion_repository_factory",
            ),
        ):
            if not callable(value):
                raise TypeError(f"{field_name} must be callable")
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")

        self._session_factory = session_factory
        self._ownership_guard = ownership_guard or _no_op
        self._job_repository_factory = job_repository_factory
        self._event_repository_factory = event_repository_factory
        self._retention_repository_factory = retention_repository_factory
        self._mail_ingestion_repository_factory = mail_ingestion_repository_factory

    def resume_or_claim_next(self, now: datetime) -> AutomationJobAcquisition | None:
        """Acquire one job and append the corresponding audit event atomically."""

        def operation(session: Session) -> AutomationJobAcquisition | None:
            acquisition = self._job_repository_factory(session).resume_or_claim_next(
                now
            )
            if acquisition is None:
                return None
            source_ingestion = None
            if acquisition.job.glpi_ticket_id is None:
                source_ingestion = self._mail_ingestion_repository_factory(
                    session
                ).get_by_uuid(acquisition.job.uuid)
            self._append_event(
                session,
                acquisition.job,
                event_type=(
                    JobEventType.PROCESSING_RESUMED
                    if acquisition.resumed
                    else JobEventType.PROCESSING_STARTED
                ),
                occurred_at=now,
            )
            return AutomationJobAcquisition(
                job=acquisition.job,
                previous_status=acquisition.previous_status,
                resumed=acquisition.resumed,
                source_received_at=_source_received_at(
                    source_ingestion,
                    acquisition.job.uuid,
                ),
            )

        return self._transaction(operation)

    def record_transcription_submitted(
        self,
        job_uuid: UUID,
        provider_job_id: str,
        now: datetime,
    ) -> JobSnapshot:
        """Persist a provider identifier and its event exactly once."""

        def operation(session: Session) -> JobSnapshot:
            mutation = self._job_repository_factory(
                session
            ).record_transcription_submitted(job_uuid, provider_job_id, now)
            if mutation.changed:
                self._append_event(
                    session,
                    mutation.job,
                    event_type=JobEventType.TRANSCRIPTION_SUBMITTED,
                    occurred_at=now,
                )
            return mutation.job

        return self._transaction(operation)

    def record_transcription_succeeded(
        self,
        job_uuid: UUID,
        now: datetime,
    ) -> JobSnapshot:
        """Advance to GLPI creation and append its audit event atomically."""

        def operation(session: Session) -> JobSnapshot:
            snapshot = self._job_repository_factory(
                session
            ).advance_after_transcription(
                job_uuid,
                now,
            )
            self._append_event(
                session,
                snapshot,
                event_type=JobEventType.TRANSCRIPTION_SUCCEEDED,
                occurred_at=now,
            )
            return snapshot

        return self._transaction(operation)

    def record_transcription_failed(
        self,
        job_uuid: UUID,
        *,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> JobSnapshot:
        """Audit a terminal transcription failure and continue to ticketing."""

        def operation(session: Session) -> JobSnapshot:
            snapshot = self._job_repository_factory(
                session
            ).advance_after_transcription(
                job_uuid,
                now,
            )
            self._append_event(
                session,
                snapshot,
                event_type=JobEventType.TRANSCRIPTION_FAILED,
                occurred_at=now,
                error_scope=ErrorScope.JOB,
                error_category=error_category,
                error_code=error_code,
            )
            return snapshot

        return self._transaction(operation)

    def schedule_retry(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_scope: ErrorScope,
        error_category: str,
        error_code: str,
        next_attempt_at: datetime,
        now: datetime,
    ) -> JobSnapshot:
        """Schedule a retry and append its sanitized event atomically."""

        def operation(session: Session) -> JobSnapshot:
            snapshot = self._job_repository_factory(session).schedule_retry(
                job_uuid,
                stage=stage,
                error_scope=error_scope,
                error_category=error_category,
                error_code=error_code,
                next_attempt_at=next_attempt_at,
                now=now,
            )
            self._append_event(
                session,
                snapshot,
                event_type=JobEventType.RETRY_SCHEDULED,
                occurred_at=now,
                error_scope=error_scope,
                error_category=error_category,
                error_code=error_code,
            )
            return snapshot

        return self._transaction(operation)

    def record_ticket_confirmed(
        self,
        job_uuid: UUID,
        ticket_id: int,
        now: datetime,
    ) -> JobSnapshot:
        """Persist a GLPI ticket and append its audit event atomically."""

        def operation(session: Session) -> JobSnapshot:
            snapshot = self._job_repository_factory(session).record_ticket_confirmed(
                job_uuid, ticket_id, now
            )
            self._append_event(
                session,
                snapshot,
                event_type=JobEventType.TICKET_CONFIRMED,
                occurred_at=now,
            )
            return snapshot

        return self._transaction(operation)

    def mark_completed(
        self,
        job_uuid: UUID,
        *,
        hard_delete_at: datetime,
        next_check_at: datetime,
        now: datetime,
    ) -> JobSnapshot:
        """Complete a job, activate retention and append one event atomically."""

        def operation(session: Session) -> JobSnapshot:
            snapshot = self._job_repository_factory(session).mark_completed(
                job_uuid,
                now=now,
            )
            self._retention_repository_factory(session).activate(
                job_uuid,
                policy=RetentionPolicy.TICKET_OR_DEADLINE,
                hard_delete_at=hard_delete_at,
                next_check_at=next_check_at,
                now=now,
            )
            self._append_event(
                session,
                snapshot,
                event_type=JobEventType.JOB_COMPLETED,
                occurred_at=now,
            )
            return snapshot

        return self._transaction(operation)

    def mark_failed(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_category: str,
        error_code: str,
        hard_delete_at: datetime,
        now: datetime,
    ) -> JobSnapshot:
        """Fail a job, schedule deadline retention and append one event."""

        def operation(session: Session) -> JobSnapshot:
            snapshot = self._job_repository_factory(session).mark_failed(
                job_uuid,
                stage=stage,
                error_category=error_category,
                error_code=error_code,
                now=now,
            )
            self._retention_repository_factory(session).activate(
                job_uuid,
                policy=RetentionPolicy.DEADLINE_ONLY,
                hard_delete_at=hard_delete_at,
                next_check_at=None,
                now=now,
            )
            self._append_event(
                session,
                snapshot,
                event_type=JobEventType.JOB_FAILED,
                occurred_at=now,
                error_scope=ErrorScope.JOB,
                error_category=error_category,
                error_code=error_code,
            )
            return snapshot

        return self._transaction(operation)

    def mark_blocked(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_scope: ErrorScope,
        error_category: str,
        error_code: str,
        hard_delete_at: datetime,
        now: datetime,
    ) -> JobSnapshot:
        """Block a job, schedule deadline retention and append one event."""

        def operation(session: Session) -> JobSnapshot:
            snapshot = self._job_repository_factory(session).mark_blocked(
                job_uuid,
                stage=stage,
                error_scope=error_scope,
                error_category=error_category,
                error_code=error_code,
                now=now,
            )
            self._retention_repository_factory(session).activate(
                job_uuid,
                policy=RetentionPolicy.DEADLINE_ONLY,
                hard_delete_at=hard_delete_at,
                next_check_at=None,
                now=now,
            )
            self._append_event(
                session,
                snapshot,
                event_type=JobEventType.JOB_BLOCKED,
                occurred_at=now,
                error_scope=error_scope,
                error_category=error_category,
                error_code=error_code,
            )
            return snapshot

        return self._transaction(operation)

    def _append_event(
        self,
        session: Session,
        snapshot: JobSnapshot,
        *,
        event_type: JobEventType,
        occurred_at: datetime,
        error_scope: ErrorScope | None = None,
        error_category: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self._event_repository_factory(session).append(
            snapshot.uuid,
            occurred_at=occurred_at,
            status=snapshot.status,
            stage=snapshot.stage,
            event_type=event_type,
            actor=JobEventActor.WORKER_AUTOMATION,
            error_scope=error_scope,
            error_category=error_category,
            technical_code=error_code,
        )

    def _transaction(
        self,
        operation: Callable[[Session], _ResultT],
    ) -> _ResultT:
        self._ownership_guard()
        session = self._session_factory()
        try:
            with session.begin():
                result = operation(session)
        finally:
            session.close()
        self._ownership_guard()
        return result


def _no_op() -> None:
    return None


def _source_received_at(
    source_ingestion: MailIngestionSnapshot | None,
    job_uuid: UUID,
) -> datetime | None:
    """Return trusted UTC source time or ``None`` for an inconsistent link."""
    if source_ingestion is None:
        return None
    if source_ingestion.uuid != job_uuid:
        return None
    if source_ingestion.job_uuid != job_uuid:
        return None
    if source_ingestion.disposition is not MailIngestionDisposition.ACCEPTED:
        return None
    received_at = source_ingestion.received_at
    if not isinstance(received_at, datetime) or received_at.utcoffset() is None:
        return None
    return received_at.astimezone(UTC)


__all__ = ["AutomationJobAcquisition", "AutomationJobLifecycleService"]
