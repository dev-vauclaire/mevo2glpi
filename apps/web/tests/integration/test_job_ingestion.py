"""PostgreSQL and filesystem integration tests for persistent ingestion."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from glpi_ticketing_database import (
    JobStatus,
    TicketCreationJob,
    TicketCreationJobEvent,
    create_session_factory,
)
from glpi_ticketing_web.services import (
    GetAudioJob,
    GetDownloadableAudio,
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
        get_audio_job=get_audio_job,
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
    assert ingestion_context.client.get(response.headers["location"]).json() == {
        "audio_uuid": str(audio_uuid),
        "state": "pending",
        "stage": None,
    }

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
        session.add(
            TicketCreationJob(
                uuid=audio_uuid,
                status=JobStatus.COMPLETED.value,
                stage="finalization",
                storage_key=storage_key,
                audio_extension="ogg",
                audio_size_bytes=len(SYNTHETIC_AUDIO),
                glpi_ticket_id=42,
                finished_at=now,
            )
        )
    (ingestion_context.audio_root / storage_key).write_bytes(SYNTHETIC_AUDIO)

    response = ingestion_context.client.get(f"/audios/{audio_uuid}")

    assert response.status_code == 200
    assert response.content == SYNTHETIC_AUDIO
    assert response.headers["content-type"] == "audio/ogg"


def _upload(context: IngestionContext, filename: str, content: bytes):
    return context.client.post(
        "/api/jobs",
        files={"audio_file": (filename, content, "application/octet-stream")},
    )
