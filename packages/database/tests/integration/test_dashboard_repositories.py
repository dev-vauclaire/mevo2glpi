"""PostgreSQL contracts used by the MVP dashboard services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine

from mevo2glpi_database import (
    AudioExtension,
    AudioRetention,
    AudioRetentionRepository,
    ErrorScope,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
    PendingJob,
    RetentionPolicy,
    RetentionStatus,
    TicketCreationJob,
    TicketCreationJobEventRepository,
    TicketCreationJobRepository,
    create_session_factory,
)

pytestmark = pytest.mark.database

NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)


def test_job_keyset_pagination_is_stable_with_tied_timestamps(
    migrated_engine: Engine,
) -> None:
    uuids = tuple(
        UUID(f"00000000-0000-0000-0000-{index:012d}") for index in range(1, 5)
    )
    with migrated_engine.begin() as connection:
        connection.execute(
            TicketCreationJob.__table__.insert(),
            [
                _job_values(uuids[0], updated_at=NOW),
                _job_values(uuids[1], updated_at=NOW),
                _job_values(
                    uuids[2],
                    updated_at=NOW - timedelta(minutes=1),
                    status=JobStatus.FAILED,
                ),
                _job_values(uuids[3], updated_at=NOW - timedelta(minutes=2)),
            ],
        )

    session_factory = create_session_factory(migrated_engine)
    with session_factory() as session:
        repository = TicketCreationJobRepository(session)
        first_page = repository.list_page(limit=2)
        assert first_page.next_cursor is not None
        second_page = repository.list_page(
            limit=2,
            cursor=first_page.next_cursor,
        )
        failed_page = repository.list_page(limit=25, status=JobStatus.FAILED)
        exact_page = repository.list_page(limit=25, job_uuid=uuids[3])

    assert tuple(item.uuid for item in first_page.items) == (uuids[1], uuids[0])
    assert first_page.has_more is True
    assert tuple(item.uuid for item in second_page.items) == (uuids[2], uuids[3])
    assert second_page.has_more is False
    assert second_page.next_cursor is None
    assert tuple(item.uuid for item in failed_page.items) == (uuids[2],)
    assert tuple(item.uuid for item in exact_page.items) == (uuids[3],)


def test_event_keyset_pagination_is_stable_with_tied_timestamps(
    migrated_engine: Engine,
) -> None:
    job_uuid = UUID("00000000-0000-0000-0000-000000000010")
    session_factory = create_session_factory(migrated_engine)
    with session_factory.begin() as session:
        TicketCreationJobRepository(session).add_pending(
            PendingJob(
                uuid=job_uuid,
                storage_key=f"{job_uuid}.wav",
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=128,
            )
        )
        event_repository = TicketCreationJobEventRepository(session)
        for event_type in (
            JobEventType.JOB_CREATED,
            JobEventType.JOB_FAILED,
            JobEventType.JOB_BLOCKED,
            JobEventType.OPERATOR_RETRY_REQUESTED,
        ):
            event_repository.append(
                job_uuid,
                occurred_at=NOW,
                status=JobStatus.PENDING,
                stage=None,
                event_type=event_type,
                actor=JobEventActor.WEB,
            )

    with session_factory() as session:
        repository = TicketCreationJobEventRepository(session)
        first_page = repository.list_page(job_uuid, limit=2)
        assert first_page.next_cursor is not None
        second_page = repository.list_page(
            job_uuid,
            limit=2,
            cursor=first_page.next_cursor,
        )

    first_ids = tuple(item.event_id for item in first_page.items)
    second_ids = tuple(item.event_id for item in second_page.items)
    assert first_ids == tuple(sorted(first_ids, reverse=True))
    assert second_ids == tuple(sorted(second_ids, reverse=True))
    assert len(set((*first_ids, *second_ids))) == 4
    assert first_page.has_more is True
    assert second_page.has_more is False


def test_operator_retry_transitions_commit_under_database_constraints(
    migrated_engine: Engine,
) -> None:
    job_uuid = UUID("00000000-0000-0000-0000-000000000020")
    session_factory = create_session_factory(migrated_engine)
    with session_factory.begin() as session:
        TicketCreationJobRepository(session).add_pending(
            PendingJob(
                uuid=job_uuid,
                storage_key=f"{job_uuid}.wav",
                audio_extension=AudioExtension.WAV,
                audio_size_bytes=128,
            )
        )
        job = session.get(TicketCreationJob, job_uuid)
        assert job is not None
        job.status = JobStatus.BLOCKED.value
        job.stage = JobStage.TRANSCRIPTION.value
        job.last_error_scope = ErrorScope.JOB.value
        job.last_error_category = "operator_required"
        job.last_error_code = "ambiguous_result"
        job.finished_at = NOW
        AudioRetentionRepository(session).activate(
            job_uuid,
            policy=RetentionPolicy.DEADLINE_ONLY,
            hard_delete_at=NOW + timedelta(days=30),
            next_check_at=None,
            now=NOW,
        )

    retry_time = NOW + timedelta(minutes=1)
    with session_factory.begin() as session:
        job_snapshot = TicketCreationJobRepository(session).rearm_for_operator_retry(
            job_uuid, now=retry_time
        )
        retention_snapshot = AudioRetentionRepository(
            session
        ).deactivate_for_operator_retry(job_uuid, now=retry_time)

    assert job_snapshot.status is JobStatus.PENDING
    assert job_snapshot.stage is None
    assert job_snapshot.finished_at is None
    assert retention_snapshot.status is RetentionStatus.INACTIVE
    assert retention_snapshot.hard_delete_at is None
    with session_factory() as session:
        persisted_retention = session.get(AudioRetention, job_uuid)
        assert persisted_retention is not None
        assert persisted_retention.status == RetentionStatus.INACTIVE.value


def _job_values(
    job_uuid: UUID,
    *,
    updated_at: datetime,
    status: JobStatus = JobStatus.PENDING,
) -> dict[str, object]:
    failed = status is JobStatus.FAILED
    return {
        "uuid": job_uuid,
        "status": status.value,
        "stage": JobStage.TRANSCRIPTION.value if failed else None,
        "storage_key": f"{job_uuid}.wav",
        "audio_extension": AudioExtension.WAV.value,
        "audio_size_bytes": 128,
        "last_error_scope": ErrorScope.JOB.value if failed else None,
        "last_error_category": "terminal" if failed else None,
        "last_error_code": "invalid_audio" if failed else None,
        "created_at": NOW - timedelta(hours=1),
        "updated_at": updated_at,
        "finished_at": updated_at if failed else None,
    }
