"""Focused contracts for the HTTP controller layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException, Response

import glpi_ticketing_web.controllers.jobs as jobs_controller_module
from glpi_ticketing_database import (
    AudioExtension,
    JobEventActor,
    JobEventType,
    JobStage,
    JobStatus,
)
from glpi_ticketing_web.controllers import JobController, ManagedStreamingResponse
from glpi_ticketing_web.services import (
    AudioStorageUnavailableError,
    DownloadableAudio,
    InvalidCursorError,
    JobEventListPage,
    JobEventView,
    JobListPage,
    JobRetryConflictError,
    RetriedJob,
)

AUDIO_UUID = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")


@dataclass
class FakeDownloadService:
    downloadable: DownloadableAudio | Exception

    def execute(self, _audio_uuid: UUID) -> DownloadableAudio:
        if isinstance(self.downloadable, Exception):
            raise self.downloadable
        return self.downloadable


class FakeRetryService:
    def __init__(self, result: RetriedJob | None | Exception) -> None:
        self.result = result

    def execute(self, _audio_uuid: UUID) -> RetriedJob | None:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeListService:
    def __init__(self, result: JobListPage | Exception) -> None:
        self.result = result

    def execute(self, **_kwargs: object) -> JobListPage:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    ("event_type", "actor"),
    [
        (
            JobEventType.RETENTION_RETRY_SCHEDULED,
            JobEventActor.WORKER_RETENTION,
        ),
        (JobEventType.RETENTION_BLOCKED, JobEventActor.WORKER_RETENTION),
        (JobEventType.AUDIO_DELETION_STARTED, JobEventActor.WORKER_RETENTION),
        (JobEventType.AUDIO_DELETED, JobEventActor.WORKER_RETENTION),
        (JobEventType.JOB_CREATED, JobEventActor.WORKER_PUBLISHER),
    ],
)
def test_event_history_serializes_supported_event_actors(
    event_type: JobEventType,
    actor: JobEventActor,
) -> None:
    event = JobEventView(
        event_id=1,
        event_type=event_type,
        occurred_at=datetime(2026, 8, 29, tzinfo=UTC),
        state=JobStatus.COMPLETED,
        stage=JobStage.FINALIZATION,
        actor=actor,
        error_scope=None,
        error_category=None,
        technical_code=None,
    )
    event_service = SimpleNamespace(
        execute=lambda *_args, **_kwargs: JobEventListPage(
            items=(event,),
            next_cursor=None,
        )
    )
    controller = JobController(
        submit_audio_job=object(),  # type: ignore[arg-type]
        list_audio_jobs=object(),  # type: ignore[arg-type]
        get_audio_job=object(),  # type: ignore[arg-type]
        list_audio_job_events=event_service,  # type: ignore[arg-type]
        retry_audio_job=object(),  # type: ignore[arg-type]
        get_downloadable_audio=object(),  # type: ignore[arg-type]
    )

    response = controller.list_job_events(AUDIO_UUID, limit=25, cursor=None)

    assert response.model_dump(mode="json") == {
        "items": [
            {
                "event_id": 1,
                "event_type": event_type.value,
                "occurred_at": "2026-08-29T00:00:00Z",
                "state": "completed",
                "stage": "finalization",
                "actor": actor.value,
                "error_scope": None,
                "error_category": None,
                "technical_code": None,
            }
        ],
        "next_cursor": None,
    }


def test_download_closes_audio_if_response_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_stream = BytesIO(b"synthetic-audio")
    downloadable = DownloadableAudio(
        uuid=AUDIO_UUID,
        extension=AudioExtension.WAV,
        size_bytes=len(audio_stream.getvalue()),
        _stream=audio_stream,
    )
    controller = JobController(
        submit_audio_job=object(),  # type: ignore[arg-type]
        list_audio_jobs=object(),  # type: ignore[arg-type]
        get_audio_job=object(),  # type: ignore[arg-type]
        list_audio_job_events=object(),  # type: ignore[arg-type]
        retry_audio_job=object(),  # type: ignore[arg-type]
        get_downloadable_audio=FakeDownloadService(downloadable),  # type: ignore[arg-type]
    )

    def fail_response(*_args, **_kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(
        jobs_controller_module,
        "ManagedStreamingResponse",
        fail_response,
    )

    with pytest.raises(RuntimeError, match="response construction failed"):
        controller.download_audio(AUDIO_UUID)

    assert audio_stream.closed


def test_retry_returns_202_pending_and_job_location() -> None:
    controller = _controller(
        retry_service=FakeRetryService(
            RetriedJob(audio_uuid=AUDIO_UUID, state=JobStatus.PENDING)
        )
    )
    response = Response()

    body = controller.retry_job(
        _request(),  # type: ignore[arg-type]
        response,
        AUDIO_UUID,
    )

    assert response.status_code == 202
    assert response.headers["location"] == f"/api/jobs/{AUDIO_UUID}"
    assert body.model_dump(mode="json") == {
        "audio_uuid": str(AUDIO_UUID),
        "state": "pending",
    }


@pytest.mark.parametrize(
    ("service_result", "expected_status"),
    [
        (None, 404),
        (JobRetryConflictError("private detail"), 409),
        (AudioStorageUnavailableError("private volume failure"), 503),
    ],
)
def test_retry_translates_unknown_and_conflicting_jobs(
    service_result: RetriedJob | None | Exception,
    expected_status: int,
) -> None:
    controller = _controller(retry_service=FakeRetryService(service_result))

    with pytest.raises(HTTPException) as captured_error:
        controller.retry_job(
            _request(),  # type: ignore[arg-type]
            Response(),
            AUDIO_UUID,
        )

    assert captured_error.value.status_code == expected_status
    assert "private detail" not in str(captured_error.value.detail)
    assert "private volume failure" not in str(captured_error.value.detail)


def test_download_translates_storage_unavailability_to_503() -> None:
    controller = JobController(
        submit_audio_job=object(),  # type: ignore[arg-type]
        list_audio_jobs=object(),  # type: ignore[arg-type]
        get_audio_job=object(),  # type: ignore[arg-type]
        list_audio_job_events=object(),  # type: ignore[arg-type]
        retry_audio_job=object(),  # type: ignore[arg-type]
        get_downloadable_audio=FakeDownloadService(
            AudioStorageUnavailableError("private volume failure")
        ),  # type: ignore[arg-type]
    )

    with pytest.raises(HTTPException) as captured_error:
        controller.download_audio(AUDIO_UUID)

    assert captured_error.value.status_code == 503
    assert "private volume failure" not in str(captured_error.value.detail)


def test_invalid_list_cursor_is_translated_to_422() -> None:
    controller = _controller(
        list_service=FakeListService(InvalidCursorError("private cursor payload"))
    )

    with pytest.raises(HTTPException) as captured_error:
        controller.list_jobs(
            state=None,
            audio_uuid=None,
            limit=25,
            cursor="opaque",
        )

    assert captured_error.value.status_code == 422
    assert "private cursor payload" not in str(captured_error.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_message_number", [1, 2])
async def test_managed_stream_closes_audio_when_asgi_send_fails(
    failing_message_number: int,
) -> None:
    audio_stream = BytesIO(b"synthetic-audio")
    downloadable = DownloadableAudio(
        uuid=AUDIO_UUID,
        extension=AudioExtension.WAV,
        size_bytes=len(audio_stream.getvalue()),
        _stream=audio_stream,
    )

    async def stream_audio() -> AsyncIterator[bytes]:
        # Keep this response-lifecycle test independent from AnyIO's sync
        # iterator threadpool. The real synchronous download path is exercised
        # by the web and database integration tests.
        for chunk in downloadable.iter_bytes():
            yield chunk

    response = ManagedStreamingResponse(
        stream_audio(),
        close_callback=downloadable.close,
    )
    sent_messages = 0

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        nonlocal sent_messages
        sent_messages += 1
        if sent_messages == failing_message_number:
            raise RuntimeError("ASGI send failed")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/audios/{AUDIO_UUID}",
        "raw_path": f"/audios/{AUDIO_UUID}".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    with pytest.raises(RuntimeError, match="ASGI send failed"):
        await response(scope, receive, send)  # type: ignore[arg-type]

    assert audio_stream.closed


def _controller(
    *,
    retry_service: FakeRetryService | None = None,
    list_service: FakeListService | None = None,
) -> JobController:
    return JobController(
        submit_audio_job=object(),  # type: ignore[arg-type]
        list_audio_jobs=list_service
        or FakeListService(JobListPage(items=(), next_cursor=None)),  # type: ignore[arg-type]
        get_audio_job=object(),  # type: ignore[arg-type]
        list_audio_job_events=object(),  # type: ignore[arg-type]
        retry_audio_job=retry_service or FakeRetryService(None),  # type: ignore[arg-type]
        get_downloadable_audio=object(),  # type: ignore[arg-type]
    )


def _request() -> SimpleNamespace:
    class FakeApp:
        def url_path_for(self, _name: str, *, audio_uuid: str) -> str:
            return f"/api/jobs/{audio_uuid}"

    return SimpleNamespace(app=FakeApp())
