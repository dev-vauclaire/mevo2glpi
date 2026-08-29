from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from testcontainers.compose import DockerCompose
from testcontainers.core.docker_client import DockerClient

from glpi_ticketing_glpi import TicketStatus

GLPI_TEST_DIRECTORY = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "glpi-client"
    / "tests"
    / "integration"
    / "glpi"
)
GLPI_HTTP_READY_TIMEOUT_SECONDS = 300.0
GLPI_API_READY_TIMEOUT_SECONDS = 60.0
GLPI_HTTP_POLL_INTERVAL_SECONDS = 1.0
GLPI_SERVICE_STATE_TIMEOUT_SECONDS = 30.0
GLPI_SERVICE_STOP_GRACE_SECONDS = 10
GLPI_SERVICE_NAME = "glpi"
GLPI_NETWORK_LOGICAL_NAME = "glpi_test"
GLPI_INTERNAL_BASE_URL = "http://glpi"
GLPI_CONTROLLABLE_SERVICES = frozenset({GLPI_SERVICE_NAME})
GLPI_BOOTSTRAP_COMMAND = ["php", "/opt/glpi-test/bootstrap_oauth.php"]
GLPI_SET_TICKET_STATUS_COMMAND = [
    "php",
    "/opt/glpi-test/set_ticket_status.php",
]


@dataclass(frozen=True, slots=True)
class OAuthBootstrapResult:
    """Stable credentials returned by the idempotent GLPI bootstrap."""

    username: str
    password: str = field(repr=False)
    user_id: int
    client_id: str
    client_secret: str = field(repr=False)
    oauth_client_id: int
    grant_type: str
    scope: str

    def is_identical_to(self, other: OAuthBootstrapResult) -> bool:
        """Compare two results without exposing credentials in pytest output."""
        return (
            self.username == other.username
            and hmac.compare_digest(self.password, other.password)
            and self.user_id == other.user_id
            and hmac.compare_digest(self.client_id, other.client_id)
            and hmac.compare_digest(self.client_secret, other.client_secret)
            and self.oauth_client_id == other.oauth_client_id
            and self.grant_type == other.grant_type
            and self.scope == other.scope
        )


@dataclass(frozen=True, slots=True)
class TicketStatusTransitionResult:
    """Minimal result returned by the test-only GLPI transition helper."""

    ticket_id: int
    status: TicketStatus


@dataclass(slots=True)
class GlpiTestEnvironment:
    """Session-scoped handle to the disposable GLPI integration stack."""

    base_url: str
    internal_base_url: str
    project_name: str
    network_name: str
    version: str
    first_bootstrap: OAuthBootstrapResult = field(repr=False)
    second_bootstrap: OAuthBootstrapResult = field(repr=False)
    image_metadata: str = field(repr=False)
    ephemeral_secrets: tuple[str, ...] = field(repr=False)
    _compose: DockerCompose = field(repr=False, compare=False)
    _docker_client: DockerClient = field(repr=False, compare=False)

    @property
    def credentials(self) -> OAuthBootstrapResult:
        return self.first_bootstrap

    def exec_glpi(self, command: list[str]) -> tuple[str, str, int]:
        """Run a diagnostic command in the already-running GLPI container."""
        return _exec_glpi(self._compose, self._docker_client, command)

    def stop_service(self, service_name: str) -> None:
        """Stop the allowlisted GLPI API service for an outage scenario."""
        _require_controllable_service(service_name, GLPI_CONTROLLABLE_SERVICES)
        _stop_service(self._compose, self._docker_client, service_name)

    def start_service(self, service_name: str) -> None:
        """Start an allowlisted service and wait for the GLPI API contract."""
        _require_controllable_service(service_name, GLPI_CONTROLLABLE_SERVICES)
        _start_service(self._compose, self._docker_client, service_name)
        self.base_url = _get_loopback_base_url(self._compose, self._docker_client)
        _wait_for_http(
            f"{self.base_url}/api.php/v2.3.0/doc.json",
            timeout_seconds=GLPI_HTTP_READY_TIMEOUT_SECONDS,
        )

    def set_ticket_status(
        self,
        ticket_id: int,
        status: TicketStatus,
    ) -> TicketStatusTransitionResult:
        """Apply one allowlisted status transition through GLPI business classes."""
        if (
            isinstance(ticket_id, bool)
            or not isinstance(ticket_id, int)
            or ticket_id <= 0
        ):
            raise ValueError("ticket_id must be a positive integer")
        if not isinstance(status, TicketStatus) or status not in {
            TicketStatus.SOLVED,
            TicketStatus.CLOSED,
        }:
            raise ValueError("status must be SOLVED or CLOSED")

        stdout, _, exit_code = _exec_glpi(
            self._compose,
            self._docker_client,
            GLPI_SET_TICKET_STATUS_COMMAND,
            environment={
                "GLPI_TEST_TICKET_ID": str(ticket_id),
                "GLPI_TEST_TICKET_STATUS": str(int(status)),
                "GLPI_TEST_USER_ID": str(self.credentials.user_id),
            },
        )
        if exit_code != 0:
            raise RuntimeError("The GLPI test ticket transition helper failed")
        result = _parse_ticket_status_transition(stdout)
        if result.ticket_id != ticket_id or result.status is not status:
            raise RuntimeError("The GLPI test ticket transition result is inconsistent")
        return result


