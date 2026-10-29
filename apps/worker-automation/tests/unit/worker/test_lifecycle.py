from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call, create_autospec
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from glpi_ticketing_database import (
    AudioExtension,
    ErrorScope,
    JobAcquisition,
    JobEventActor,
    JobEventType,
    JobMutation,
    JobSnapshot,
    JobStage,
    JobStatus,
    MailIngestionDisposition,
    RetentionPolicy,
)
from glpi_ticketing_worker_automation.worker import (
    AutomationJobAcquisition,
    AutomationJobLifecycleService,
)

JOB_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
SOURCE_RECEIVED_AT = datetime(2026, 8, 27, 7, 30, tzinfo=UTC)


def test_claim_and_event_are_orchestrated_in_one_application_transaction() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    event_repository = MagicMock()
    mail_repository = MagicMock()
    acquisition = JobAcquisition(
        job=_snapshot(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION),
        previous_status=JobStatus.PENDING,
        resumed=False,
    )
    job_repository.resume_or_claim_next.return_value = acquisition
    mail_repository.get_by_uuid.return_value = SimpleNamespace(
        uuid=JOB_UUID,
        job_uuid=JOB_UUID,
        disposition=MailIngestionDisposition.ACCEPTED,
        received_at=SOURCE_RECEIVED_AT,
    )
    guards: list[str] = []
    service = _service(
        session,
        job_repository=job_repository,
        event_repository=event_repository,
        mail_repository=mail_repository,
        ownership_guard=lambda: guards.append("guard"),
    )

    result = service.resume_or_claim_next(NOW)

    assert result == AutomationJobAcquisition(
        job=acquisition.job,
        previous_status=JobStatus.PENDING,
        resumed=False,
        source_received_at=SOURCE_RECEIVED_AT,
    )
    job_repository.resume_or_claim_next.assert_called_once_with(NOW)
    mail_repository.get_by_uuid.assert_called_once_with(JOB_UUID)
    event_repository.append.assert_called_once_with(
        JOB_UUID,
        occurred_at=NOW,
        status=JobStatus.PROCESSING,
        stage=JobStage.TRANSCRIPTION,
        event_type=JobEventType.PROCESSING_STARTED,
        actor=JobEventActor.WORKER_AUTOMATION,
        error_scope=None,
        error_category=None,
        technical_code=None,
    )
    session.begin.assert_called_once_with()
    session.close.assert_called_once_with()
    assert guards == ["guard", "guard"]


def test_claim_without_matching_mail_ingestion_has_no_source_date() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    event_repository = MagicMock()
    mail_repository = MagicMock()
    acquisition = JobAcquisition(
        job=_snapshot(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION),
        previous_status=JobStatus.PENDING,
        resumed=False,
    )
    job_repository.resume_or_claim_next.return_value = acquisition
    mail_repository.get_by_uuid.return_value = None
    service = _service(
        session,
        job_repository=job_repository,
        event_repository=event_repository,
        mail_repository=mail_repository,
    )

    result = service.resume_or_claim_next(NOW)

    assert result is not None
    assert result.source_received_at is None
    event_repository.append.assert_called_once()


def test_claim_normalizes_aware_source_date_to_utc() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    event_repository = MagicMock()
    mail_repository = MagicMock()
    acquisition = JobAcquisition(
        job=_snapshot(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION),
        previous_status=JobStatus.PENDING,
        resumed=False,
    )
    job_repository.resume_or_claim_next.return_value = acquisition
    mail_repository.get_by_uuid.return_value = SimpleNamespace(
        uuid=JOB_UUID,
        job_uuid=JOB_UUID,
        disposition=MailIngestionDisposition.ACCEPTED,
        received_at=datetime(
            2026,
            8,
            27,
            9,
            30,
            tzinfo=timezone(timedelta(hours=2)),
        ),
    )
    service = _service(
        session,
        job_repository=job_repository,
        event_repository=event_repository,
        mail_repository=mail_repository,
    )

    result = service.resume_or_claim_next(NOW)

    assert result is not None
    assert result.source_received_at == SOURCE_RECEIVED_AT


def test_idempotent_submission_does_not_duplicate_its_event() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    event_repository = MagicMock()
    snapshot = _snapshot(
        status=JobStatus.PROCESSING,
        stage=JobStage.TRANSCRIPTION,
        provider_job_id="provider-1",
    )
    job_repository.record_transcription_submitted.return_value = JobMutation(
        job=snapshot,
        changed=False,
    )
    service = _service(
        session,
        job_repository=job_repository,
        event_repository=event_repository,
    )

    result = service.record_transcription_submitted(JOB_UUID, "provider-1", NOW)

    assert result == snapshot
    event_repository.append.assert_not_called()


def test_terminal_transcription_failure_is_audited_before_ticketing() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    event_repository = MagicMock()
    snapshot = _snapshot(
        status=JobStatus.PROCESSING,
        stage=JobStage.GLPI_CREATION,
    )
    job_repository.advance_after_transcription.return_value = snapshot
    service = _service(
        session,
        job_repository=job_repository,
        event_repository=event_repository,
    )

    result = service.record_transcription_failed(
        JOB_UUID,
        error_category="terminal",
        error_code="invalid_transcription",
        now=NOW,
    )

    assert result == snapshot
    job_repository.advance_after_transcription.assert_called_once_with(JOB_UUID, NOW)
    event_repository.append.assert_called_once_with(
        JOB_UUID,
        occurred_at=NOW,
        status=JobStatus.PROCESSING,
        stage=JobStage.GLPI_CREATION,
        event_type=JobEventType.TRANSCRIPTION_FAILED,
        actor=JobEventActor.WORKER_AUTOMATION,
        error_scope=ErrorScope.JOB,
        error_category="terminal",
        technical_code="invalid_transcription",
    )


