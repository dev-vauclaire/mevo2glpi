"""Validate the standalone, staging-only provider Compose contract."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.deployment

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SIMULATORS_DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker" / "providers"
SIMULATORS_COMPOSE_FILE = SIMULATORS_DOCKER_ROOT / "docker-compose.staging.yml"
SIMULATORS_ENV_FILE = SIMULATORS_DOCKER_ROOT / "staging.env.example"

SIMULATOR_SERVICES = {
    "microsoft-graph-simulator",
    "transcribe-ai-simulator",
}
DOCUMENTED_SECRET_NAMES = {
    "MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN",
    "TRANSCRIBE_AI_SIMULATOR_API_KEY",
}


def _example_environment() -> dict[str, str]:
    source = SIMULATORS_ENV_FILE.read_text(encoding="utf-8")
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
def simulators_compose_config() -> dict[str, object]:
    """Render the simulator manifest once without starting any container."""
    documented_names = set(_example_environment())
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(SIMULATORS_ENV_FILE),
            "--file",
            str(SIMULATORS_COMPOSE_FILE),
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
            "Docker Compose could not render the simulators manifest "
            f"(exit_code={result.returncode}): {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def test_simulators_environment_documents_every_interpolated_variable() -> None:
    compose_source = SIMULATORS_COMPOSE_FILE.read_text(encoding="utf-8")
    example_source = SIMULATORS_ENV_FILE.read_text(encoding="utf-8")
    active_compose_source = "\n".join(
        line
        for line in compose_source.splitlines()
        if not line.lstrip().startswith("#")
    )
    interpolated_names = set(
        re.findall(r"\$\{([A-Z][A-Z0-9_]*)", active_compose_source)
    )
    documented_values = _example_environment()

    assert set(documented_values) == interpolated_names
    assert documented_values["COMPOSE_PROJECT_NAME"] == "gta-staging-simulators"
    assert documented_values["SIMULATORS_NETWORK_NAME"] == (
        "gta-staging-simulators-network"
    )
    assert (
        documented_values["MICROSOFT_GRAPH_SIMULATOR_AUDIO_FIXTURES_HOST_PATH"]
        == "/srv/gta-staging/audio-fixtures"
    )
    assert {
        name: documented_values[name] for name in DOCUMENTED_SECRET_NAMES
    } == dict.fromkeys(DOCUMENTED_SECRET_NAMES, "CHANGE_ME")
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password|token)=[0-9a-f]{32,}",
        example_source,
    )


def test_simulators_manifest_contains_only_the_active_staging_simulators(
    simulators_compose_config: dict[str, object],
) -> None:
    assert simulators_compose_config["name"] == "gta-staging-simulators"
    assert set(_services(simulators_compose_config)) == SIMULATOR_SERVICES


def test_simulator_services_are_isolated_and_have_stable_network_aliases(
    simulators_compose_config: dict[str, object],
) -> None:
    services = _services(simulators_compose_config)
    expected_aliases = {
        "microsoft-graph-simulator": "gta-staging-microsoft-graph",
        "transcribe-ai-simulator": "gta-staging-transcribe-ai",
    }
    expected_ports = {
        "microsoft-graph-simulator": "8080",
        "transcribe-ai-simulator": "8080",
    }

    for service_name, alias in expected_aliases.items():
        service = services[service_name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["expose"] == [expected_ports[service_name]]
        assert "ports" not in service
        assert set(service["networks"]) == {"simulators"}
        assert service["networks"]["simulators"]["aliases"] == [alias]


def test_graph_simulator_mounts_only_operator_audio_fixtures_read_only(
    simulators_compose_config: dict[str, object],
) -> None:
    services = _services(simulators_compose_config)
    graph_volumes = services["microsoft-graph-simulator"].get("volumes", [])

    assert len(graph_volumes) == 1
    audio_fixtures = graph_volumes[0]
    assert audio_fixtures["type"] == "bind"
    assert audio_fixtures["source"] == "/srv/gta-staging/audio-fixtures"
    assert audio_fixtures["target"] == "/fixtures/audio"
    assert audio_fixtures["read_only"] is True
    bind_options = audio_fixtures.get("bind")
    if bind_options is not None:
        assert isinstance(bind_options, dict)
        if "create_host_path" in bind_options:
            assert bind_options["create_host_path"] is False

    compose_source = SIMULATORS_COMPOSE_FILE.read_text(encoding="utf-8")
    active_compose_source = "\n".join(
        line
        for line in compose_source.splitlines()
        if not line.lstrip().startswith("#")
    )
    graph_service_source = active_compose_source.split(
        "  microsoft-graph-simulator:",
        maxsplit=1,
    )[1].split("\n  transcribe-ai-simulator:", maxsplit=1)[0]
    assert re.search(
        r"(?m)^        bind:\s*\n          create_host_path: false\s*$",
        graph_service_source,
    )
    assert services["transcribe-ai-simulator"].get("volumes", []) == []


def test_simulators_manifest_uses_only_the_shared_simulator_network(
    simulators_compose_config: dict[str, object],
) -> None:
    assert simulators_compose_config["networks"]["simulators"] == {
        "name": "gta-staging-simulators-network",
        "ipam": {},
        "external": True,
    }
    assert set(simulators_compose_config["networks"]) == {"simulators"}
    assert simulators_compose_config.get("volumes", {}) == {}


def test_simulator_build_context_contains_only_required_workspace_sources() -> None:
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    workspace = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    members = workspace["tool"]["uv"]["workspace"]["members"]

    for required_path in (
        "!pyproject.toml",
        "!uv.lock",
        "!LICENSE",
        "!NOTICE",
        "!providers/simulators/microsoft-graph-simulator/pyproject.toml",
        "!providers/simulators/microsoft-graph-simulator/src/**",
        "!providers/simulators/openai-transcription-simulator/pyproject.toml",
        "!providers/simulators/openai-transcription-simulator/src/**",
        "!providers/simulators/transcribe-ai-simulator/pyproject.toml",
        "!providers/simulators/transcribe-ai-simulator/src/**",
    ):
        assert required_path in dockerignore

    for simulator_name in (
        "microsoft-graph-simulator",
        "openai-transcription-simulator",
        "transcribe-ai-simulator",
    ):
        dockerfile = (SIMULATORS_DOCKER_ROOT / simulator_name / "Dockerfile").read_text(
            encoding="utf-8"
        )
        assert "python:3.12.14-slim-bookworm@sha256:" in dockerfile
        assert "ghcr.io/astral-sh/uv:0.10.11@sha256:" in dockerfile
        assert "UV_PROJECT_ENVIRONMENT=/app/.venv" in dockerfile
        assert "COPY --from=builder /app/.venv /app/.venv" in dockerfile
        assert (
            "COPY --chown=10001:10001 --chmod=0444 LICENSE NOTICE "
            "/usr/share/licenses/mevo2glpi/"
        ) in dockerfile
        assert 'org.opencontainers.image.licenses="EUPL-1.2"' in dockerfile
        assert "/app/providers/.venv" not in dockerfile
        assert "providers/uv.lock" not in dockerfile
        assert "providers/pyproject.toml" not in dockerfile
        for member in members:
            manifest = f"{member}/pyproject.toml"
            assert f"COPY {manifest} {manifest}" in dockerfile
        assert "HEALTHCHECK " in dockerfile
        assert "/health" in dockerfile
