from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from glpi_ticketing_worker_automation.transcription import (
    JobStatus,
    JobUuid,
    TranscriptionClient,
    TranscriptionHttpError,
    TranscriptionProtocolError,
    TranscriptionSubmissionRejectedError,
    TranscriptionTransportError,
)

BASE_URL = "https://transcription.example.test/internal"
API_KEY = "synthetic-api-key"
JOB_UUID = JobUuid("7a85ed93")
SYNTHETIC_AUDIO = b"RIFF\x10\x00\x00\x00WAVEfmt \x00\x00\x00\x00data"


def make_transcription_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[TranscriptionClient, httpx.Client]:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = TranscriptionClient(
        base_url=BASE_URL,
        api_key=SecretStr(API_KEY),
        http_client=http_client,
        timeout=3.5,
    )
    return client, http_client


@pytest.mark.parametrize(
    ("extension", "expected_media_type"),
    [
        (".wav", "audio/wav"),
        (".mp3", "audio/mpeg"),
        (".ogg", "audio/ogg"),
        (".m4a", "audio/mp4"),
    ],
)
def test_transcription_client_submit_sends_expected_multipart_request(
    tmp_path: Path,
    extension: str,
    expected_media_type: str,
) -> None:
    audio_path = tmp_path / f"audio{extension}"
    audio_path.write_bytes(SYNTHETIC_AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert request.method == "POST"
        assert str(request.url) == (f"{BASE_URL}/api/batchTranscription/createJob")
        assert request.headers["X-API-KEY"] == API_KEY
        assert b'name="audioFile"' in body
        assert f'filename="audio{extension}"'.encode() in body
        assert f"Content-Type: {expected_media_type}".encode() in body
        assert SYNTHETIC_AUDIO in body
        assert request.extensions["timeout"] == {
            "connect": 3.5,
            "read": 3.5,
            "write": 3.5,
            "pool": 3.5,
        }
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "job_uuid": str(JOB_UUID),
                    "status": "Votre demande est dans la file d'attente",
                },
            },
        )

    client, http_client = make_transcription_client(handler)
    with http_client:
        submission = client.submit(audio_path)

    assert submission.job_uuid == JOB_UUID
    assert submission.response_log.provider_status == "success"
    assert "Votre demande" not in repr(submission.response_log)


def test_transcription_client_submit_rejects_unsupported_audio_extension(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "audio.flac"
    audio_path.write_bytes(SYNTHETIC_AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("unsupported audio files must be rejected before the HTTP request")

    client, http_client = make_transcription_client(handler)
    with (
        http_client,
        pytest.raises(ValueError, match="unsupported audio file extension"),
    ):
        client.submit(audio_path)


def test_transcription_client_submit_rejection_is_sanitized(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(SYNTHETIC_AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "message": " Refusé\npar le serveur "},
        )

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionSubmissionRejectedError) as error_info:
        client.submit(audio_path)

    assert "Refusé par le serveur" not in str(error_info.value)
    assert API_KEY not in str(error_info.value)
    assert error_info.value.response_log.provider_status == "error"


@pytest.mark.parametrize("status_code", [400, 422])
def test_transcription_client_submit_recognizes_error_envelope_on_4xx(
    tmp_path: Path,
    status_code: int,
) -> None:
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(SYNTHETIC_AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"status": "error", "message": "Provider rejection"},
        )

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionSubmissionRejectedError) as error_info:
        client.submit(audio_path)

    assert error_info.value.response_log.http_status == status_code
    assert "Provider rejection" not in str(error_info.value)


@pytest.mark.parametrize(
    ("provider_job_status", "expected_status"),
    [
        ("PENDING", JobStatus.PENDING),
        ("PROCESSING", JobStatus.PROCESSING),
    ],
)
def test_transcription_client_get_job_maps_non_terminal_statuses(
    provider_job_status: str,
    expected_status: JobStatus,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == (
            f"{BASE_URL}/api/batchTranscription/result?job_uuid={JOB_UUID}"
        )
        assert request.headers["X-API-KEY"] == API_KEY
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"job_id": str(JOB_UUID), "status": provider_job_status},
            },
        )

    client, http_client = make_transcription_client(handler)
    with http_client:
        snapshot = client.get_job(JOB_UUID)

    assert snapshot.status is expected_status
    assert snapshot.transcription is None
    assert snapshot.response_log.job_status == provider_job_status


