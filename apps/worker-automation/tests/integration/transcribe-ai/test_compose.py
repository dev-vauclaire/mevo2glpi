from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

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


def test_requested_transcription_device_is_used(transcribe_ai_environment) -> None:
    assert (
        transcribe_ai_environment.runtime_device
        == transcribe_ai_environment.requested_device
    )
