"""GLPI image, bootstrap and Compose integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from glpi_ticketing_worker_automation.glpi import GLPI_API_VERSION

pytestmark = pytest.mark.glpi

GLPI_TEST_DIRECTORY = Path(__file__).resolve().parent
GLPI_COMPOSE_FILE = GLPI_TEST_DIRECTORY / "compose.yml"
GLPI_DOCKERFILE = GLPI_TEST_DIRECTORY / "image" / "Dockerfile"
GLPI_BOOTSTRAP = GLPI_TEST_DIRECTORY / "image" / "bootstrap_oauth.php"
PINNED_GLPI_API_VERSION = "2.3.0"
PINNED_OPENAPI_VERSION = "3.0.0"
GLPI_API_ROOT = f"/api.php/v{PINNED_GLPI_API_VERSION}"
GLPI_OPENAPI_PATH = f"{GLPI_API_ROOT}/doc.json"
GLPI_TICKET_OPENAPI_PATH = "/Assistance/Ticket"
GLPI_HTTP_TIMEOUT_SECONDS = 10.0


def test_glpi_container_reports_version_11(glpi_environment) -> None:
    assert glpi_environment.version.split(".", maxsplit=1)[0] == "11"


def test_glpi_auto_install_is_complete(glpi_environment) -> None:
    _, _, config_exit_code = glpi_environment.exec_glpi(
        [
            "php",
            "-r",
            "exit(is_file('/var/glpi/config/config_db.php') ? 0 : 1);",
        ]
    )
    _, _, schema_exit_code = glpi_environment.exec_glpi(
        [
            "php",
            "bin/console",
            "database:check_schema_integrity",
            "--no-interaction",
            "--no-ansi",
        ]
    )

    assert config_exit_code == 0
    assert schema_exit_code == 0
    parsed_url = urlparse(glpi_environment.base_url)
    assert parsed_url.hostname == "127.0.0.1"
    assert parsed_url.port not in {None, 80}


def test_glpi_api_contract_is_pinned_to_v2_3_0(glpi_environment) -> None:
    response = httpx.get(
        f"{glpi_environment.base_url}{GLPI_OPENAPI_PATH}",
        follow_redirects=False,
        timeout=GLPI_HTTP_TIMEOUT_SECONDS,
    )

    assert response.status_code == httpx.codes.OK
    try:
        document = response.json()
    except ValueError:
        pytest.fail("The pinned GLPI OpenAPI endpoint returned invalid JSON")

    if not isinstance(document, Mapping):
        pytest.fail("The pinned GLPI OpenAPI endpoint returned an invalid document")

    info = document.get("info")
    paths = document.get("paths")
    if not isinstance(info, Mapping) or not isinstance(paths, Mapping):
        pytest.fail("The pinned GLPI OpenAPI document is missing required sections")

    ticket_path = paths.get(GLPI_TICKET_OPENAPI_PATH)
    if not isinstance(ticket_path, Mapping):
        pytest.fail("The pinned GLPI OpenAPI document is missing the ticket resource")

    assert GLPI_API_VERSION == PINNED_GLPI_API_VERSION
    assert document.get("openapi") == PINNED_OPENAPI_VERSION
    assert info.get("version") == PINNED_GLPI_API_VERSION
    assert isinstance(ticket_path.get("post"), Mapping)


def test_oauth_bootstrap_is_idempotent(glpi_environment) -> None:
    assert glpi_environment.first_bootstrap.is_identical_to(
        glpi_environment.second_bootstrap
    )


def test_custom_image_contains_no_real_secret(glpi_environment) -> None:
    compose_file = GLPI_COMPOSE_FILE.read_text(encoding="utf-8")
    dockerfile = GLPI_DOCKERFILE.read_text(encoding="utf-8")
    bootstrap = GLPI_BOOTSTRAP.read_text(encoding="utf-8")
    source_and_metadata = "\n".join(
        (compose_file, dockerfile, bootstrap, glpi_environment.image_metadata)
    )

    if any(
        secret in source_and_metadata for secret in glpi_environment.ephemeral_secrets
    ):
        pytest.fail("The custom GLPI image contains an ephemeral test secret")

    normalized_dockerfile = dockerfile.lower()
    normalized_compose = compose_file.lower()
    assert "latest" not in normalized_dockerfile + normalized_compose
    assert (
        "from glpi/glpi:11.0.8@sha256:"
        "ca6b53ef948e940f41e1f4af31c001097897e819b66d146d3e5cac51ac7e9f14"
        in normalized_dockerfile
    )
    assert (
        "mysql:8.4.11@sha256:"
        "b3b90af2a6552ae30c266fdb7d5dd55f3afb72404bb78d37fe8a23eb857fd3fb"
        in normalized_compose
    )
    assert ":latest" not in glpi_environment.image_metadata.lower()
    assert "-glpi-test:11.0.8" in glpi_environment.image_metadata.lower()
    assert "env glpi_test_" not in normalized_dockerfile
