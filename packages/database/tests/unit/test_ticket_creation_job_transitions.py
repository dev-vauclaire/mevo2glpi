from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

import mevo2glpi_database as database_api
from mevo2glpi_database import (
    AudioExtension,
    ErrorScope,
    InvalidJobTransitionError,
    JobAcquisition,
    JobStage,
    JobStatus,
    TicketCreationJob,
    TicketCreationJobRepository,
)

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_resume_processing_has_priority_and_increments_real_attempt() -> None:
    session = _session()
    processing = _job(status=JobStatus.PROCESSING, stage=JobStage.GLPI_CREATION)
    processing.transcription_provider_job_id = "abandoned-provider-job"
    session.scalars.return_value = _scalar_result(processing)

    acquisition = TicketCreationJobRepository(session).resume_or_claim_next(NOW)

    assert acquisition == JobAcquisition(
        job=acquisition.job,  # type: ignore[union-attr]
        previous_status=JobStatus.PROCESSING,
        resumed=True,
    )
    assert acquisition is not None
    assert acquisition.job.status is JobStatus.PROCESSING
    assert acquisition.job.stage is JobStage.TRANSCRIPTION
    assert acquisition.job.attempt_count == 3
    assert acquisition.job.transcription_provider_job_id is None
    assert session.scalars.call_count == 1
    session.add.assert_not_called()


def test_resume_finalization_preserves_provider_identifier() -> None:
    session = _session()
    processing = _job(status=JobStatus.PROCESSING, stage=JobStage.FINALIZATION)
    processing.glpi_ticket_id = 42
    processing.transcription_provider_job_id = "provider-before-crash"
    session.scalars.return_value = _scalar_result(processing)

    acquisition = TicketCreationJobRepository(session).resume_or_claim_next(NOW)

    assert acquisition is not None
    assert acquisition.job.stage is JobStage.FINALIZATION
    assert acquisition.job.glpi_ticket_id == 42
    assert acquisition.job.transcription_provider_job_id == "provider-before-crash"


def test_due_retry_is_claimed_before_pending_and_preserves_retry_counter() -> None:
    session = _session()
    due_retry = _job(
        status=JobStatus.RETRY_SCHEDULED,
        stage=JobStage.GLPI_CREATION,
    )
    due_retry.next_attempt_at = NOW
    due_retry.last_error_scope = ErrorScope.SYSTEM.value
    due_retry.last_error_category = "unavailable"
    due_retry.last_error_code = "http_503"
    due_retry.consecutive_retry_count = 2
    session.scalars.side_effect = [
        _scalar_result(None),
        _scalar_result(due_retry),
    ]

    acquisition = TicketCreationJobRepository(session).resume_or_claim_next(NOW)

    assert acquisition is not None
    assert acquisition.previous_status is JobStatus.RETRY_SCHEDULED
    assert acquisition.job.status is JobStatus.PROCESSING
    assert acquisition.job.stage is JobStage.TRANSCRIPTION
    assert acquisition.job.attempt_count == 3
    assert acquisition.job.consecutive_retry_count == 2
    assert acquisition.job.next_attempt_at is None
    assert acquisition.job.last_error_scope is None
    assert session.scalar.call_count == 0


def test_future_system_retry_suspends_pending_claims() -> None:
    session = _session()
    session.scalars.side_effect = [_scalar_result(None), _scalar_result(None)]
    session.scalar.return_value = uuid4()

    assert TicketCreationJobRepository(session).resume_or_claim_next(NOW) is None

    assert session.scalars.call_count == 2
    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_pending_is_claimed_when_no_system_retry_exists() -> None:
    session = _session()
    pending = _job(status=JobStatus.PENDING, stage=None)
    pending.attempt_count = 0
    pending.first_started_at = None
    session.scalars.side_effect = [
        _scalar_result(None),
        _scalar_result(None),
        _scalar_result(pending),
    ]
    session.scalar.return_value = None

    acquisition = TicketCreationJobRepository(session).resume_or_claim_next(NOW)

    assert acquisition is not None
    assert acquisition.previous_status is JobStatus.PENDING
    assert acquisition.resumed is False
    assert acquisition.job.first_started_at == NOW
    assert acquisition.job.attempt_count == 1
    session.add.assert_not_called()


def test_transcription_and_ticket_boundaries_mutate_only_the_job() -> None:
    session = _session()
    job = _job(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION)
    job.consecutive_retry_count = 3
    session.get.return_value = job
    repository = TicketCreationJobRepository(session)

    submitted = repository.record_transcription_submitted(
        job.uuid,
        "provider-123",
        NOW,
    )
    succeeded = repository.advance_after_transcription(
        job.uuid,
        NOW + timedelta(seconds=1),
    )
    ticketed = repository.record_ticket_confirmed(
        job.uuid,
        1234,
        NOW + timedelta(seconds=2),
    )

    assert submitted.changed is True
    assert submitted.job.transcription_provider_job_id == "provider-123"
    assert succeeded.stage is JobStage.GLPI_CREATION
    assert ticketed.stage is JobStage.FINALIZATION
    assert ticketed.glpi_ticket_id == 1234
    assert ticketed.consecutive_retry_count == 3
    session.add.assert_not_called()


