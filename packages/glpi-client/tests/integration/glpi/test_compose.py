"""GLPI image, bootstrap and Compose integration tests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from mevo2glpi_glpi import GLPI_API_VERSION

pytestmark = pytest.mark.glpi

GLPI_TEST_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
GLPI_IMAGE_DIRECTORY = REPOSITORY_ROOT / "infra" / "docker" / "providers" / "glpi-test"
GLPI_COMPOSE_FILE = GLPI_TEST_DIRECTORY / "compose.yml"
GLPI_DOCKERFILE = GLPI_IMAGE_DIRECTORY / "Dockerfile"
GLPI_BOOTSTRAP = GLPI_IMAGE_DIRECTORY / "bootstrap_oauth.php"
GLPI_REQUEST_SOURCE_BOOTSTRAP = GLPI_IMAGE_DIRECTORY / "bootstrap_request_source.php"
GLPI_STATUS_HELPER = GLPI_IMAGE_DIRECTORY / "set_ticket_status.php"
PINNED_GLPI_API_VERSION = "2.3.0"
PINNED_OPENAPI_VERSION = "3.0.0"
GLPI_API_ROOT = f"/api.php/v{PINNED_GLPI_API_VERSION}"
GLPI_OPENAPI_PATH = f"{GLPI_API_ROOT}/doc.json"
GLPI_TICKET_OPENAPI_PATH = "/Assistance/Ticket"
GLPI_TICKET_BY_ID_OPENAPI_PATH = "/Assistance/Ticket/{id}"
GLPI_REQUEST_TYPE_OPENAPI_PATH = "/Dropdowns/RequestType"
GLPI_HTTP_TIMEOUT_SECONDS = 10.0


def test_glpi_compose_uses_the_canonical_test_image() -> None:
    compose_file = GLPI_COMPOSE_FILE.read_text(encoding="utf-8")

    assert GLPI_IMAGE_DIRECTORY.is_dir()
    assert "context: ../../../../../infra/docker/providers/glpi-test" in compose_file
    assert "GLPI_TEST_CLIENT_ID:" in compose_file
    assert "GLPI_TEST_CLIENT_SECRET:" in compose_file
    assert not (GLPI_TEST_DIRECTORY / "image" / "Dockerfile").exists()


def _require_mapping(
    parent: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        pytest.fail(f"The pinned GLPI OpenAPI document has no mapping at {key}")
    return value


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


def test_glpi_fixture_exposes_safe_internal_network_contract(
    glpi_environment,
) -> None:
    internal_url = urlparse(glpi_environment.internal_base_url)

    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", glpi_environment.project_name)
    assert glpi_environment.network_name == (
        f"{glpi_environment.project_name}_glpi_test"
    )
    assert internal_url.scheme == "http"
    assert internal_url.hostname == "glpi"
    assert internal_url.port is None
    assert internal_url.path in {"", "/"}
    assert internal_url.username is None
    assert internal_url.password is None
    assert internal_url.query == ""
    assert internal_url.fragment == ""

    rendered = repr(glpi_environment)
    assert "_compose" not in rendered
    assert "_docker_client" not in rendered
    assert all(secret not in rendered for secret in glpi_environment.ephemeral_secrets)


def test_glpi_fixture_service_control_is_allowlisted_and_restores_readiness(
    glpi_environment,
) -> None:
    for forbidden_service in ("", "db", "unknown"):
        with pytest.raises(ValueError, match="not controllable"):
            glpi_environment.start_service(forbidden_service)
        with pytest.raises(ValueError, match="not controllable"):
            glpi_environment.stop_service(forbidden_service)

    glpi_environment.stop_service("glpi")
    glpi_environment.start_service("glpi")

    response = httpx.get(
        f"{glpi_environment.base_url}{GLPI_OPENAPI_PATH}",
        follow_redirects=False,
        timeout=GLPI_HTTP_TIMEOUT_SECONDS,
    )
    assert response.status_code == httpx.codes.OK


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
    ticket_by_id_path = paths.get(GLPI_TICKET_BY_ID_OPENAPI_PATH)
    request_type_path = paths.get(GLPI_REQUEST_TYPE_OPENAPI_PATH)
    if not isinstance(ticket_path, Mapping):
        pytest.fail("The pinned GLPI OpenAPI document is missing the ticket resource")
    if not isinstance(ticket_by_id_path, Mapping):
        pytest.fail("The pinned GLPI OpenAPI document is missing direct ticket lookup")
    if not isinstance(request_type_path, Mapping):
        pytest.fail(
            "The pinned GLPI OpenAPI document is missing the request type resource"
        )

    assert GLPI_API_VERSION == PINNED_GLPI_API_VERSION
    assert document.get("openapi") == PINNED_OPENAPI_VERSION
    assert info.get("version") == PINNED_GLPI_API_VERSION
    assert isinstance(ticket_path.get("post"), Mapping)
    assert isinstance(request_type_path.get("get"), Mapping)
    assert isinstance(request_type_path.get("post"), Mapping)
    get_by_id = _require_mapping(ticket_by_id_path, "get")
    responses = _require_mapping(get_by_id, "responses")
    success_response = _require_mapping(responses, "200")
    content = _require_mapping(success_response, "content")
    json_content = _require_mapping(content, "application/json")
    response_schema = _require_mapping(json_content, "schema")
    assert response_schema.get("$ref") == "#/components/schemas/Ticket"

    components = _require_mapping(document, "components")
    schemas = _require_mapping(components, "schemas")
    ticket_schema = _require_mapping(schemas, "Ticket")
    ticket_properties = _require_mapping(ticket_schema, "properties")
    status_schema = _require_mapping(ticket_properties, "status")
    status_properties = _require_mapping(status_schema, "properties")
    status_id_schema = _require_mapping(status_properties, "id")
    request_type_schema = _require_mapping(ticket_properties, "request_type")
    request_type_properties = _require_mapping(request_type_schema, "properties")
    request_type_id_schema = _require_mapping(request_type_properties, "id")
    request_type_name_schema = _require_mapping(request_type_properties, "name")
    opening_date_schema = _require_mapping(ticket_properties, "date")

    assert status_schema.get("type") == "object"
    assert status_id_schema.get("type") == "integer"
    assert status_id_schema.get("format") == "int64"
    assert status_id_schema.get("enum") == [1, 10, 2, 3, 4, 5, 6]
    assert request_type_schema.get("type") == "object"
    assert request_type_schema.get("x-itemtype") == "RequestType"
    assert request_type_schema.get("x-full-schema") == "RequestType"
    assert request_type_id_schema.get("type") == "integer"
    assert request_type_id_schema.get("format") == "int64"
    assert request_type_name_schema.get("type") == "string"
    assert opening_date_schema.get("type") == "string"
    assert opening_date_schema.get("format") == "date-time"
    assert opening_date_schema.get("readOnly") is not True


def test_oauth_bootstrap_is_idempotent(glpi_environment) -> None:
    assert glpi_environment.first_bootstrap.is_identical_to(
        glpi_environment.second_bootstrap
    )


def test_request_source_bootstrap_is_idempotent(glpi_environment) -> None:
    request_source = glpi_environment.request_source

    assert glpi_environment.first_request_source.is_identical_to(
        glpi_environment.second_request_source
    )
    assert request_source.id > 0
    assert request_source.name == "MEVO"
    assert request_source.is_active is True
    assert request_source.is_ticketheader is True


def test_custom_image_contains_no_real_secret(glpi_environment) -> None:
    compose_file = GLPI_COMPOSE_FILE.read_text(encoding="utf-8")
    dockerfile = GLPI_DOCKERFILE.read_text(encoding="utf-8")
    bootstrap = GLPI_BOOTSTRAP.read_text(encoding="utf-8")
    request_source_bootstrap = GLPI_REQUEST_SOURCE_BOOTSTRAP.read_text(encoding="utf-8")
    status_helper = GLPI_STATUS_HELPER.read_text(encoding="utf-8")
    source_and_metadata = "\n".join(
        (
            compose_file,
            dockerfile,
            bootstrap,
            request_source_bootstrap,
            status_helper,
            glpi_environment.image_metadata,
        )
    )

    if any(
        secret in source_and_metadata for secret in glpi_environment.ephemeral_secrets
    ):
        pytest.fail("The custom GLPI image contains an ephemeral test secret")

    normalized_dockerfile = dockerfile.lower()
    normalized_compose = compose_file.lower()
    assert "latest" not in normalized_dockerfile + normalized_compose
    assert (
        "from glpi/glpi:11.0.7@sha256:"
        "7e100562afe13cce132fe46c0a21ddb94f7ed2c06950da9c4b34ce734a1ad4ac"
        in normalized_dockerfile
    )
    assert (
        "mysql:8.4.11@sha256:"
        "b3b90af2a6552ae30c266fdb7d5dd55f3afb72404bb78d37fe8a23eb857fd3fb"
        in normalized_compose
    )
    assert ":latest" not in glpi_environment.image_metadata.lower()
    assert "-glpi-test:11.0.7" in glpi_environment.image_metadata.lower()
    assert "env glpi_test_" not in normalized_dockerfile
    assert "bootstrap_request_source.php" in normalized_dockerfile
    assert "set_ticket_status.php" in normalized_dockerfile
    assert "INSERT " not in request_source_bootstrap
    assert "UPDATE " not in request_source_bootstrap
    assert "DELETE " not in request_source_bootstrap
    assert "new RequestType" in request_source_bootstrap
    assert "$requestType->add" in request_source_bootstrap
    assert "$requestType->update" in request_source_bootstrap
    assert "Europe/Paris" in bootstrap
    assert "$user->update" in bootstrap
    assert "GLPI_TEST_CLIENT_ID" in bootstrap
    assert "GLPI_TEST_CLIENT_SECRET" in bootstrap
    assert "hash_equals($storedClientSecret, $clientSecret)" in bootstrap
    assert "INSERT " not in status_helper
    assert "UPDATE " not in status_helper
    assert "DELETE " not in status_helper
    assert "Ticket::isAllowedStatus" in status_helper
    assert "$ticket->update" in status_helper
