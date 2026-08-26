import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from glpi_ticketing_web import web
from glpi_ticketing_web.web import create_app

SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"
MAX_UPLOAD_BYTES = 64
UUID_ATTRIBUTE_PATTERN = re.compile(r'data-audio-uuid="([^"]+)"')


@dataclass(frozen=True, slots=True)
class WebTestContext:
    client: TestClient
    pending_directory: Path
    processing_directory: Path
    completed_directory: Path
    failed_directory: Path


@pytest.fixture
def web_context(tmp_path: Path) -> Iterator[WebTestContext]:
    directories = {
        name: tmp_path / name
        for name in ("pending", "processing", "completed", "failed")
    }
    for directory in directories.values():
        directory.mkdir()

    app = create_app(
        pending_directory=directories["pending"],
        completed_directory=directories["completed"],
        max_upload_bytes=MAX_UPLOAD_BYTES,
    )
    with TestClient(app) as client:
        yield WebTestContext(
            client=client,
            pending_directory=directories["pending"],
            processing_directory=directories["processing"],
            completed_directory=directories["completed"],
            failed_directory=directories["failed"],
        )


def test_health_returns_200(web_context: WebTestContext) -> None:
    response = web_context.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_upload_returns_202_and_uuid(web_context: WebTestContext) -> None:
    index_response = web_context.client.get("/")

    assert index_response.status_code == 200
    assert 'method="post"' in index_response.text
    assert 'enctype="multipart/form-data"' in index_response.text
    assert 'name="audio_file"' in index_response.text
    assert "/audios" in index_response.text

    response = _upload_audio(web_context.client, "recording.wav", SYNTHETIC_AUDIO)
    audio_uuid = _extract_audio_uuid(response)

    assert response.status_code == 202
    assert response.headers["location"] == f"/audios/{audio_uuid}"
    assert str(audio_uuid) in response.text


def test_upload_publishes_only_completed_pending_file(
    web_context: WebTestContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_atomic_write = web.atomic_write
    atomic_write_destinations: list[Path] = []

    def tracked_atomic_write(destination: Path, source: BinaryIO) -> Path:
        atomic_write_destinations.append(destination)
        return real_atomic_write(destination, source)

    monkeypatch.setattr(web, "atomic_write", tracked_atomic_write)

    response = _upload_audio(
        web_context.client,
        "patient-recording.WAV",
        SYNTHETIC_AUDIO,
    )
    audio_uuid = _extract_audio_uuid(response)

    pending_files = list(web_context.pending_directory.iterdir())
    assert pending_files == [web_context.pending_directory / f"{audio_uuid}.wav"]
    assert atomic_write_destinations == pending_files
    assert pending_files[0].read_bytes() == SYNTHETIC_AUDIO
    assert not list(web_context.pending_directory.glob("*.part"))
    assert "patient-recording" not in pending_files[0].name
    assert not list(web_context.processing_directory.iterdir())
    assert not list(web_context.completed_directory.iterdir())
    assert not list(web_context.failed_directory.iterdir())


def test_upload_rejects_unsupported_extension(web_context: WebTestContext) -> None:
    response = _upload_audio(web_context.client, "recording.flac", SYNTHETIC_AUDIO)

    assert response.status_code == 415
    assert not list(web_context.pending_directory.iterdir())


def test_upload_rejects_oversized_file(web_context: WebTestContext) -> None:
    response = _upload_audio(
        web_context.client,
        "recording.mp3",
        b"a" * (MAX_UPLOAD_BYTES + 1),
    )

    assert response.status_code == 413
    assert not list(web_context.pending_directory.iterdir())


def test_download_returns_completed_audio(web_context: WebTestContext) -> None:
    audio_uuid = uuid4()
    completed_audio = web_context.completed_directory / f"{audio_uuid}.mp3"
    completed_audio.write_bytes(SYNTHETIC_AUDIO)

    response = web_context.client.get(f"/audios/{audio_uuid}")

    assert response.status_code == 200
    assert response.content == SYNTHETIC_AUDIO
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-audio-uuid"] == str(audio_uuid)


def test_download_rejects_invalid_uuid(web_context: WebTestContext) -> None:
    response = web_context.client.get("/audios/not-a-uuid")

    assert response.status_code == 422


def test_download_returns_404_for_unknown_audio(
    web_context: WebTestContext,
) -> None:
    response = web_context.client.get(f"/audios/{uuid4()}")

    assert response.status_code == 404
    assert "x-audio-uuid" not in response.headers


def test_web_never_reads_processing_directory(web_context: WebTestContext) -> None:
    audio_uuid = uuid4()
    processing_audio = web_context.processing_directory / f"{audio_uuid}.ogg"
    processing_audio.write_bytes(SYNTHETIC_AUDIO)

    response = web_context.client.get(f"/audios/{audio_uuid}")

    assert response.status_code == 404
    assert response.content != SYNTHETIC_AUDIO
    assert not hasattr(
        web_context.client.app.state.web_config,
        "processing_directory",
    )


def test_web_never_reads_failed_directory(web_context: WebTestContext) -> None:
    audio_uuid = uuid4()
    failed_audio = web_context.failed_directory / f"{audio_uuid}.m4a"
    failed_audio.write_bytes(SYNTHETIC_AUDIO)

    response = web_context.client.get(f"/audios/{audio_uuid}")
    openapi_paths = web_context.client.get("/openapi.json").json()["paths"]

    assert response.status_code == 404
    assert response.content != SYNTHETIC_AUDIO
    assert not hasattr(web_context.client.app.state.web_config, "failed_directory")
    assert all("failed" not in path for path in openapi_paths)


def _upload_audio(client: TestClient, filename: str, content: bytes) -> Response:
    return client.post(
        "/audios",
        files={"audio_file": (filename, content, "application/octet-stream")},
    )


def _extract_audio_uuid(response: Response) -> UUID:
    match = UUID_ATTRIBUTE_PATTERN.search(response.text)
    assert match is not None
    audio_uuid = UUID(match.group(1))
    assert audio_uuid.version == 4
    assert str(audio_uuid) == match.group(1)
    return audio_uuid
