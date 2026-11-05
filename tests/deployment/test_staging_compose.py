"""Validate staging with separate simulator and transcription stacks."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from .conftest import (
    APPS_DOCKER_ROOT,
    COMMON_COMPOSE_SERVICES_FILE,
    STAGING_COMPOSE_FILE,
    SYNTHETIC_APP_COMMIT_SHA,
)

pytestmark = pytest.mark.deployment

STAGING_SERVICES = {
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


def test_staging_entrypoint_exposes_only_the_five_applications() -> None:
    source = STAGING_COMPOSE_FILE.read_text(encoding="utf-8")

    assert "include:" not in source
    for service_name in STAGING_SERVICES:
        declaration = f"""  {service_name}:
    extends:
      file: ./docker-compose.common.services.yml
      service: {service_name}
"""
        assert declaration in source

    for removed_service in (
        "provider-simulator",
        "parakeet",
        "transcription-api",
        "glpi-db",
        "glpi",
        "glpi-bootstrap",
    ):
        assert f"  {removed_service}:\n" not in source


def test_staging_environment_documents_every_interpolated_variable() -> None:
    compose_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            STAGING_COMPOSE_FILE,
            COMMON_COMPOSE_SERVICES_FILE,
        )
    )
    example_source = (APPS_DOCKER_ROOT / "staging.env.example").read_text(
        encoding="utf-8"
    )
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented_values = dict(
        re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", example_source, re.MULTILINE)
    )

    assert set(documented_values) == interpolated_names
    assert documented_values["COMPOSE_PROJECT_NAME"] == "gta-staging"
    assert documented_values["APP_ENVIRONMENT"] == "staging"
    assert documented_values["APP_RELEASE"] == "main"
    assert documented_values["APP_COMMIT_SHA"] == "CHANGE_ME"
    assert documented_values["GLPI_TICKET_TYPE"] == "1"
    assert documented_values["GLPI_TICKET_REQUEST_SOURCE_ID"] == (
        "CHANGE_ME_WITH_THE_STAGING_MEVO_REQUEST_SOURCE_ID"
    )
    assert documented_values["AUDIO_VOLUME_NAME"].startswith("gta-staging-")
    assert documented_values["DATABASE_NETWORK_NAME"].startswith("gta-staging-")
    assert "@postgres-staging:5432/" in documented_values["DATABASE_URL"]
    assert "sslmode=disable" in documented_values["DATABASE_URL"]
    assert "sslmode=verify-full" not in documented_values["DATABASE_URL"]
    assert documented_values["SIMULATORS_NETWORK_NAME"] == (
        "gta-staging-simulators-network"
    )
    assert documented_values["TRANSCRIPTION_NETWORK_NAME"] == (
        "gta-staging-transcription-network"
    )
    assert documented_values["TRANSCRIPTION_PROVIDER"] == "openai_compatible"
    assert documented_values["PUBLISHER_GRAPH_BASE_URL"] == (
        "http://gta-staging-microsoft-graph:8080/v1.0"
    )
    assert documented_values["LEGACY_TRANSCRIPTION_BASE_URL"] == (
        "http://gta-staging-transcribe-ai:8080"
    )
    assert documented_values["TRANSCRIPTION_HTTP_TIMEOUT_SECONDS"] == "120"
    assert documented_values["ORIGINATING_PHONE_NUMBER_KEY_HOST_PATH"] == (
        "/etc/gta/secrets/staging-originating-phone-number.key"
    )
    assert documented_values["GLPI_BASE_URL"].startswith("https://")
    assert "PUBLISHER_ENTRA_CLIENT_ID" not in documented_values
    assert "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_HOST_PATH" not in documented_values
    assert not any(name.startswith("PROVIDER_SIMULATOR_") for name in documented_values)
    assert not any(name.startswith("GLPI_DB_") for name in documented_values)
    assert "GLPI_ADMIN_USERNAME" not in documented_values
    assert "GLPI_OAUTH_CLIENT_NAME" not in documented_values
    assert not re.search(
        r"(?i)(?:api[_-]?key|secret|password)=[0-9a-f]{32,}",
        example_source,
    )


def test_staging_manifest_uses_separate_external_dependency_networks(
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
    assert config["networks"]["simulators"]["name"] == (
        "gta-staging-simulators-network"
    )
    assert config["networks"]["simulators"]["external"] is True
    assert config["networks"]["transcription"]["name"] == (
        "gta-staging-transcription-network"
    )
    assert config["networks"]["transcription"]["external"] is True
    assert set(config["networks"]) == {
        "database",
        "dokploy-network",
        "simulators",
        "transcription",
    }
    assert config["volumes"] == {"audio-data": {"name": "gta-staging-audio-data"}}

    assert set(services["database-migrations"]["networks"]) == {"database"}
    assert set(services["web"]["networks"]) == {"database", "dokploy-network"}
    assert set(services["worker-automation"]["networks"]) == {
        "database",
        "simulators",
        "transcription",
    }
    assert set(services["worker-publisher"]["networks"]) == {
        "database",
        "simulators",
    }
    assert set(services["worker-retention"]["networks"]) == {"database"}


def test_staging_services_share_the_release_identity(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())

    for service in services.values():
        assert service["build"]["args"] == {
            "IMAGE_VERSION": "main",
            "VCS_REF": SYNTHETIC_APP_COMMIT_SHA,
        }
        assert service["environment"]["APP_ENVIRONMENT"] == "staging"


def test_staging_uses_the_separated_simulator_and_transcription_services(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())
    publisher_environment = services["worker-publisher"]["environment"]
    automation_environment = services["worker-automation"]["environment"]

    assert publisher_environment["PUBLISHER_GRAPH_AUTH_MODE"] == "synthetic"
    assert publisher_environment["PUBLISHER_GRAPH_BASE_URL"] == (
        "http://gta-staging-microsoft-graph:8080/v1.0"
    )
    assert automation_environment["LEGACY_TRANSCRIPTION_BASE_URL"] == (
        "http://gta-staging-transcribe-ai:8080"
    )
    assert automation_environment["TRANSCRIPTION_PROVIDER"] == "openai_compatible"
    assert automation_environment["OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL"] == (
        "http://gta-staging-transcription:5092/v1"
    )
    assert automation_environment["OPENAI_COMPATIBLE_TRANSCRIPTION_MODEL"] == (
        "parakeet-ultra"
    )
    assert automation_environment["OPENAI_COMPATIBLE_TRANSCRIPTION_LANGUAGE"] == "fr"
    assert automation_environment["TRANSCRIPTION_HTTP_TIMEOUT_SECONDS"] == "120"
    assert automation_environment["GLPI_TICKET_TYPE"] == "1"
    assert automation_environment["GLPI_TICKET_REQUEST_SOURCE_ID"] == (
        "CHANGE_ME_WITH_THE_STAGING_MEVO_REQUEST_SOURCE_ID"
    )
    assert "LEGACY_TRANSCRIPTION_API_KEY" in automation_environment
    assert "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY" in automation_environment
    assert not any(
        name.startswith("PUBLISHER_ENTRA_") for name in publisher_environment
    )
    assert (
        _volume_for(
            services["worker-publisher"],
            "/run/secrets/publisher-entra-private-key.pem",
        )
        is None
    )

    phone_key_target = "/run/secrets/originating-phone-number.key"
    for service_name in ("worker-publisher", "worker-automation"):
        service = services[service_name]
        phone_key = _volume_for(service, phone_key_target)
        assert phone_key is not None
        assert phone_key["read_only"] is True
        assert service["environment"]["ORIGINATING_PHONE_NUMBER_KEY_PATH"] == (
            phone_key_target
        )
    assert (
        _volume_for(
            services["worker-publisher"],
            "/run/secrets/publisher-entra-certificate.pem",
        )
        is None
    )


def test_staging_glpi_is_external_https_and_uses_the_ca_bundle(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())
    ca_target = "/etc/ssl/certs/gta-ca-bundle.pem"
    automation = services["worker-automation"]
    retention = services["worker-retention"]

    for worker in (automation, retention):
        environment = worker["environment"]
        assert environment["GLPI_BASE_URL"].startswith("https://")
        assert environment["SSL_CERT_FILE"] == ca_target
        assert _volume_for(worker, ca_target) is not None
        for credential_name in (
            "GLPI_USERNAME",
            "GLPI_PASSWORD",
            "GLPI_CLIENT_ID",
            "GLPI_CLIENT_SECRET",
        ):
            assert credential_name in environment

    assert (
        automation["environment"]["GLPI_BASE_URL"]
        == (retention["environment"]["GLPI_BASE_URL"])
    )


def test_staging_credentials_follow_least_privilege(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())
    publisher_environment = services["worker-publisher"]["environment"]
    automation_environment = services["worker-automation"]["environment"]
    retention_environment = services["worker-retention"]["environment"]
    web_environment = services["web"]["environment"]

    assert "TRANSCRIBE_AI_SIMULATOR_API_KEY" not in publisher_environment
    assert "LEGACY_TRANSCRIPTION_API_KEY" not in publisher_environment
    assert "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY" not in publisher_environment
    assert not any(name.startswith("GLPI_") for name in publisher_environment)
    assert "LEGACY_TRANSCRIPTION_API_KEY" not in retention_environment
    assert "LEGACY_TRANSCRIPTION_API_KEY" in automation_environment
    assert "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY" not in retention_environment
    assert "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY" in automation_environment
    assert not any(name.startswith("GLPI_") for name in web_environment)
    assert "LEGACY_TRANSCRIPTION_API_KEY" not in web_environment
    assert "OPENAI_COMPATIBLE_TRANSCRIPTION_API_KEY" not in web_environment
    for environment in (
        publisher_environment,
        retention_environment,
        web_environment,
    ):
        assert "GLPI_TICKET_REQUEST_SOURCE_ID" not in environment


def test_staging_workers_depend_only_on_database_migrations(
    render_staging_compose: Callable[[], dict[str, object]],
) -> None:
    services = _services(render_staging_compose())

    for service_name in (
        "web",
        "worker-automation",
        "worker-publisher",
        "worker-retention",
    ):
        assert services[service_name]["depends_on"] == {
            "database-migrations": {
                "condition": "service_completed_successfully",
                "required": True,
            }
        }
