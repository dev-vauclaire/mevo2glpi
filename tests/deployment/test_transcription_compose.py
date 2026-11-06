"""Validate the reusable private transcription API stack."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.deployment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPTION_DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker" / "transcription"
TRANSCRIPTION_COMPOSE_FILE = (
    TRANSCRIPTION_DOCKER_ROOT / "docker-compose.transcription.yml"
)
STAGING_TRANSCRIPTION_ENV_FILE = TRANSCRIPTION_DOCKER_ROOT / "staging.env.example"
PRODUCTION_TRANSCRIPTION_ENV_FILE = TRANSCRIPTION_DOCKER_ROOT / "production.env.example"
TRANSCRIPTION_APP_ROOT = REPOSITORY_ROOT / "apps" / "transcription-api"
TRANSCRIPTION_DOCKERFILE = TRANSCRIPTION_APP_ROOT / "Dockerfile"
TRANSCRIPTION_IMAGE = "mevo2glpi-transcription-api:parakeet-ultra-q8"
TRANSCRIBE_CPP_VERSION = "0.4.1"
PARAKEET_ULTRA_MODEL_URL = (
    "https://huggingface.co/handy-computer/parakeet-ultra-gguf/resolve/"
    "a1539be1b849ea7463b88343e5277b373d0c4ea9/"
    "parakeet-ultra-Q8_0.gguf"
)
PARAKEET_ULTRA_MODEL_SHA256 = (
    "007a59761e9258779f189df396b50d12b83bb5b79e66df4b955c230d2f2a0a59"
)
DOCUMENTED_SECRET_NAMES = {"TRANSCRIPTION_API_KEY"}


def _example_environment(
    path: Path = STAGING_TRANSCRIPTION_ENV_FILE,
) -> dict[str, str]:
    source = path.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def _clean_environment(documented_names: set[str]) -> dict[str, str]:
    """Prevent active Compose variables from overriding the example contract."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in documented_names
        and not name.startswith("COMPOSE_")
        and not name.startswith("MEVO2GLPI_")
    }


@pytest.fixture(scope="module")
def transcription_compose_config() -> dict[str, object]:
    """Render the transcription manifest once without building its image."""
    return _render_compose(STAGING_TRANSCRIPTION_ENV_FILE)


def _render_compose(environment_file: Path) -> dict[str, object]:
    documented_names = set(_example_environment(environment_file))
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(environment_file),
            "--file",
            str(TRANSCRIPTION_COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=_clean_environment(documented_names),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "Docker Compose could not render the transcription manifest "
            f"(exit_code={result.returncode}): {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def test_transcription_environment_documents_every_interpolated_variable() -> None:
    compose_source = TRANSCRIPTION_COMPOSE_FILE.read_text(encoding="utf-8")
    example_source = STAGING_TRANSCRIPTION_ENV_FILE.read_text(encoding="utf-8")
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented_values = _example_environment()

    assert set(documented_values) == interpolated_names
    assert documented_values["COMPOSE_PROJECT_NAME"] == (
        "mevo2glpi-staging-transcription"
    )
    assert documented_values["APP_RELEASE"] == "main"
    assert documented_values["APP_COMMIT_SHA"] == "CHANGE_ME"
    assert documented_values["TRANSCRIPTION_NETWORK_NAME"] == (
        "mevo2glpi-staging-transcription-network"
    )
    assert documented_values["TRANSCRIPTION_NETWORK_ALIAS"] == (
        "mevo2glpi-staging-transcription"
    )
    assert {
        name: documented_values[name] for name in DOCUMENTED_SECRET_NAMES
    } == dict.fromkeys(DOCUMENTED_SECRET_NAMES, "CHANGE_ME")
    assert documented_values["TRANSCRIPTION_MODEL_PATH"] == (
        "/models/parakeet-ultra-Q8_0.gguf"
    )
    assert documented_values["TRANSCRIPTION_MODEL_NAME"] == "parakeet-ultra"
    assert documented_values["PARAKEET_ULTRA_MODEL_URL"] == PARAKEET_ULTRA_MODEL_URL
    assert documented_values["PARAKEET_ULTRA_MODEL_SHA256"] == (
        PARAKEET_ULTRA_MODEL_SHA256
    )
    assert documented_values["TRANSCRIPTION_CPUS"] == "2.0"
    assert documented_values["TRANSCRIPTION_CPU_THREADS"] == "2"
    assert documented_values["TRANSCRIPTION_MEMORY_LIMIT"] == "4g"
    assert documented_values["TRANSCRIPTION_LOG_LEVEL"] == "info"
    assert documented_values["TRANSCRIPTION_MAX_UPLOAD_BYTES"] == "26214400"
    assert documented_values["TRANSCRIPTION_MAX_AUDIO_SECONDS"] == "600"
    assert documented_values["TRANSCRIPTION_FFMPEG_TIMEOUT_SECONDS"] == "60"
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password|token)=[0-9a-f]{32,}",
        example_source,
    )


