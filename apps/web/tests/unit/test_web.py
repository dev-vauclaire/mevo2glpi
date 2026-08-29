from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import glpi_ticketing_web.services.jobs.downloads as jobs_module
from glpi_ticketing_database import (
    AudioExtension,
    JobStage,
    JobStatus,
    RetentionStatus,
)
from glpi_ticketing_web.services import (
    AudioStorageConflictError,
    AudioStorageUnavailableError,
    EmptyAudioError,
    GetDownloadableAudio,
    JobEventListPage,
    JobListPage,
    JobPersistenceUnavailableError,
    JobQueryUnavailableError,
    JobView,
    ObservabilitySnapshot,
    RetriedJob,
    SubmittedJob,
    UnsupportedAudioUploadError,
    UploadTooLargeError,
)
from glpi_ticketing_web.web import create_app

SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"
MAX_UPLOAD_BYTES = 64


@dataclass
class FakeSubmitAudioJob:
    results: list[SubmittedJob | Exception]
    uploads: list[tuple[str | None, bytes]] = field(default_factory=list)
    streams: list[BinaryIO] = field(default_factory=list)
    prevalidated_filenames: list[str | None] = field(default_factory=list)

    def prevalidate(self, filename: str | None) -> str:
        self.prevalidated_filenames.append(filename)
        return Path(filename).suffix.removeprefix(".").lower() if filename else ""

    def execute(self, filename: str | None, source: BinaryIO) -> SubmittedJob:
        self.streams.append(source)
        self.uploads.append((filename, source.read()))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass
class FakeGetAudioJob:
    snapshots: dict[UUID, JobView] = field(default_factory=dict)
    error: Exception | None = None

    def execute(self, audio_uuid: UUID) -> JobView | None:
        if self.error is not None:
            raise self.error
        return self.snapshots.get(audio_uuid)


class FakeListAudioJobs:
    def execute(self, **_kwargs: object) -> JobListPage:
        return JobListPage(items=(), next_cursor=None)


class FakeListAudioJobEvents:
    def execute(self, _audio_uuid: UUID, **_kwargs: object) -> JobEventListPage:
        return JobEventListPage(items=(), next_cursor=None)


class FakeRetryAudioJob:
    def execute(self, audio_uuid: UUID) -> RetriedJob:
        return RetriedJob(audio_uuid=audio_uuid, state=JobStatus.PENDING)


class FakeGetObservability:
    def execute(self) -> ObservabilitySnapshot:
        return ObservabilitySnapshot(
            observed_at=datetime(2026, 8, 29, tzinfo=UTC),
            workers=(),
            alerts=(),
        )


@dataclass(frozen=True, slots=True)
class WebTestContext:
    client: TestClient
    audio_root: Path
    submit: FakeSubmitAudioJob
    query: FakeGetAudioJob


@pytest.fixture
def web_context(tmp_path: Path) -> Iterator[WebTestContext]:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    submit = FakeSubmitAudioJob(
        results=[SubmittedJob(uuid4()), SubmittedJob(uuid4()), SubmittedJob(uuid4())]
    )
    query = FakeGetAudioJob()
    app = create_app(
        audio_root=audio_root,
        submit_audio_job=submit,
        list_audio_jobs=FakeListAudioJobs(),
        get_audio_job=query,
        list_audio_job_events=FakeListAudioJobEvents(),
        retry_audio_job=FakeRetryAudioJob(),
        get_downloadable_audio=GetDownloadableAudio(audio_root, query),
        get_observability=FakeGetObservability(),  # type: ignore[arg-type]
        max_upload_bytes=MAX_UPLOAD_BYTES,
    )
    with TestClient(app) as client:
        yield WebTestContext(
            client=client,
            audio_root=audio_root,
            submit=submit,
            query=query,
        )


def test_health_returns_200(web_context: WebTestContext) -> None:
    response = web_context.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_page_references_versioned_static_assets(
    web_context: WebTestContext,
) -> None:
    response = web_context.client.get("/")

    assert response.status_code == 200
    assert 'href="http://testserver/static/css/app.css"' in response.text
    assert 'src="http://testserver/static/js/app.js"' in response.text
    assert web_context.client.get("/static/css/app.css").status_code == 200
    assert web_context.client.get("/static/js/app.js").status_code == 200


