"""PostgreSQL contracts for the retention application lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from mevo2glpi_database import (
    AudioExtension,
    AudioRetention,
    AudioRetentionRepository,
    DeletionReason,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    PendingJob,
    RetentionCandidateKind,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
    create_session_factory,
)
from mevo2glpi_glpi import TicketStatus
from mevo2glpi_worker_retention.retention.lifecycle import (
    RetentionJobLifecycleService,
    StaleRetentionCandidateError,
)
from mevo2glpi_worker_retention.retention.outcomes import RetentionFailure
from mevo2glpi_worker_retention.retention.policies import RetentionCheckPolicy
from mevo2glpi_worker_retention.retention.ports import (
    AudioPurgeWork,
    CheckRecoveryWork,
    GlpiCheckWork,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
ACTIVATED_AT = NOW - timedelta(days=1)


def test_closed_ticket_deletion_is_durable_resumable_and_audited(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(101)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=NOW,
    )
    lifecycle = _lifecycle(session_factory)

    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    assert candidate.kind is RetentionCandidateKind.START_GLPI_CHECK
    assert not hasattr(candidate.retention, "_sa_instance_state")
    check_work = lifecycle.prepare_candidate(candidate, NOW)
    assert isinstance(check_work, GlpiCheckWork)

    purge_work = lifecycle.start_deletion_after_check(
        check_work,
        reason=DeletionReason.TICKET_CLOSED,
        ticket_status=TicketStatus.CLOSED,
        now=NOW + timedelta(seconds=1),
    )
    assert isinstance(purge_work, AudioPurgeWork)
    assert purge_work.reason is DeletionReason.TICKET_CLOSED
    assert not hasattr(purge_work, "_sa_instance_state")

    resumed_candidate = lifecycle.find_next_candidate(NOW + timedelta(seconds=2))
    assert resumed_candidate is not None
    assert resumed_candidate.kind is RetentionCandidateKind.RESUME_DELETION
    resumed_work = lifecycle.prepare_candidate(
        resumed_candidate,
        NOW + timedelta(seconds=2),
    )
    assert resumed_work == purge_work

    lifecycle.mark_deleted(resumed_work, now=NOW + timedelta(seconds=3))

    with session_factory() as session:
        job = session.get(TicketCreationJob, job_uuid)
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    assert retention is not None
    assert retention.status == RetentionStatus.DELETED.value
    assert retention.deletion_reason == DeletionReason.TICKET_CLOSED.value
    assert retention.last_glpi_status_id == int(TicketStatus.CLOSED)
    assert retention.audio_deleted_at == NOW + timedelta(seconds=3)
    assert [event.event_type for event in events] == [
        JobEventType.AUDIO_DELETION_STARTED.value,
        JobEventType.AUDIO_DELETED.value,
    ]
    assert all(event.actor == JobEventActor.WORKER_RETENTION.value for event in events)


@pytest.mark.parametrize(
    ("job_status", "expected_reason"),
    [
        (JobStatus.COMPLETED, DeletionReason.DEADLINE_EXPIRED),
        (JobStatus.FAILED, DeletionReason.FAILED_TTL),
        (JobStatus.BLOCKED, DeletionReason.BLOCKED_TTL),
    ],
)
def test_deadline_preparation_uses_the_terminal_job_reason(
    migrated_engine: Engine,
    job_status: JobStatus,
    expected_reason: DeletionReason,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(110 + list(JobStatus).index(job_status))
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=job_status,
        hard_delete_at=NOW,
        next_check_at=(
            NOW - timedelta(hours=1) if job_status is JobStatus.COMPLETED else None
        ),
    )
    lifecycle = _lifecycle(session_factory)

    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    assert candidate.kind is RetentionCandidateKind.START_DELETION_AT_DEADLINE
    work = lifecycle.prepare_candidate(candidate, NOW)

    assert isinstance(work, AudioPurgeWork)
    assert work.reason is expected_reason
    with session_factory() as session:
        job = session.get(TicketCreationJob, job_uuid)
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert job is not None
    assert job.status == job_status.value
    assert retention is not None
    assert retention.status == RetentionStatus.DELETING.value
    assert retention.deletion_reason == expected_reason.value
    assert [event.event_type for event in events] == [
        JobEventType.AUDIO_DELETION_STARTED.value
    ]


def test_deletion_retry_resumes_then_marks_deleted_with_atomic_events(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(120)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.FAILED,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    lifecycle = _lifecycle(session_factory)
    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    purge_work = lifecycle.prepare_candidate(candidate, NOW)
    assert isinstance(purge_work, AudioPurgeWork)
    retry_at = NOW + timedelta(minutes=5)
    failure = RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category="storage_unavailable",
        technical_code="unlink_failed",
    )

    lifecycle.schedule_retry(
        purge_work,
        failure=failure,
        next_attempt_at=retry_at,
        now=NOW + timedelta(seconds=1),
    )

    assert lifecycle.find_next_candidate(retry_at - timedelta(microseconds=1)) is None
    retry_candidate = lifecycle.find_next_candidate(retry_at)
    assert retry_candidate is not None
    assert retry_candidate.kind is RetentionCandidateKind.RESUME_DELETION_RETRY
    resumed_work = lifecycle.prepare_candidate(retry_candidate, retry_at)
    assert isinstance(resumed_work, AudioPurgeWork)
    assert resumed_work.consecutive_retry_count == 1
    lifecycle.mark_deleted(resumed_work, now=retry_at + timedelta(seconds=1))

    with session_factory() as session:
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert retention is not None
    assert retention.status == RetentionStatus.DELETED.value
    assert retention.consecutive_retry_count == 0
    assert retention.last_error_category is None
    assert retention.last_error_code is None
    assert [event.event_type for event in events] == [
        JobEventType.AUDIO_DELETION_STARTED.value,
        JobEventType.RETENTION_RETRY_SCHEDULED.value,
        JobEventType.AUDIO_DELETED.value,
    ]
    retry_event = events[1]
    assert retry_event.error_scope == ErrorScope.SYSTEM.value
    assert retry_event.error_category == "storage_unavailable"
    assert retry_event.technical_code == "unlink_failed"


def test_glpi_retry_persists_the_probe_and_its_event(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(130)
    hard_delete_at = NOW + timedelta(days=30)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=hard_delete_at,
        next_check_at=NOW,
    )
    lifecycle = _lifecycle(session_factory)
    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    work = lifecycle.prepare_candidate(candidate, NOW)
    assert isinstance(work, GlpiCheckWork)
    retry_at = NOW + timedelta(minutes=5)
    failure = RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category="glpi_unavailable",
        technical_code="http_503",
    )

    lifecycle.schedule_retry(
        work,
        failure=failure,
        next_attempt_at=retry_at,
        now=NOW + timedelta(seconds=1),
    )

    probe = lifecycle.get_glpi_probe_gate()
    assert probe is not None
    assert probe.job_uuid == job_uuid
    assert probe.next_check_at == retry_at
    assert not hasattr(probe, "_sa_instance_state")
    with session_factory() as session:
        events = _retention_events(session, job_uuid)
    assert [event.event_type for event in events] == [
        JobEventType.RETENTION_RETRY_SCHEDULED.value
    ]


def test_glpi_job_failure_blocks_only_retention_and_appends_event(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(140)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=NOW,
    )
    lifecycle = _lifecycle(session_factory)
    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    work = lifecycle.prepare_candidate(candidate, NOW)
    assert isinstance(work, GlpiCheckWork)
    failure = RetentionFailure(
        scope=ErrorScope.JOB,
        category="conflict",
        technical_code="external_id_mismatch",
    )

    lifecycle.mark_blocked(
        work,
        failure=failure,
        now=NOW + timedelta(seconds=1),
    )

    with session_factory() as session:
        job = session.get(TicketCreationJob, job_uuid)
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    assert retention is not None
    assert retention.status == RetentionStatus.BLOCKED.value
    assert retention.last_error_category == "conflict"
    assert retention.last_error_code == "external_id_mismatch"
    assert [event.event_type for event in events] == [
        JobEventType.RETENTION_BLOCKED.value
    ]
    assert events[0].error_scope == ErrorScope.JOB.value


def test_solved_ticket_is_rescheduled_without_an_audit_transition(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(150)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=NOW,
    )
    lifecycle = _lifecycle(session_factory)
    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None
    work = lifecycle.prepare_candidate(candidate, NOW)
    assert isinstance(work, GlpiCheckWork)
    next_check_at = NOW + timedelta(hours=6)

    lifecycle.reschedule_glpi_check(
        work,
        ticket_status=TicketStatus.SOLVED,
        next_check_at=next_check_at,
        now=NOW + timedelta(seconds=1),
    )

    with session_factory() as session:
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert retention is not None
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.last_glpi_status_id == int(TicketStatus.SOLVED)
    assert retention.next_check_at == next_check_at
    assert events == []


def test_interrupted_check_is_recovered_before_another_remote_call(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(160)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=NOW,
    )
    lifecycle = _lifecycle(session_factory)
    initial = lifecycle.find_next_candidate(NOW)
    assert initial is not None
    assert isinstance(lifecycle.prepare_candidate(initial, NOW), GlpiCheckWork)

    recovery_candidate = lifecycle.find_next_candidate(NOW + timedelta(seconds=1))
    assert recovery_candidate is not None
    assert recovery_candidate.kind is RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK
    recovery = lifecycle.prepare_candidate(
        recovery_candidate,
        NOW + timedelta(seconds=1),
    )

    assert recovery == CheckRecoveryWork(job_uuid=job_uuid)
    with session_factory() as session:
        retention = session.get(AudioRetention, job_uuid)
    assert retention is not None
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.next_check_at == NOW + timedelta(seconds=1)
    assert retention.check_attempt_count == 1


def test_event_failure_rolls_back_deletion_intent_and_event(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(170)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.FAILED,
        hard_delete_at=NOW,
        next_check_at=None,
    )
    lifecycle = _lifecycle(
        session_factory,
        event_repository_factory=_FailAfterAppendEventRepository,
    )
    candidate = lifecycle.find_next_candidate(NOW)
    assert candidate is not None

    with pytest.raises(_SyntheticEventFailure):
        lifecycle.prepare_candidate(candidate, NOW)

    with session_factory() as session:
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert retention is not None
    assert retention.status == RetentionStatus.SCHEDULED.value
    assert retention.deletion_reason is None
    assert retention.deletion_started_at is None
    assert events == []


def test_selected_candidate_becoming_checking_is_reported_as_stale(
    migrated_engine: Engine,
) -> None:
    session_factory = create_session_factory(migrated_engine)
    job_uuid = _uuid(180)
    _add_terminal_retention(
        session_factory,
        job_uuid,
        status=JobStatus.COMPLETED,
        hard_delete_at=NOW + timedelta(days=30),
        next_check_at=NOW,
    )
    lifecycle = _lifecycle(session_factory)
    selected = lifecycle.find_next_candidate(NOW)
    assert selected is not None
    with session_factory.begin() as session:
        AudioRetentionRepository(session).start_glpi_check(job_uuid, now=NOW)

    with pytest.raises(StaleRetentionCandidateError) as caught:
        lifecycle.prepare_candidate(selected, NOW + timedelta(seconds=1))

    assert caught.value.job_uuid == job_uuid
    with session_factory() as session:
        retention = session.get(AudioRetention, job_uuid)
        events = _retention_events(session, job_uuid)
    assert retention is not None
    assert retention.status == RetentionStatus.CHECKING.value
    assert retention.check_attempt_count == 1
    assert events == []


class _SyntheticEventFailure(RuntimeError):
    """Force rollback after both transition and event were flushed."""


class _FailAfterAppendEventRepository(TicketCreationJobEventRepository):
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
        super().append(
            job_uuid,
            occurred_at=occurred_at,
            status=status,
            stage=stage,
            event_type=event_type,
            actor=actor,
            error_scope=error_scope,
            error_category=error_category,
            technical_code=technical_code,
        )
        raise _SyntheticEventFailure


def _lifecycle(
    session_factory: sessionmaker[Session],
    *,
    event_repository_factory=TicketCreationJobEventRepository,
) -> RetentionJobLifecycleService:
    return RetentionJobLifecycleService(
        session_factory=session_factory,
        check_policy=RetentionCheckPolicy(check_interval=timedelta(hours=6)),
        event_repository_factory=event_repository_factory,
    )


def _add_terminal_retention(
    session_factory: sessionmaker[Session],
    job_uuid: UUID,
    *,
    status: JobStatus,
    hard_delete_at: datetime,
    next_check_at: datetime | None,
) -> None:
    with session_factory.begin() as session:
        job_repository = TicketCreationJobRepository(session)
        job_repository.add_pending(
            PendingJob(
                uuid=job_uuid,
                storage_key=f"{job_uuid}.wav",
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=128,
            )
        )
        acquisition = job_repository.resume_or_claim_next(ACTIVATED_AT)
        assert acquisition is not None
        if status is JobStatus.COMPLETED:
            provider_job_id = f"provider-{job_uuid}"
            job_repository.record_transcription_submitted(
                job_uuid,
                provider_job_id,
                ACTIVATED_AT,
            )
            job_repository.advance_after_transcription(
                job_uuid,
                ACTIVATED_AT,
            )
            job_repository.record_ticket_confirmed(
                job_uuid,
                _ticket_id(job_uuid),
                ACTIVATED_AT,
            )
            job_repository.mark_completed(job_uuid, now=ACTIVATED_AT)
            policy = RetentionPolicy.TICKET_OR_DEADLINE
        elif status is JobStatus.FAILED:
            job_repository.mark_failed(
                job_uuid,
                stage=JobStage.TRANSCRIPTION,
                error_category="terminal",
                error_code="remote_failure",
                now=ACTIVATED_AT,
            )
            policy = RetentionPolicy.DEADLINE_ONLY
        elif status is JobStatus.BLOCKED:
            job_repository.mark_blocked(
                job_uuid,
                stage=JobStage.TRANSCRIPTION,
                error_scope=ErrorScope.JOB,
                error_category="ambiguous_result",
                error_code="operation_unknown",
                now=ACTIVATED_AT,
            )
            policy = RetentionPolicy.DEADLINE_ONLY
        else:
            raise AssertionError("test helper requires a terminal job")

        AudioRetentionRepository(session).activate(
            job_uuid,
            policy=policy,
            hard_delete_at=hard_delete_at,
            next_check_at=next_check_at,
            now=ACTIVATED_AT,
        )


def _retention_events(
    session: Session,
    job_uuid: UUID,
) -> list[TicketCreationJobEvent]:
    return list(
        session.scalars(
            select(TicketCreationJobEvent)
            .where(TicketCreationJobEvent.job_uuid == job_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()
    )


def _uuid(suffix: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


def _ticket_id(job_uuid: UUID) -> int:
    return job_uuid.int % 1_000_000 + 1