def test_production_environment_uses_the_production_identity_and_network() -> None:
    compose_source = TRANSCRIPTION_COMPOSE_FILE.read_text(encoding="utf-8")
    example_source = PRODUCTION_TRANSCRIPTION_ENV_FILE.read_text(encoding="utf-8")
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    staging_values = _example_environment(STAGING_TRANSCRIPTION_ENV_FILE)
    production_values = _example_environment(PRODUCTION_TRANSCRIPTION_ENV_FILE)

    assert set(production_values) == interpolated_names
    assert production_values["COMPOSE_PROJECT_NAME"] == ("mevo2glpi-prod-transcription")
    assert production_values["APP_RELEASE"] == "CHANGE_ME"
    assert production_values["APP_COMMIT_SHA"] == "CHANGE_ME"
    assert production_values["TRANSCRIPTION_NETWORK_NAME"] == (
        "mevo2glpi-prod-transcription-network"
    )
    assert production_values["TRANSCRIPTION_NETWORK_ALIAS"] == (
        "mevo2glpi-prod-transcription"
    )
    assert production_values["TRANSCRIPTION_API_KEY"] == "CHANGE_ME"

    environment_specific_names = {
        "APP_COMMIT_SHA",
        "APP_RELEASE",
        "COMPOSE_PROJECT_NAME",
        "TRANSCRIPTION_NETWORK_ALIAS",
        "TRANSCRIPTION_NETWORK_NAME",
    }
    assert {
        name: value
        for name, value in production_values.items()
        if name not in environment_specific_names
    } == {
        name: value
        for name, value in staging_values.items()
        if name not in environment_specific_names
    }
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password|token)=[0-9a-f]{32,}",
        example_source,
    )


def test_production_environment_renders_the_private_production_network() -> None:
    config = _render_compose(PRODUCTION_TRANSCRIPTION_ENV_FILE)
    service = _services(config)["transcription-api"]

    assert config["name"] == "mevo2glpi-prod-transcription"
    assert service["networks"]["transcription"]["aliases"] == [
        "mevo2glpi-prod-transcription"
    ]
    assert config["networks"]["transcription"] == {
        "name": "mevo2glpi-prod-transcription-network",
        "ipam": {},
        "external": True,
    }


def test_transcription_manifest_contains_only_the_api(
    transcription_compose_config: dict[str, object],
) -> None:
    assert transcription_compose_config["name"] == ("mevo2glpi-staging-transcription")
    assert set(_services(transcription_compose_config)) == {"transcription-api"}


def test_transcription_api_is_configured_for_private_cpu_inference(
    transcription_compose_config: dict[str, object],
) -> None:
    service = _services(transcription_compose_config)["transcription-api"]

    assert service["image"] == TRANSCRIPTION_IMAGE
    assert service["pull_policy"] == "build"
    build_context = Path(service["build"]["context"]).resolve()
    assert build_context == REPOSITORY_ROOT
    assert (build_context / service["build"]["dockerfile"]).resolve() == (
        TRANSCRIPTION_DOCKERFILE
    )
    assert service["build"]["args"] == {
        "IMAGE_VERSION": "main",
        "PARAKEET_ULTRA_MODEL_SHA256": PARAKEET_ULTRA_MODEL_SHA256,
        "PARAKEET_ULTRA_MODEL_URL": PARAKEET_ULTRA_MODEL_URL,
        "VCS_REF": "CHANGE_ME",
    }
    assert service["environment"] == {
        "TRANSCRIPTION_API_KEY": "CHANGE_ME",
        "TRANSCRIPTION_CPU_THREADS": "2",
        "TRANSCRIPTION_FFMPEG_PATH": "/usr/bin/ffmpeg",
        "TRANSCRIPTION_FFMPEG_TIMEOUT_SECONDS": "60",
        "TRANSCRIPTION_HOST": "0.0.0.0",
        "TRANSCRIPTION_LOG_LEVEL": "info",
        "TRANSCRIPTION_MAX_AUDIO_SECONDS": "600",
        "TRANSCRIPTION_MAX_UPLOAD_BYTES": "26214400",
        "TRANSCRIPTION_MODEL_NAME": "parakeet-ultra",
        "TRANSCRIPTION_MODEL_PATH": "/models/parakeet-ultra-Q8_0.gguf",
        "TRANSCRIPTION_PORT": "5092",
    }
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["pids_limit"] == 128
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "45s"
    assert service["cpus"] == 2.0
    assert service["mem_limit"] == "4294967296"
    assert service["healthcheck"]["start_period"] == "2m0s"
    healthcheck = " ".join(service["healthcheck"]["test"])
    assert "/health/ready" in healthcheck
    assert "curl" not in healthcheck


def test_transcription_api_is_reachable_only_on_the_private_network(
    transcription_compose_config: dict[str, object],
) -> None:
    service = _services(transcription_compose_config)["transcription-api"]

    assert service["expose"] == ["5092"]
    assert "ports" not in service
    assert "labels" not in service
    assert set(service["networks"]) == {"transcription"}
    assert service["networks"]["transcription"]["aliases"] == [
        "mevo2glpi-staging-transcription"
    ]
    assert transcription_compose_config["networks"]["transcription"] == {
        "name": "mevo2glpi-staging-transcription-network",
        "ipam": {},
        "external": True,
    }
    assert set(transcription_compose_config["networks"]) == {"transcription"}
    assert transcription_compose_config.get("volumes", {}) == {}


def test_transcription_image_pins_runtime_and_model_artifact() -> None:
    dockerfile = TRANSCRIPTION_DOCKERFILE.read_text(encoding="utf-8")

    assert f"ARG TRANSCRIBE_CPP_VERSION={TRANSCRIBE_CPP_VERSION}" in dockerfile
    assert PARAKEET_ULTRA_MODEL_URL in dockerfile
    assert PARAKEET_ULTRA_MODEL_SHA256 in dockerfile
    assert "sha256sum --check --strict" in dockerfile
    assert "latest" not in dockerfile.casefold()
