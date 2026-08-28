"""PostgreSQL and filesystem integration tests for persistent ingestion."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select, text
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
    ListAudioJobEvents,
    ListAudioJobs,
    RetryAudioJob,
    SubmitAudioJob,
)
from glpi_ticketing_web.web import create_app

pytestmark = pytest.mark.database

SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"
MAX_UPLOAD_BYTES = 64


@dataclass(frozen=True, slots=True)
class IngestionContext:
    client: TestClient
    audio_root: Path
    session_factory: sessionmaker[Session]
    engine: Engine


@pytest.fixture
def ingestion_context(
    migrated_engine: Engine,
    tmp_path: Path,
) -> Iterator[IngestionContext]:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    session_factory = create_session_factory(migrated_engine)
    get_audio_job = GetAudioJob(session_factory)
    app = create_app(
        audio_root=audio_root,
        submit_audio_job=SubmitAudioJob(
            audio_root=audio_root,
            session_factory=session_factory,
            max_upload_bytes=MAX_UPLOAD_BYTES,
        ),
        list_audio_jobs=ListAudioJobs(session_factory),
        get_audio_job=get_audio_job,
        list_audio_job_events=ListAudioJobEvents(session_factory),
        retry_audio_job=RetryAudioJob(
            audio_root=audio_root,
            session_factory=session_factory,
            get_audio_job=get_audio_job,
        ),
        get_downloadable_audio=GetDownloadableAudio(audio_root, get_audio_job),
        max_upload_bytes=MAX_UPLOAD_BYTES,
    )
    with TestClient(app) as client:
        yield IngestionContext(
            client=client,
            audio_root=audio_root,
            session_factory=session_factory,
            engine=migrated_engine,
        )


def test_create_job_persists_audio_job_and_initial_event(
    ingestion_context: IngestionContext,
) -> None:
    response = _upload(ingestion_context, "patient recording.WAV", SYNTHETIC_AUDIO)
    audio_uuid = UUID(response.json()["audio_uuid"])

    assert response.status_code == 202
    assert response.json() == {
        "audio_uuid": str(audio_uuid),
        "state": "pending",
    }
    assert response.headers["location"] == f"/api/jobs/{audio_uuid}"
    detail = ingestion_context.client.get(response.headers["location"]).json()
    assert detail["audio_uuid"] == str(audio_uuid)
    assert detail["state"] == "pending"
    assert detail["stage"] is None
    assert detail["retention_status"] is None
    assert "storage_key" not in detail
    assert "transcription_provider_job_id" not in detail

    with ingestion_context.session_factory() as session:
        job = session.get(TicketCreationJob, audio_uuid)
        events = session.scalars(
            select(TicketCreationJobEvent).where(
                TicketCreationJobEvent.job_uuid == audio_uuid
            )
        ).all()

    assert job is not None
    assert job.status == "pending"
    assert job.stage is None
    assert job.storage_key == f"{audio_uuid}.wav"
    assert job.audio_extension == "wav"
    assert job.audio_size_bytes == len(SYNTHETIC_AUDIO)
    assert len(events) == 1
    assert events[0].status == "pending"
    assert events[0].stage is None
    assert events[0].event_type == "job_created"
    assert events[0].actor == "web"
    assert (ingestion_context.audio_root / job.storage_key).read_bytes() == (
        SYNTHETIC_AUDIO
    )
    assert not list(ingestion_context.audio_root.glob("*.part"))
    assert "patient recording" not in job.storage_key


def test_audio_is_published_before_database_insert(
    ingestion_context: IngestionContext,
) -> None:
    observations: list[bytes] = []

    def observe_first_job_insert(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if statement.lstrip().startswith("INSERT INTO ticket_creation_jobs"):
            audio_files = [
                path
                for path in ingestion_context.audio_root.iterdir()
                if not path.name.endswith(".part")
            ]
            assert len(audio_files) == 1
            observations.append(audio_files[0].read_bytes())

    event.listen(
        ingestion_context.engine,
        "before_cursor_execute",
        observe_first_job_insert,
    )
    try:
        response = _upload(ingestion_context, "recording.wav", SYNTHETIC_AUDIO)
    finally:
        event.remove(
            ingestion_context.engine,
            "before_cursor_execute",
            observe_first_job_insert,
        )

    assert response.status_code == 202
    assert observations == [SYNTHETIC_AUDIO]


def test_two_uploads_create_distinct_consistent_jobs(
    ingestion_context: IngestionContext,
) -> None:
    first = _upload(ingestion_context, "first.wav", SYNTHETIC_AUDIO)
    second = _upload(ingestion_context, "second.mp3", b"ID3synthetic")
    first_uuid = UUID(first.json()["audio_uuid"])
    second_uuid = UUID(second.json()["audio_uuid"])

    with ingestion_context.session_factory() as session:
        jobs = session.scalars(
            select(TicketCreationJob).order_by(TicketCreationJob.uuid)
        ).all()
        events_count = session.scalar(
            select(func.count()).select_from(TicketCreationJobEvent)
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first_uuid != second_uuid
    assert len(jobs) == 2
    assert events_count == 2
    assert {job.uuid for job in jobs} == {first_uuid, second_uuid}
    assert {path.name for path in ingestion_context.audio_root.iterdir()} == {
        f"{first_uuid}.wav",
        f"{second_uuid}.mp3",
    }


@pytest.mark.parametrize(
    ("filename", "content", "expected_status"),
    [
        ("recording.flac", SYNTHETIC_AUDIO, 415),
        ("recording.wav", b"", 422),
        ("recording.wav", b"x" * (MAX_UPLOAD_BYTES + 1), 413),
    ],
)
def test_invalid_upload_never_creates_database_or_storage_state(
    ingestion_context: IngestionContext,
    filename: str,
    content: bytes,
    expected_status: int,
) -> None:
    response = _upload(ingestion_context, filename, content)

    with ingestion_context.session_factory() as session:
        jobs_count = session.scalar(select(func.count()).select_from(TicketCreationJob))
        events_count = session.scalar(
            select(func.count()).select_from(TicketCreationJobEvent)
        )

    assert response.status_code == expected_status
    assert jobs_count == 0
    assert events_count == 0
    assert not list(ingestion_context.audio_root.iterdir())


def test_event_insert_failure_rolls_back_job_and_compensates_audio(
    ingestion_context: IngestionContext,
) -> None:
    with ingestion_context.engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE ticket_creation_job_events "
                "ADD CONSTRAINT ck_test_reject_job_created "
                "CHECK (event_type <> 'job_created')"
            )
        )

    response = _upload(ingestion_context, "recording.wav", SYNTHETIC_AUDIO)

    with ingestion_context.session_factory() as session:
        jobs_count = session.scalar(select(func.count()).select_from(TicketCreationJob))
        events_count = session.scalar(
            select(func.count()).select_from(TicketCreationJobEvent)
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Audio job submission is temporarily unavailable"
    }
    assert jobs_count == 0
    assert events_count == 0
    assert not list(ingestion_context.audio_root.iterdir())


def test_completed_job_audio_is_downloaded_by_canonical_key(
    ingestion_context: IngestionContext,
) -> None:
    audio_uuid = uuid4()
    storage_key = f"{audio_uuid}.ogg"
    now = datetime.now(UTC)
    with ingestion_context.session_factory.begin() as session:
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
    (ingestion_context.audio_root / storage_key).write_bytes(SYNTHETIC_AUDIO)

    response = ingestion_context.client.get(f"/audios/{audio_uuid}")

    assert response.status_code == 200
    assert response.content == SYNTHETIC_AUDIO
    assert response.headers["content-type"] == "audio/ogg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{audio_uuid}.ogg"'
    )


def test_job_dashboard_uses_stable_keyset_pagination_and_exact_filters(
    ingestion_context: IngestionContext,
) -> None:
    audio_uuids = [
        UUID(
            _upload(ingestion_context, f"audio-{index}.wav", SYNTHETIC_AUDIO).json()[
                "audio_uuid"
            ]
        )
        for index in range(3)
    ]
    base_time = datetime(2026, 8, 28, 12, tzinfo=UTC)
    with ingestion_context.session_factory.begin() as session:
        for index, audio_uuid in enumerate(audio_uuids):
            job = session.get(TicketCreationJob, audio_uuid)
            assert job is not None
            job.updated_at = base_time + timedelta(minutes=index)

    first = ingestion_context.client.get("/api/jobs", params={"limit": 2})
    second = ingestion_context.client.get(
        "/api/jobs",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    filtered = ingestion_context.client.get(
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
    ingestion_context: IngestionContext,
) -> None:
    first_uuid = UUID(
        _upload(ingestion_context, "first.wav", SYNTHETIC_AUDIO).json()["audio_uuid"]
    )
    second_uuid = UUID(
        _upload(ingestion_context, "second.wav", SYNTHETIC_AUDIO).json()["audio_uuid"]
    )

    response = ingestion_context.client.get(
        f"/api/jobs/{first_uuid}/events",
        params={"limit": 1},
    )
    unknown = ingestion_context.client.get(f"/api/jobs/{uuid4()}/events")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["event_type"] == "job_created"
    assert str(second_uuid) not in response.text
    assert unknown.status_code == 404


def test_operator_retry_is_atomic_and_preserves_ticket_and_attempt_history(
    ingestion_context: IngestionContext,
) -> None:
    audio_uuid = UUID(
        _upload(ingestion_context, "failed.wav", SYNTHETIC_AUDIO).json()["audio_uuid"]
    )
    now = datetime.now(UTC)
    _make_retryable(ingestion_context, audio_uuid, now=now)

    response = ingestion_context.client.post(f"/api/jobs/{audio_uuid}/retry")

    with ingestion_context.session_factory() as session:
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
    ingestion_context: IngestionContext,
) -> None:
    audio_uuid = UUID(
        _upload(ingestion_context, "failed.wav", SYNTHETIC_AUDIO).json()["audio_uuid"]
    )
    _make_retryable(ingestion_context, audio_uuid, now=datetime.now(UTC))
    with ingestion_context.engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE ticket_creation_job_events "
                "ADD CONSTRAINT ck_test_reject_operator_retry "
                "CHECK (event_type <> 'operator_retry_requested')"
            )
        )

    response = ingestion_context.client.post(f"/api/jobs/{audio_uuid}/retry")

    with ingestion_context.session_factory() as session:
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
    ingestion_context: IngestionContext,
) -> None:
    audio_uuid = UUID(
        _upload(ingestion_context, "failed.wav", SYNTHETIC_AUDIO).json()["audio_uuid"]
    )
    now = datetime.now(UTC)
    _make_retryable(ingestion_context, audio_uuid, now=now)
    deletion_started_at = now - timedelta(minutes=5)
    retry_at = now + timedelta(minutes=5)
    with ingestion_context.session_factory.begin() as session:
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

    response = ingestion_context.client.post(f"/api/jobs/{audio_uuid}/retry")

    with ingestion_context.session_factory() as session:
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
    ingestion_context: IngestionContext,
) -> None:
    audio_uuid = UUID(
        _upload(ingestion_context, "completed.wav", SYNTHETIC_AUDIO).json()[
            "audio_uuid"
        ]
    )
    now = datetime.now(UTC)
    with ingestion_context.session_factory.begin() as session:
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

    response = ingestion_context.client.get(f"/audios/{audio_uuid}")

    assert response.status_code == 410
    assert response.json() == {"detail": "Audio file has been purged"}


def _make_retryable(
    context: IngestionContext,
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


def _upload(context: IngestionContext, filename: str, content: bytes):
    return context.client.post(
        "/api/jobs",
        files={"audio_file": (filename, content, "application/octet-stream")},
    )
