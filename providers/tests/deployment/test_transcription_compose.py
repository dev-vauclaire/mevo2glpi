"""Validate the reusable private Parakeet transcription stack."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.deployment

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TRANSCRIPTION_DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker" / "transcription"
TRANSCRIPTION_COMPOSE_FILE = (
    TRANSCRIPTION_DOCKER_ROOT / "docker-compose.transcription.yml"
)
TRANSCRIPTION_ENV_FILE = TRANSCRIPTION_DOCKER_ROOT / "transcription.env.example"
PARAKEET_IMAGE = (
    "ghcr.io/achetronic/parakeet:0.8.0-int8@"
    "sha256:00f8a02ec0ca6a7d6d5ee9f959060d8498b14f741a25e914941d22547a3f37f4"
)
DOCUMENTED_SECRET_NAMES = {"PARAKEET_API_KEY"}


def _example_environment() -> dict[str, str]:
    source = TRANSCRIPTION_ENV_FILE.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def _clean_environment(documented_names: set[str]) -> dict[str, str]:
    """Prevent active Compose variables from overriding the example contract."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in documented_names
        and not name.startswith("COMPOSE_")
        and not name.startswith("GTA_")
    }


@pytest.fixture(scope="module")
def transcription_compose_config() -> dict[str, object]:
    """Render the transcription manifest once without pulling its image."""
    documented_names = set(_example_environment())
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(TRANSCRIPTION_ENV_FILE),
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
    example_source = TRANSCRIPTION_ENV_FILE.read_text(encoding="utf-8")
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented_values = _example_environment()

    assert set(documented_values) == interpolated_names
    assert documented_values["COMPOSE_PROJECT_NAME"] == "gta-staging-transcription"
    assert documented_values["TRANSCRIPTION_NETWORK_NAME"] == (
        "gta-staging-transcription-network"
    )
    assert documented_values["PARAKEET_NETWORK_ALIAS"] == "gta-staging-parakeet"
    assert {
        name: documented_values[name] for name in DOCUMENTED_SECRET_NAMES
    } == dict.fromkeys(DOCUMENTED_SECRET_NAMES, "CHANGE_ME")
    assert documented_values["PARAKEET_CPUS"] == "2.0"
    assert documented_values["PARAKEET_MEMORY_LIMIT"] == "3g"
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password|token)=[0-9a-f]{32,}",
        example_source,
    )


def test_transcription_manifest_contains_only_parakeet(
    transcription_compose_config: dict[str, object],
) -> None:
    assert transcription_compose_config["name"] == "gta-staging-transcription"
    assert set(_services(transcription_compose_config)) == {"parakeet"}


def test_parakeet_is_pinned_and_configured_for_private_cpu_inference(
    transcription_compose_config: dict[str, object],
) -> None:
    parakeet = _services(transcription_compose_config)["parakeet"]

    assert parakeet["image"] == PARAKEET_IMAGE
    assert parakeet["environment"] == {
        "PARAKEET_API_KEY": "CHANGE_ME",
        "PARAKEET_GPU": "cpu",
        "PARAKEET_LOG_FORMAT": "json",
        "PARAKEET_LOG_LEVEL": "info",
        "PARAKEET_LONG_AUDIO": "false",
        "PARAKEET_WORKERS": "1",
    }
    assert parakeet["read_only"] is True
    assert parakeet["cap_drop"] == ["ALL"]
    assert parakeet["security_opt"] == ["no-new-privileges:true"]
    assert parakeet["pids_limit"] == 128
    assert parakeet["restart"] == "unless-stopped"
    assert parakeet["stop_grace_period"] == "45s"
    assert parakeet["cpus"] == 2.0
    assert parakeet["mem_limit"] == "3221225472"
    assert parakeet["healthcheck"]["start_period"] == "1m30s"
    assert "/health" in " ".join(parakeet["healthcheck"]["test"])
    assert "curl" not in " ".join(parakeet["healthcheck"]["test"])


def test_parakeet_is_reachable_only_through_the_private_shared_network(
    transcription_compose_config: dict[str, object],
) -> None:
    parakeet = _services(transcription_compose_config)["parakeet"]

    assert parakeet["expose"] == ["5092"]
    assert "ports" not in parakeet
    assert "labels" not in parakeet
    assert set(parakeet["networks"]) == {"transcription"}
    assert parakeet["networks"]["transcription"]["aliases"] == ["gta-staging-parakeet"]
    assert transcription_compose_config["networks"]["transcription"] == {
        "name": "gta-staging-transcription-network",
        "ipam": {},
        "external": True,
    }
    assert set(transcription_compose_config["networks"]) == {"transcription"}
    assert transcription_compose_config.get("volumes", {}) == {}
