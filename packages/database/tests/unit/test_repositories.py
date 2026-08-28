from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

import glpi_ticketing_database as database_api
from glpi_ticketing_database import (
    AudioExtension,
    ErrorScope,
    JobRepository,
    JobSnapshot,
    JobStage,
    JobStatus,
    PendingJob,
    TicketCreationJob,
    TicketCreationJobEvent,
)


def test_add_pending_flushes_job_and_initial_event_in_same_session() -> None:
    session = create_autospec(Session, instance=True)
    pending_job = _pending_job()
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)

    def populate_server_defaults() -> None:
        job = _added_entities(session)[0]
        job.attempt_count = 0
        job.consecutive_retry_count = 0
        job.created_at = now
        job.updated_at = now

    session.flush.side_effect = populate_server_defaults

    snapshot = JobRepository(session).add_pending(pending_job)

    job, event = _added_entities(session)
    assert isinstance(job, TicketCreationJob)
    assert job.uuid == pending_job.uuid
    assert job.status == JobStatus.PENDING.value
    assert job.stage is None
    assert job.storage_key == pending_job.storage_key
    assert job.audio_extension == AudioExtension.WAV.value
    assert job.audio_size_bytes == 42
    assert isinstance(event, TicketCreationJobEvent)
    assert event.job_uuid == pending_job.uuid
    assert event.status == JobStatus.PENDING.value
    assert event.stage is None
    assert event.event_type == "job_created"
    assert event.actor == "web"
    assert snapshot == _expected_pending_snapshot(pending_job, now)
    session.flush.assert_called_once_with()


def test_add_pending_does_not_own_the_transaction() -> None:
    session = create_autospec(Session, instance=True)
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)

    def populate_server_defaults() -> None:
        job = _added_entities(session)[0]
        job.attempt_count = 0
        job.consecutive_retry_count = 0
        job.created_at = now
        job.updated_at = now

    session.flush.side_effect = populate_server_defaults

    JobRepository(session).add_pending(_pending_job())

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def test_add_pending_propagates_flush_error_without_rolling_back() -> None:
    session = create_autospec(Session, instance=True)
    session.flush.side_effect = RuntimeError("database failure")

    with pytest.raises(RuntimeError, match="database failure"):
        JobRepository(session).add_pending(_pending_job())

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


@pytest.mark.parametrize(
    "pending_job",
    [
        PendingJob(
            uuid=uuid4(),
            storage_key="different.wav",
            audio_extension=AudioExtension.WAV,
            audio_size_bytes=42,
        ),
        PendingJob(
            uuid=uuid4(),
            storage_key="audio.wav",
            audio_extension=AudioExtension.WAV,
            audio_size_bytes=0,
        ),
    ],
)
def test_add_pending_rejects_inconsistent_values_before_session_mutation(
    pending_job: PendingJob,
) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises(ValueError):
        JobRepository(session).add_pending(pending_job)

    session.add_all.assert_not_called()
    session.flush.assert_not_called()


def test_add_pending_requires_typed_audio_extension() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    pending_job = PendingJob(
        uuid=job_uuid,
        storage_key=f"{job_uuid}.wav",
        audio_extension="wav",  # type: ignore[arg-type]
        audio_size_bytes=42,
    )

    with pytest.raises(TypeError, match="AudioExtension"):
        JobRepository(session).add_pending(pending_job)

    session.add_all.assert_not_called()


def test_get_by_uuid_returns_typed_immutable_snapshot() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    job = _persisted_job(job_uuid, now)
    session.get.return_value = job

    snapshot = JobRepository(session).get_by_uuid(job_uuid)

    assert snapshot == JobSnapshot(
        uuid=job_uuid,
        status=JobStatus.RETRY_SCHEDULED,
        stage=JobStage.TRANSCRIPTION,
        storage_key=f"{job_uuid}.mp3",
        audio_extension=AudioExtension.MP3,
        audio_size_bytes=84,
        glpi_ticket_id=None,
        transcription_provider_job_id="provider-job",
        attempt_count=2,
        consecutive_retry_count=1,
        next_attempt_at=now + timedelta(minutes=1),
        last_error_scope=ErrorScope.SYSTEM,
        last_error_category="service_unavailable",
        last_error_code="transcription_unavailable",
        created_at=now,
        first_started_at=now,
        updated_at=now,
        finished_at=None,
    )
    session.get.assert_called_once_with(TicketCreationJob, job_uuid)
    session.flush.assert_not_called()


def test_get_by_uuid_returns_none_for_an_unknown_job() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    session.get.return_value = None

    assert JobRepository(session).get_by_uuid(job_uuid) is None

    session.get.assert_called_once_with(TicketCreationJob, job_uuid)


def test_repository_contracts_are_exposed_by_the_package() -> None:
    assert database_api.JobRepository is JobRepository
    assert database_api.JobSnapshot is JobSnapshot
    assert database_api.PendingJob is PendingJob


def _pending_job() -> PendingJob:
    job_uuid = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")
    return PendingJob(
        uuid=job_uuid,
        storage_key=f"{job_uuid}.wav",
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=42,
    )


def _added_entities(session: Any) -> tuple[Any, Any]:
    entities = session.add_all.call_args.args[0]
    return tuple(entities)


def _expected_pending_snapshot(
    pending_job: PendingJob,
    now: datetime,
) -> JobSnapshot:
    return JobSnapshot(
        uuid=pending_job.uuid,
        status=JobStatus.PENDING,
        stage=None,
        storage_key=pending_job.storage_key,
        audio_extension=pending_job.audio_extension,
        audio_size_bytes=pending_job.audio_size_bytes,
        glpi_ticket_id=None,
        transcription_provider_job_id=None,
        attempt_count=0,
        consecutive_retry_count=0,
        next_attempt_at=None,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        created_at=now,
        first_started_at=None,
        updated_at=now,
        finished_at=None,
    )


def _persisted_job(job_uuid: UUID, now: datetime) -> TicketCreationJob:
    return TicketCreationJob(
        uuid=job_uuid,
        status=JobStatus.RETRY_SCHEDULED.value,
        stage=JobStage.TRANSCRIPTION.value,
        storage_key=f"{job_uuid}.mp3",
        audio_extension=AudioExtension.MP3.value,
        audio_size_bytes=84,
        glpi_ticket_id=None,
        transcription_provider_job_id="provider-job",
        attempt_count=2,
        consecutive_retry_count=1,
        next_attempt_at=now + timedelta(minutes=1),
        last_error_scope=ErrorScope.SYSTEM.value,
        last_error_category="service_unavailable",
        last_error_code="transcription_unavailable",
        created_at=now,
        first_started_at=now,
        updated_at=now,
        finished_at=None,
    )