def _new_environment() -> dict[str, str]:
    suffix = secrets.token_hex(6)
    return {
        "COMPOSE_PROJECT_NAME": f"glpitest{suffix}",
        "GLPI_DB_HOST": "db",
        "GLPI_DB_PORT": "3306",
        "GLPI_DB_NAME": "glpi_test",
        "GLPI_DB_USER": "glpi_test",
        "GLPI_DB_PASSWORD": secrets.token_urlsafe(32),
        "GLPI_DB_ROOT_PASSWORD": secrets.token_urlsafe(32),
        "GLPI_TEST_USERNAME": f"glpi-test-user-{suffix}",
        "GLPI_TEST_PASSWORD": f"T!9a-{secrets.token_urlsafe(32)}",
        "GLPI_TEST_OAUTH_CLIENT_NAME": f"glpi-test-oauth-{suffix}",
    }


def _wait_for_http(
    url: str,
    *,
    timeout_seconds: float,
    reject_location_fragment: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status: int | None = None

    with httpx.Client(follow_redirects=False, timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url)
                last_status = response.status_code
                location = response.headers.get("location", "")
                rejected = (
                    reject_location_fragment is not None
                    and reject_location_fragment in location
                )
                if response.status_code == httpx.codes.OK and not rejected:
                    return
            except httpx.HTTPError:
                pass

            time.sleep(GLPI_HTTP_POLL_INTERVAL_SECONDS)

    status = "unreachable" if last_status is None else str(last_status)
    raise RuntimeError(f"GLPI did not become ready (last HTTP status: {status})")


def _parse_bootstrap_result(stdout: str) -> OAuthBootstrapResult:
    expected_keys = {
        "username",
        "password",
        "user_id",
        "client_id",
        "client_secret",
        "oauth_client_id",
        "grant_type",
        "scope",
    }
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exception:
        raise RuntimeError("GLPI bootstrap did not return valid JSON") from exception

    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise RuntimeError("GLPI bootstrap returned an unexpected JSON contract")

    string_keys = expected_keys - {"user_id", "oauth_client_id"}
    if any(
        not isinstance(payload[key], str) or not payload[key] for key in string_keys
    ):
        raise RuntimeError("GLPI bootstrap returned an invalid credential field")
    if any(
        not isinstance(payload[key], int) or payload[key] <= 0
        for key in ("user_id", "oauth_client_id")
    ):
        raise RuntimeError("GLPI bootstrap returned an invalid database identifier")

    return OAuthBootstrapResult(**payload)


def _parse_ticket_status_transition(stdout: str) -> TicketStatusTransitionResult:
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exception:
        raise RuntimeError(
            "The GLPI test ticket transition did not return valid JSON"
        ) from exception

    if not isinstance(payload, dict) or set(payload) != {"ticket_id", "status"}:
        raise RuntimeError(
            "The GLPI test ticket transition returned an invalid contract"
        )
    ticket_id = payload["ticket_id"]
    status_id = payload["status"]
    if (
        isinstance(ticket_id, bool)
        or not isinstance(ticket_id, int)
        or ticket_id <= 0
        or isinstance(status_id, bool)
        or not isinstance(status_id, int)
    ):
        raise RuntimeError("The GLPI test ticket transition returned invalid values")
    try:
        status = TicketStatus(status_id)
    except ValueError:
        raise RuntimeError(
            "The GLPI test ticket transition returned an unsupported status"
        ) from None
    if status not in {TicketStatus.SOLVED, TicketStatus.CLOSED}:
        raise RuntimeError(
            "The GLPI test ticket transition returned a forbidden status"
        )
    return TicketStatusTransitionResult(ticket_id=ticket_id, status=status)


def _run_bootstrap(
    compose: DockerCompose,
    docker_client: DockerClient,
) -> OAuthBootstrapResult:
    stdout, _, exit_code = _exec_glpi(
        compose,
        docker_client,
        GLPI_BOOTSTRAP_COMMAND,
    )
    if exit_code != 0:
        raise RuntimeError("GLPI OAuth bootstrap failed with a non-zero exit code")

    return _parse_bootstrap_result(stdout)


def _read_glpi_version(
    compose: DockerCompose,
    docker_client: DockerClient,
) -> str:
    stdout, _, exit_code = _exec_glpi(
        compose,
        docker_client,
        ["php", "bin/console", "--version", "--no-ansi"],
    )
    if exit_code != 0:
        raise RuntimeError("Unable to read the GLPI runtime version")

    match = re.search(r"\b(\d+\.\d+\.\d+)\b", stdout)
    if match is None:
        raise RuntimeError("The GLPI runtime returned an unexpected version")
    return match.group(1)


def _read_image_metadata(
    compose: DockerCompose,
    docker_client: DockerClient,
) -> str:
    container = _get_glpi_docker_container(compose, docker_client)
    image = container.image
    config = image.attrs.get("Config", {})
    environment = config.get("Env", []) or []
    history = [layer.get("CreatedBy", "") for layer in image.history()]
    return json.dumps(
        {"environment": environment, "history": history, "tags": image.tags},
        sort_keys=True,
    )


def _get_glpi_docker_container(
    compose: DockerCompose,
    docker_client: DockerClient,
):
    compose_container = compose.get_container(
        service_name=GLPI_SERVICE_NAME,
        include_all=True,
    )
    if compose_container.ID is None:
        raise RuntimeError("The GLPI container has no Docker identifier")

    return docker_client.client.containers.get(compose_container.ID)


def _require_controllable_service(
    service_name: str,
    allowed_services: frozenset[str],
) -> None:
    if service_name not in allowed_services:
        raise ValueError("service_name is not controllable by the GLPI fixture")


def _stop_service(
    compose: DockerCompose,
    docker_client: DockerClient,
    service_name: str,
) -> None:
    container = _get_glpi_docker_container(compose, docker_client)
    try:
        container.reload()
        status = container.attrs.get("State", {}).get("Status")
        if status not in {"dead", "exited"}:
            container.stop(timeout=GLPI_SERVICE_STOP_GRACE_SECONDS)
    except Exception:
        raise RuntimeError("Unable to stop the GLPI test service") from None
    _wait_for_service_state(
        container,
        expected_states=frozenset({"dead", "exited"}),
        transition="stop",
    )


def _start_service(
    compose: DockerCompose,
    docker_client: DockerClient,
    service_name: str,
) -> None:
    container = _get_glpi_docker_container(compose, docker_client)
    try:
        container.reload()
        if container.attrs.get("State", {}).get("Status") != "running":
            container.start()
    except Exception:
        raise RuntimeError("Unable to start the GLPI test service") from None
    _wait_for_service_state(
        container,
        expected_states=frozenset({"running"}),
        transition="start",
    )


def _wait_for_service_state(
    container,
    *,
    expected_states: frozenset[str],
    transition: str,
) -> None:
    deadline = time.monotonic() + GLPI_SERVICE_STATE_TIMEOUT_SECONDS
    last_state = "unknown"
    while time.monotonic() < deadline:
        try:
            container.reload()
            last_state = str(container.attrs.get("State", {}).get("Status", "missing"))
        except Exception:
            raise RuntimeError(
                f"Unable to observe the GLPI service during {transition}"
            ) from None
        if last_state in expected_states:
            return
        time.sleep(GLPI_HTTP_POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"The GLPI service did not {transition} (last state: {last_state})"
    )


def _require_network_name(
    compose: DockerCompose,
    docker_client: DockerClient,
    project_name: str,
) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project_name) is None:
        raise RuntimeError("The GLPI Compose project name is unsafe")
    network_name = f"{project_name}_{GLPI_NETWORK_LOGICAL_NAME}"
    container = _get_glpi_docker_container(compose, docker_client)
    container.reload()
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    if not isinstance(networks, Mapping) or network_name not in networks:
        raise RuntimeError("GLPI is not attached to the expected private network")
    return network_name


