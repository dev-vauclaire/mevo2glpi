"""Short multi-model transactions for the retention application."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from sqlalchemy.orm import Session

from mevo2glpi_database import (
    AudioRetentionRepository,
    AudioRetentionSnapshot,
    DeletionReason,
    ErrorScope,
    InvalidRetentionTransitionError,
    JobEventActor,
    JobEventType,
    JobNotFoundError,
    JobSnapshot,
    JobStatus,
    MailIngestionDisposition,
    MailIngestionEventRepository,
    MailIngestionNotFoundError,
    MailIngestionRepository,
    MailIngestionStatus,
    RetentionCandidate,
    RetentionCandidateKind,
    RetentionNotFoundError,
    RetentionStatus,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
)
from mevo2glpi_glpi import TicketStatus

from .outcomes import RetentionFailure
from .policies import RetentionCheckPolicy
from .ports import (
    AudioPurgeWork,
    CheckRecoveryWork,
    DatabasePurgeWork,
    ExternalRetentionWork,
    GlpiCheckWork,
    PreparedRetentionWork,
)

_ResultT = TypeVar("_ResultT")


class StaleRetentionCandidateError(RuntimeError):
    """Report that another transaction invalidated a selected candidate."""

    def __init__(self, job_uuid: UUID) -> None:
        self.job_uuid = job_uuid
        super().__init__("retention candidate is no longer eligible")


class RetentionJobInvariantError(RuntimeError):
    """Report a persisted job/retention relationship that cannot be processed."""


class RetentionJobLifecycleService:
    """Coordinate job, retention and event repositories in short transactions."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        check_policy: RetentionCheckPolicy,
        ownership_guard: Callable[[], None] | None = None,
        job_repository_factory: Callable[
            [Session], TicketCreationJobRepository
        ] = TicketCreationJobRepository,
        retention_repository_factory: Callable[
            [Session], AudioRetentionRepository
        ] = AudioRetentionRepository,
        event_repository_factory: Callable[
            [Session], TicketCreationJobEventRepository
        ] = TicketCreationJobEventRepository,
        ingestion_repository_factory: Callable[
            [Session], MailIngestionRepository
        ] = MailIngestionRepository,
        ingestion_event_repository_factory: Callable[
            [Session], MailIngestionEventRepository
        ] = MailIngestionEventRepository,
    ) -> None:
        for value, field_name in (
            (session_factory, "session_factory"),
            (job_repository_factory, "job_repository_factory"),
            (retention_repository_factory, "retention_repository_factory"),
            (event_repository_factory, "event_repository_factory"),
            (ingestion_repository_factory, "ingestion_repository_factory"),
            (
                ingestion_event_repository_factory,
                "ingestion_event_repository_factory",
            ),
        ):
            if not callable(value):
                raise TypeError(f"{field_name} must be callable")
        if not isinstance(check_policy, RetentionCheckPolicy):
            raise TypeError("check_policy must be a RetentionCheckPolicy")
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")

        self._session_factory = session_factory
        self._check_policy = check_policy
        self._ownership_guard = ownership_guard or _no_op
        self._job_repository_factory = job_repository_factory
        self._retention_repository_factory = retention_repository_factory
        self._event_repository_factory = event_repository_factory
        self._ingestion_repository_factory = ingestion_repository_factory
        self._ingestion_event_repository_factory = ingestion_event_repository_factory

    def find_next_candidate(self, now: datetime) -> RetentionCandidate | None:
        """Read the next candidate without retaining a transaction or lock."""
        return self._read(
            lambda session: self._retention_repository_factory(
                session
            ).find_next_candidate(now)
        )

    def get_glpi_probe_gate(self) -> AudioRetentionSnapshot | None:
        """Return the persisted GLPI probe, including when it is not yet due."""
        return self._read(
            lambda session: self._retention_repository_factory(
                session
            ).get_glpi_probe_gate()
        )

    def prepare_candidate(
        self,
        candidate: RetentionCandidate,
        now: datetime,
    ) -> PreparedRetentionWork:
        """Lock job then retention and durably prepare one external operation."""
        if not isinstance(candidate, RetentionCandidate):
            raise TypeError("candidate must be a RetentionCandidate")
        job_uuid = candidate.retention.job_uuid

        def operation(session: Session) -> PreparedRetentionWork:
            job = self._job_repository_factory(session).lock_by_uuid(job_uuid)
            retention_repository = self._retention_repository_factory(session)

            if candidate.kind is RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK:
                self._require_completed_job(job)
                retention_repository.recover_interrupted_check(job_uuid, now=now)
                return CheckRecoveryWork(job_uuid=job_uuid)

            if candidate.kind is RetentionCandidateKind.START_GLPI_CHECK:
                self._require_completed_job(job)
                retention = retention_repository.start_glpi_check(job_uuid, now=now)
                if job.glpi_ticket_id is None or retention.hard_delete_at is None:
                    raise RetentionJobInvariantError(
                        "GLPI retention work has incomplete persisted identifiers"
                    )
                return GlpiCheckWork(
                    job_uuid=job_uuid,
                    storage_key=job.storage_key,
                    ticket_id=job.glpi_ticket_id,
                    hard_delete_at=retention.hard_delete_at,
                    consecutive_retry_count=retention.consecutive_retry_count,
                )

            if candidate.kind is RetentionCandidateKind.START_DELETION_AT_DEADLINE:
                self._require_terminal_job(job)
                reason = self._check_policy.deadline_reason(job.status)
                retention = retention_repository.start_deletion(
                    job_uuid,
                    reason=reason,
                    now=now,
                )
                self._append_event(
                    session,
                    job,
                    event_type=JobEventType.AUDIO_DELETION_STARTED,
                    occurred_at=now,
                )
                return self._purge_work(job, retention)

            if candidate.kind in {
                RetentionCandidateKind.RESUME_DELETION,
                RetentionCandidateKind.RESUME_DELETION_RETRY,
            }:
                self._require_terminal_job(job)
                retention = retention_repository.resume_deletion(job_uuid, now=now)
                return self._purge_work(job, retention)

            if candidate.kind is RetentionCandidateKind.PURGE_DELETED_AGGREGATE:
                self._require_terminal_job(job)
                retention = retention_repository.get_by_job_uuid(job_uuid)
                if retention is None or retention.status is not RetentionStatus.DELETED:
                    raise StaleRetentionCandidateError(job_uuid)
                if retention.deletion_reason is None:
                    raise RetentionJobInvariantError("deletion context has no reason")
                return DatabasePurgeWork(
                    job_uuid=job.uuid,
                    reason=retention.deletion_reason,
                )

            raise AssertionError("unsupported retention candidate kind")

        return self._stale_aware_transaction(job_uuid, operation)

    def reschedule_glpi_check(
        self,
        work: GlpiCheckWork,
        *,
        ticket_status: TicketStatus,
        next_check_at: datetime,
        now: datetime,
    ) -> None:
        """Persist a successful non-closed ticket check."""
        _require_glpi_work(work)
        if not isinstance(ticket_status, TicketStatus):
            raise TypeError("ticket_status must be a TicketStatus")

        def operation(session: Session) -> None:
            job = self._job_repository_factory(session).lock_by_uuid(work.job_uuid)
            self._require_completed_job(job)
            self._retention_repository_factory(session).reschedule_after_glpi_check(
                work.job_uuid,
                glpi_status_id=int(ticket_status),
                next_check_at=next_check_at,
                now=now,
            )

        self._stale_aware_transaction(work.job_uuid, operation)

    def start_deletion_after_check(
        self,
        work: GlpiCheckWork,
        *,
        reason: DeletionReason,
        now: datetime,
        ticket_status: TicketStatus | None = None,
    ) -> AudioPurgeWork:
        """Persist deletion intent after a GLPI result or elapsed deadline."""
        _require_glpi_work(work)
        if not isinstance(reason, DeletionReason):
            raise TypeError("reason must be a DeletionReason")
        if ticket_status is not None and not isinstance(ticket_status, TicketStatus):
            raise TypeError("ticket_status must be a TicketStatus or None")
        if reason is DeletionReason.TICKET_CLOSED:
            if ticket_status is not TicketStatus.CLOSED:
                raise ValueError("ticket_closed deletion requires CLOSED status")
            status_id: int | None = int(ticket_status)
        elif reason is DeletionReason.DEADLINE_EXPIRED:
            status_id = int(ticket_status) if ticket_status is not None else None
        else:
            if ticket_status is not None:
                raise ValueError("deadline-only deletion does not accept ticket_status")
            status_id = None

        def operation(session: Session) -> AudioPurgeWork:
            job = self._job_repository_factory(session).lock_by_uuid(work.job_uuid)
            self._require_completed_job(job)
            retention = self._retention_repository_factory(session).start_deletion(
                work.job_uuid,
                reason=reason,
                now=now,
                last_glpi_status_id=status_id,
            )
            self._append_event(
                session,
                job,
                event_type=JobEventType.AUDIO_DELETION_STARTED,
                occurred_at=now,
            )
            return self._purge_work(job, retention)

        return self._stale_aware_transaction(work.job_uuid, operation)

    def schedule_retry(
        self,
        work: ExternalRetentionWork,
        *,
        failure: RetentionFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        """Persist a retry and its sanitized audit event atomically."""
        _require_external_work(work)
        _require_failure(failure)

        def operation(session: Session) -> None:
            job = self._job_repository_factory(session).lock_by_uuid(work.job_uuid)
            self._require_terminal_job(job)
            self._retention_repository_factory(session).schedule_retry(
                work.job_uuid,
                error_category=failure.category,
                error_code=failure.technical_code,
                next_check_at=next_attempt_at,
                now=now,
            )
            self._append_event(
                session,
                job,
                event_type=JobEventType.RETENTION_RETRY_SCHEDULED,
                occurred_at=now,
                failure=failure,
            )

        self._stale_aware_transaction(work.job_uuid, operation)

    def mark_blocked(
        self,
        work: ExternalRetentionWork,
        *,
        failure: RetentionFailure,
        now: datetime,
    ) -> None:
        """Persist a job-scoped retention ambiguity and its event."""
        _require_external_work(work)
        _require_failure(failure)
        if failure.scope is not ErrorScope.JOB:
            raise ValueError("a blocked retention failure must have job scope")

        def operation(session: Session) -> None:
            job = self._job_repository_factory(session).lock_by_uuid(work.job_uuid)
            self._require_terminal_job(job)
            self._retention_repository_factory(session).mark_blocked(
                work.job_uuid,
                error_category=failure.category,
                error_code=failure.technical_code,
                now=now,
            )
            self._append_event(
                session,
                job,
                event_type=JobEventType.RETENTION_BLOCKED,
                occurred_at=now,
                failure=failure,
            )

        self._stale_aware_transaction(work.job_uuid, operation)

    def purge_database_aggregate(
        self,
        work: AudioPurgeWork | DatabasePurgeWork,
    ) -> None:
        """Delete every persisted row owned by a successfully purged audio."""
        _require_purge_work(work)

        def operation(session: Session) -> None:
            job_repository = self._job_repository_factory(session)
            job = job_repository.lock_by_uuid(work.job_uuid)
            self._require_terminal_job(job)
            retention_repository = self._retention_repository_factory(session)
            retention = retention_repository.get_by_job_uuid(work.job_uuid)
            if retention is None or retention.status not in {
                RetentionStatus.DELETING,
                RetentionStatus.DELETED,
            }:
                raise StaleRetentionCandidateError(work.job_uuid)
            if retention.deletion_reason is not work.reason:
                raise RetentionJobInvariantError(
                    "audio purge reason does not match persisted deletion intent"
                )

            ingestion_repository = self._ingestion_repository_factory(session)
            ingestion = ingestion_repository.get_by_job_uuid(work.job_uuid)
            if ingestion is not None:
                if (
                    ingestion.job_uuid != work.job_uuid
                    or ingestion.status is not MailIngestionStatus.COMPLETED
                    or ingestion.disposition is not MailIngestionDisposition.ACCEPTED
                ):
                    raise RetentionJobInvariantError(
                        "linked ingestion is not a completed acceptance"
                    )
                self._ingestion_event_repository_factory(
                    session
                ).delete_by_ingestion_uuid(ingestion.uuid)
                ingestion_repository.delete_by_uuid(ingestion.uuid)

            self._event_repository_factory(session).delete_by_job_uuid(work.job_uuid)
            retention_repository.delete_by_job_uuid(work.job_uuid)
            job_repository.delete_by_uuid(work.job_uuid)

        self._stale_aware_transaction(work.job_uuid, operation)

    def _append_event(
        self,
        session: Session,
        job: JobSnapshot,
        *,
        event_type: JobEventType,
        occurred_at: datetime,
        failure: RetentionFailure | None = None,
    ) -> None:
        self._event_repository_factory(session).append(
            job.uuid,
            occurred_at=occurred_at,
            status=job.status,
            stage=job.stage,
            event_type=event_type,
            actor=JobEventActor.WORKER_RETENTION,
            error_scope=failure.scope if failure is not None else None,
            error_category=failure.category if failure is not None else None,
            technical_code=failure.technical_code if failure is not None else None,
        )

    @staticmethod
    def _purge_work(
        job: JobSnapshot,
        retention: AudioRetentionSnapshot,
    ) -> AudioPurgeWork:
        if retention.deletion_reason is None:
            raise RetentionJobInvariantError("deletion context has no reason")
        return AudioPurgeWork(
            job_uuid=job.uuid,
            storage_key=job.storage_key,
            reason=retention.deletion_reason,
            consecutive_retry_count=retention.consecutive_retry_count,
        )

    @staticmethod
    def _require_terminal_job(job: JobSnapshot) -> None:
        if job.status not in {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.BLOCKED,
        }:
            raise RetentionJobInvariantError("active retention requires a terminal job")

    @classmethod
    def _require_completed_job(cls, job: JobSnapshot) -> None:
        cls._require_terminal_job(job)
        if job.status is not JobStatus.COMPLETED or job.glpi_ticket_id is None:
            raise RetentionJobInvariantError(
                "GLPI retention checks require a completed ticketed job"
            )

    def _read(self, operation: Callable[[Session], _ResultT]) -> _ResultT:
        self._ownership_guard()
        session = self._session_factory()
        try:
            result = operation(session)
        finally:
            session.close()
        self._ownership_guard()
        return result

    def _stale_aware_transaction(
        self,
        job_uuid: UUID,
        operation: Callable[[Session], _ResultT],
    ) -> _ResultT:
        self._ownership_guard()
        session = self._session_factory()
        try:
            try:
                with session.begin():
                    result = operation(session)
            except (
                InvalidRetentionTransitionError,
                JobNotFoundError,
                MailIngestionNotFoundError,
                RetentionNotFoundError,
            ) as error:
                raise StaleRetentionCandidateError(job_uuid) from error
        finally:
            session.close()
        self._ownership_guard()
        return result


class RejectedIngestionRetentionLifecycleService:
    """Delete terminal rejected-ingestion aggregates in short transactions."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        ownership_guard: Callable[[], None] | None = None,
        ingestion_repository_factory: Callable[
            [Session], MailIngestionRepository
        ] = MailIngestionRepository,
        event_repository_factory: Callable[
            [Session], MailIngestionEventRepository
        ] = MailIngestionEventRepository,
    ) -> None:
        for value, field_name in (
            (session_factory, "session_factory"),
            (ingestion_repository_factory, "ingestion_repository_factory"),
            (event_repository_factory, "event_repository_factory"),
        ):
            if not callable(value):
                raise TypeError(f"{field_name} must be callable")
        if ownership_guard is not None and not callable(ownership_guard):
            raise TypeError("ownership_guard must be callable")

        self._session_factory = session_factory
        self._ownership_guard = ownership_guard or _no_op
        self._ingestion_repository_factory = ingestion_repository_factory
        self._event_repository_factory = event_repository_factory

    def count_expired(self, cutoff: datetime) -> int:
        """Count eligible rows for report mode without mutating PostgreSQL."""
        _require_aware_datetime(cutoff, "cutoff")
        self._ownership_guard()
        session = self._session_factory()
        try:
            result = self._ingestion_repository_factory(session).count_expired_rejected(
                cutoff
            )
        finally:
            session.close()
        self._ownership_guard()
        return result

    def purge_expired(self, cutoff: datetime, *, limit: int) -> int:
        """Delete one locked and bounded batch, including its audit events."""
        _require_aware_datetime(cutoff, "cutoff")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit must be an integer")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        self._ownership_guard()
        session = self._session_factory()
        try:
            with session.begin():
                ingestion_repository = self._ingestion_repository_factory(session)
                event_repository = self._event_repository_factory(session)
                candidates = ingestion_repository.list_expired_rejected_for_update(
                    cutoff,
                    limit,
                )
                for candidate in candidates:
                    event_repository.delete_by_ingestion_uuid(candidate.uuid)
                    ingestion_repository.delete_by_uuid(candidate.uuid)
                purged_count = len(candidates)
        finally:
            session.close()
        self._ownership_guard()
        return purged_count


def _require_external_work(value: object) -> None:
    if not isinstance(value, GlpiCheckWork | AudioPurgeWork):
        raise TypeError("work must be a GlpiCheckWork or AudioPurgeWork")


def _require_glpi_work(value: object) -> None:
    if not isinstance(value, GlpiCheckWork):
        raise TypeError("work must be a GlpiCheckWork")


def _require_purge_work(value: object) -> None:
    if not isinstance(value, AudioPurgeWork | DatabasePurgeWork):
        raise TypeError("work must be an AudioPurgeWork or DatabasePurgeWork")


def _require_failure(value: object) -> None:
    if not isinstance(value, RetentionFailure):
        raise TypeError("failure must be a RetentionFailure")


def _no_op() -> None:
    return None


def _require_aware_datetime(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


__all__ = [
    "RetentionJobInvariantError",
    "RetentionJobLifecycleService",
    "RejectedIngestionRetentionLifecycleService",
    "StaleRetentionCandidateError",
]
