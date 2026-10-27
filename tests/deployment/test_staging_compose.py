"""Validate the isolated Dokploy staging contract without real providers."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from .conftest import (
    COMMON_COMPOSE_SERVICES_FILE,
    DOCKER_ROOT,
    STAGING_COMPOSE_FILE,
)

pytestmark = pytest.mark.deployment

STAGING_SERVICES = {
    "database-migrations",
    "web",
    "worker-automation",
    "worker-publisher",
    "worker-retention",
    "provider-simulator",
    "glpi-db",
    "glpi",
    "glpi-bootstrap",
}
COMMON_APPLICATION_SERVICES = {
    "database-migrations",
    "web",
    "worker-automation",
    "worker-publisher",
    "worker-retention",
}


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def _volume_for(service: dict[str, Any], target: str) -> dict[str, Any] | None:
    for volume in service.get("volumes", []):
        if volume["target"] == target:
            return volume
    return None


def test_staging_entrypoint_exposes_every_service_to_dokploy() -> None:
    source = STAGING_COMPOSE_FILE.read_text(encoding="utf-8")

    assert "include:" not in source
    for service_name in COMMON_APPLICATION_SERVICES:
        declaration = f"""  {service_name}:
    extends:
      file: ./docker-compose.common.services.yml
      service: {service_name}