def test_provider_submission_is_idempotent_only_for_the_same_identifier() -> None:
    session = _session()
    job = _job(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION)
    job.transcription_provider_job_id = "provider-123"
    session.get.return_value = job
    repository = TicketCreationJobRepository(session)

    mutation = repository.record_transcription_submitted(
        job.uuid,
        "provider-123",
        NOW,
    )

    assert mutation.job.transcription_provider_job_id == "provider-123"
    assert mutation.changed is False
    session.add.assert_not_called()
    session.flush.assert_not_called()

    with pytest.raises(InvalidJobTransitionError, match="different"):
        repository.record_transcription_submitted(job.uuid, "provider-456", NOW)


def test_transcription_advance_does_not_require_a_remote_identifier() -> None:
    session = _session()
    job = _job(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION)
    job.transcription_provider_job_id = "provider-persisted"
    session.get.return_value = job

    snapshot = TicketCreationJobRepository(session).advance_after_transcription(
        job.uuid,
        NOW,
    )

    assert snapshot.stage is JobStage.GLPI_CREATION
    assert snapshot.transcription_provider_job_id == "provider-persisted"
    session.add.assert_not_called()
    session.flush.assert_called_once()


def test_schedule_retry_increments_only_the_job_retry_counter() -> None:
    session = _session()
    job = _job(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION)
    session.get.return_value = job

    snapshot = TicketCreationJobRepository(session).schedule_retry(
        job.uuid,
        stage=JobStage.TRANSCRIPTION,
        error_scope=ErrorScope.SYSTEM,
        error_category="unavailable",
        error_code="submission_unavailable",
        next_attempt_at=NOW + timedelta(seconds=5),
        now=NOW,
    )

    assert snapshot.status is JobStatus.RETRY_SCHEDULED
    assert snapshot.attempt_count == 2
    assert snapshot.consecutive_retry_count == 1
    assert snapshot.next_attempt_at == NOW + timedelta(seconds=5)
    session.add.assert_not_called()


@pytest.mark.parametrize("method_name", ["mark_failed", "mark_blocked"])
def test_failure_transitions_mutate_only_the_job(
    method_name: str,
) -> None:
    session = _session()
    job = _job(status=JobStatus.PROCESSING, stage=JobStage.TRANSCRIPTION)
    session.get.return_value = job
    repository = TicketCreationJobRepository(session)
    kwargs = {
        "stage": JobStage.TRANSCRIPTION,
        "error_category": "terminal",
        "error_code": "remote_job_failed",
        "now": NOW,
    }
    if method_name == "mark_blocked":
        kwargs["error_scope"] = ErrorScope.JOB

    snapshot = getattr(repository, method_name)(job.uuid, **kwargs)

    expected_status = (
        JobStatus.FAILED if method_name == "mark_failed" else JobStatus.BLOCKED
    )
    assert snapshot.status is expected_status
    session.add.assert_not_called()
    assert session.flush.call_count == 1


def test_completed_transition_mutates_only_the_job() -> None:
    session = _session()
    job = _job(status=JobStatus.PROCESSING, stage=JobStage.FINALIZATION)
    job.glpi_ticket_id = 1234
    job.consecutive_retry_count = 4
    session.get.return_value = job

    snapshot = TicketCreationJobRepository(session).mark_completed(
        job.uuid,
        now=NOW,
    )

    assert snapshot.status is JobStatus.COMPLETED
    assert snapshot.finished_at == NOW
    assert snapshot.consecutive_retry_count == 4
    session.add.assert_not_called()


def test_transition_rejects_wrong_persisted_stage_without_mutation() -> None:
    session = _session()
    job = _job(status=JobStatus.PROCESSING, stage=JobStage.GLPI_CREATION)
    session.get.return_value = job

    with pytest.raises(InvalidJobTransitionError):
        TicketCreationJobRepository(session).record_transcription_submitted(
            job.uuid,
            "provider-123",
            NOW,
        )

    session.add.assert_not_called()
    session.flush.assert_not_called()


def test_ticket_creation_job_repository_never_owns_transaction_or_session() -> None:
    session = _session()
    session.scalars.side_effect = [_scalar_result(None), _scalar_result(None)]
    session.scalar.return_value = uuid4()

    TicketCreationJobRepository(session).resume_or_claim_next(NOW)

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def test_ticket_creation_job_contracts_are_exported_by_package() -> None:
    assert database_api.JobAcquisition is JobAcquisition
    assert database_api.TicketCreationJobRepository is TicketCreationJobRepository


def _session() -> Session:
    return create_autospec(Session, instance=True)


def _scalar_result(value: TicketCreationJob | None) -> MagicMock:
    result = MagicMock()
    result.first.return_value = value
    return result


def _job(
    *,
    status: JobStatus,
    stage: JobStage | None,
) -> TicketCreationJob:
    job_uuid = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")
    return TicketCreationJob(
        uuid=job_uuid,
        status=status.value,
        stage=stage.value if stage is not None else None,
        storage_key=f"{job_uuid}.wav",
        audio_extension=AudioExtension.WAV.value,
        audio_size_bytes=42,
        glpi_ticket_id=None,
        transcription_provider_job_id=None,
        attempt_count=2,
        consecutive_retry_count=0,
        next_attempt_at=None,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        created_at=NOW - timedelta(hours=1),
        first_started_at=NOW - timedelta(minutes=30),
        updated_at=NOW - timedelta(minutes=1),
        finished_at=None,
    )