def test_completion_orders_job_retention_then_event_in_same_transaction() -> None:
    session = create_autospec(Session, instance=True)
    manager = MagicMock()
    job_repository = MagicMock()
    retention_repository = MagicMock()
    event_repository = MagicMock()
    manager.attach_mock(job_repository, "job")
    manager.attach_mock(retention_repository, "retention")
    manager.attach_mock(event_repository, "event")
    snapshot = _snapshot(
        status=JobStatus.COMPLETED,
        stage=JobStage.FINALIZATION,
        ticket_id=42,
    )
    job_repository.mark_completed.return_value = snapshot
    service = _service(
        session,
        job_repository=job_repository,
        event_repository=event_repository,
        retention_repository=retention_repository,
    )
    hard_delete_at = NOW + timedelta(days=90)
    next_check_at = NOW + timedelta(hours=6)

    service.mark_completed(
        JOB_UUID,
        hard_delete_at=hard_delete_at,
        next_check_at=next_check_at,
        now=NOW,
    )

    assert manager.mock_calls == [
        call.job.mark_completed(JOB_UUID, now=NOW),
        call.retention.activate(
            JOB_UUID,
            policy=RetentionPolicy.TICKET_OR_DEADLINE,
            hard_delete_at=hard_delete_at,
            next_check_at=next_check_at,
            now=NOW,
        ),
        call.event.append(
            JOB_UUID,
            occurred_at=NOW,
            status=JobStatus.COMPLETED,
            stage=JobStage.FINALIZATION,
            event_type=JobEventType.JOB_COMPLETED,
            actor=JobEventActor.WORKER_AUTOMATION,
            error_scope=None,
            error_category=None,
            technical_code=None,
        ),
    ]
    session.begin.assert_called_once_with()


def test_event_failure_escapes_the_application_transaction_and_closes_session() -> None:
    session = create_autospec(Session, instance=True)
    job_repository = MagicMock()
    event_repository = MagicMock()
    job_repository.mark_failed.return_value = _snapshot(
        status=JobStatus.FAILED,
        stage=JobStage.TRANSCRIPTION,
        error_scope=ErrorScope.JOB,
        error_category="terminal",
        error_code="remote_job_failed",
    )
    event_repository.append.side_effect = RuntimeError("event persistence failed")
    service = _service(
        session,
        job_repository=job_repository,
        event_repository=event_repository,
    )

    with pytest.raises(RuntimeError, match="event persistence failed"):
        service.mark_failed(
            JOB_UUID,
            stage=JobStage.TRANSCRIPTION,
            error_category="terminal",
            error_code="remote_job_failed",
            hard_delete_at=NOW + timedelta(days=30),
            now=NOW,
        )

    session.close.assert_called_once_with()
    session.begin.return_value.__exit__.assert_called_once()


def test_first_ownership_guard_failure_does_not_open_a_session() -> None:
    session_factory = MagicMock()
    ownership_error = RuntimeError("lock lost")
    ownership_guard = MagicMock(side_effect=ownership_error)
    service = AutomationJobLifecycleService(
        session_factory=session_factory,
        ownership_guard=ownership_guard,
    )

    with pytest.raises(RuntimeError, match="lock lost"):
        service.resume_or_claim_next(NOW)

    ownership_guard.assert_called_once_with()
    session_factory.assert_not_called()


def _service(
    session: Session,
    *,
    job_repository: MagicMock,
    event_repository: MagicMock,
    retention_repository: MagicMock | None = None,
    mail_repository: MagicMock | None = None,
    ownership_guard=None,
) -> AutomationJobLifecycleService:
    return AutomationJobLifecycleService(
        session_factory=lambda: session,
        ownership_guard=ownership_guard,
        job_repository_factory=lambda _session: job_repository,
        event_repository_factory=lambda _session: event_repository,
        retention_repository_factory=lambda _session: (
            retention_repository if retention_repository is not None else MagicMock()
        ),
        mail_ingestion_repository_factory=lambda _session: (
            mail_repository if mail_repository is not None else MagicMock()
        ),
    )


def _snapshot(
    *,
    status: JobStatus,
    stage: JobStage | None,
    provider_job_id: str | None = None,
    ticket_id: int | None = None,
    error_scope: ErrorScope | None = None,
    error_category: str | None = None,
    error_code: str | None = None,
) -> JobSnapshot:
    return JobSnapshot(
        uuid=JOB_UUID,
        status=status,
        stage=stage,
        storage_key=f"{JOB_UUID}.wav",
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=42,
        glpi_ticket_id=ticket_id,
        transcription_provider_job_id=provider_job_id,
        attempt_count=1,
        consecutive_retry_count=0,
        next_attempt_at=None,
        last_error_scope=error_scope,
        last_error_category=error_category,
        last_error_code=error_code,
        created_at=NOW - timedelta(minutes=1),
        first_started_at=NOW - timedelta(seconds=30),
        updated_at=NOW,
        finished_at=NOW if status in {JobStatus.COMPLETED, JobStatus.FAILED} else None,
    )
