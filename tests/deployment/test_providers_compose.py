"""Validate the standalone, staging-only providers Compose contract."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from .conftest import (
    PROVIDERS_COMPOSE_FILE,
    PROVIDERS_ENV_FILE,
)

pytestmark = pytest.mark.deployment

PROVIDER_SERVICES = {
    "microsoft-graph-simulator",
    "transcribe-ai-simulator",
    "glpi-db",
    "glpi-staging",
    "glpi-bootstrap",
}
DOCUMENTED_SECRET_NAMES = {
    "GLPI_ADMIN_PASSWORD",
    "GLPI_CLIENT_SECRET",
    "GLPI_DB_PASSWORD",
    "GLPI_DB_ROOT_PASSWORD",
    "GLPI_PASSWORD",
    "MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN",
    "TRANSCRIBE_AI_SIMULATOR_API_KEY",
}


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def test_providers_environment_documents_every_interpolated_variable() -> None:
    compose_source = PROVIDERS_COMPOSE_FILE.read_text(encoding="utf-8")
    example_source = PROVIDERS_ENV_FILE.read_text(encoding="utf-8")
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented_values = dict(
        re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", example_source, re.MULTILINE)
    )

    assert set(documented_values) == interpolated_names
    assert documented_values["COMPOSE_PROJECT_NAME"] == "gta-staging-providers"
    assert documented_values["PROVIDERS_NETWORK_NAME"] == (
        "gta-staging-providers-network"
    )
    assert documented_values["GLPI_DATA_VOLUME_NAME"].startswith(
        "gta-staging-providers-"
    )
    assert documented_values["GLPI_DATABASE_VOLUME_NAME"].startswith(
        "gta-staging-providers-"
    )
    assert {
        name: documented_values[name] for name in DOCUMENTED_SECRET_NAMES
    } == dict.fromkeys(DOCUMENTED_SECRET_NAMES, "CHANGE_ME")
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password|token)=[0-9a-f]{32,}",
        example_source,
    )


def test_providers_manifest_contains_only_the_five_expected_services(
    render_providers_compose: Callable[[], dict[str, object]],
) -> None:
    config = render_providers_compose()

    assert config["name"] == "gta-staging-providers"
    assert set(_services(config)) == PROVIDER_SERVICES


def test_simulators_are_isolated_and_have_stable_network_aliases(
    render_providers_compose: Callable[[], dict[str, object]],
) -> None:
    config = render_providers_compose()
    services = _services(config)
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


def test_glpi_networks_keep_mysql_private_and_traefik_separate(
    render_providers_compose: Callable[[], dict[str, object]],
) -> None:
    config = render_providers_compose()
    services = _services(config)

    assert config["networks"]["providers"] == {
        "name": "gta-staging-providers-network",
        "ipam": {},
        "external": True,
    }
    assert config["networks"]["glpi-database"]["internal"] is True
    assert config["networks"]["dokploy-network"]["external"] is True
    assert set(services["glpi-db"]["networks"]) == {"glpi-database"}
    assert set(services["glpi-bootstrap"]["networks"]) == {"glpi-database"}
    assert set(services["glpi-staging"]["networks"]) == {
        "glpi-database",
        "providers",
        "dokploy-network",
    }
    assert services["glpi-staging"]["networks"]["providers"]["aliases"] == [
        "gta-staging-glpi"
    ]
    assert services["glpi-staging"]["expose"] == ["80"]
    assert "ports" not in services["glpi-staging"]


def test_glpi_images_bootstrap_and_fresh_volumes_are_explicit(
    render_providers_compose: Callable[[], dict[str, object]],
) -> None:
    config = render_providers_compose()
    services = _services(config)
    compose_root = PROVIDERS_COMPOSE_FILE.parent
    dockerfile = (compose_root / "glpi-staging" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    bootstrap = (compose_root / "glpi-staging" / "bootstrap_oauth.php").read_text(
        encoding="utf-8"
    )

    assert (
        "glpi/glpi:11.0.7@sha256:"
        "7e100562afe13cce132fe46c0a21ddb94f7ed2c06950da9c4b34ce734a1ad4ac"
    ) in dockerfile
    assert services["glpi-db"]["image"].startswith("mysql:8.4.11@sha256:")
    assert services["glpi-staging"]["image"] == ("gta-staging-providers-glpi:11.0.7")
    assert services["glpi-bootstrap"]["image"] == services["glpi-staging"]["image"]
    assert services["glpi-staging"]["depends_on"]["glpi-db"]["condition"] == (
        "service_healthy"
    )
    assert (
        services["glpi-bootstrap"]["depends_on"]["glpi-staging"]["condition"]
        == "service_started"
    )
    assert services["glpi-bootstrap"]["restart"] == "no"
    assert "INSERT " not in bootstrap
    assert "UPDATE " not in bootstrap
    assert "DELETE " not in bootstrap
    assert "str_contains(strtoupper($value), 'CHANGE_ME')" in bootstrap
    mysql_healthcheck = " ".join(services["glpi-db"]["healthcheck"]["test"])
    assert "CHANGE_ME" in mysql_healthcheck
    assert config["volumes"] == {
        "glpi-data": {"name": "gta-staging-providers-glpi-data"},
        "glpi-database-data": {"name": "gta-staging-providers-glpi-database-data"},
    }


def test_simulator_build_context_contains_only_required_workspace_sources() -> None:
    repository_root = PROVIDERS_COMPOSE_FILE.parents[3]
    dockerignore = (repository_root / ".dockerignore").read_text(encoding="utf-8")

    for required_path in (
        "!providers/pyproject.toml",
        "!providers/uv.lock",
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
        dockerfile = (
            PROVIDERS_COMPOSE_FILE.parent / simulator_name / "Dockerfile"
        ).read_text(encoding="utf-8")
        assert "python:3.12.14-slim-bookworm@sha256:" in dockerfile
        assert "ghcr.io/astral-sh/uv:0.10.11@sha256:" in dockerfile
        assert "UV_PROJECT_ENVIRONMENT=/app/.venv" in dockerfile
        assert "COPY --from=builder /app/.venv /app/.venv" in dockerfile
        assert "/app/providers/.venv" not in dockerfile
        assert "HEALTHCHECK " in dockerfile
        assert "/health" in dockerfile