"""
        assert declaration in source
    for service_name in STAGING_SERVICES - COMMON_APPLICATION_SERVICES:
        assert f"  {service_name}:\n" in source


def test_staging_environment_documents_every_interpolated_variable() -> None:
    compose_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            STAGING_COMPOSE_FILE,
            COMMON_COMPOSE_SERVICES_FILE,
        )
    )
    example_source = (DOCKER_ROOT / "staging.env.example").read_text(encoding="utf-8")
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented_values = dict(
        re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", example_source, re.MULTILINE)
    )

    assert set(documented_values) == interpolated_names
    assert documented_values["COMPOSE_PROJECT_NAME"] == "gta-staging"
    assert documented_values["AUDIO_VOLUME_NAME"].startswith("gta-staging-")
    assert documented_values["DATABASE_NETWORK_NAME"].startswith("gta-staging-")
    assert "PUBLISHER_ENTRA_CLIENT_ID" not in documented_values
    assert "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_HOST_PATH" not in documented_values
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password)=[0-9a-f]{32,}",
        example_source,
    )


def test_staging_manifest_isolates_services_networks_and_volumes(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    config = render_staging_compose()
    services = _services(config)

    assert set(services) == STAGING_SERVICES
    assert config["name"] == "gta-staging"
    assert config["networks"]["database"]["name"] == ("gta-staging-database-network")
    assert config["networks"]["database"]["external"] is True
    assert config["networks"]["dokploy-network"]["name"] == "dokploy-network"
    assert config["networks"]["dokploy-network"]["external"] is True
    assert config["networks"]["staging-providers"]["internal"] is True
    assert config["networks"]["glpi-database"]["internal"] is True
    assert config["volumes"] == {
        "audio-data": {"name": "gta-staging-audio-data"},
        "glpi-data": {"name": "gta-staging-glpi-data"},
        "glpi-database-data": {"name": "gta-staging-glpi-database-data"},
    }

    assert set(services["web"]["networks"]) == {"database", "dokploy-network"}
    assert set(services["glpi"]["networks"]) == {
        "dokploy-network",
        "glpi-database",
        "staging-providers",
    }
    assert set(services["provider-simulator"]["networks"]) == {"staging-providers"}
    assert set(services["glpi-db"]["networks"]) == {"glpi-database"}


def test_staging_simulators_and_glpi_have_no_direct_host_port(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())

    for service_name in ("provider-simulator", "glpi", "glpi-db"):
        assert "ports" not in services[service_name]
    assert services["provider-simulator"]["expose"] == ["8080"]
    assert services["glpi"]["expose"] == ["80"]
    assert "dokploy-network" not in services["provider-simulator"]["networks"]
    assert "dokploy-network" in services["glpi"]["networks"]


def test_staging_publisher_cannot_receive_entra_material(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())
    publisher = services["worker-publisher"]
    environment = publisher["environment"]

    assert environment["PUBLISHER_GRAPH_AUTH_MODE"] == "synthetic"
    assert environment["PUBLISHER_GRAPH_BASE_URL"] == (
        "http://provider-simulator:8080/v1.0"
    )
    assert not any(name.startswith("PUBLISHER_ENTRA_") for name in environment)
    assert (
        _volume_for(
            publisher,
            "/run/secrets/publisher-entra-private-key.pem",
        )
        is None
    )
    assert (
        _volume_for(
            publisher,
            "/run/secrets/publisher-entra-certificate.pem",
        )
        is None
    )


def test_staging_credentials_follow_least_privilege(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())
    simulator_environment = services["provider-simulator"]["environment"]
    publisher_environment = services["worker-publisher"]["environment"]
    automation_environment = services["worker-automation"]["environment"]
    web_environment = services["web"]["environment"]

    assert "PROVIDER_SIMULATOR_ADMIN_TOKEN" in simulator_environment
    assert "PROVIDER_SIMULATOR_ADMIN_TOKEN" not in publisher_environment
    assert "PROVIDER_SIMULATOR_ADMIN_TOKEN" not in automation_environment
    assert "PROVIDER_SIMULATOR_TRANSCRIPTION_API_KEY" not in web_environment
    assert "GLPI_DB_ROOT_PASSWORD" not in automation_environment
    assert "GLPI_DB_PASSWORD" not in automation_environment
    assert not any(name.startswith("GLPI_") for name in web_environment)


def test_staging_glpi_is_pinned_bootstrapped_and_persistent(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())
    glpi = services["glpi"]
    bootstrap = services["glpi-bootstrap"]
    dockerfile = (DOCKER_ROOT / "glpi-staging" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    bootstrap_source = (DOCKER_ROOT / "glpi-staging" / "bootstrap_oauth.php").read_text(
        encoding="utf-8"
    )

    assert (
        "glpi/glpi:11.0.7@sha256:"
        "7e100562afe13cce132fe46c0a21ddb94f7ed2c06950da9c4b34ce734a1ad4ac"
    ) in dockerfile
    assert ":latest" not in dockerfile.lower()
    assert glpi["image"] == "gta-staging-glpi:11.0.7"
    assert bootstrap["image"] == glpi["image"]
    assert _volume_for(glpi, "/var/glpi")["source"] == "glpi-data"
    assert _volume_for(bootstrap, "/var/glpi")["source"] == "glpi-data"
    assert bootstrap["restart"] == "no"
    assert bootstrap["depends_on"]["glpi"]["condition"] == "service_started"
    assert ".gta-staging-bootstrap-ready" in glpi["healthcheck"]["test"][1]
    assert "readiness timeout" in bootstrap["entrypoint"][2]
    assert "INSERT " not in bootstrap_source
    assert "UPDATE " not in bootstrap_source
    assert "DELETE " not in bootstrap_source
    assert "GLPI_CLIENT_SECRET" not in dockerfile


def test_provider_simulator_is_included_in_the_restricted_build_context() -> None:
    dockerignore = (DOCKER_ROOT.parent.parent / ".dockerignore").read_text(
        encoding="utf-8"
    )

    for required_path in (
        "!apps/provider-simulator/",
        "!apps/provider-simulator/pyproject.toml",
        "!apps/provider-simulator/src/",
        "!apps/provider-simulator/src/**",
    ):
        assert required_path in dockerignore


def test_staging_dependencies_gate_workers_on_required_bootstraps(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())

    assert set(services["worker-automation"]["depends_on"]) == {
        "database-migrations",
        "provider-simulator",
        "glpi-bootstrap",
    }
    assert set(services["worker-publisher"]["depends_on"]) == {
        "database-migrations",
        "provider-simulator",
    }
    assert set(services["worker-retention"]["depends_on"]) == {
        "database-migrations",
        "glpi-bootstrap",
    }
    assert services["glpi"]["depends_on"]["glpi-db"]["condition"] == ("service_healthy")
