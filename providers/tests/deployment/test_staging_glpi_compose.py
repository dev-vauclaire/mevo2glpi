"""Validate the autonomous staging GLPI Compose contract."""

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
GLPI_DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker" / "providers" / "glpi-test"
GLPI_COMPOSE_FILE = GLPI_DOCKER_ROOT / "docker-compose.staging.yml"
GLPI_ENV_FILE = GLPI_DOCKER_ROOT / "staging.env.example"

EXPECTED_SERVICES = {"glpi-db", "glpi-test", "glpi-bootstrap"}
PLACEHOLDER_SECRET_NAMES = {
    "GLPI_TEST_CLIENT_SECRET",
    "GLPI_TEST_DB_PASSWORD",
    "GLPI_TEST_DB_ROOT_PASSWORD",
    "GLPI_TEST_PASSWORD",
}


def _example_environment() -> dict[str, str]:
    source = GLPI_ENV_FILE.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def _clean_environment(documented_names: set[str]) -> dict[str, str]:
    """Prevent the host environment from changing the rendered contract."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in documented_names and not name.startswith("COMPOSE_")
    }


@pytest.fixture(scope="module")
def staging_glpi_compose_config() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(GLPI_ENV_FILE),
            "--file",
            str(GLPI_COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=_clean_environment(set(_example_environment())),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "Docker Compose could not render the staging GLPI manifest "
            f"(exit_code={result.returncode}): {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def test_staging_glpi_environment_documents_every_interpolated_variable() -> None:
    compose_source = GLPI_COMPOSE_FILE.read_text(encoding="utf-8")
    interpolated_names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_source))
    documented_values = _example_environment()

    assert set(documented_values) == interpolated_names
    assert documented_values["COMPOSE_PROJECT_NAME"] == "mevo2glpi-staging-glpi"
    assert documented_values["DOKPLOY_NETWORK_NAME"] == "dokploy-network"
    assert documented_values["GLPI_TEST_USERNAME"].startswith("glpi-test-")
    assert documented_values["GLPI_TEST_OAUTH_CLIENT_NAME"].startswith("glpi-test-")
    assert {
        name: documented_values[name].startswith("CHANGE_ME")
        for name in PLACEHOLDER_SECRET_NAMES
    } == dict.fromkeys(PLACEHOLDER_SECRET_NAMES, True)


def test_staging_glpi_manifest_contains_only_expected_services(
    staging_glpi_compose_config: dict[str, object],
) -> None:
    assert staging_glpi_compose_config["name"] == "mevo2glpi-staging-glpi"
    assert set(_services(staging_glpi_compose_config)) == EXPECTED_SERVICES


def test_staging_glpi_uses_pinned_images_and_private_database(
    staging_glpi_compose_config: dict[str, object],
) -> None:
    services = _services(staging_glpi_compose_config)
    database = services["glpi-db"]

    assert database["image"] == (
        "mysql:8.4.11@sha256:"
        "b3b90af2a6552ae30c266fdb7d5dd55f3afb72404bb78d37fe8a23eb857fd3fb"
    )
    assert set(database["networks"]) == {"glpi-database"}
    assert "ports" not in database
    assert "expose" not in database
    assert staging_glpi_compose_config["networks"]["glpi-database"]["internal"] is True

    dockerfile = (GLPI_DOCKER_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "FROM glpi/glpi:11.0.7@sha256:"
        "7e100562afe13cce132fe46c0a21ddb94f7ed2c06950da9c4b34ce734a1ad4ac"
    ) in dockerfile
    assert ":latest" not in dockerfile


def test_only_glpi_http_service_joins_dokploy_network(
    staging_glpi_compose_config: dict[str, object],
) -> None:
    services = _services(staging_glpi_compose_config)

    assert set(services["glpi-test"]["networks"]) == {
        "glpi-database",
        "dokploy-network",
    }
    assert services["glpi-test"]["expose"] == ["80"]
    assert "ports" not in services["glpi-test"]
    assert set(services["glpi-bootstrap"]["networks"]) == {"glpi-database"}
    assert (
        staging_glpi_compose_config["networks"]["dokploy-network"]["external"] is True
    )


def test_bootstrap_credentials_are_not_exposed_to_long_running_services(
    staging_glpi_compose_config: dict[str, object],
) -> None:
    services = _services(staging_glpi_compose_config)
    bootstrap_only_names = {
        "GLPI_TEST_CLIENT_ID",
        "GLPI_TEST_CLIENT_SECRET",
        "GLPI_TEST_OAUTH_CLIENT_NAME",
        "GLPI_TEST_PASSWORD",
        "GLPI_TEST_REQUEST_SOURCE_NAME",
        "GLPI_TEST_USERNAME",
    }

    assert bootstrap_only_names <= set(services["glpi-bootstrap"]["environment"])
    for service_name in ("glpi-db", "glpi-test"):
        assert bootstrap_only_names.isdisjoint(services[service_name]["environment"])


def test_staging_glpi_persists_database_and_application_state(
    staging_glpi_compose_config: dict[str, object],
) -> None:
    services = _services(staging_glpi_compose_config)

    assert services["glpi-db"]["volumes"] == [
        {
            "type": "volume",
            "source": "glpi-database-data",
            "target": "/var/lib/mysql",
            "volume": {},
        }
    ]
    expected_glpi_volume = {
        "type": "volume",
        "source": "glpi-data",
        "target": "/var/glpi",
        "volume": {},
    }
    assert services["glpi-test"]["volumes"] == [expected_glpi_volume]
    assert services["glpi-bootstrap"]["volumes"] == [expected_glpi_volume]
    assert staging_glpi_compose_config["volumes"] == {
        "glpi-data": {"name": "mevo2glpi-staging-glpi-data"},
        "glpi-database-data": {"name": "mevo2glpi-staging-glpi-database-data"},
    }


def test_staging_glpi_bootstrap_is_one_shot_and_idempotent_by_contract(
    staging_glpi_compose_config: dict[str, object],
) -> None:
    bootstrap = _services(staging_glpi_compose_config)["glpi-bootstrap"]
    entrypoint = "\n".join(bootstrap["entrypoint"])

    assert "bootstrap_oauth.php" in entrypoint
    assert "bootstrap_request_source.php" in entrypoint
    assert bootstrap["depends_on"]["glpi-test"]["condition"] == "service_healthy"
    assert bootstrap["restart"] == "no"
    assert bootstrap["read_only"] is True
    assert bootstrap["cap_drop"] == ["ALL"]
    assert bootstrap["security_opt"] == ["no-new-privileges:true"]

    oauth_bootstrap = (GLPI_DOCKER_ROOT / "bootstrap_oauth.php").read_text(
        encoding="utf-8"
    )
    source_bootstrap = (GLPI_DOCKER_ROOT / "bootstrap_request_source.php").read_text(
        encoding="utf-8"
    )
    assert "getFromDBByCrit" in oauth_bootstrap
    assert "getFromDBByCrit" in source_bootstrap
    assert "INSERT INTO" not in oauth_bootstrap.upper()
    assert "INSERT INTO" not in source_bootstrap.upper()


def test_staging_glpi_examples_contain_no_real_secret() -> None:
    example_source = GLPI_ENV_FILE.read_text(encoding="utf-8")

    assert not re.search(
        r"(?i)(?:password|secret|token)=[0-9a-f]{32,}",
        example_source,
    )
    assert all(
        value.startswith("CHANGE_ME")
        for name, value in _example_environment().items()
        if name in PLACEHOLDER_SECRET_NAMES
    )
