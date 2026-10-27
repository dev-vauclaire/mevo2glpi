"""Validate the standalone, staging-only providers Compose contract."""

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
PROVIDERS_DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker" / "providers"
PROVIDERS_COMPOSE_FILE = PROVIDERS_DOCKER_ROOT / "docker-compose.providers.yml"
PROVIDERS_ENV_FILE = PROVIDERS_DOCKER_ROOT / "providers.env.example"

PROVIDER_SERVICES = {
    "microsoft-graph-simulator",
    "transcribe-ai-simulator",
}
DOCUMENTED_SECRET_NAMES = {
    "MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN",
    "TRANSCRIBE_AI_SIMULATOR_API_KEY",
}


def _example_environment() -> dict[str, str]:
    source = PROVIDERS_ENV_FILE.read_text(encoding="utf-8")
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
def providers_compose_config() -> dict[str, object]:
    """Render the provider manifest once without starting any container."""
    documented_names = set(_example_environment())
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(PROVIDERS_ENV_FILE),
            "--file",
            str(PROVIDERS_COMPOSE_FILE),
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
            "Docker Compose could not render the providers manifest "
            f"(exit_code={result.returncode}): {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def test_providers_environment_documents_every_interpolated_variable() -> None:
    compose_source = PROVIDERS_COMPOSE_FILE.read_text(encoding="utf-8")
    example_source = PROVIDERS_ENV_FILE.read_text(encoding="utf-8")
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
    assert documented_values["COMPOSE_PROJECT_NAME"] == "gta-staging-providers"
    assert documented_values["PROVIDERS_NETWORK_NAME"] == (
        "gta-staging-providers-network"
    )
    assert {
        name: documented_values[name] for name in DOCUMENTED_SECRET_NAMES
    } == dict.fromkeys(DOCUMENTED_SECRET_NAMES, "CHANGE_ME")
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password|token)=[0-9a-f]{32,}",
        example_source,
    )


def test_providers_manifest_contains_only_the_two_active_simulators(
    providers_compose_config: dict[str, object],
) -> None:
    assert providers_compose_config["name"] == "gta-staging-providers"
    assert set(_services(providers_compose_config)) == PROVIDER_SERVICES


def test_simulators_are_isolated_and_have_stable_network_aliases(
    providers_compose_config: dict[str, object],
) -> None:
    services = _services(providers_compose_config)
    expected_aliases = {
        "microsoft-graph-simulator": "gta-staging-microsoft-graph",
        "transcribe-ai-simulator": "gta-staging-transcribe-ai",
    }

    for service_name, alias in expected_aliases.items():
        service = services[service_name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["expose"] == ["8080"]
        assert "ports" not in service
        assert set(service["networks"]) == {"providers"}
        assert service["networks"]["providers"]["aliases"] == [alias]


def test_providers_manifest_uses_only_the_shared_provider_network(
    providers_compose_config: dict[str, object],
) -> None:
    assert providers_compose_config["networks"]["providers"] == {
        "name": "gta-staging-providers-network",
        "ipam": {},
        "external": True,
    }
    assert set(providers_compose_config["networks"]) == {"providers"}
    assert providers_compose_config.get("volumes", {}) == {}


def test_optional_glpi_simulation_remains_versioned_but_disabled() -> None:
    compose_source = PROVIDERS_COMPOSE_FILE.read_text(encoding="utf-8")
    dockerfile = (PROVIDERS_DOCKER_ROOT / "glpi-staging" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    bootstrap = (
        PROVIDERS_DOCKER_ROOT / "glpi-staging" / "bootstrap_oauth.php"
    ).read_text(encoding="utf-8")

    assert (
        "glpi/glpi:11.0.7@sha256:"
        "7e100562afe13cce132fe46c0a21ddb94f7ed2c06950da9c4b34ce734a1ad4ac"
    ) in dockerfile
    for disabled_resource in (
        "glpi-db",
        "glpi-staging",
        "glpi-bootstrap",
        "glpi-database",
        "dokploy-network",
        "glpi-data",
        "glpi-database-data",
    ):
        assert re.search(
            rf"(?m)^\s*#\s+{re.escape(disabled_resource)}:", compose_source
        )
    assert "INSERT " not in bootstrap
    assert "UPDATE " not in bootstrap
    assert "DELETE " not in bootstrap
    assert "str_contains(strtoupper($value), 'CHANGE_ME')" in bootstrap


def test_simulator_build_context_contains_only_required_workspace_sources() -> None:
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    workspace = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    members = workspace["tool"]["uv"]["workspace"]["members"]

    for required_path in (
        "!pyproject.toml",
        "!uv.lock",
        "!providers/simulators/microsoft-graph-simulator/pyproject.toml",
        "!providers/simulators/microsoft-graph-simulator/src/**",
        "!providers/simulators/transcribe-ai-simulator/pyproject.toml",
        "!providers/simulators/transcribe-ai-simulator/src/**",
    ):
        assert required_path in dockerignore

    for simulator_name in (
        "microsoft-graph-simulator",
        "transcribe-ai-simulator",
    ):
        dockerfile = (PROVIDERS_DOCKER_ROOT / simulator_name / "Dockerfile").read_text(
            encoding="utf-8"
        )
        assert "python:3.12.14-slim-bookworm@sha256:" in dockerfile
        assert "ghcr.io/astral-sh/uv:0.10.11@sha256:" in dockerfile
        assert "UV_PROJECT_ENVIRONMENT=/app/.venv" in dockerfile
        assert "COPY --from=builder /app/.venv /app/.venv" in dockerfile
        assert "/app/providers/.venv" not in dockerfile
        assert "providers/uv.lock" not in dockerfile
        assert "providers/pyproject.toml" not in dockerfile
        for member in members:
            manifest = f"{member}/pyproject.toml"
            assert f"COPY {manifest} {manifest}" in dockerfile
        assert "HEALTHCHECK " in dockerfile
        assert "/health" in dockerfile
