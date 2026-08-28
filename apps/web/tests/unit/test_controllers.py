"""Focused contracts for the HTTP controller layer."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.templating import Jinja2Templates

import glpi_ticketing_web.controllers.jobs as jobs_controller_module
from glpi_ticketing_database import AudioExtension
from glpi_ticketing_web.controllers import JobController, ManagedStreamingResponse
from glpi_ticketing_web.services import DownloadableAudio

AUDIO_UUID = UUID("8ce23f5b-4e31-49f1-ae23-4eacdbba48f8")


@dataclass
class FakeDownloadService:
    downloadable: DownloadableAudio

    def execute(self, _audio_uuid: UUID) -> DownloadableAudio:
        return self.downloadable


def test_download_closes_audio_if_response_construction_fails(
    tmp_path: Path,
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
        get_audio_job=object(),  # type: ignore[arg-type]
        get_downloadable_audio=FakeDownloadService(downloadable),  # type: ignore[arg-type]
        templates=Jinja2Templates(directory=tmp_path),
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
    response = ManagedStreamingResponse(
        downloadable.iter_bytes(),
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
