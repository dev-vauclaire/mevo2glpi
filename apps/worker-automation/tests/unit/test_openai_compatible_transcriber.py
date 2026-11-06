"""Contract tests for the synchronous OpenAI-compatible transcriber."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from mevo2glpi_worker_automation.transcription import (
    OpenAICompatibleTranscriber,
    TranscriptionBlockedError,
    TranscriptionErrorCategory,
    TranscriptionRetryRequired,
    TranscriptionTerminalError,
)

AUDIO_UUID = UUID("123e4567-e89b-42d3-a456-426614174000")
API_KEY = "synthetic-provider-key"
TRANSCRIPTION = "Texte synthétique sans donnée réelle"


def _audio(tmp_path: Path, extension: str = ".wav") -> Path:
    path = tmp_path / f"{AUDIO_UUID}{extension}"
    path.write_bytes(b"synthetic-audio")
    return path


def _transcriber(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[OpenAICompatibleTranscriber, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        OpenAICompatibleTranscriber(
            base_url="https://asr.example.test/prefix/v1",
            api_key=SecretStr(API_KEY),
            model="synthetic-openai-model",
            language="fr",
            http_client=client,
            timeout=12,
        ),
        client,
    )


@pytest.mark.parametrize(
    ("extension", "media_type"),
    [
        (".wav", "audio/wav"),
        (".ogg", "audio/ogg"),
        (".mp3", "audio/mpeg"),
        (".m4a", "audio/mp4"),
    ],
)
def test_transcribe_uses_the_strict_openai_multipart_contract(
    tmp_path: Path,
    extension: str,
    media_type: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = request.read()
        return httpx.Response(200, json={"text": TRANSCRIPTION})

    transcriber, client = _transcriber(handler)
    audio_path = _audio(tmp_path, extension)
    try:
        result = transcriber.transcribe(audio_path)
    finally:
        client.close()

    request = captured["request"]
    assert isinstance(request, httpx.Request)
    assert request.method == "POST"
    assert request.url == "https://asr.example.test/prefix/v1/audio/transcriptions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["content-type"].startswith("multipart/form-data;")
    body = captured["body"]
    assert isinstance(body, bytes)
    for expected in (
        b'name="file"',
        audio_path.name.encode(),
        media_type.encode(),
        b'name="model"',
        b"synthetic-openai-model",
        b'name="language"',
        b"fr",
        b'name="response_format"',
        b"json",
    ):
        assert expected in body
    assert result.audio_path == audio_path
    assert result.audio_uuid == AUDIO_UUID
    assert result.transcription == TRANSCRIPTION
    assert result.segments == ()
    assert TRANSCRIPTION not in repr(result)


@pytest.mark.parametrize("status_code", [408, 425, 429, 502, 503, 504, 599])
def test_retryable_http_statuses_request_a_retry(
    tmp_path: Path,
    status_code: int,
) -> None:
    transcriber, client = _transcriber(
        lambda _request: httpx.Response(
            status_code,
            json={"detail": "remote body must stay private"},
        )
    )
    try:
        with pytest.raises(TranscriptionRetryRequired) as captured:
            transcriber.transcribe(_audio(tmp_path))
    finally:
        client.close()

    assert captured.value.code == "transcription_unavailable"
    assert "remote body" not in str(captured.value)


def test_transport_error_requests_a_sanitized_retry(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic upstream detail", request=request)

    transcriber, client = _transcriber(handler)
    try:
        with pytest.raises(TranscriptionRetryRequired) as captured:
            transcriber.transcribe(_audio(tmp_path))
    finally:
        client.close()

    assert captured.value.code == "transcription_unavailable"
    assert "upstream detail" not in str(captured.value)


@pytest.mark.parametrize("status_code", [400, 413, 415, 422, 500])
def test_configured_http_statuses_are_terminal(
    tmp_path: Path,
    status_code: int,
) -> None:
    transcriber, client = _transcriber(
        lambda _request: httpx.Response(status_code, json={"detail": "private"})
    )
    try:
        with pytest.raises(TranscriptionTerminalError) as captured:
            transcriber.transcribe(_audio(tmp_path))
    finally:
        client.close()

    assert captured.value.code == f"http_{status_code}"
    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    ("status_code", "category", "automatic_probe"),
    [
        (401, TranscriptionErrorCategory.AUTHENTICATION, True),
        (403, TranscriptionErrorCategory.AUTHORIZATION, True),
        (404, TranscriptionErrorCategory.INVALID_REQUEST, False),
        (201, TranscriptionErrorCategory.PROTOCOL, False),
        (307, TranscriptionErrorCategory.PROTOCOL, False),
    ],
)
def test_global_http_failures_block_without_exposing_the_response(
    tmp_path: Path,
    status_code: int,
    category: TranscriptionErrorCategory,
    automatic_probe: bool,
) -> None:
    transcriber, client = _transcriber(
        lambda _request: httpx.Response(status_code, text="private upstream response")
    )
    try:
        with pytest.raises(TranscriptionBlockedError) as captured:
            transcriber.transcribe(_audio(tmp_path))
    finally:
        client.close()

    assert captured.value.category is category
    assert captured.value.code == f"http_{status_code}"
    assert captured.value.automatic_probe is automatic_probe
    assert "private upstream" not in str(captured.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"text": 42}),
    ],
)
def test_invalid_success_payload_blocks_the_system(
    tmp_path: Path,
    response: httpx.Response,
) -> None:
    transcriber, client = _transcriber(lambda _request: response)
    try:
        with pytest.raises(TranscriptionBlockedError) as captured:
            transcriber.transcribe(_audio(tmp_path))
    finally:
        client.close()

    assert captured.value.category is TranscriptionErrorCategory.PROTOCOL
    assert captured.value.code == "invalid_transcription_response"
    assert captured.value.automatic_probe is False


@pytest.mark.parametrize("text", ["", "   ", "unsafe\x00text"])
def test_empty_or_unsafe_text_is_a_terminal_audio_failure(
    tmp_path: Path,
    text: str,
) -> None:
    transcriber, client = _transcriber(
        lambda _request: httpx.Response(200, json={"text": text})
    )
    try:
        with pytest.raises(TranscriptionTerminalError) as captured:
            transcriber.transcribe(_audio(tmp_path))
    finally:
        client.close()

    assert captured.value.code == "invalid_transcription"
    assert text not in str(captured.value) or not text.strip()


def test_secret_is_excluded_from_adapter_repr() -> None:
    transcriber, client = _transcriber(
        lambda _request: httpx.Response(200, json={"text": TRANSCRIPTION})
    )
    try:
        assert API_KEY not in repr(transcriber)
    finally:
        client.close()