def test_transcription_client_get_job_returns_completed_transcription() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "job_id": str(JOB_UUID),
                    "status": "COMPLETED",
                    "result": {
                        "full_text": "Coucou je suis une transcription",
                        "language": "fr",
                        "segments": [],
                    },
                    "transcription_time": 1.25,
                },
            },
        )

    client, http_client = make_transcription_client(handler)
    with http_client:
        snapshot = client.get_job(JOB_UUID)

    assert snapshot.status is JobStatus.COMPLETED
    assert snapshot.transcription == "Coucou je suis une transcription"
    assert "Coucou" not in repr(snapshot)
    assert "Coucou" not in repr(snapshot.response_log)


@pytest.mark.parametrize("provider_job_status", ["FAILED", "CANCELLED", "UNKNOWN"])
def test_transcription_client_get_job_maps_other_job_statuses_to_failed(
    provider_job_status: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"job_id": str(JOB_UUID), "status": provider_job_status},
            },
        )

    client, http_client = make_transcription_client(handler)
    with http_client:
        snapshot = client.get_job(JOB_UUID)

    assert snapshot.status is JobStatus.FAILED
    assert snapshot.transcription is None
    expected_log_status = "FAILED" if provider_job_status == "FAILED" else "OTHER"
    assert snapshot.response_log.job_status == expected_log_status


def test_transcription_client_get_job_maps_top_level_error_to_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "error", "message": "Job introuvable"},
        )

    client, http_client = make_transcription_client(handler)
    with http_client:
        snapshot = client.get_job(JOB_UUID)

    assert snapshot.status is JobStatus.FAILED
    assert snapshot.response_log.provider_status == "error"
    assert "Job introuvable" not in repr(snapshot.response_log)


def test_transcription_client_get_job_maps_4xx_error_envelope_to_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"status": "error", "message": "Job introuvable"},
        )

    client, http_client = make_transcription_client(handler)
    with http_client:
        snapshot = client.get_job(JOB_UUID)

    assert snapshot.status is JobStatus.FAILED
    assert snapshot.response_log.http_status == 404
    assert snapshot.response_log.provider_status == "error"


def test_transcription_client_get_job_keeps_429_error_envelope_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"status": "error", "message": "Too many requests"},
        )

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionHttpError) as error_info:
        client.get_job(JOB_UUID)

    assert error_info.value.status_code == 429
    assert error_info.value.retryable is True


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [
        (400, False),
        (404, False),
        (422, False),
        (429, True),
        (500, False),
        (502, True),
        (503, True),
    ],
)
def test_transcription_client_http_errors_are_classified(
    tmp_path: Path,
    status_code: int,
    retryable: bool,
) -> None:
    audio_path = tmp_path / "audio.ogg"
    audio_path.write_bytes(SYNTHETIC_AUDIO)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=f"sensitive {API_KEY}")

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionHttpError) as error_info:
        client.submit(audio_path)

    assert error_info.value.status_code == status_code
    assert error_info.value.retryable is retryable
    assert API_KEY not in str(error_info.value)


def test_transcription_client_transport_error_does_not_expose_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionTransportError) as error_info:
        client.get_job(JOB_UUID)

    assert API_KEY not in str(error_info.value)
    assert error_info.value.operation == "get_job"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"status": "success", "data": {}}),
    ],
)
def test_transcription_client_invalid_responses_raise_protocol_error(
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionProtocolError):
        client.get_job(JOB_UUID)


def test_transcription_client_completed_job_requires_a_result_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "job_id": str(JOB_UUID),
                    "status": "COMPLETED",
                    "transcription": "old response shape",
                },
            },
        )

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionProtocolError, match="result"):
        client.get_job(JOB_UUID)


@pytest.mark.parametrize("full_text", [None, 42, ["not", "a", "string"]])
def test_transcription_client_completed_job_requires_string_full_text(
    full_text: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "job_id": str(JOB_UUID),
                    "status": "COMPLETED",
                    "result": {"full_text": full_text},
                },
            },
        )

    client, http_client = make_transcription_client(handler)
    with http_client, pytest.raises(TranscriptionProtocolError, match="full_text"):
        client.get_job(JOB_UUID)


def test_transcription_client_rejects_a_mismatched_response_job_uuid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"job_id": "another-job", "status": "PENDING"},
            },
        )

    client, http_client = make_transcription_client(handler)
    with (
        http_client,
        pytest.raises(
            TranscriptionProtocolError,
            match="does not match",
        ),
    ):
        client.get_job(JOB_UUID)
