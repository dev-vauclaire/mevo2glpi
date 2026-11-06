from __future__ import annotations

import logging

import pytest
from httpx2 import ASGITransport, AsyncClient

from mevo2glpi_openai_transcription_simulator.settings import (
    OpenAiTranscriptionSimulatorSettings,
)
from mevo2glpi_openai_transcription_simulator.web import (
    SYNTHETIC_TRANSCRIPTION,
    create_app,
)

API_KEY = "synthetic-openai-key"
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncClient:
    settings = OpenAiTranscriptionSimulatorSettings(
        openai_transcription_simulator_host="127.0.0.1",
        openai_transcription_simulator_port=8080,
        openai_transcription_simulator_api_key=API_KEY,
        openai_transcription_simulator_max_upload_bytes=1024,
        _env_file=None,
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_app(settings)),
        base_url="http://openai-transcription-simulator:8080",
    ) as value:
        yield value


def _request(
    *,
    file_content: bytes = b"synthetic-audio",
    fields: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "headers": {"Authorization": f"Bearer {API_KEY}"},
        "data": fields
        or {
            "model": "synthetic-openai-model",
            "language": "fr",
            "response_format": "json",
        },
        "files": {"file": ("audio.ogg", file_content, "audio/ogg")},
    }


async def test_health_requires_no_secret(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_transcription_contract_is_deterministic_and_not_logged(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio = b"private-synthetic-audio"

    with caplog.at_level(logging.INFO):
        response = await client.post(
            "/v1/audio/transcriptions", **_request(file_content=audio)
        )

    assert response.status_code == 200
    assert response.json() == {"text": SYNTHETIC_TRANSCRIPTION}
    assert API_KEY not in caplog.text
    assert audio.decode() not in caplog.text
    assert SYNTHETIC_TRANSCRIPTION not in caplog.text


async def test_wrong_bearer_is_rejected_without_echoing_it(
    client: AsyncClient,
) -> None:
    request = _request()
    request["headers"] = {"Authorization": "Bearer wrong-synthetic-key"}

    response = await client.post("/v1/audio/transcriptions", **request)

    assert response.status_code == 401
    assert API_KEY not in response.text
    assert "wrong-synthetic-key" not in response.text


@pytest.mark.parametrize("missing_field", ["model", "language", "response_format"])
async def test_required_text_fields_are_enforced(
    client: AsyncClient,
    missing_field: str,
) -> None:
    fields = {
        "model": "synthetic-openai-model",
        "language": "fr",
        "response_format": "json",
    }
    del fields[missing_field]

    response = await client.post(
        "/v1/audio/transcriptions",
        **_request(fields=fields),
    )

    assert response.status_code == 400


async def test_only_json_response_format_is_accepted(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        **_request(
            fields={
                "model": "synthetic-openai-model",
                "language": "fr",
                "response_format": "verbose_json",
            }
        ),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "response_format_not_supported"


async def test_empty_audio_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        **_request(file_content=b""),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "audio_empty"


async def test_oversized_audio_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        **_request(file_content=b"a" * 1025),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "audio_too_large"


async def test_oversized_declared_request_is_rejected_before_parsing(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "multipart/form-data; boundary=synthetic",
            "Content-Length": str(26 * 1024 * 1024),
        },
        content=b"--synthetic--\r\n",
    )

    assert response.status_code == 413
