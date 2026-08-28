"""Repositories for the persistent audio-to-ticket lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from .models import (
    AudioExtension,
    ErrorScope,
    JobEventActor,
    JobStage,
    JobStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
)

_JOB_CREATED_EVENT_TYPE = "job_created"


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingJob:
    """Validated values needed to persist a newly published audio."""

    uuid: UUID
    storage_key: str
    audio_extension: AudioExtension
    audio_size_bytes: int


@dataclass(frozen=True, slots=True, kw_only=True)
class JobSnapshot:
    """Immutable, typed view of one persisted lifecycle job."""

    uuid: UUID
    status: JobStatus
    stage: JobStage | None
    storage_key: str
    audio_extension: AudioExtension
    audio_size_bytes: int
    glpi_ticket_id: int | None
    transcription_provider_job_id: str | None
    attempt_count: int
    consecutive_retry_count: int
    next_attempt_at: datetime | None
    last_error_scope: ErrorScope | None
    last_error_category: str | None
    last_error_code: str | None
    created_at: datetime
    first_started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None


class JobRepository:
    """Persist and read lifecycle jobs using a caller-owned session.

    Transaction ownership deliberately remains with the application. Repository
    methods may flush SQL statements, but never commit, roll back or close the
    injected session.
    """

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def add_pending(self, pending_job: PendingJob) -> JobSnapshot:
        """Add a pending job and its initial append-only event, then flush them."""

        _validate_pending_job(pending_job)
        job = TicketCreationJob(
            uuid=pending_job.uuid,
            status=JobStatus.PENDING.value,
            stage=None,
            storage_key=pending_job.storage_key,
            audio_extension=pending_job.audio_extension.value,
            audio_size_bytes=pending_job.audio_size_bytes,
        )
        event = TicketCreationJobEvent(
            job_uuid=pending_job.uuid,
            status=JobStatus.PENDING.value,
            stage=None,
            event_type=_JOB_CREATED_EVENT_TYPE,
            actor=JobEventActor.WEB.value,
        )

        self._session.add_all((job, event))
        self._session.flush()
        return _to_snapshot(job)

    def get_by_uuid(self, job_uuid: UUID) -> JobSnapshot | None:
        """Return a detached typed view found through the primary key."""

        if not isinstance(job_uuid, UUID):
            raise TypeError("job_uuid must be a UUID")
        job = self._session.get(TicketCreationJob, job_uuid)
        if job is None:
            return None
        return _to_snapshot(job)


def _validate_pending_job(pending_job: PendingJob) -> None:
    if not isinstance(pending_job, PendingJob):
        raise TypeError("pending_job must be a PendingJob")
    if not isinstance(pending_job.uuid, UUID):
        raise TypeError("pending_job.uuid must be a UUID")
    if not isinstance(pending_job.audio_extension, AudioExtension):
        raise TypeError("pending_job.audio_extension must be an AudioExtension")
    if isinstance(pending_job.audio_size_bytes, bool) or not isinstance(
        pending_job.audio_size_bytes, int
    ):
        raise TypeError("pending_job.audio_size_bytes must be an integer")
    if pending_job.audio_size_bytes <= 0:
        raise ValueError("pending_job.audio_size_bytes must be positive")

    expected_storage_key = f"{pending_job.uuid}.{pending_job.audio_extension.value}"
    if pending_job.storage_key != expected_storage_key:
        raise ValueError("pending_job.storage_key is not canonical")


def _to_snapshot(job: TicketCreationJob) -> JobSnapshot:
    return JobSnapshot(
        uuid=job.uuid,
        status=JobStatus(job.status),
        stage=JobStage(job.stage) if job.stage is not None else None,
        storage_key=job.storage_key,
        audio_extension=AudioExtension(job.audio_extension),
        audio_size_bytes=job.audio_size_bytes,
        glpi_ticket_id=job.glpi_ticket_id,
        transcription_provider_job_id=job.transcription_provider_job_id,
        attempt_count=job.attempt_count,
        consecutive_retry_count=job.consecutive_retry_count,
        next_attempt_at=job.next_attempt_at,
        last_error_scope=(
            ErrorScope(job.last_error_scope)
            if job.last_error_scope is not None
            else None
        ),
        last_error_category=job.last_error_category,
        last_error_code=job.last_error_code,
        created_at=job.created_at,
        first_started_at=job.first_started_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
    )


__all__ = ["JobRepository", "JobSnapshot", "PendingJob"]
