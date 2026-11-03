"""Validate the persistent PostgreSQL stack used for local development."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.deployment

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = (
    REPOSITORY_ROOT / "infra" / "docker" / "development" / "docker-compose.postgres.yml"
)
ROOT_ENV_EXAMPLE = REPOSITORY_ROOT / ".env.example"
POSTGRES_IMAGE = (
    "postgres:18.6-alpine3.24@sha256:"
    "10a5c69a83035ab5239f5620d708520e8b28f5aa1f2b20143e6024532edadfb5"
)
DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://gta_dev:gta_dev_only@127.0.0.1:5432/glpi_ticketing_dev"
)


def _clean_environment(*, published_port: str | None = None) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("COMPOSE_") and name != "GTA_DEV_POSTGRES_PORT"
    }
    if published_port is not None:
        environment["GTA_DEV_POSTGRES_PORT"] = published_port
    return environment


def _render_compose(*, published_port: str | None = None) -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=_clean_environment(published_port=published_port),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            "Docker Compose could not render the development PostgreSQL manifest "
            f"(exit_code={result.returncode}): {result.stderr.strip()}"
        )
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


@pytest.fixture(scope="module")
def postgres_compose_config() -> dict[str, object]:
    """Render the manifest once without pulling or starting PostgreSQL."""
    return _render_compose()


def _services(config: dict[str, object]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    assert isinstance(services, dict)
    return services


def _root_example_environment() -> dict[str, str]:
    source = ROOT_ENV_EXAMPLE.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def test_development_postgres_manifest_contains_only_postgres(
    postgres_compose_config: dict[str, object],
) -> None:
    assert postgres_compose_config["name"] == "gta-development-postgres"
    assert set(_services(postgres_compose_config)) == {"postgres"}


def test_development_postgres_uses_pinned_local_configuration(
    postgres_compose_config: dict[str, object],
) -> None:
    service = _services(postgres_compose_config)["postgres"]

    assert service["image"] == POSTGRES_IMAGE
    assert "latest" not in service["image"].casefold()
    assert service["command"] == ["postgres", "-c", "timezone=UTC"]
    assert service["environment"] == {
        "POSTGRES_DB": "glpi_ticketing_dev",
        "POSTGRES_PASSWORD": "gta_dev_only",
        "POSTGRES_USER": "gta_dev",
    }


def test_development_postgres_is_exposed_only_on_loopback(
    postgres_compose_config: dict[str, object],
) -> None:
    service = _services(postgres_compose_config)["postgres"]

    assert service["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "protocol": "tcp",
            "published": "5432",
            "target": 5432,
        }
    ]


def test_development_postgres_port_can_be_overridden() -> None:
    config = _render_compose(published_port="55432")
    service = _services(config)["postgres"]

    assert service["ports"][0]["host_ip"] == "127.0.0.1"
    assert service["ports"][0]["published"] == "55432"
    assert service["ports"][0]["target"] == 5432


def test_development_postgres_has_a_readiness_probe(
    postgres_compose_config: dict[str, object],
) -> None:
    healthcheck = _services(postgres_compose_config)["postgres"]["healthcheck"]

    assert healthcheck == {
        "interval": "1s",
        "retries": 60,
        "start_period": "2s",
        "test": [
            "CMD-SHELL",
            (
                "pg_isready --host=127.0.0.1 --username=$$POSTGRES_USER "
                "--dbname=$$POSTGRES_DB"
            ),
        ],
        "timeout": "5s",
    }


def test_development_postgres_persists_data_in_a_named_volume(
    postgres_compose_config: dict[str, object],
) -> None:
    service = _services(postgres_compose_config)["postgres"]

    assert service["volumes"] == [
        {
            "source": "postgres-data",
            "target": "/var/lib/postgresql",
            "type": "volume",
            "volume": {},
        }
    ]
    assert postgres_compose_config["volumes"] == {
        "postgres-data": {"name": "gta-development-postgres18-data"}
    }


def test_development_postgres_has_bounded_runtime_defaults(
    postgres_compose_config: dict[str, object],
) -> None:
    service = _services(postgres_compose_config)["postgres"]

    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "30s"
    assert service["shm_size"] == "134217728"
    assert service["logging"] == {
        "driver": "json-file",
        "options": {"max-file": "3", "max-size": "10m"},
    }


def test_root_environment_matches_development_postgres_defaults() -> None:
    database_url = _root_example_environment()["DATABASE_URL"]
    parsed_url = urlsplit(database_url)

    assert database_url == DEFAULT_DATABASE_URL
    assert parsed_url.scheme == "postgresql+psycopg"
    assert parsed_url.hostname == "127.0.0.1"
    assert parsed_url.port == 5432
    assert parsed_url.username == "gta_dev"
    assert parsed_url.password == "gta_dev_only"
    assert parsed_url.path == "/glpi_ticketing_dev"
