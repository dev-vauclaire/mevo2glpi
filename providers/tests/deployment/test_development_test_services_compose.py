"""Validate the loopback-only development test-services stack."""

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
COMPOSE_FILE = (
    REPOSITORY_ROOT
    / "infra"
    / "docker"
    / "development"
    / "docker-compose.test-services.yml"
)
ROOT_ENV_EXAMPLE = REPOSITORY_ROOT / ".env.example"
EXPECTED_SERVICES = {
    "glpi-bootstrap",
    "glpi-db",
    "glpi-test",
    "microsoft-graph-simulator",
    "openai-transcription-simulator",
    "transcribe-ai-simulator",
}


def _clean_environment() -> dict[str, str]:
    controlled_prefixes = (
        "COMPOSE_",
        "GLPI_",
        "GTA_DEV_",
        "LEGACY_TRANSCRIPTION_",
        "MICROSOFT_GRAPH_",
        "OPENAI_",
        "PUBLISHER_",
        "TRANSCRIBE_AI_",
    )
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(controlled_prefixes)
    }


def _example_environment() -> dict[str, str]:
    source = ROOT_ENV_EXAMPLE.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def _render_compose(**overrides: str) -> dict[str, object]:
    environment = {
        **_clean_environment(),
        **overrides,
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT_ENV_EXAMPLE),
            "--file",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "Docker Compose could not render the development test-services "
            f"manifest (exit_code={result.returncode}): {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(scope="module")
def test_services_config() -> dict[str, object]:
    return _render_compose()


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def test_development_manifest_contains_only_expected_test_services(
    test_services_config: dict[str, object],
) -> None:
    assert test_services_config["name"] == "gta-development-test-services"
    assert set(_services(test_services_config)) == EXPECTED_SERVICES


def test_development_http_services_are_bound_only_to_loopback(
    test_services_config: dict[str, object],
) -> None:
    services = _services(test_services_config)
    expected_ports = {
        "microsoft-graph-simulator": (18080, 8080),
        "transcribe-ai-simulator": (18081, 8080),
        "openai-transcription-simulator": (18082, 8080),
        "glpi-test": (18083, 80),
    }

    for service_name, (published, target) in expected_ports.items():
        assert services[service_name]["ports"] == [
            {
                "host_ip": "127.0.0.1",
                "mode": "ingress",
                "protocol": "tcp",
                "published": str(published),
                "target": target,
            }
        ]
    assert "ports" not in services["glpi-db"]
    assert "ports" not in services["glpi-bootstrap"]


def test_development_http_ports_are_configurable() -> None:
    config = _render_compose(
        GTA_DEV_MICROSOFT_GRAPH_PORT="28080",
        GTA_DEV_TRANSCRIBE_AI_PORT="28081",
        GTA_DEV_OPENAI_TRANSCRIPTION_PORT="28082",
        GTA_DEV_GLPI_PORT="28083",
    )
    services = _services(config)

    assert services["microsoft-graph-simulator"]["ports"][0]["published"] == "28080"
    assert services["transcribe-ai-simulator"]["ports"][0]["published"] == "28081"
    assert (
        services["openai-transcription-simulator"]["ports"][0]["published"] == "28082"
    )
    assert services["glpi-test"]["ports"][0]["published"] == "28083"


def test_root_environment_targets_the_loopback_test_services() -> None:
    environment = _example_environment()

    assert environment["PUBLISHER_GRAPH_AUTH_MODE"] == "synthetic"
    assert environment["PUBLISHER_GRAPH_BASE_URL"] == ("http://127.0.0.1:18080/v1.0")
    assert environment["LEGACY_TRANSCRIPTION_BASE_URL"] == ("http://127.0.0.1:18081")
    assert environment["OPENAI_COMPATIBLE_TRANSCRIPTION_BASE_URL"] == (
        "http://127.0.0.1:18082/v1"
    )
    assert environment["GLPI_BASE_URL"] == "http://127.0.0.1:18083"
    assert environment["GLPI_TICKET_REQUEST_SOURCE_ID"] == ""


def test_glpi_database_is_private_and_persistent(
    test_services_config: dict[str, object],
) -> None:
    services = _services(test_services_config)
    networks = test_services_config["networks"]

    assert set(services["glpi-db"]["networks"]) == {"glpi-database"}
    assert set(services["glpi-test"]["networks"]) == {
        "glpi-database",
        "test-services",
    }
    assert networks["glpi-database"]["internal"] is True
    assert services["glpi-db"]["volumes"] == [
        {
            "source": "glpi-database-data",
            "target": "/var/lib/mysql",
            "type": "volume",
            "volume": {},
        }
    ]
    assert services["glpi-test"]["volumes"] == [
        {
            "source": "glpi-data",
            "target": "/var/glpi",
            "type": "volume",
            "volume": {},
        }
    ]
    assert test_services_config["volumes"] == {
        "glpi-data": {"name": "gta-development-glpi-data"},
        "glpi-database-data": {"name": "gta-development-glpi-database-data"},
    }


def test_glpi_uses_the_canonical_test_image_and_idempotent_bootstrap(
    test_services_config: dict[str, object],
) -> None:
    services = _services(test_services_config)
    glpi = services["glpi-test"]
    bootstrap = services["glpi-bootstrap"]

    assert Path(glpi["build"]["context"]).resolve() == (
        REPOSITORY_ROOT / "infra" / "docker" / "providers" / "glpi-test"
    )
    assert glpi["build"]["dockerfile"] == "Dockerfile"
    assert bootstrap["image"] == glpi["image"]
    command = " ".join(bootstrap["entrypoint"])
    assert "bootstrap_oauth.php >/dev/null" in command
    assert "bootstrap_request_source.php >/dev/null" in command
    assert bootstrap["restart"] == "no"


def test_simulators_are_hardened_and_use_synthetic_configuration(
    test_services_config: dict[str, object],
) -> None:
    services = _services(test_services_config)
    for service_name in (
        "microsoft-graph-simulator",
        "transcribe-ai-simulator",
        "openai-transcription-simulator",
    ):
        service = services[service_name]
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["pids_limit"] == 128

    assert services["openai-transcription-simulator"]["environment"] == {
        "OPENAI_TRANSCRIPTION_SIMULATOR_API_KEY": ("synthetic-development-openai-key"),
        "OPENAI_TRANSCRIPTION_SIMULATOR_HOST": "0.0.0.0",
        "OPENAI_TRANSCRIPTION_SIMULATOR_MAX_UPLOAD_BYTES": "26214400",
        "OPENAI_TRANSCRIPTION_SIMULATOR_PORT": "8080",
    }
