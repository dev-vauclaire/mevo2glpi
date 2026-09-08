"""PostgreSQL and filesystem integration tests for the read-only job API."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from glpi_ticketing_database import (
    AudioRetention,
    JobStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    create_session_factory,
)
from glpi_ticketing_web.services import (
    GetAudioJob,
    GetDownloadableAudio,
    GetObservability,
    ListAudioJobEvents,
    ListAudioJobs,
    ObservabilityThresholds,
    RetryAudioJob,
)
from glpi_ticketing_web.web import create_app

pytestmark = pytest.mark.database

SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"
OBSERVED_AT = datetime(2026, 8, 29, 12, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class JobApiContext:
    client: TestClient
    audio_root: Path
    session_factory: sessionmaker[Session]
    engine: Engine


@pytest.fixture
def job_api_context(
    migrated_engine: Engine,
    tmp_path: Path,
) -> Iterator[JobApiContext]:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    session_factory = create_session_factory(migrated_engine)
    get_audio_job = GetAudioJob(session_factory)
    thresholds = ObservabilityThresholds.from_seconds(
        worker_stale_after=45,
        pending_alert_after=900,
        retry_overdue_grace=60,
        retention_deadline_grace=120,
    )
    app = create_app(
        audio_root=audio_root,
        list_audio_jobs=ListAudioJobs(session_factory),
        get_audio_job=get_audio_job,
        list_audio_job_events=ListAudioJobEvents(session_factory),
        retry_audio_job=RetryAudioJob(
            audio_root=audio_root,
            session_factory=session_factory,
            get_audio_job=get_audio_job,
        ),
        get_downloadable_audio=GetDownloadableAudio(audio_root, get_audio_job),
        get_observability=GetObservability(
            session_factory,
            thresholds=thresholds,
            clock=lambda: OBSERVED_AT,
        ),
    )
    with TestClient(app) as client:
        yield JobApiContext(
            client=client,
            audio_root=audio_root,
            session_factory=session_factory,
            engine=migrated_engine,
        )


def test_job_collection_rejects_ingestion_without_creating_state(
    job_api_context: JobApiContext,
) -> None:
    response = job_api_context.client.post("/api/jobs")

    with job_api_context.session_factory() as session:
        jobs_count = session.scalar(select(func.count()).select_from(TicketCreationJob))
        events_count = session.scalar(
            select(func.count()).select_from(TicketCreationJobEvent)
        )

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert jobs_count == 0
    assert events_count == 0
    assert not list(job_api_context.audio_root.iterdir())


def test_completed_job_audio_is_downloaded_by_canonical_key(
    job_api_context: JobApiContext,
) -> None:
    audio_uuid = uuid4()
    storage_key = f"{audio_uuid}.ogg"
    now = datetime.now(UTC)
    with job_api_context.session_factory.begin() as session:
        job = TicketCreationJob(
            uuid=audio_uuid,
            status=JobStatus.COMPLETED.value,
            stage="finalization",
            storage_key=storage_key,
            audio_extension="ogg",
            audio_size_bytes=len(SYNTHETIC_AUDIO),
            glpi_ticket_id=42,
            finished_at=now,
        )
        session.add(job)
        session.flush()
        session.add(
            AudioRetention(
                job_uuid=audio_uuid,
                policy="ticket_or_deadline",
                status="scheduled",
                hard_delete_at=now + timedelta(days=30),
                next_check_at=now + timedelta(hours=6),
            )
        )
    (job_api_context.audio_root / storage_key).write_bytes(SYNTHETIC_AUDIO)

    response = job_api_context.client.get(f"/audios/{audio_uuid}")

    assert response.status_code == 200
    assert response.content == SYNTHETIC_AUDIO
    assert response.headers["content-type"] == "audio/ogg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{audio_uuid}.ogg"'
    )


def test_job_dashboard_uses_stable_keyset_pagination_and_exact_filters(
    job_api_context: JobApiContext,
) -> None:
    base_time = datetime(2026, 8, 28, 12, tzinfo=UTC)
    audio_uuids = [
        _seed_pending_job(
            job_api_context,
            occurred_at=base_time + timedelta(minutes=index),
        )
        for index in range(3)
    ]

    first = job_api_context.client.get("/api/jobs", params={"limit": 2})
    second = job_api_context.client.get(
        "/api/jobs",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    filtered = job_api_context.client.get(
        "/api/jobs",
        params={"state": "pending", "audio_uuid": str(audio_uuids[1])},
    )

    assert first.status_code == 200
    assert [UUID(item["audio_uuid"]) for item in first.json()["items"]] == [
        audio_uuids[2],
        audio_uuids[1],
    ]
    assert first.json()["next_cursor"] is not None
    assert [UUID(item["audio_uuid"]) for item in second.json()["items"]] == [
        audio_uuids[0]
    ]
    assert second.json()["next_cursor"] is None
    assert [item["audio_uuid"] for item in filtered.json()["items"]] == [
        str(audio_uuids[1])
    ]


def test_event_history_is_scoped_to_its_job_and_paginated_separately(
    job_api_context: JobApiContext,
) -> None:
    first_uuid = _seed_pending_job(job_api_context)
    second_uuid = _seed_pending_job(job_api_context)

    response = job_api_context.client.get(
        f"/api/jobs/{first_uuid}/events",
        params={"limit": 1},
    )
    unknown = job_api_context.client.get(f"/api/jobs/{uuid4()}/events")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["event_type"] == "job_created"
    assert response.json()["items"][0]["actor"] == "worker_publisher"
    assert str(second_uuid) not in response.text
    assert unknown.status_code == 404


def test_operator_retry_is_atomic_and_preserves_ticket_and_attempt_history(
    job_api_context: JobApiContext,
) -> None:
    audio_uuid = _seed_pending_job(job_api_context)
    now = datetime.now(UTC)
    _make_retryable(job_api_context, audio_uuid, now=now)

    response = job_api_context.client.post(f"/api/jobs/{audio_uuid}/retry")

    with job_api_context.session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        retention = session.get(AudioRetention, audio_uuid)
        events = session.scalars(
            select(TicketCreationJobEvent)
            .where(TicketCreationJobEvent.job_uuid == audio_uuid)
            .order_by(TicketCreationJobEvent.event_id)
        ).all()
    assert response.status_code == 202
    assert response.headers["location"] == f"/api/jobs/{audio_uuid}"
    assert response.json() == {"audio_uuid": str(audio_uuid), "state": "pending"}
    assert job is not None
    assert job.status == "pending"
    assert job.stage is None
    assert job.glpi_ticket_id == 42
    assert job.attempt_count == 5
    assert job.consecutive_retry_count == 0
    assert retention is not None
    assert retention.status == "inactive"
    assert retention.hard_delete_at is None
    assert events[-1].event_type == "operator_retry_requested"
    assert events[-1].actor == "web"


def test_retry_event_failure_rolls_back_job_and_retention(
    job_api_context: JobApiContext,
) -> None:
    audio_uuid = _seed_pending_job(job_api_context)
    _make_retryable(job_api_context, audio_uuid, now=datetime.now(UTC))
    with job_api_context.engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE ticket_creation_job_events "
                "ADD CONSTRAINT ck_test_reject_operator_retry "
                "CHECK (event_type <> 'operator_retry_requested')"
            )
        )

    response = job_api_context.client.post(f"/api/jobs/{audio_uuid}/retry")

    with job_api_context.session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        retention = session.get(AudioRetention, audio_uuid)
        operator_event_count = session.scalar(
            select(func.count())
            .select_from(TicketCreationJobEvent)
            .where(
                TicketCreationJobEvent.job_uuid == audio_uuid,
                TicketCreationJobEvent.event_type == "operator_retry_requested",
            )
        )
    assert response.status_code == 503
    assert job is not None
    assert job.status == "failed"
    assert job.glpi_ticket_id == 42
    assert retention is not None
    assert retention.status == "scheduled"
    assert operator_event_count == 0


def test_operator_retry_rejects_a_retention_whose_deletion_has_started(
    job_api_context: JobApiContext,
) -> None:
    audio_uuid = _seed_pending_job(job_api_context)
    now = datetime.now(UTC)
    _make_retryable(job_api_context, audio_uuid, now=now)
    deletion_started_at = now - timedelta(minutes=5)
    retry_at = now + timedelta(minutes=5)
    with job_api_context.session_factory.begin() as session:
        retention = session.get(AudioRetention, audio_uuid)
        assert retention is not None
        retention.status = "retry_scheduled"
        retention.hard_delete_at = now - timedelta(minutes=10)
        retention.next_check_at = retry_at
        retention.consecutive_retry_count = 1
        retention.deletion_reason = "failed_ttl"
        retention.deletion_started_at = deletion_started_at
        retention.last_error_category = "storage_unavailable"
        retention.last_error_code = "filesystem_busy"

    response = job_api_context.client.post(f"/api/jobs/{audio_uuid}/retry")

    with job_api_context.session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        retention = session.get(AudioRetention, audio_uuid)
    assert response.status_code == 409
    assert response.json() == {"detail": "Job cannot be retried"}
    assert job is not None
    assert job.status == "failed"
    assert retention is not None
    assert retention.status == "retry_scheduled"
    assert retention.next_check_at == retry_at
    assert retention.deletion_started_at == deletion_started_at


def test_deleted_retention_returns_410_even_while_bytes_still_exist(
    job_api_context: JobApiContext,
) -> None:
    audio_uuid = _seed_pending_job(job_api_context)
    now = datetime.now(UTC)
    with job_api_context.session_factory.begin() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        assert job is not None
        job.status = "completed"
        job.stage = "finalization"
        job.glpi_ticket_id = 42
        job.finished_at = now
        job.updated_at = now
        session.add(
            AudioRetention(
                job_uuid=audio_uuid,
                policy="ticket_or_deadline",
                status="deleted",
                hard_delete_at=now,
                deletion_reason="deadline_expired",
                deletion_started_at=now,
                audio_deleted_at=now,
                created_at=now,
                updated_at=now,
            )
        )

    response = job_api_context.client.get(f"/audios/{audio_uuid}")

    assert response.status_code == 410
    assert response.json() == {"detail": "Audio file has been purged"}


def _seed_pending_job(
    context: JobApiContext,
    *,
    occurred_at: datetime | None = None,
) -> UUID:
    audio_uuid = uuid4()
    timestamp = occurred_at or datetime.now(UTC)
    storage_key = f"{audio_uuid}.wav"
    with context.session_factory.begin() as session:
        session.add(
            TicketCreationJob(
                uuid=audio_uuid,
                status="pending",
                stage=None,
                storage_key=storage_key,
                audio_extension="wav",
                audio_size_bytes=len(SYNTHETIC_AUDIO),
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        session.flush()
        session.add(
            TicketCreationJobEvent(
                job_uuid=audio_uuid,
                occurred_at=timestamp,
                status="pending",
                stage=None,
                event_type="job_created",
                actor="worker_publisher",
            )
        )
    (context.audio_root / storage_key).write_bytes(SYNTHETIC_AUDIO)
    return audio_uuid


def _make_retryable(
    context: JobApiContext,
    audio_uuid: UUID,
    *,
    now: datetime,
) -> None:
    with context.session_factory.begin() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        assert job is not None
        job.status = "failed"
        job.stage = "finalization"
        job.glpi_ticket_id = 42
        job.attempt_count = 5
        job.consecutive_retry_count = 3
        job.last_error_scope = "job"
        job.last_error_category = "validation"
        job.last_error_code = "terminal_error"
        job.finished_at = now
        job.updated_at = now
        session.add(
            AudioRetention(
                job_uuid=audio_uuid,
                policy="deadline_only",
                status="scheduled",
                hard_delete_at=now + timedelta(days=7),
                created_at=now,
                updated_at=now,
            )
        )
