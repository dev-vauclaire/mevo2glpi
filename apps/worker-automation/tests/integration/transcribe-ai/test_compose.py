from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from glpi_ticketing_common.audio_files import (
    SUPPORTED_AUDIO_STORAGE_EXTENSIONS,
)

pytestmark = [pytest.mark.transcribe_ai, pytest.mark.timeout(900)]

TRANSCRIBE_AI_TEST_DIRECTORY = Path(__file__).resolve().parent
COMPOSE_FILE = TRANSCRIBE_AI_TEST_DIRECTORY / "compose.yml"
CPU_COMPOSE_FILE = TRANSCRIBE_AI_TEST_DIRECTORY / "compose.cpu.yml"
GPU_COMPOSE_FILE = TRANSCRIBE_AI_TEST_DIRECTORY / "compose.gpu.yml"
EXPECTED_IMAGES = {
    "postgres:16.10-alpine@sha256:029660641a0cfc575b14f336ba448fb8a75fd595d42e1fa316b9fb4378742297",
    "redis:7.4.5-alpine@sha256:bb186d083732f669da90be8b0f975a37812b15e913465bb14d845db72a4e3e08",
    "devauclaire/transcribe-ai-api:1.0.0@sha256:6b6d09523c133e0c134d81b87a089928df4ead835716cf31641b2111f1106157",
    "devauclaire/transcribe-ai-mono-voice-worker:1.0.0@sha256:3b7a3b3292d58aa544c9b5ffb65c14dfaaa95f5bc1bd9777e20bea975f500f59",
    "devauclaire/transcribe-ai-mono-voice-stt:1.0.0@sha256:ef2ebf883bf2b6fa597559396ebb31fd345ec5bcec499f8e828e866f8103f44b",
}
EXPECTED_AUDIO_FORMATS = frozenset({"wav", "mp3", "ogg", "m4a"})


def test_transcribe_ai_images_are_pinned() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    image_references = {
        line.removeprefix("image:").strip()
        for raw_line in compose.splitlines()
        if (line := raw_line.strip()).startswith("image:")
    }

    assert image_references == EXPECTED_IMAGES
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", image) for image in image_references)
    assert ":latest" not in compose.casefold()
    assert "container_name:" not in compose
    assert "host_ip: 127.0.0.1" in compose
    assert "published: 0" in compose


def test_cpu_and_gpu_overlays_are_explicit() -> None:
    cpu_compose = CPU_COMPOSE_FILE.read_text(encoding="utf-8")
    gpu_compose = GPU_COMPOSE_FILE.read_text(encoding="utf-8")

    assert 'CUDA_VISIBLE_DEVICES: ""' in cpu_compose
    assert "NVIDIA_VISIBLE_DEVICES: void" in cpu_compose
    assert 'CUDA_VISIBLE_DEVICES: "0"' in gpu_compose
    assert "driver: nvidia" in gpu_compose
    assert "capabilities: [gpu]" in gpu_compose


def test_transcribe_ai_api_formats_match_supported_worker_formats() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    format_lines = [
        line.partition(":")[2].strip().strip("\"'")
        for raw_line in compose.splitlines()
        if (line := raw_line.strip()).startswith("FORMAT_AUDIO_ALLOWED:")
    ]

    assert len(format_lines) == 1
    configured_formats = tuple(
        audio_format.strip() for audio_format in format_lines[0].split(",")
    )
    assert all(configured_formats)
    assert len(configured_formats) == len(set(configured_formats))
    assert frozenset(configured_formats) == EXPECTED_AUDIO_FORMATS
    assert frozenset(configured_formats) == SUPPORTED_AUDIO_STORAGE_EXTENSIONS


def test_transcribe_ai_api_is_reachable_on_loopback(
    transcribe_ai_environment,
) -> None:
    parsed_url = urlparse(transcribe_ai_environment.base_url)
    assert parsed_url.hostname == "127.0.0.1"
    assert parsed_url.port is not None

    response = httpx.get(
        f"{transcribe_ai_environment.base_url}/api/batchTranscription/result",
        params={"job_uuid": "transcribe-ai-test-readiness"},
        headers={
            "X-API-KEY": transcribe_ai_environment.api_key.get_secret_value(),
        },
        timeout=10.0,
        follow_redirects=False,
    )
    assert response.status_code == httpx.codes.NOT_FOUND


def test_transcribe_ai_fixture_exposes_safe_internal_network_contract(
    transcribe_ai_environment,
) -> None:
    internal_url = urlparse(transcribe_ai_environment.internal_base_url)

    assert re.fullmatch(
        r"[a-z0-9][a-z0-9_-]*",
        transcribe_ai_environment.project_name,
    )
    assert transcribe_ai_environment.network_name == (
        f"{transcribe_ai_environment.project_name}_transcribe_ai_test"
    )
    assert internal_url.scheme == "http"
    assert internal_url.hostname == "api"
    assert internal_url.port == 5000
    assert internal_url.path in {"", "/"}
    assert internal_url.username is None
    assert internal_url.password is None
    assert internal_url.query == ""
    assert internal_url.fragment == ""

    rendered = repr(transcribe_ai_environment)
    if transcribe_ai_environment.api_key.get_secret_value() in rendered:
        pytest.fail("The Transcribe AI fixture repr exposes its API key")
    assert "_compose" not in rendered
    assert "_docker_client" not in rendered
    assert "_compose_environment" not in rendered


def test_transcribe_ai_fixture_service_control_is_allowlisted_and_restores_readiness(
    transcribe_ai_environment,
) -> None:
    for forbidden_service in ("", "postgres", "redis", "mono-voice-stt", "unknown"):
        with pytest.raises(ValueError, match="not controllable"):
            transcribe_ai_environment.start_service(forbidden_service)
        with pytest.raises(ValueError, match="not controllable"):
            transcribe_ai_environment.stop_service(forbidden_service)

    for service_name in ("api", "mono-voice-worker"):
        try:
            transcribe_ai_environment.stop_service(service_name)
        finally:
            transcribe_ai_environment.start_service(service_name)

    response = httpx.get(
        f"{transcribe_ai_environment.base_url}/api/batchTranscription/result",
        params={"job_uuid": "transcribe-ai-restart-readiness"},
        headers={
            "X-API-KEY": transcribe_ai_environment.api_key.get_secret_value(),
        },
        timeout=10.0,
        follow_redirects=False,
    )
    assert response.status_code == httpx.codes.NOT_FOUND


def test_requested_transcription_device_is_used(transcribe_ai_environment) -> None:
    assert (
        transcribe_ai_environment.runtime_device
        == transcribe_ai_environment.requested_device
    )