def test_api_upload_returns_202_uuid_state_and_resolvable_location(
    web_context: WebTestContext,
) -> None:
    expected_uuid = web_context.submit.results[0].audio_uuid

    response = _upload_audio(
        web_context.client,
        "/api/jobs",
        "recording.wav",
        SYNTHETIC_AUDIO,
    )

    assert response.status_code == 202
    assert response.json() == {
        "audio_uuid": str(expected_uuid),
        "state": "pending",
    }
    assert response.headers["location"] == f"/api/jobs/{expected_uuid}"
    assert web_context.submit.uploads == [("recording.wav", SYNTHETIC_AUDIO)]
    assert web_context.submit.prevalidated_filenames == ["recording.wav"]
    assert web_context.submit.streams[0].closed


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (UnsupportedAudioUploadError("secret"), 415),
        (UploadTooLargeError("secret"), 413),
        (EmptyAudioError("secret"), 422),
        (AudioStorageConflictError("secret"), 409),
        (AudioStorageUnavailableError("secret"), 503),
        (JobPersistenceUnavailableError("database_write_failed"), 503),
    ],
)
def test_upload_errors_are_sanitized(
    tmp_path: Path,
    error: Exception,
    expected_status: int,
) -> None:
    audio_root = tmp_path / "audios"
    audio_root.mkdir()
    query = FakeGetAudioJob()
    app = create_app(
        audio_root=audio_root,
        submit_audio_job=FakeSubmitAudioJob(results=[error]),
        list_audio_jobs=FakeListAudioJobs(),
        get_audio_job=query,
        list_audio_job_events=FakeListAudioJobEvents(),
        retry_audio_job=FakeRetryAudioJob(),
        get_downloadable_audio=GetDownloadableAudio(audio_root, query),
        get_observability=FakeGetObservability(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = _upload_audio(client, "/api/jobs", "recording.wav", b"audio")

    assert response.status_code == expected_status
    assert "secret" not in response.text


def test_api_upload_requires_multipart_audio_file(web_context: WebTestContext) -> None:
    response = web_context.client.post("/api/jobs")

    assert response.status_code == 422
    assert not web_context.submit.uploads


def test_minimal_job_detail_returns_persisted_state(
    web_context: WebTestContext,
) -> None:
    snapshot = _job_snapshot(status=JobStatus.PROCESSING)
    web_context.query.snapshots[snapshot.audio_uuid] = snapshot

    response = web_context.client.get(f"/api/jobs/{snapshot.audio_uuid}")

    assert response.status_code == 200
    body = response.json()
    assert body["audio_uuid"] == str(snapshot.audio_uuid)
    assert body["state"] == "processing"
    assert body["stage"] == "transcription"
    assert body["attempt_count"] == 0
    assert body["retention_status"] is None
    assert "storage_key" not in body
    assert "transcription_provider_job_id" not in body


def test_job_detail_returns_404_for_unknown_uuid(web_context: WebTestContext) -> None:
    response = web_context.client.get(f"/api/jobs/{uuid4()}")

    assert response.status_code == 404


def test_job_query_unavailability_is_sanitized(
    web_context: WebTestContext,
) -> None:
    web_context.query.error = JobQueryUnavailableError("secret database URL")

    response = web_context.client.get(f"/api/jobs/{uuid4()}")

    assert response.status_code == 503
    assert "secret database URL" not in response.text


def test_download_returns_completed_audio_from_its_storage_key(
    web_context: WebTestContext,
) -> None:
    snapshot = _job_snapshot(
        status=JobStatus.COMPLETED,
        extension=AudioExtension.MP3,
    )
    web_context.query.snapshots[snapshot.audio_uuid] = snapshot
    completed_audio = web_context.audio_root / _storage_key(snapshot)
    completed_audio.write_bytes(SYNTHETIC_AUDIO)

    response = web_context.client.get(f"/audios/{snapshot.audio_uuid}")

    assert response.status_code == 200
    assert response.content == SYNTHETIC_AUDIO
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-audio-uuid"] == str(snapshot.audio_uuid)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{snapshot.audio_uuid}.mp3"'
    )


def test_download_survives_concurrent_audio_deletion(
    web_context: WebTestContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _job_snapshot(status=JobStatus.COMPLETED)
    web_context.query.snapshots[snapshot.audio_uuid] = snapshot
    audio_path = web_context.audio_root / _storage_key(snapshot)
    audio_path.write_bytes(SYNTHETIC_AUDIO)
    real_open_audio_stream = jobs_module._open_audio_stream

    def open_then_delete(path: Path) -> tuple[BinaryIO, int]:
        opened_audio = real_open_audio_stream(path)
        path.unlink()
        return opened_audio

    monkeypatch.setattr(jobs_module, "_open_audio_stream", open_then_delete)

    response = web_context.client.get(f"/audios/{snapshot.audio_uuid}")

    assert response.status_code == 200
    assert response.content == SYNTHETIC_AUDIO


def test_download_never_follows_a_path_replaced_after_open(
    web_context: WebTestContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _job_snapshot(status=JobStatus.COMPLETED)
    web_context.query.snapshots[snapshot.audio_uuid] = snapshot
    audio_path = web_context.audio_root / _storage_key(snapshot)
    audio_path.write_bytes(SYNTHETIC_AUDIO)
    outside_audio = web_context.audio_root.parent / "outside.wav"
    outside_audio.write_bytes(b"outside-secret-audio")
    real_open_audio_stream = jobs_module._open_audio_stream

    def open_then_replace(path: Path) -> tuple[BinaryIO, int]:
        opened_audio = real_open_audio_stream(path)
        path.unlink()
        path.symlink_to(outside_audio)
        return opened_audio

    monkeypatch.setattr(jobs_module, "_open_audio_stream", open_then_replace)

    response = web_context.client.get(f"/audios/{snapshot.audio_uuid}")

    assert response.status_code == 200
    assert response.content == SYNTHETIC_AUDIO
    assert response.content != outside_audio.read_bytes()


def test_download_rejects_fifo_without_waiting_for_a_writer(
    web_context: WebTestContext,
) -> None:
    snapshot = _job_snapshot(status=JobStatus.COMPLETED)
    web_context.query.snapshots[snapshot.audio_uuid] = snapshot
    os.mkfifo(web_context.audio_root / _storage_key(snapshot))

    response = web_context.client.get(f"/audios/{snapshot.audio_uuid}")

    assert response.status_code == 500
    assert response.json() == {"detail": "Audio storage invariant violated"}


@pytest.mark.parametrize(
    "job_status",
    [
        JobStatus.PENDING,
        JobStatus.PROCESSING,
        JobStatus.RETRY_SCHEDULED,
        JobStatus.FAILED,
        JobStatus.BLOCKED,
    ],
)
def test_download_refuses_every_non_completed_job(
    web_context: WebTestContext,
    job_status: JobStatus,
) -> None:
    snapshot = _job_snapshot(status=job_status)
    web_context.query.snapshots[snapshot.audio_uuid] = snapshot
    (web_context.audio_root / _storage_key(snapshot)).write_bytes(SYNTHETIC_AUDIO)

    response = web_context.client.get(f"/audios/{snapshot.audio_uuid}")

    assert response.status_code == 404
    assert response.content != SYNTHETIC_AUDIO


def test_download_rejects_invalid_uuid(web_context: WebTestContext) -> None:
    response = web_context.client.get("/audios/not-a-uuid")

    assert response.status_code == 422


def test_download_returns_404_for_unknown_audio(
    web_context: WebTestContext,
) -> None:
    response = web_context.client.get(f"/audios/{uuid4()}")

    assert response.status_code == 404
    assert "x-audio-uuid" not in response.headers


def test_web_exposes_no_directory_derived_state(web_context: WebTestContext) -> None:
    config = web_context.client.app.state.web_config
    openapi_paths = web_context.client.get("/openapi.json").json()["paths"]

    assert config.audio_root == web_context.audio_root.resolve()
    assert not hasattr(config, "pending_directory")
    assert not hasattr(config, "processing_directory")
    assert not hasattr(config, "completed_directory")
    assert not hasattr(config, "failed_directory")
    assert all("failed" not in path for path in openapi_paths)


def _upload_audio(
    client: TestClient,
    path: str,
    filename: str,
    content: bytes,
):
    return client.post(
        path,
        files={"audio_file": (filename, content, "application/octet-stream")},
    )


def _job_snapshot(
    *,
    status: JobStatus,
    extension: AudioExtension = AudioExtension.WAV,
) -> JobView:
    audio_uuid = uuid4()
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    stage = None if status is JobStatus.PENDING else JobStage.TRANSCRIPTION
    glpi_ticket_id = None
    finished_at = None
    if status is JobStatus.COMPLETED:
        stage = JobStage.FINALIZATION
        glpi_ticket_id = 42
        finished_at = now
    elif status in {JobStatus.FAILED, JobStatus.BLOCKED}:
        finished_at = now
    return JobView(
        audio_uuid=audio_uuid,
        state=status,
        stage=stage,
        audio_extension=extension,
        audio_size_bytes=len(SYNTHETIC_AUDIO),
        glpi_ticket_id=glpi_ticket_id,
        attempt_count=0,
        consecutive_retry_count=0,
        next_attempt_at=None,
        last_error_scope=None,
        last_error_category=None,
        last_error_code=None,
        created_at=now,
        first_started_at=None,
        updated_at=now,
        finished_at=finished_at,
        retention_status=(
            RetentionStatus.SCHEDULED if status is JobStatus.COMPLETED else None
        ),
        audio_deleted_at=None,
    )


def _storage_key(job: JobView) -> str:
    return f"{job.audio_uuid}.{job.audio_extension.value}"