def _exec_glpi(
    compose: DockerCompose,
    docker_client: DockerClient,
    command: list[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, str, int]:
    container = _get_glpi_docker_container(compose, docker_client)
    result = container.exec_run(command, demux=True, environment=environment)
    stdout_bytes, stderr_bytes = result.output
    stdout = "" if stdout_bytes is None else stdout_bytes.decode(errors="replace")
    stderr = "" if stderr_bytes is None else stderr_bytes.decode(errors="replace")
    exit_code = -1 if result.exit_code is None else result.exit_code
    return stdout, stderr, exit_code


def _get_loopback_base_url(
    compose: DockerCompose,
    docker_client: DockerClient,
) -> str:
    container = _get_glpi_docker_container(compose, docker_client)
    bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("80/tcp")
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise RuntimeError("GLPI must expose exactly one HTTP port")

    binding = bindings[0]
    if binding.get("HostIp") != "127.0.0.1":
        raise RuntimeError("GLPI HTTP must be bound to IPv4 loopback")

    try:
        port = int(binding["HostPort"])
    except (KeyError, TypeError, ValueError) as exception:
        raise RuntimeError("GLPI has no usable random HTTP port") from exception
    if port == 80:
        raise RuntimeError("GLPI must use a random host port")

    return f"http://127.0.0.1:{port}"


def _assert_expected_credentials(
    result: OAuthBootstrapResult,
    environment: Mapping[str, str],
) -> None:
    matches = (
        result.username == environment["GLPI_TEST_USERNAME"]
        and hmac.compare_digest(result.password, environment["GLPI_TEST_PASSWORD"])
        and result.grant_type == "password"
        and result.scope == "api"
    )
    if not matches:
        raise RuntimeError("GLPI bootstrap credentials do not match the test contract")


@pytest.fixture(scope="session")
def _prevent_parallel_glpi_suite() -> Iterator[None]:
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The GLPI integration suite must not run with pytest-xdist")
    yield


def _clean_test_resources(
    compose: DockerCompose,
    docker_client: DockerClient,
    environment: Mapping[str, str],
) -> None:
    project_name = environment["COMPOSE_PROJECT_NAME"]
    image_reference = f"{project_name}-glpi-test:11.0.8"
    cleanup_failed = False

    try:
        compose.stop()
    except Exception:
        cleanup_failed = True

    try:
        images = docker_client.client.images.list(name=image_reference)
        if any(image_reference in image.tags for image in images):
            docker_client.client.images.remove(image_reference, force=False)
    except Exception:
        cleanup_failed = True

    try:
        if compose.get_containers(include_all=True):
            cleanup_failed = True

        network_name = f"{project_name}_glpi_test"
        if any(
            network.name == network_name
            for network in docker_client.client.networks.list(names=[network_name])
        ):
            cleanup_failed = True

        volume_names = {
            f"{project_name}_glpi_data",
            f"{project_name}_mysql_data",
        }
        remaining_volumes = docker_client.client.volumes.list(
            filters={"name": project_name}
        )
        if any(volume.name in volume_names for volume in remaining_volumes):
            cleanup_failed = True

        remaining_images = docker_client.client.images.list(name=image_reference)
        if any(image_reference in image.tags for image in remaining_images):
            cleanup_failed = True
    except Exception:
        cleanup_failed = True
    finally:
        docker_client.client.close()

    if cleanup_failed:
        raise RuntimeError("The disposable GLPI test resources were not fully removed")


@pytest.fixture(scope="session")
def glpi_environment(
    _prevent_parallel_glpi_suite: None,
) -> Iterator[GlpiTestEnvironment]:
    """Build one disposable GLPI stack and remove all its resources at teardown."""
    environment = _new_environment()
    docker_client = DockerClient()
    compose = DockerCompose(
        context=GLPI_TEST_DIRECTORY,
        compose_file_name="compose.yml",
        build=True,
        pull=False,
        wait=False,
    )

    with patch.dict(os.environ, environment, clear=False):
        try:
            compose.start()
            base_url = _get_loopback_base_url(compose, docker_client)

            _wait_for_http(
                f"{base_url}/",
                timeout_seconds=GLPI_HTTP_READY_TIMEOUT_SECONDS,
                reject_location_fragment="/install/",
            )

            first_bootstrap = _run_bootstrap(compose, docker_client)
            second_bootstrap = _run_bootstrap(compose, docker_client)
            _assert_expected_credentials(first_bootstrap, environment)
            _assert_expected_credentials(second_bootstrap, environment)

            _wait_for_http(
                f"{base_url}/api.php/v2.3.0/doc.json",
                timeout_seconds=GLPI_API_READY_TIMEOUT_SECONDS,
            )

            version = _read_glpi_version(compose, docker_client)
            image_metadata = _read_image_metadata(compose, docker_client)
            project_name = environment["COMPOSE_PROJECT_NAME"]
            network_name = _require_network_name(
                compose,
                docker_client,
                project_name,
            )
            ephemeral_secrets = (
                environment["GLPI_DB_PASSWORD"],
                environment["GLPI_DB_ROOT_PASSWORD"],
                environment["GLPI_TEST_PASSWORD"],
                first_bootstrap.client_secret,
            )

            yield GlpiTestEnvironment(
                base_url=base_url,
                internal_base_url=GLPI_INTERNAL_BASE_URL,
                project_name=project_name,
                network_name=network_name,
                version=version,
                first_bootstrap=first_bootstrap,
                second_bootstrap=second_bootstrap,
                image_metadata=image_metadata,
                ephemeral_secrets=ephemeral_secrets,
                _compose=compose,
                _docker_client=docker_client,
            )
        finally:
            _clean_test_resources(compose, docker_client, environment)
