"""Unit contracts for the retention application's transactional lifecycle."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, create_autospec
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from mevo2glpi_database import (
    AudioExtension,
    AudioRetentionSnapshot,
    DeletionReason,
    ErrorScope,
    InvalidRetentionTransitionError,
    JobEventActor,
    JobEventType,
    JobSnapshot,
    JobStage,
    JobStatus,
    MailIngestionDisposition,
    MailIngestionStatus,
    RetentionCandidate,
    RetentionCandidateKind,
    RetentionPolicy,
    RetentionStatus,
)
from mevo2glpi_glpi import TicketStatus
from mevo2glpi_worker_retention.retention.lifecycle import (
    RetentionJobInvariantError,
    RetentionJobLifecycleService,
    StaleRetentionCandidateError,
)
from mevo2glpi_worker_retention.retention.outcomes import RetentionFailure
from mevo2glpi_worker_retention.retention.policies import RetentionCheckPolicy
from mevo2glpi_worker_retention.retention.ports import (
    AudioPurgeWork,
    CheckRecoveryWork,
    DatabasePurgeWork,
    GlpiCheckWork,
)

JOB_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
HARD_DELETE_AT = NOW + timedelta(days=30)
STORAGE_KEY = f"{JOB_UUID}.wav"


@pytest.mark.parametrize(
    ("kind", "status", "repository_method", "expected_type"),
    [
        (
            RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK,
            RetentionStatus.CHECKING,
            "recover_interrupted_check",
            CheckRecoveryWork,
        ),
        (
            RetentionCandidateKind.START_GLPI_CHECK,
            RetentionStatus.SCHEDULED,
            "start_glpi_check",
            GlpiCheckWork,
        ),
        (
            RetentionCandidateKind.RESUME_DELETION,
            RetentionStatus.DELETING,
            "resume_deletion",
            AudioPurgeWork,
        ),
        (
            RetentionCandidateKind.RESUME_DELETION_RETRY,
            RetentionStatus.RETRY_SCHEDULED,
            "resume_deletion",
            AudioPurgeWork,
        ),
    ],
)
def test_prepare_candidate_locks_job_before_retention_transition(
    kind: RetentionCandidateKind,
    status: RetentionStatus,
    repository_method: str,
    expected_type: type,
) -> None:
    session = create_autospec(Session, instance=True)
    manager = MagicMock()
    job_repository = MagicMock()
    retention_repository = MagicMock()
    event_repository = MagicMock()
    manager.attach_mock(job_repository, "job")
    manager.attach_mock(retention_repository, "retention")
    manager.attach_mock(event_repository, "event")
    job_repository.lock_by_uuid.return_value = _job_snapshot()
    transitioned = _retention_snapshot(
        status=(
            RetentionStatus.CHECKING
            if kind is RetentionCandidateKind.START_GLPI_CHECK
            else RetentionStatus.DELETING
        ),
        deletion_reason=(
            None
            if kind
            in {
                RetentionCandidateKind.RECOVER_INTERRUPTED_CHECK,
                RetentionCandidateKind.START_GLPI_CHECK,
            }
            else DeletionReason.TICKET_CLOSED
        ),
    )
    getattr(retention_repository, repository_method).return_value = transitioned
    service = _service(
        session,
        job_repository=job_repository,
        retention_repository=retention_repository,
        event_repository=event_repository,
    )

    result = service.prepare_candidate(_candidate(kind, status=status), NOW)

    assert isinstance(result, expected_type)
    assert result.job_uuid == JOB_UUID
    assert manager.mock_calls[:2] == [
        call.job.lock_by_uuid(JOB_UUID),
        getattr(call.retention, repository_method)(JOB_UUID, now=NOW),
    ]
    event_repository.append.assert_not_called()
    session.begin.assert_called_once_with()
    session.close.assert_called_once_with()


def test_prepare_deadline_orders_job_retention_then_event() -> None:
    session = create_autospec(Session, instance=True)
    manager = MagicMock()
    job_repository = MagicMock()
    retention_repository = MagicMock()
    event_repository = MagicMock()
    manager.attach_mock(job_repository, "job")
    manager.attach_mock(retention_repository, "retention")
    manager.attach_mock(event_repository, "event")
    job = _job_snapshot(status=JobStatus.FAILED, ticket_id=None)
    job_repository.lock_by_uuid.return_value = job
    retention_repository.start_deletion.return_value = _retention_snapshot(
        status=RetentionStatus.DELETING,
        policy=RetentionPolicy.DEADLINE_ONLY,
        deletion_reason=DeletionReason.FAILED_TTL,
    )
    service = _service(
        session,
        job_repository=job_repository,
        retention_repository=retention_repository,
        event_repository=event_repository,
    )

    work = service.prepare_candidate(
        _candidate(
            RetentionCandidateKind.START_DELETION_AT_DEADLINE,
            status=RetentionStatus.SCHEDULED,
            policy=RetentionPolicy.DEADLINE_ONLY,
        ),
        NOW,
    )

    assert work == AudioPurgeWork(
        job_uuid=JOB_UUID,
        storage_key=STORAGE_KEY,
        reason=DeletionReason.FAILED_TTL,
        consecutive_retry_count=0,
    )
    assert manager.mock_calls == [
        call.job.lock_by_uuid(JOB_UUID),
        call.retention.start_deletion(
            JOB_UUID,
            reason=DeletionReason.FAILED_TTL,
            now=NOW,
        ),
        call.event.append(
            JOB_UUID,
            occurred_at=NOW,
            status=JobStatus.FAILED,
            stage=JobStage.TRANSCRIPTION,
            event_type=JobEventType.AUDIO_DELETION_STARTED,
            actor=JobEventActor.WORKER_RETENTION,
            error_scope=None,
            error_category=None,
            technical_code=None,
        ),
    ]


def test_closed_ticket_deletion_persists_intent_before_event() -> None:
    session = create_autospec(Session, instance=True)
    manager = MagicMock()
    job_repository = MagicMock()
    retention_repository = MagicMock()
    event_repository = MagicMock()
    manager.attach_mock(job_repository, "job")
    manager.attach_mock(retention_repository, "retention")
    manager.attach_mock(event_repository, "event")
    job = _job_snapshot()
    job_repository.lock_by_uuid.return_value = job
    retention_repository.start_deletion.return_value = _retention_snapshot(
        status=RetentionStatus.DELETING,
        deletion_reason=DeletionReason.TICKET_CLOSED,
    )
    service = _service(
        session,
        job_repository=job_repository,
        retention_repository=retention_repository,
        event_repository=event_repository,
    )
    check_work = _glpi_work()

    purge_work = service.start_deletion_after_check(
        check_work,
        reason=DeletionReason.TICKET_CLOSED,
        ticket_status=TicketStatus.CLOSED,
        now=NOW,
    )

    assert purge_work.reason is DeletionReason.TICKET_CLOSED
    assert manager.mock_calls == [
        call.job.lock_by_uuid(JOB_UUID),
        call.retention.start_deletion(
            JOB_UUID,
            reason=DeletionReason.TICKET_CLOSED,
            now=NOW,
            last_glpi_status_id=int(TicketStatus.CLOSED),
        ),
        call.event.append(
            JOB_UUID,
            occurred_at=NOW,
            status=JobStatus.COMPLETED,
            stage=JobStage.FINALIZATION,
            event_type=JobEventType.AUDIO_DELETION_STARTED,
            actor=JobEventActor.WORKER_RETENTION,
            error_scope=None,
            error_category=None,
            technical_code=None,
        ),
    ]


def test_retry_transition_and_sanitized_event_share_one_transaction() -> None:
    session = create_autospec(Session, instance=True)
    manager = MagicMock()
    job_repository = MagicMock()
    retention_repository = MagicMock()
    event_repository = MagicMock()
    manager.attach_mock(job_repository, "job")
    manager.attach_mock(retention_repository, "retention")
    manager.attach_mock(event_repository, "event")
    job = _job_snapshot()
    job_repository.lock_by_uuid.return_value = job
    service = _service(
        session,
        job_repository=job_repository,
        retention_repository=retention_repository,
        event_repository=event_repository,
    )
    failure = RetentionFailure(
        scope=ErrorScope.SYSTEM,
        category="glpi_unavailable",
        technical_code="http_503",
    )
    next_attempt_at = NOW + timedelta(minutes=5)

    service.schedule_retry(
        _glpi_work(),
        failure=failure,
        next_attempt_at=next_attempt_at,
        now=NOW,
    )

    assert manager.mock_calls == [
        call.job.lock_by_uuid(JOB_UUID),
        call.retention.schedule_retry(
            JOB_UUID,
            error_category="glpi_unavailable",
            error_code="http_503",
            next_check_at=next_attempt_at,
            now=NOW,
        ),
        call.event.append(
            JOB_UUID,
            occurred_at=NOW,
            status=JobStatus.COMPLETED,
            stage=JobStage.FINALIZATION,
            event_type=JobEventType.RETENTION_RETRY_SCHEDULED,
            actor=JobEventActor.WORKER_RETENTION,
            error_scope=ErrorScope.SYSTEM,
            error_category="glpi_unavailable",
            technical_code="http_503",
        ),
    ]
    session.begin.assert_called_once_with()


def test_repository_stale_error_is_translated_and_session_is_closed() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    retention_repository = MagicMock()
    event_repository = MagicMock()
    job_repository.lock_by_uuid.return_value = _job_snapshot()
    retention_repository.start_glpi_check.side_effect = InvalidRetentionTransitionError(
        "candidate changed"
    )
    service = _service(
        session,
        job_repository=job_repository,
        retention_repository=retention_repository,
        event_repository=event_repository,
    )

    with pytest.raises(StaleRetentionCandidateError) as caught:
        service.prepare_candidate(
            _candidate(
                RetentionCandidateKind.START_GLPI_CHECK,
                status=RetentionStatus.SCHEDULED,
            ),
            NOW,
        )

    assert caught.value.job_uuid == JOB_UUID
    assert isinstance(caught.value.__cause__, InvalidRetentionTransitionError)
    event_repository.append.assert_not_called()
    session.close.assert_called_once_with()


def test_persisted_job_invariant_is_not_misclassified_as_a_stale_race() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    retention_repository = MagicMock()
    event_repository = MagicMock()
    job_repository.lock_by_uuid.return_value = _job_snapshot(
        status=JobStatus.PROCESSING,
        ticket_id=None,
    )
    service = _service(
        session,
        job_repository=job_repository,
        retention_repository=retention_repository,
        event_repository=event_repository,
    )

    with pytest.raises(RetentionJobInvariantError):
        service.prepare_candidate(
            _candidate(
                RetentionCandidateKind.START_GLPI_CHECK,
                status=RetentionStatus.SCHEDULED,
            ),
            NOW,
        )

    retention_repository.start_glpi_check.assert_not_called()
    event_repository.append.assert_not_called()
    session.close.assert_called_once_with()


def test_candidate_read_returns_a_detached_immutable_snapshot() -> None:
    session = create_autospec(Session, instance=True)
    retention_repository = MagicMock()
    candidate = _candidate(
        RetentionCandidateKind.START_GLPI_CHECK,
        status=RetentionStatus.SCHEDULED,
    )
    retention_repository.find_next_candidate.return_value = candidate
    service = _service(
        session,
        job_repository=MagicMock(),
        retention_repository=retention_repository,
        event_repository=MagicMock(),
    )

    result = service.find_next_candidate(NOW)

    assert result is candidate
    assert not hasattr(result.retention, "_sa_instance_state")
    session.close.assert_called_once_with()
    with pytest.raises(FrozenInstanceError):
        result.retention.status = RetentionStatus.CHECKING  # type: ignore[misc]


def test_legacy_deleted_candidate_is_prepared_for_database_only_purge() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    retention_repository = MagicMock()
    job_repository.lock_by_uuid.return_value = _job_snapshot()
    retention_repository.get_by_job_uuid.return_value = _retention_snapshot(
        status=RetentionStatus.DELETED,
        deletion_reason=DeletionReason.TICKET_CLOSED,
    )
    service = _service(
        session,
        job_repository=job_repository,
        retention_repository=retention_repository,
        event_repository=MagicMock(),
    )

    work = service.prepare_candidate(
        _candidate(
            RetentionCandidateKind.PURGE_DELETED_AGGREGATE,
            status=RetentionStatus.DELETED,
        ),
        NOW,
    )

    assert work == DatabasePurgeWork(
        job_uuid=JOB_UUID,
        reason=DeletionReason.TICKET_CLOSED,
    )
    retention_repository.get_by_job_uuid.assert_called_once_with(JOB_UUID)


def test_database_aggregate_purge_orders_every_child_before_its_parent() -> None:
    session = create_autospec(Session, instance=True)
    manager = MagicMock()
    job_repository = MagicMock()
    retention_repository = MagicMock()
    job_event_repository = MagicMock()
    ingestion_repository = MagicMock()
    ingestion_event_repository = MagicMock()
    for name, repository in (
        ("job", job_repository),
        ("retention", retention_repository),
        ("job_event", job_event_repository),
        ("ingestion", ingestion_repository),
        ("ingestion_event", ingestion_event_repository),
    ):
        manager.attach_mock(repository, name)
    job_repository.lock_by_uuid.return_value = _job_snapshot()
    retention_repository.get_by_job_uuid.return_value = _retention_snapshot(
        status=RetentionStatus.DELETING,
        deletion_reason=DeletionReason.TICKET_CLOSED,
    )
    ingestion = MagicMock(
        uuid=JOB_UUID,
        job_uuid=JOB_UUID,
        status=MailIngestionStatus.COMPLETED,
        disposition=MailIngestionDisposition.ACCEPTED,
    )
    ingestion_repository.get_by_job_uuid.return_value = ingestion
    service = RetentionJobLifecycleService(
        session_factory=lambda: session,
        check_policy=RetentionCheckPolicy(check_interval=timedelta(hours=6)),
        job_repository_factory=lambda _session: job_repository,
        retention_repository_factory=lambda _session: retention_repository,
        event_repository_factory=lambda _session: job_event_repository,
        ingestion_repository_factory=lambda _session: ingestion_repository,
        ingestion_event_repository_factory=lambda _session: ingestion_event_repository,
    )

    service.purge_database_aggregate(
        AudioPurgeWork(
            job_uuid=JOB_UUID,
            storage_key=STORAGE_KEY,
            reason=DeletionReason.TICKET_CLOSED,
            consecutive_retry_count=0,
        ),
    )

    assert manager.mock_calls == [
        call.job.lock_by_uuid(JOB_UUID),
        call.retention.get_by_job_uuid(JOB_UUID),
        call.ingestion.get_by_job_uuid(JOB_UUID),
        call.ingestion_event.delete_by_ingestion_uuid(JOB_UUID),
        call.ingestion.delete_by_uuid(JOB_UUID),
        call.job_event.delete_by_job_uuid(JOB_UUID),
        call.retention.delete_by_job_uuid(JOB_UUID),
        call.job.delete_by_uuid(JOB_UUID),
    ]
    session.begin.assert_called_once_with()
    session.close.assert_called_once_with()


def test_database_aggregate_purge_rejects_an_active_linked_ingestion() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    retention_repository = MagicMock()
    job_event_repository = MagicMock()
    ingestion_repository = MagicMock()
    ingestion_event_repository = MagicMock()
    job_repository.lock_by_uuid.return_value = _job_snapshot()
    retention_repository.get_by_job_uuid.return_value = _retention_snapshot(
        status=RetentionStatus.DELETING,
        deletion_reason=DeletionReason.TICKET_CLOSED,
    )
    ingestion_repository.get_by_job_uuid.return_value = MagicMock(
        uuid=JOB_UUID,
        job_uuid=JOB_UUID,
        status=MailIngestionStatus.PROCESSING,
        disposition=MailIngestionDisposition.ACCEPTED,
    )
    service = RetentionJobLifecycleService(
        session_factory=lambda: session,
        check_policy=RetentionCheckPolicy(check_interval=timedelta(hours=6)),
        job_repository_factory=lambda _session: job_repository,
        retention_repository_factory=lambda _session: retention_repository,
        event_repository_factory=lambda _session: job_event_repository,
        ingestion_repository_factory=lambda _session: ingestion_repository,
        ingestion_event_repository_factory=lambda _session: ingestion_event_repository,
    )

    with pytest.raises(
        RetentionJobInvariantError,
        match="linked ingestion is not a completed acceptance",
    ):
        service.purge_database_aggregate(
            AudioPurgeWork(
                job_uuid=JOB_UUID,
                storage_key=STORAGE_KEY,
                reason=DeletionReason.TICKET_CLOSED,
                consecutive_retry_count=0,
            )
        )

    ingestion_event_repository.delete_by_ingestion_uuid.assert_not_called()
    ingestion_repository.delete_by_uuid.assert_not_called()
    job_event_repository.delete_by_job_uuid.assert_not_called()
    retention_repository.delete_by_job_uuid.assert_not_called()
    job_repository.delete_by_uuid.assert_not_called()


def _service(
    session: Session,
    *,
    job_repository: MagicMock,
    retention_repository: MagicMock,
    event_repository: MagicMock,
) -> RetentionJobLifecycleService:
    return RetentionJobLifecycleService(
        session_factory=lambda: session,
        check_policy=RetentionCheckPolicy(check_interval=timedelta(hours=6)),
        job_repository_factory=lambda _session: job_repository,
        retention_repository_factory=lambda _session: retention_repository,
        event_repository_factory=lambda _session: event_repository,
    )


def _candidate(
    kind: RetentionCandidateKind,
    *,
    status: RetentionStatus,
    policy: RetentionPolicy = RetentionPolicy.TICKET_OR_DEADLINE,
) -> RetentionCandidate:
    return RetentionCandidate(
        kind=kind,
        retention=_retention_snapshot(status=status, policy=policy),
    )


def _job_snapshot(
    *,
    status: JobStatus = JobStatus.COMPLETED,
    ticket_id: int | None = 42,
) -> JobSnapshot:
    stage = (
        JobStage.FINALIZATION
        if status is JobStatus.COMPLETED
        else JobStage.TRANSCRIPTION
    )
    return JobSnapshot(
        uuid=JOB_UUID,
        status=status,
        stage=stage,
        storage_key=STORAGE_KEY,
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=42,
        glpi_ticket_id=ticket_id,
        transcription_provider_job_id=None,
        attempt_count=1,
        consecutive_retry_count=0,
        next_attempt_at=None,
        last_error_scope=(ErrorScope.JOB if status is JobStatus.FAILED else None),
        last_error_category=("terminal" if status is JobStatus.FAILED else None),
        last_error_code=("remote_failure" if status is JobStatus.FAILED else None),
        created_at=NOW - timedelta(days=1),
        first_started_at=NOW - timedelta(hours=1),
        updated_at=NOW,
        finished_at=NOW,
    )


def _retention_snapshot(
    *,
    status: RetentionStatus,
    policy: RetentionPolicy = RetentionPolicy.TICKET_OR_DEADLINE,
    deletion_reason: DeletionReason | None = None,
) -> AudioRetentionSnapshot:
    deleting = deletion_reason is not None
    return AudioRetentionSnapshot(
        job_uuid=JOB_UUID,
        policy=policy,
        status=status,
        hard_delete_at=HARD_DELETE_AT,
        next_check_at=(NOW if status is RetentionStatus.SCHEDULED else None),
        last_checked_at=NOW - timedelta(minutes=1),
        last_glpi_status_id=None,
        check_attempt_count=1,
        consecutive_retry_count=(1 if status is RetentionStatus.RETRY_SCHEDULED else 0),
        deletion_reason=deletion_reason,
        deletion_started_at=(NOW - timedelta(minutes=1) if deleting else None),
        audio_deleted_at=None,
        last_error_category=None,
        last_error_code=None,
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
    )


def _glpi_work() -> GlpiCheckWork:
    return GlpiCheckWork(
        job_uuid=JOB_UUID,
        storage_key=STORAGE_KEY,
        ticket_id=42,
        hard_delete_at=HARD_DELETE_AT,
        consecutive_retry_count=0,
    )
