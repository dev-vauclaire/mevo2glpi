from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from mevo2glpi_transcription_api.api import create_app
from mevo2glpi_transcription_api.audio import (
    AudioDurationExceeded,
    AudioNormalizationError,
    NormalizedAudio,
)
from mevo2glpi_transcription_api.engines import (
    EngineInferenceError,
    EngineLoadError,
    TranscriptionOutput,
)

API_KEY = "synthetic-transcription-key"
TRANSCRIPT = "Texte fictif sans donnée réelle"


@dataclass
class FakeEngine:
    load_error: bool = False
    inference_error: bool = False
    ready: bool = False
    closed: bool = False
    received_pcm: bytes | None = None
    received_language: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.ready

    def load(self) -> None:
        if self.load_error:
            raise EngineLoadError("private model diagnostic")
        self.ready = True

    def transcribe(
        self,
        samples: bytes,
        *,
        language: str | None,
    ) -> TranscriptionOutput:
        if self.inference_error:
            raise EngineInferenceError("private native diagnostic")
        self.received_pcm = samples
        self.received_language = language
        return TranscriptionOutput(text=TRANSCRIPT, language="fr")

    def close(self) -> None:
        self.ready = False
        self.closed = True


class FakeNormalizer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.received: bytes | None = None

    async def normalize(self, audio: bytes) -> NormalizedAudio:
        self.received = audio
        if self.error is not None:
            raise self.error
        return NormalizedAudio(pcm=b"\x00\x00\x00\x00", duration_seconds=1)


class BlockingNormalizer(FakeNormalizer):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def normalize(self, audio: bytes) -> NormalizedAudio:
        self.received = audio
        self.started.set()
        await self.release.wait()
        return NormalizedAudio(pcm=b"\x00\x00\x00\x00", duration_seconds=1)


def _client(
    *,
    engine: FakeEngine | None = None,
    normalizer: FakeNormalizer | None = None,
    max_upload_bytes: int = 1024,
) -> tuple[TestClient, FakeEngine, FakeNormalizer]:
    engine = engine or FakeEngine()
    normalizer = normalizer or FakeNormalizer()
    app = create_app(
        engine=engine,
        normalizer=normalizer,  # type: ignore[arg-type]
        api_key=SecretStr(API_KEY),
        model_name="parakeet-ultra",
        max_upload_bytes=max_upload_bytes,
    )
    return TestClient(app), engine, normalizer


def _request(client: TestClient, **overrides: object):
    kwargs: dict[str, object] = {
        "headers": {"Authorization": f"Bearer {API_KEY}"},
        "data": {
            "model": "parakeet-ultra",
            "language": "fr",
            "response_format": "json",
        },
        "files": {"file": ("synthetic.wav", b"synthetic-audio", "audio/wav")},
    }
    kwargs.update(overrides)
    return client.post("/v1/audio/transcriptions", **kwargs)  # type: ignore[arg-type]


def test_api_uses_the_project_identity() -> None:
    client, _engine, _normalizer = _client()

    assert client.app.title == "mevo2glpi transcription API"


def test_health_distinguishes_liveness_and_readiness() -> None:
    client, engine, _normalizer = _client()

    with client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
    assert engine.closed is True


def test_transcription_implements_strict_openai_contract() -> None:
    client, engine, normalizer = _client()

    with client:
        response = _request(client)

    assert response.status_code == 200
    assert response.json() == {"text": TRANSCRIPT}
    assert normalizer.received == b"synthetic-audio"
    assert engine.received_pcm == b"\x00\x00\x00\x00"
    assert engine.received_language == "fr"


@pytest.mark.parametrize("authorization", [None, "Basic x", "Bearer wrong"])
def test_transcription_requires_valid_bearer_token(
    authorization: str | None,
) -> None:
    client, _engine, normalizer = _client()
    headers = {} if authorization is None else {"Authorization": authorization}

    with client:
        response = _request(client, headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert normalizer.received is None


@pytest.mark.parametrize(
    "data",
    [
        {"model": "other", "language": "fr", "response_format": "json"},
        {
            "model": "parakeet-ultra",
            "language": "fr",
            "response_format": "verbose_json",
        },
    ],
)
def test_transcription_rejects_unsupported_contract_options(
    data: dict[str, str],
) -> None:
    client, _engine, normalizer = _client()

    with client:
        response = _request(client, data=data)

    assert response.status_code == 400
    assert normalizer.received is None


def test_transcription_rejects_unsupported_media_type() -> None:
    client, _engine, normalizer = _client()

    with client:
        response = _request(
            client,
            files={"file": ("synthetic.txt", b"not-audio", "text/plain")},
        )

    assert response.status_code == 415
    assert normalizer.received is None


def test_transcription_bounds_upload_before_normalization() -> None:
    client, _engine, normalizer = _client(max_upload_bytes=4)

    with client:
        response = _request(client)

    assert response.status_code == 413
    assert normalizer.received is None


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (AudioNormalizationError("private"), 422, "invalid_audio"),
        (AudioDurationExceeded("private"), 413, "audio_duration_exceeded"),
    ],
)
def test_transcription_maps_audio_errors_without_details(
    error: Exception,
    status_code: int,
    detail: str,
) -> None:
    client, _engine, _normalizer = _client(normalizer=FakeNormalizer(error))

    with client:
        response = _request(client)

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert "private" not in response.text


def test_inference_failure_is_retryable_service_unavailability() -> None:
    client, _engine, _normalizer = _client(engine=FakeEngine(inference_error=True))

    with client:
        response = _request(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "transcription_engine_unavailable"}
    assert "private" not in response.text


def test_failed_model_load_aborts_startup_without_private_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _engine, _normalizer = _client(engine=FakeEngine(load_error=True))

    with caplog.at_level(logging.ERROR), pytest.raises(EngineLoadError), client:
        pass

    assert "private model diagnostic" not in caplog.text


def test_logs_never_contain_filename_audio_or_transcript(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _engine, _normalizer = _client()
    filename = "sensitive-name.wav"
    audio = b"sensitive-audio-bytes"

    with caplog.at_level(logging.INFO), client:
        response = _request(
            client,
            files={"file": (filename, audio, "audio/wav")},
        )

    assert response.status_code == 200
    assert filename not in caplog.text
    assert audio.decode() not in caplog.text
    assert TRANSCRIPT not in caplog.text
    assert "transcription completed" in caplog.text


@pytest.mark.asyncio
async def test_concurrent_transcription_is_rejected_with_retryable_429() -> None:
    engine = FakeEngine()
    normalizer = BlockingNormalizer()
    app = create_app(
        engine=engine,
        normalizer=normalizer,  # type: ignore[arg-type]
        api_key=SecretStr(API_KEY),
        model_name="parakeet-ultra",
        max_upload_bytes=1024,
    )
    request = {
        "headers": {"Authorization": f"Bearer {API_KEY}"},
        "data": {
            "model": "parakeet-ultra",
            "language": "fr",
            "response_format": "json",
        },
        "files": {"file": ("synthetic.wav", b"synthetic-audio", "audio/wav")},
    }

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://transcription.test",
        ) as client:
            first_request = asyncio.create_task(
                client.post("/v1/audio/transcriptions", **request)
            )
            await asyncio.wait_for(normalizer.started.wait(), timeout=1)
            second_response = await client.post(
                "/v1/audio/transcriptions",
                **request,
            )
            normalizer.release.set()
            first_response = await asyncio.wait_for(first_request, timeout=1)

    assert second_response.status_code == 429
    assert second_response.headers["retry-after"] == "1"
    assert first_response.status_code == 200
