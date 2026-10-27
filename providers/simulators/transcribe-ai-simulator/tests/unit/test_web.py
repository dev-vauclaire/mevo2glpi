from __future__ import annotations

import logging

import pytest
from httpx2 import ASGITransport, AsyncClient

from glpi_ticketing_transcribe_ai_simulator.settings import (
    TranscribeAiSimulatorSettings,
)
from glpi_ticketing_transcribe_ai_simulator.web import (
    SYNTHETIC_TRANSCRIPTION,
    create_app,
)

API_KEY = "synthetic-transcription-key"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:
    settings = TranscribeAiSimulatorSettings(
        transcribe_ai_simulator_host="127.0.0.1",
        transcribe_ai_simulator_port=8080,
        transcribe_ai_simulator_api_key=API_KEY,
        _env_file=None,
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings)),
        base_url="http://transcribe-ai-simulator:8080",
    ) as value:
        yield value


async def test_health_requires_no_secret(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_contract_returns_pending_then_completed_without_logging_data(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        submission = await client.post(
            "/api/batchTranscription/createJob",
            headers={"X-API-KEY": API_KEY},
            files={"audioFile": ("audio.ogg", b"OggS-synthetic", "audio/ogg")},
        )
        job_uuid = submission.json()["data"]["job_uuid"]
        pending = await client.get(
            "/api/batchTranscription/result",
            params={"job_uuid": job_uuid},
            headers={"X-API-KEY": API_KEY},
        )
        completed = await client.get(
            "/api/batchTranscription/result",
            params={"job_uuid": job_uuid},
            headers={"X-API-KEY": API_KEY},
        )

    assert submission.status_code == 200
    assert pending.json()["data"] == {"job_id": job_uuid, "status": "PENDING"}
    assert completed.json()["data"]["status"] == "COMPLETED"
    assert completed.json()["data"]["result"]["full_text"] == (SYNTHETIC_TRANSCRIPTION)
    assert API_KEY not in caplog.text
    assert SYNTHETIC_TRANSCRIPTION not in caplog.text


async def test_wrong_api_key_is_rejected_without_echoing_it(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/batchTranscription/createJob",
        headers={"X-API-KEY": "wrong-transcription-key"},
        files={"audioFile": ("audio.wav", b"synthetic", "audio/wav")},
    )

    assert response.status_code == 401
    assert API_KEY not in response.text
    assert "wrong-transcription-key" not in response.text


async def test_job_not_found_is_terminal_contract_error(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/batchTranscription/result",
        params={"job_uuid": "00000000-0000-4000-8000-000000000001"},
        headers={"X-API-KEY": API_KEY},
    )

    assert response.status_code == 404
    assert response.json() == {"status": "error", "message": "job_not_found"}


async def test_create_job_requires_named_audio_file(client: AsyncClient) -> None:
    response = await client.post(
        "/api/batchTranscription/createJob",
        headers={"X-API-KEY": API_KEY},
        files={"other": ("audio.wav", b"synthetic", "audio/wav")},
    )

    assert response.status_code == 400
    assert response.json() == {"status": "error", "message": "audio_file_missing"}
