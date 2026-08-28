"""Repository dedicated to the ``TicketCreationJob`` model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..models import (
    AudioExtension,
    ErrorScope,
    JobStage,
    JobStatus,
    TicketCreationJob,
)
from ._validation import (
    validate_aware_datetime,
    validate_page_limit,
    validate_positive_integer,
    validate_safe_identifier,
    validate_uuid,
)


class JobNotFoundError(LookupError):
    """Report a transition requested for an unknown job UUID."""


class InvalidJobTransitionError(RuntimeError):
    """Report a transition incompatible with the persisted lifecycle state."""


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


@dataclass(frozen=True, slots=True, kw_only=True)
class JobCursor:
    """Stable keyset cursor for the dashboard job ordering."""

    updated_at: datetime
    uuid: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class JobPage:
    """One bounded page of jobs and the cursor for the following page."""

    items: tuple[JobSnapshot, ...]
    next_cursor: JobCursor | None
    has_more: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class JobAcquisition:
    """Describe one deterministic worker claim or crash recovery."""

    job: JobSnapshot
    previous_status: JobStatus
    resumed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class JobMutation:
    """Describe a job mutation and whether persisted state actually changed."""

    job: JobSnapshot
    changed: bool


class TicketCreationJobRepository:
    """Read and mutate only jobs using a caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("session must be a SQLAlchemy Session")
        self._session = session

    def add_pending(self, pending_job: PendingJob) -> JobSnapshot:
        """Add one pending job without creating related records."""
        _validate_pending_job(pending_job)
        job = TicketCreationJob(
            uuid=pending_job.uuid,
            status=JobStatus.PENDING.value,
            stage=None,
            storage_key=pending_job.storage_key,
            audio_extension=pending_job.audio_extension.value,
            audio_size_bytes=pending_job.audio_size_bytes,
        )
        self._session.add(job)
        self._session.flush()
        return _to_snapshot(job)

    def get_by_uuid(self, job_uuid: UUID) -> JobSnapshot | None:
        """Return a detached typed view found through the primary key."""
        validate_uuid(job_uuid, "job_uuid")
        job = self._session.get(TicketCreationJob, job_uuid)
        return _to_snapshot(job) if job is not None else None

    def list_page(
        self,
        *,
        limit: int,
        status: JobStatus | None = None,
        job_uuid: UUID | None = None,
        cursor: JobCursor | None = None,
    ) -> JobPage:
        """List jobs with deterministic descending keyset pagination."""
        limit = validate_page_limit(limit)
        if status is not None and not isinstance(status, JobStatus):
            raise TypeError("status must be a JobStatus or None")
        if job_uuid is not None:
            validate_uuid(job_uuid, "job_uuid")
        if cursor is not None:
            if not isinstance(cursor, JobCursor):
                raise TypeError("cursor must be a JobCursor or None")
            validate_aware_datetime(cursor.updated_at, "cursor.updated_at")
            validate_uuid(cursor.uuid, "cursor.uuid")

        statement = select(TicketCreationJob)
        if status is not None:
            statement = statement.where(TicketCreationJob.status == status.value)
        if job_uuid is not None:
            statement = statement.where(TicketCreationJob.uuid == job_uuid)
        if cursor is not None:
            statement = statement.where(
                or_(
                    TicketCreationJob.updated_at < cursor.updated_at,
                    and_(
                        TicketCreationJob.updated_at == cursor.updated_at,
                        TicketCreationJob.uuid < cursor.uuid,
                    ),
                )
            )
        statement = statement.order_by(
            TicketCreationJob.updated_at.desc(),
            TicketCreationJob.uuid.desc(),
        ).limit(limit + 1)

        jobs = tuple(self._session.scalars(statement).all())
        has_more = len(jobs) > limit
        items = tuple(_to_snapshot(job) for job in jobs[:limit])
        next_cursor = None
        if has_more:
            last_item = items[-1]
            next_cursor = JobCursor(
                updated_at=last_item.updated_at,
                uuid=last_item.uuid,
            )
        return JobPage(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def rearm_for_operator_retry(
        self,
        job_uuid: UUID,
        *,
        now: datetime,
    ) -> JobSnapshot:
        """Return a failed or blocked job to pending for an operator retry."""
        validate_uuid(job_uuid, "job_uuid")
        now = validate_aware_datetime(now, "now")
        job = self._session.get(
            TicketCreationJob,
            job_uuid,
            with_for_update=True,
        )
        if job is None:
            raise JobNotFoundError(f"job {job_uuid} does not exist")
        if job.status not in (JobStatus.FAILED.value, JobStatus.BLOCKED.value):
            raise InvalidJobTransitionError(f"job {job_uuid} is not failed or blocked")

        job.status = JobStatus.PENDING.value
        job.stage = None
        job.transcription_provider_job_id = None
        job.consecutive_retry_count = 0
        job.next_attempt_at = None
        _clear_job_error(job)
        job.updated_at = now
        job.finished_at = None
        self._session.flush()
        return _to_snapshot(job)

    def resume_or_claim_next(self, now: datetime) -> JobAcquisition | None:
        """Resume processing, otherwise claim a due retry then a pending job."""
        now = validate_aware_datetime(now, "now")
        job = self._select_processing()
        if job is not None:
            previous_status = JobStatus.PROCESSING
            resumed = True
        else:
            job = self._select_due_retry(now)
            if job is None:
                if self._has_scheduled_system_retry():
                    return None
                job = self._select_pending()
            if job is None:
                return None
            previous_status = JobStatus(job.status)
            resumed = False

        job.status = JobStatus.PROCESSING.value
        job.stage = (
            JobStage.FINALIZATION.value
            if job.glpi_ticket_id is not None
            else JobStage.TRANSCRIPTION.value
        )
        if job.glpi_ticket_id is None:
            job.transcription_provider_job_id = None
        job.attempt_count += 1
        if job.first_started_at is None:
            job.first_started_at = now
        job.next_attempt_at = None
        _clear_job_error(job)
        job.finished_at = None
        job.updated_at = now
        self._session.flush()
        return JobAcquisition(
            job=_to_snapshot(job),
            previous_status=previous_status,
            resumed=resumed,
        )

    def record_transcription_submitted(
        self,
        job_uuid: UUID,
        provider_job_id: str,
        now: datetime,
    ) -> JobMutation:
        """Persist a remote transcription identifier before polling it."""
        provider_job_id = _validate_provider_job_id(provider_job_id)
        now = validate_aware_datetime(now, "now")
        job = self._lock_processing_job(job_uuid, JobStage.TRANSCRIPTION)
        if job.transcription_provider_job_id is not None:
            if job.transcription_provider_job_id == provider_job_id:
                return JobMutation(job=_to_snapshot(job), changed=False)
            raise InvalidJobTransitionError(
                "a different transcription provider identifier is already persisted"
            )
        job.transcription_provider_job_id = provider_job_id
        job.updated_at = now
        self._session.flush()
        return JobMutation(job=_to_snapshot(job), changed=True)

    def record_transcription_succeeded(
        self,
        job_uuid: UUID,
        provider_job_id: str,
        now: datetime,
    ) -> JobSnapshot:
        """Advance a processing job without persisting transcription content."""
        provider_job_id = _validate_provider_job_id(provider_job_id)
        now = validate_aware_datetime(now, "now")
        job = self._lock_processing_job(job_uuid, JobStage.TRANSCRIPTION)
        if job.transcription_provider_job_id != provider_job_id:
            raise InvalidJobTransitionError(
                "transcription success must match the persisted provider identifier"
            )
        job.stage = JobStage.GLPI_CREATION.value
        job.updated_at = now
        self._session.flush()
        return _to_snapshot(job)

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
        """Release a processing job into a persisted retry schedule."""
        stage = _validate_stage(stage)
        error_scope = _validate_error_scope(error_scope)
        error_category = validate_safe_identifier(error_category, "error_category")
        error_code = validate_safe_identifier(error_code, "error_code")
        now = validate_aware_datetime(now, "now")
        next_attempt_at = validate_aware_datetime(next_attempt_at, "next_attempt_at")
        if next_attempt_at <= now:
            raise ValueError("next_attempt_at must be later than now")

        job = self._lock_processing_job(job_uuid, stage)
        job.status = JobStatus.RETRY_SCHEDULED.value
        job.next_attempt_at = next_attempt_at
        job.consecutive_retry_count += 1
        job.last_error_scope = error_scope.value
        job.last_error_category = error_category
        job.last_error_code = error_code
        job.updated_at = now
        self._session.flush()
        return _to_snapshot(job)

    def record_ticket_confirmed(
        self,
        job_uuid: UUID,
        ticket_id: int,
        now: datetime,
    ) -> JobSnapshot:
        """Persist the GLPI idempotency boundary before final completion."""
        ticket_id = validate_positive_integer(ticket_id, "ticket_id")
        now = validate_aware_datetime(now, "now")
        job = self._lock_processing_job(job_uuid, JobStage.GLPI_CREATION)
        job.glpi_ticket_id = ticket_id
        job.stage = JobStage.FINALIZATION.value
        job.updated_at = now
        self._session.flush()
        return _to_snapshot(job)

    def mark_completed(self, job_uuid: UUID, *, now: datetime) -> JobSnapshot:
        """Mark a ticketed job completed without touching retention or events."""
        now = validate_aware_datetime(now, "now")
        job = self._lock_processing_job(job_uuid, JobStage.FINALIZATION)
        if job.glpi_ticket_id is None:
            raise InvalidJobTransitionError(
                "completion requires a GLPI ticket identifier"
            )
        _apply_terminal_job_state(job, status=JobStatus.COMPLETED, now=now)
        self._session.flush()
        return _to_snapshot(job)

    def mark_failed(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> JobSnapshot:
        """Persist an explicitly terminal, job-specific failure."""
        stage = _validate_stage(stage)
        error_category = validate_safe_identifier(error_category, "error_category")
        error_code = validate_safe_identifier(error_code, "error_code")
        now = validate_aware_datetime(now, "now")
        job = self._lock_processing_job(job_uuid, stage)
        _apply_terminal_job_state(
            job,
            status=JobStatus.FAILED,
            now=now,
            error_scope=ErrorScope.JOB,
            error_category=error_category,
            error_code=error_code,
        )
        self._session.flush()
        return _to_snapshot(job)

    def mark_blocked(
        self,
        job_uuid: UUID,
        *,
        stage: JobStage,
        error_scope: ErrorScope,
        error_category: str,
        error_code: str,
        now: datetime,
    ) -> JobSnapshot:
        """Persist a job whose safe automatic continuation is ambiguous."""
        stage = _validate_stage(stage)
        error_scope = _validate_error_scope(error_scope)
        error_category = validate_safe_identifier(error_category, "error_category")
        error_code = validate_safe_identifier(error_code, "error_code")
        now = validate_aware_datetime(now, "now")
        job = self._lock_processing_job(job_uuid, stage)
        _apply_terminal_job_state(
            job,
            status=JobStatus.BLOCKED,
            now=now,
            error_scope=error_scope,
            error_category=error_category,
            error_code=error_code,
        )
        self._session.flush()
        return _to_snapshot(job)

    def _select_processing(self) -> TicketCreationJob | None:
        statement = (
            select(TicketCreationJob)
            .where(TicketCreationJob.status == JobStatus.PROCESSING.value)
            .limit(1)
            .with_for_update()
        )
        return self._session.scalars(statement).first()

    def _select_due_retry(self, now: datetime) -> TicketCreationJob | None:
        statement = (
            select(TicketCreationJob)
            .where(
                TicketCreationJob.status == JobStatus.RETRY_SCHEDULED.value,
                TicketCreationJob.next_attempt_at <= now,
            )
            .order_by(
                TicketCreationJob.next_attempt_at,
                TicketCreationJob.created_at,
                TicketCreationJob.uuid,
            )
            .limit(1)
            .with_for_update()
        )
        return self._session.scalars(statement).first()

    def _select_pending(self) -> TicketCreationJob | None:
        statement = (
            select(TicketCreationJob)
            .where(TicketCreationJob.status == JobStatus.PENDING.value)
            .order_by(TicketCreationJob.created_at, TicketCreationJob.uuid)
            .limit(1)
            .with_for_update()
        )
        return self._session.scalars(statement).first()

    def _has_scheduled_system_retry(self) -> bool:
        statement = (
            select(TicketCreationJob.uuid)
            .where(
                TicketCreationJob.status == JobStatus.RETRY_SCHEDULED.value,
                TicketCreationJob.last_error_scope == ErrorScope.SYSTEM.value,
            )
            .limit(1)
        )
        return self._session.scalar(statement) is not None

    def _lock_processing_job(
        self,
        job_uuid: UUID,
        expected_stage: JobStage,
    ) -> TicketCreationJob:
        validate_uuid(job_uuid, "job_uuid")
        job = self._session.get(
            TicketCreationJob,
            job_uuid,
            with_for_update=True,
        )
        if job is None:
            raise JobNotFoundError(f"job {job_uuid} does not exist")
        if (
            job.status != JobStatus.PROCESSING.value
            or job.stage != expected_stage.value
        ):
            raise InvalidJobTransitionError(
                f"job {job_uuid} is not processing at stage {expected_stage.value}"
            )
        return job


def _clear_job_error(job: TicketCreationJob) -> None:
    job.last_error_scope = None
    job.last_error_category = None
    job.last_error_code = None


def _apply_terminal_job_state(
    job: TicketCreationJob,
    *,
    status: JobStatus,
    now: datetime,
    error_scope: ErrorScope | None = None,
    error_category: str | None = None,
    error_code: str | None = None,
) -> None:
    job.status = status.value
    job.next_attempt_at = None
    job.last_error_scope = error_scope.value if error_scope is not None else None
    job.last_error_category = error_category
    job.last_error_code = error_code
    job.finished_at = now
    job.updated_at = now


def _validate_pending_job(pending_job: PendingJob) -> None:
    if not isinstance(pending_job, PendingJob):
        raise TypeError("pending_job must be a PendingJob")
    validate_uuid(pending_job.uuid, "pending_job.uuid")
    if not isinstance(pending_job.audio_extension, AudioExtension):
        raise TypeError("pending_job.audio_extension must be an AudioExtension")
    validate_positive_integer(pending_job.audio_size_bytes, "audio_size_bytes")
    expected_storage_key = f"{pending_job.uuid}.{pending_job.audio_extension.value}"
    if pending_job.storage_key != expected_storage_key:
        raise ValueError("pending_job.storage_key is not canonical")


def _validate_stage(stage: JobStage) -> JobStage:
    if not isinstance(stage, JobStage):
        raise TypeError("stage must be a JobStage")
    return stage


def _validate_error_scope(error_scope: ErrorScope) -> ErrorScope:
    if not isinstance(error_scope, ErrorScope):
        raise TypeError("error_scope must be an ErrorScope")
    return error_scope


def _validate_provider_job_id(provider_job_id: str) -> str:
    if not isinstance(provider_job_id, str):
        raise TypeError("provider_job_id must be a string")
    if (
        not provider_job_id
        or provider_job_id != provider_job_id.strip()
        or len(provider_job_id) > 255
        or any(not character.isprintable() for character in provider_job_id)
    ):
        raise ValueError("provider_job_id must be a printable identifier")
    return provider_job_id


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


__all__ = [
    "InvalidJobTransitionError",
    "JobAcquisition",
    "JobCursor",
    "JobMutation",
    "JobNotFoundError",
    "JobPage",
    "JobSnapshot",
    "PendingJob",
    "TicketCreationJobRepository",
]
