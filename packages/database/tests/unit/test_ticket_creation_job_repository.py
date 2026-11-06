from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

import mevo2glpi_database as database_api
from mevo2glpi_database import (
    AudioExtension,
    ErrorScope,
    InvalidJobTransitionError,
    JobCursor,
    JobPage,
    JobSnapshot,
    JobStage,
    JobStatus,
    PendingJob,
    TicketCreationJob,
    TicketCreationJobRepository,
)


def test_add_pending_flushes_only_the_job_model() -> None:
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

    snapshot = TicketCreationJobRepository(session).add_pending(pending_job)

    (job,) = _added_entities(session)
    assert isinstance(job, TicketCreationJob)
    assert job.uuid == pending_job.uuid
    assert job.status == JobStatus.PENDING.value
    assert job.stage is None
    assert job.storage_key == pending_job.storage_key
    assert job.audio_extension == AudioExtension.WAV.value
    assert job.audio_size_bytes == 42
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

    TicketCreationJobRepository(session).add_pending(_pending_job())

    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def test_add_pending_propagates_flush_error_without_rolling_back() -> None:
    session = create_autospec(Session, instance=True)
    session.flush.side_effect = RuntimeError("database failure")

    with pytest.raises(RuntimeError, match="database failure"):
        TicketCreationJobRepository(session).add_pending(_pending_job())

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
        TicketCreationJobRepository(session).add_pending(pending_job)

    session.add.assert_not_called()
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
        TicketCreationJobRepository(session).add_pending(pending_job)

    session.add.assert_not_called()


def test_get_by_uuid_returns_typed_immutable_snapshot() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    job = _persisted_job(job_uuid, now)
    session.get.return_value = job

    snapshot = TicketCreationJobRepository(session).get_by_uuid(job_uuid)

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

    assert TicketCreationJobRepository(session).get_by_uuid(job_uuid) is None

    session.get.assert_called_once_with(TicketCreationJob, job_uuid)


def test_lock_by_uuid_returns_snapshot_without_mutating_the_job() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    job = _persisted_job(job_uuid, now)
    session.get.return_value = job

    snapshot = TicketCreationJobRepository(session).lock_by_uuid(job_uuid)

    assert snapshot == _snapshot_for(job)
    session.get.assert_called_once_with(
        TicketCreationJob,
        job_uuid,
        with_for_update=True,
    )
    session.flush.assert_not_called()
    session.commit.assert_not_called()


def test_lock_by_uuid_rejects_an_unknown_job() -> None:
    session = create_autospec(Session, instance=True)
    session.get.return_value = None

    with pytest.raises(database_api.JobNotFoundError, match="does not exist"):
        TicketCreationJobRepository(session).lock_by_uuid(uuid4())

    session.flush.assert_not_called()


def test_list_page_returns_a_stable_cursor_from_the_last_visible_job() -> None:
    session = create_autospec(Session, instance=True)
    oldest_time = datetime(2026, 8, 27, 10, tzinfo=UTC)
    newest_time = datetime(2026, 8, 27, 12, tzinfo=UTC)
    jobs = (
        _persisted_job(uuid4(), newest_time),
        _persisted_job(uuid4(), newest_time),
        _persisted_job(uuid4(), oldest_time),
    )
    session.scalars.return_value.all.return_value = jobs

    page = TicketCreationJobRepository(session).list_page(limit=2)

    assert page == JobPage(
        items=(
            _snapshot_for(jobs[0]),
            _snapshot_for(jobs[1]),
        ),
        next_cursor=JobCursor(
            updated_at=jobs[1].updated_at,
            uuid=jobs[1].uuid,
        ),
        has_more=True,
    )
    session.scalars.assert_called_once()
    session.flush.assert_not_called()


@pytest.mark.parametrize("limit", [0, 101, True])
def test_list_page_rejects_an_invalid_limit_before_reading(limit: int) -> None:
    session = create_autospec(Session, instance=True)

    with pytest.raises((TypeError, ValueError)):
        TicketCreationJobRepository(session).list_page(limit=limit)

    session.scalars.assert_not_called()


def test_rearm_for_operator_retry_clears_transient_state_only() -> None:
    session = create_autospec(Session, instance=True)
    job_uuid = uuid4()
    created_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    first_started_at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    job = _persisted_job(job_uuid, created_at)
    job.status = JobStatus.BLOCKED.value
    job.stage = JobStage.FINALIZATION.value
    job.glpi_ticket_id = 1234
    job.transcription_provider_job_id = "provider-before-retry"
    job.attempt_count = 7
    job.consecutive_retry_count = 3
    job.next_attempt_at = None
    job.last_error_scope = ErrorScope.SYSTEM.value
    job.last_error_category = "ambiguous_result"
    job.last_error_code = "glpi_result_unknown"
    job.first_started_at = first_started_at
    job.finished_at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    session.get.return_value = job
    retry_time = datetime(2026, 8, 28, 13, tzinfo=UTC)

    snapshot = TicketCreationJobRepository(session).rearm_for_operator_retry(
        job_uuid,
        now=retry_time,
    )

    assert snapshot.status is JobStatus.PENDING
    assert snapshot.stage is None
    assert snapshot.glpi_ticket_id == 1234
    assert snapshot.transcription_provider_job_id is None
    assert snapshot.attempt_count == 7
    assert snapshot.consecutive_retry_count == 0
    assert snapshot.next_attempt_at is None
    assert snapshot.last_error_scope is None
    assert snapshot.last_error_category is None
    assert snapshot.last_error_code is None
    assert snapshot.created_at == created_at
    assert snapshot.first_started_at == first_started_at
    assert snapshot.updated_at == retry_time
    assert snapshot.finished_at is None
    session.get.assert_called_once_with(
        TicketCreationJob,
        job_uuid,
        with_for_update=True,
    )
    session.flush.assert_called_once_with()
    session.commit.assert_not_called()


def test_rearm_for_operator_retry_rejects_a_non_terminal_job() -> None:
    session = create_autospec(Session, instance=True)
    job = _persisted_job(uuid4(), datetime(2026, 8, 27, 12, tzinfo=UTC))
    job.status = JobStatus.PROCESSING.value
    job.stage = JobStage.TRANSCRIPTION.value
    session.get.return_value = job

    with pytest.raises(InvalidJobTransitionError, match="failed or blocked"):
        TicketCreationJobRepository(session).rearm_for_operator_retry(
            job.uuid,
            now=datetime(2026, 8, 28, 12, tzinfo=UTC),
        )

    session.flush.assert_not_called()


def test_repository_contracts_are_exposed_by_the_package() -> None:
    assert database_api.TicketCreationJobRepository is TicketCreationJobRepository
    assert database_api.JobSnapshot is JobSnapshot
    assert database_api.JobCursor is JobCursor
    assert database_api.JobPage is JobPage
    assert database_api.PendingJob is PendingJob
    assert database_api.RetentionCandidate.__name__ == "RetentionCandidate"
    assert database_api.RetentionCandidateKind.__name__ == "RetentionCandidateKind"


def _pending_job() -> PendingJob:
    job_uuid = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")
    return PendingJob(
        uuid=job_uuid,
        storage_key=f"{job_uuid}.wav",
        audio_extension=AudioExtension.WAV,
        audio_size_bytes=42,
    )


def _added_entities(session: Any) -> tuple[Any, ...]:
    return tuple(call.args[0] for call in session.add.call_args_list)


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


def _snapshot_for(job: TicketCreationJob) -> JobSnapshot:
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
