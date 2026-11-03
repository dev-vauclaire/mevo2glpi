"""Reusable orchestration for one disposable MVP Docker Compose project."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker"
APPS_DOCKER_ROOT = DOCKER_ROOT / "apps"
BASE_COMPOSE_FILE = APPS_DOCKER_ROOT / "docker-compose.prod.yml"
COMMON_COMPOSE_SERVICES_FILE = APPS_DOCKER_ROOT / "docker-compose.common.services.yml"
STAGING_COMPOSE_FILE = APPS_DOCKER_ROOT / "docker-compose.staging.yml"
SIMULATORS_DOCKER_ROOT = DOCKER_ROOT / "providers"
SIMULATORS_COMPOSE_FILE = SIMULATORS_DOCKER_ROOT / "docker-compose.staging.yml"
SIMULATORS_ENV_FILE = SIMULATORS_DOCKER_ROOT / "staging.env.example"
TRANSCRIPTION_DOCKER_ROOT = DOCKER_ROOT / "transcription"
TRANSCRIPTION_COMPOSE_FILE = (
    TRANSCRIPTION_DOCKER_ROOT / "docker-compose.transcription.yml"
)
TRANSCRIPTION_ENV_FILE = TRANSCRIPTION_DOCKER_ROOT / "transcription.env.example"
CUSTOM_SERVICES = (
    "database-migrations",
    "web",
    "worker-automation",
    "worker-publisher",
    "worker-retention",
)
# The historical disposable stack does not start the publisher until a local
# Microsoft Graph double is available. Static deployment tests still validate
# its production image and Compose contract.
ALL_SERVICES = (
    "postgres",
    "database-migrations",
    "web",
    "worker-automation",
    "worker-retention",
)
CONTROLLABLE_MVP_SERVICES = frozenset({"worker-automation", "worker-retention"})
POLL_INTERVAL_SECONDS = 0.25
SYNTHETIC_APP_RELEASE = "v0.0.0"
SYNTHETIC_APP_COMMIT_SHA = "a" * 40
_SYNTHETIC_PUBLISHER_PRIVATE_KEY_PEM = """-----BEGIN PRIVATE KEY-----
c3ludGhldGljLXRlc3Qtb25seS1wcml2YXRlLWtleQ==
-----END PRIVATE KEY-----
"""
_SYNTHETIC_PUBLISHER_CERTIFICATE_PEM = """-----BEGIN CERTIFICATE-----
c3ludGhldGljLXRlc3Qtb25seS1jZXJ0aWZpY2F0ZQ==
-----END CERTIFICATE-----
"""


def _deployment_environment_names() -> frozenset[str]:
    """Return the variables controlled by the versioned Dokploy contract."""
    example_paths = (
        APPS_DOCKER_ROOT / ".env.example",
        APPS_DOCKER_ROOT / "staging.env.example",
        SIMULATORS_ENV_FILE,
        TRANSCRIPTION_ENV_FILE,
    )
    return frozenset(
        name
        for example_path in example_paths
        if example_path.is_file()
        for raw_line in example_path.read_text(encoding="utf-8").splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
        if (name := line.partition("=")[0])
    )


DEPLOYMENT_ENVIRONMENT_NAMES = _deployment_environment_names()


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    """Runtime credentials and internal endpoints used by both workers."""

    transcription_base_url: str
    transcription_api_key: str = field(repr=False)
    glpi_base_url: str
    glpi_username: str
    glpi_password: str = field(repr=False)
    glpi_client_id: str
    glpi_client_secret: str = field(repr=False)
    glpi_request_source_id: int

    @classmethod
    def synthetic(cls) -> ProviderConfiguration:
        """Create valid but unreachable provider settings for deployment smoke tests."""
        return cls(
            transcription_base_url="http://127.0.0.1:9",
            transcription_api_key=secrets.token_hex(24),
            glpi_base_url="http://127.0.0.1:9",
            glpi_username="synthetic-worker",
            glpi_password=secrets.token_hex(24),
            glpi_client_id="synthetic-client",
            glpi_client_secret=secrets.token_hex(24),
            glpi_request_source_id=42,
        )

    @property
    def secrets(self) -> tuple[str, str, str]:
        """Return provider secrets solely for command-output redaction."""
        return (
            self.transcription_api_key,
            self.glpi_password,
            self.glpi_client_secret,
        )


def clean_environment() -> dict[str, str]:
    """Prevent a developer's active Compose configuration from leaking into tests."""
    return {
        name: value
        for name, value in os.environ.items()
        if name not in DEPLOYMENT_ENVIRONMENT_NAMES
        and not name.startswith("COMPOSE_")
        and not name.startswith("GTA_")
    }


def run_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    input_text: str | None = None,
    check: bool = True,
    timeout_seconds: float = 180.0,
    secrets_to_redact: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run one bounded command and redact runtime secrets from raised errors."""
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Unable to run {command[0]}") from error

    if check and result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}"
        for secret_value in secrets_to_redact:
            output = output.replace(secret_value, "[REDACTED]")
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {output.strip()}"
        )
    return result


@dataclass(slots=True)
class MvpComposeEnvironment:
    """Own one isolated Compose project and its test-only PostgreSQL service."""

    project_name: str
    compose_environment_file: Path
    compose_files: tuple[Path, ...]
    _database_password: str = field(repr=False)
    _runtime_secrets: tuple[str, ...] = field(repr=False)
    _images: tuple[str, ...] = field(repr=False)
    _port_reservations: tuple[socket.socket, ...] = field(repr=False)
    _external_network_names: tuple[str, ...]

    def compose(
        self,
        *arguments: str,
        project_name: str | None = None,
        extra_files: Sequence[Path] = (),
        input_text: str | None = None,
        check: bool = True,
        timeout_seconds: float = 180.0,
    ) -> subprocess.CompletedProcess[str]:
        """Invoke Compose with the project's explicit files and clean environment."""
        command = [
            "docker",
            "compose",
            "--project-name",
            project_name or self.project_name,
            "--env-file",
            str(self.compose_environment_file),
        ]
        for compose_file in (*self.compose_files, *extra_files):
            command.extend(("-f", str(compose_file)))
        command.extend(arguments)
        return run_command(
            command,
            environment=clean_environment(),
            input_text=input_text,
            check=check,
            timeout_seconds=timeout_seconds,
            secrets_to_redact=self._runtime_secrets,
        )

    def inspect_service(
        self,
        service_name: str,
        *,
        project_name: str | None = None,
    ) -> dict[str, object]:
        """Return Docker inspect data for one Compose service container."""
        container_id = self.compose(
            "ps",
            "--all",
            "--quiet",
            service_name,
            project_name=project_name,
        ).stdout.strip()
        if not container_id:
            raise RuntimeError(f"Service has no container: {service_name}")
        result = run_command(
            ["docker", "inspect", container_id],
            environment=clean_environment(),
            secrets_to_redact=self._runtime_secrets,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker inspect returned an invalid payload")
        return payload[0]

    def inspect_image_for_service(self, service_name: str) -> dict[str, object]:
        """Inspect the exact image used by one container in the running stack."""
        image_id = str(self.inspect_service(service_name)["Image"])
        return self._inspect_image(image_id)

    def inspect_built_image_for_service(self, service_name: str) -> dict[str, object]:
        """Inspect a built service image even when its container is not started."""
        return self._inspect_image(f"{self.project_name}-{service_name}")

    def _inspect_image(self, image_reference: str) -> dict[str, object]:
        """Return Docker metadata for one image reference."""
        result = run_command(
            ["docker", "image", "inspect", image_reference],
            environment=clean_environment(),
            secrets_to_redact=self._runtime_secrets,
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker image inspect returned an invalid payload")
        return payload[0]

    def assert_image_contains_no_runtime_secret(self, service_name: str) -> None:
        """Scan image metadata and history without rendering a leaked value."""
        image_reference = f"{self.project_name}-{service_name}"
        image_result = run_command(
            ["docker", "image", "inspect", image_reference],
            environment=clean_environment(),
            secrets_to_redact=self._runtime_secrets,
        )
        image_payload = json.loads(image_result.stdout)
        if not isinstance(image_payload, list) or len(image_payload) != 1:
            raise RuntimeError("Docker image inspect returned an invalid payload")
        metadata = json.dumps(image_payload[0])
        history = run_command(
            ["docker", "history", "--no-trunc", image_reference],
            environment=clean_environment(),
            secrets_to_redact=self._runtime_secrets,
        ).stdout
        if any(
            secret in metadata or secret in history for secret in self._runtime_secrets
        ):
            raise AssertionError("A synthetic runtime secret leaked into an image")

    def run_image_python(self, service_name: str, source: str) -> None:
        """Run one image in isolation under its declared non-root user."""
        image_id = str(self.inspect_service(service_name)["Image"])
        run_command(
            [
                "docker",
                "run",
                "--rm",
                "--read-only",
                "--entrypoint",
                "python",
                image_id,
                "-c",
                source,
            ],
            environment=clean_environment(),
            secrets_to_redact=self._runtime_secrets,
        )

    def wait_for_health(self, service_name: str, timeout_seconds: float = 60) -> None:
        """Poll Docker health rather than assuming a started container is ready."""
        deadline = time.monotonic() + timeout_seconds
        last_status = "missing"
        while time.monotonic() < deadline:
            payload = self.inspect_service(service_name)
            state = payload.get("State")
            if not isinstance(state, dict):
                raise RuntimeError("Container has no state")
            health = state.get("Health")
            if isinstance(health, dict):
                last_status = str(health.get("Status", "missing"))
                if last_status == "healthy":
                    return
            if state.get("Status") in {"dead", "exited"}:
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(f"{service_name} did not become healthy: {last_status}")

    def wait_for_migration(self, timeout_seconds: float = 60) -> None:
        """Require the one-shot migration to finish successfully."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            payload = self.inspect_service("database-migrations")
            state = payload.get("State")
            if isinstance(state, dict) and state.get("Status") == "exited":
                if state.get("ExitCode") != 0:
                    raise RuntimeError("database migration failed")
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("database migration did not complete")

    def stop_service(self, service_name: str) -> None:
        """Stop one allowlisted worker and wait for its cooperative exit."""
        _require_controllable_mvp_service(service_name)
        self.compose("stop", "--timeout", "15", service_name)
        self._wait_for_service_status(
            service_name,
            expected_statuses=frozenset({"exited"}),
            transition="stop",
        )

    def start_service(self, service_name: str) -> None:
        """Start one allowlisted worker and wait for its healthcheck."""
        _require_controllable_mvp_service(service_name)
        self.compose("start", service_name)
        self.wait_for_health(service_name)

    def crash_service(self, service_name: str) -> None:
        """Stop one worker abruptly with SIGKILL and wait for its exit."""
        _require_controllable_mvp_service(service_name)
        self.compose("kill", "--signal", "SIGKILL", service_name)
        self._wait_for_service_status(
            service_name,
            expected_statuses=frozenset({"exited"}),
            transition="crash",
        )

    def _wait_for_service_status(
        self,
        service_name: str,
        *,
        expected_statuses: frozenset[str],
        transition: str,
        timeout_seconds: float = 30,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_status = "missing"
        while time.monotonic() < deadline:
            payload = self.inspect_service(service_name)
            state = payload.get("State")
            if isinstance(state, dict):
                last_status = str(state.get("Status", "missing"))
                if last_status in expected_statuses:
                    return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"{service_name} did not {transition} (last status: {last_status})"
        )

    @property
    def base_url(self) -> str:
        """Return the test-only loopback HTTP endpoint published by FastAPI."""
        payload = self.inspect_service("web")
        network = payload.get("NetworkSettings")
        if not isinstance(network, dict):
            raise RuntimeError("Web has no network settings")
        ports = network.get("Ports")
        if not isinstance(ports, dict):
            raise RuntimeError("Web has no published ports")
        bindings = ports.get("8000/tcp")
        if not isinstance(bindings, list) or len(bindings) != 1:
            raise RuntimeError("Web has no unique HTTP binding")
        return f"http://127.0.0.1:{bindings[0]['HostPort']}"

    def client(self) -> httpx.Client:
        """Build a client for the test-only direct FastAPI binding."""
        return httpx.Client(base_url=self.base_url, timeout=5)

    @property
    def postgres_port(self) -> int:
        """Return the random loopback PostgreSQL port published by an E2E overlay."""
        payload = self.inspect_service("postgres")
        network = payload.get("NetworkSettings")
        if not isinstance(network, dict):
            raise RuntimeError("PostgreSQL has no network settings")
        ports = network.get("Ports")
        if not isinstance(ports, dict):
            raise RuntimeError("PostgreSQL has no published ports")
        bindings = ports.get("5432/tcp")
        if not isinstance(bindings, list) or len(bindings) != 1:
            raise RuntimeError("PostgreSQL has no unique test binding")
        binding = bindings[0]
        if binding.get("HostIp") != "127.0.0.1":
            raise RuntimeError("PostgreSQL test access must stay on IPv4 loopback")
        try:
            return int(binding["HostPort"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("PostgreSQL has no usable test port") from error

    @property
    def psycopg_database_url(self) -> str:
        """Build the private libpq URL used by direct E2E probes."""
        return (
            "postgresql://gta:"
            f"{self._database_password}@127.0.0.1:{self.postgres_port}/"
            "glpi_ticketing_automation"
        )

    @property
    def sqlalchemy_database_url(self) -> str:
        """Build the host-side SQLAlchemy URL used by repository fixtures in 8B."""
        return (
            "postgresql+psycopg://gta:"
            f"{self._database_password}@127.0.0.1:{self.postgres_port}/"
            "glpi_ticketing_automation"
        )

    def build(self, timeout_seconds: float = 10 * 60) -> None:
        """Build only the applications exercised by the historical E2E suite."""
        self.compose("build", *CUSTOM_SERVICES, timeout_seconds=timeout_seconds)

    def start(self) -> None:
        """Start the downstream stack without requiring real Exchange credentials."""
        self._release_port_reservations()
        for network_name in self._external_network_names:
            run_command(
                ["docker", "network", "create", network_name],
                environment=clean_environment(),
                secrets_to_redact=self._runtime_secrets,
            )
        self.compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "120",
            *ALL_SERVICES,
            timeout_seconds=240,
        )
        self.wait_for_migration()
        for service_name in (
            "postgres",
            "web",
            "worker-automation",
            "worker-retention",
        ):
            self.wait_for_health(service_name)

    def _release_port_reservations(self) -> None:
        """Release explicit test ports immediately before Docker binds them."""
        for reservation in self._port_reservations:
            reservation.close()
        self._port_reservations = ()

    def destroy(self) -> None:
        """Remove project containers, private networks, volumes and local images."""
        self._release_port_reservations()
        self.compose(
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "10",
            check=False,
        )
        remaining_external_networks: list[str] = []
        for network_name in self._external_network_names:
            run_command(
                ["docker", "network", "rm", network_name],
                environment=clean_environment(),
                check=False,
                secrets_to_redact=self._runtime_secrets,
            )
            network_inspection = run_command(
                ["docker", "network", "inspect", network_name],
                environment=clean_environment(),
                check=False,
                secrets_to_redact=self._runtime_secrets,
            )
            if network_inspection.returncode == 0:
                remaining_external_networks.append(network_name)
        run_command(
            ["docker", "image", "rm", "--force", *self._images],
            environment=clean_environment(),
            check=False,
            secrets_to_redact=self._runtime_secrets,
        )
        self.assert_project_removed()
        self.assert_images_removed()
        if remaining_external_networks:
            raise RuntimeError("A disposable external network was not removed")

    def assert_project_removed(self) -> None:
        """Fail teardown if a project-owned container, network or volume remains."""
        project_filter = f"label=com.docker.compose.project={self.project_name}"
        commands = (
            [
                "docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                project_filter,
            ],
            ["docker", "network", "ls", "--quiet", "--filter", project_filter],
            ["docker", "volume", "ls", "--quiet", "--filter", project_filter],
        )
        for command in commands:
            result = run_command(
                command,
                environment=clean_environment(),
                secrets_to_redact=self._runtime_secrets,
            )
            if result.stdout.strip():
                raise RuntimeError("The disposable MVP project was not fully removed")

    def assert_images_removed(self) -> None:
        """Fail teardown if one project-specific local image tag remains."""
        for image_reference in self._images:
            result = run_command(
                ["docker", "image", "inspect", image_reference],
                environment=clean_environment(),
                check=False,
                secrets_to_redact=self._runtime_secrets,
            )
            if result.returncode == 0:
                raise RuntimeError("A disposable MVP runtime image was not removed")


def create_mvp_compose_environment(
    runtime_root: Path,
    *,
    providers: ProviderConfiguration | None = None,
    project_prefix: str = "gtamvp",
    extra_compose_files: Sequence[Path] = (),
    extra_compose_environment: Mapping[str, str] | None = None,
    database_host: str = "mvp-postgres",
    public_base_url: str | None = None,
) -> MvpComposeEnvironment:
    """Materialize private runtime configuration for one disposable MVP project."""
    provider_configuration = providers or ProviderConfiguration.synthetic()
    database_password = secrets.token_hex(24)
    postgres_password = runtime_root / "postgres.password"
    postgres_password.write_text(f"{database_password}\n", encoding="utf-8")
    postgres_password.chmod(0o640)
    publisher_private_key, publisher_certificate = _write_synthetic_publisher_pem_files(
        runtime_root
    )
    project_name = f"{project_prefix}{secrets.token_hex(5)}"
    proxy_network_name = f"{project_name}-dokploy"
    database_network_name = f"{project_name}-database"
    audio_volume_name = f"{project_name}-audio-data"
    port_reservations = _reserve_loopback_ports(2)
    web_port = int(port_reservations[0].getsockname()[1])
    postgres_port = int(port_reservations[1].getsockname()[1])
    effective_public_base_url = public_base_url or f"http://127.0.0.1:{web_port}"
    database_url = (
        "postgresql+psycopg://gta:"
        f"{database_password}@{database_host}:5432/glpi_ticketing_automation"
    )
    test_override = _write_test_compose_override(runtime_root)

    compose_environment_file = runtime_root / "compose.env"
    values = {
        "COMPOSE_PROJECT_NAME": project_name,
        "APP_ENVIRONMENT": "production",
        "APP_RELEASE": SYNTHETIC_APP_RELEASE,
        "APP_COMMIT_SHA": SYNTHETIC_APP_COMMIT_SHA,
        "DOKPLOY_NETWORK_NAME": proxy_network_name,
        "DATABASE_NETWORK_NAME": database_network_name,
        "AUDIO_VOLUME_NAME": audio_volume_name,
        "DATABASE_URL": database_url,
        "DATABASE_CONNECT_TIMEOUT_SECONDS": "2",
        "DATABASE_READY_TIMEOUT_SECONDS": "60",
        "DATABASE_READY_INTERVAL_SECONDS": "0.25",
        "PUBLIC_BASE_URL": effective_public_base_url,
        "PUBLISHER_ENTRA_TENANT_ID": "00000000-0000-4000-8000-000000000001",
        "PUBLISHER_ENTRA_CLIENT_ID": "00000000-0000-4000-8000-000000000002",
        "PUBLISHER_MAILBOX_ID": "00000000-0000-4000-8000-000000000003",
        "PUBLISHER_ENTRA_PRIVATE_KEY_PEM_HOST_PATH": str(publisher_private_key),
        "PUBLISHER_ENTRA_CERTIFICATE_PEM_HOST_PATH": str(publisher_certificate),
        "PUBLISHER_LOG_LEVEL": "INFO",
        "PUBLISHER_GRAPH_PAGE_SIZE": "50",
        "PUBLISHER_MAX_ATTACHMENT_BYTES": "26214400",
        "PUBLISHER_HTTP_TIMEOUT_SECONDS": "10",
        "PUBLISHER_POLL_INTERVAL_SECONDS": "30",
        "PUBLISHER_HEARTBEAT_INTERVAL_SECONDS": "1",
        "PUBLISHER_RETRY_INITIAL_SECONDS": "30",
        "PUBLISHER_RETRY_MAX_SECONDS": "30",
        "PUBLISHER_SYSTEM_PROBE_INTERVAL_SECONDS": "30",
        "WORKER_IDLE_INTERVAL_SECONDS": "0.2",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS": "1",
        "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS": "8",
        "WORKER_RETRY_INITIAL_SECONDS": "30",
        "WORKER_RETRY_MAX_SECONDS": "30",
        "WORKER_SYSTEM_PROBE_INTERVAL_SECONDS": "30",
        "COMPLETED_RETENTION_DAYS": "90",
        "FAILED_RETENTION_DAYS": "30",
        "BLOCKED_RETENTION_DAYS": "90",
        "RETENTION_CHECK_INTERVAL_HOURS": "6",
        "LEGACY_TRANSCRIPTION_BASE_URL": (
            provider_configuration.transcription_base_url
        ),
        "LEGACY_TRANSCRIPTION_API_KEY": provider_configuration.transcription_api_key,
        "TRANSCRIPTION_HTTP_TIMEOUT_SECONDS": "30",
        "TRANSCRIPTION_POLL_INTERVAL_SECONDS": "0.2",
        "TRANSCRIPTION_JOB_TIMEOUT_SECONDS": "900",
        "GLPI_BASE_URL": provider_configuration.glpi_base_url,
        "GLPI_USERNAME": provider_configuration.glpi_username,
        "GLPI_PASSWORD": provider_configuration.glpi_password,
        "GLPI_CLIENT_ID": provider_configuration.glpi_client_id,
        "GLPI_CLIENT_SECRET": provider_configuration.glpi_client_secret,
        "GLPI_HTTP_TIMEOUT_SECONDS": "30",
        "GLPI_TICKET_TITLE": "Synthetic audio request",
        "GLPI_TICKET_TYPE": "1",
        "GLPI_TICKET_REQUEST_SOURCE_ID": str(
            provider_configuration.glpi_request_source_id
        ),
        "GLPI_TICKET_URGENCY": "3",
        "GLPI_TICKET_IMPACT": "3",
        "RETENTION_POLL_INTERVAL_SECONDS": "0.2",
        "RETENTION_RETRY_INITIAL_SECONDS": "30",
        "RETENTION_RETRY_MAX_SECONDS": "30",
        "RETENTION_SYSTEM_PROBE_INTERVAL_SECONDS": "30",
        "DASHBOARD_WORKER_STALE_AFTER_SECONDS": "8",
        "DASHBOARD_PENDING_ALERT_AFTER_SECONDS": "900",
        "DASHBOARD_RETRY_OVERDUE_GRACE_SECONDS": "60",
        "DASHBOARD_RETENTION_DEADLINE_GRACE_SECONDS": "120",
        "GTA_POSTGRES_DB": "glpi_ticketing_automation",
        "GTA_POSTGRES_USER": "gta",
        "GTA_POSTGRES_PASSWORD_FILE": str(postgres_password),
        "GTA_POSTGRES_SECRET_GID": str(os.getgid()),
        "GTA_TEST_WEB_PORT": str(web_port),
        "GTA_E2E_POSTGRES_PORT": str(postgres_port),
        **(extra_compose_environment or {}),
    }
    try:
        compose_environment_file.write_text(
            "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
            encoding="utf-8",
        )
        compose_environment_file.chmod(0o600)
    except OSError:
        for reservation in port_reservations:
            reservation.close()
        raise
    runtime_secrets = (
        database_password,
        *provider_configuration.secrets,
    )
    return MvpComposeEnvironment(
        project_name=project_name,
        compose_environment_file=compose_environment_file,
        compose_files=(BASE_COMPOSE_FILE, test_override, *extra_compose_files),
        _database_password=database_password,
        _runtime_secrets=runtime_secrets,
        _images=tuple(f"{project_name}-{service}" for service in CUSTOM_SERVICES),
        _port_reservations=port_reservations,
        _external_network_names=(proxy_network_name, database_network_name),
    )


def _require_controllable_mvp_service(service_name: str) -> None:
    if service_name not in CONTROLLABLE_MVP_SERVICES:
        raise ValueError("service_name is not controllable by the MVP test harness")


def _write_synthetic_publisher_pem_files(runtime_root: Path) -> tuple[Path, Path]:
    """Create non-functional PEM bind sources for downstream-only Compose tests."""
    private_key = runtime_root / "publisher-private-key.pem"
    certificate = runtime_root / "publisher-certificate.pem"
    private_key.write_text(
        _SYNTHETIC_PUBLISHER_PRIVATE_KEY_PEM,
        encoding="ascii",
    )
    certificate.write_text(
        _SYNTHETIC_PUBLISHER_CERTIFICATE_PEM,
        encoding="ascii",
    )
    private_key.chmod(0o600)
    certificate.chmod(0o644)
    return private_key.resolve(strict=True), certificate.resolve(strict=True)


def _reserve_loopback_ports(count: int) -> tuple[socket.socket, ...]:
    reservations: list[socket.socket] = []
    try:
        for _ in range(count):
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservation.bind(("127.0.0.1", 0))
            reservations.append(reservation)
    except OSError:
        for reservation in reservations:
            reservation.close()
        raise
    return tuple(reservations)


def _write_test_compose_override(runtime_root: Path) -> Path:
    path = runtime_root / "compose.test.yaml"
    path.write_text(
        """services:
  postgres:
    image: postgres:16.10-alpine@sha256:029660641a0cfc575b14f336ba448fb8a75fd595d42e1fa316b9fb4378742297
    user: "70:70"
    group_add:
      - ${GTA_POSTGRES_SECRET_GID}
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      POSTGRES_DB: ${GTA_POSTGRES_DB}
      POSTGRES_USER: ${GTA_POSTGRES_USER}
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets: [postgres_password]
    volumes:
      - postgres-data:/var/lib/postgresql/data
    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777
      - /var/run/postgresql:rw,noexec,nosuid,nodev,size=16m,mode=0770,uid=70,gid=70
    shm_size: 128m
    healthcheck:
      test:
        - CMD-SHELL
        - 'pg_isready -U "$${POSTGRES_USER}" -d "$${POSTGRES_DB}"'
      interval: 1s
      timeout: 3s
      retries: 30
    ports:
      - target: 5432
        published: ${GTA_E2E_POSTGRES_PORT}
        host_ip: 127.0.0.1
    networks:
      database:
        aliases: [mvp-postgres]
      e2e-control:
    restart: unless-stopped

  web:
    ports:
      - target: 8000
        published: ${GTA_TEST_WEB_PORT}
        host_ip: 127.0.0.1

networks:
  e2e-control:

volumes:
  postgres-data:

secrets:
  postgres_password:
    file: ${GTA_POSTGRES_PASSWORD_FILE}
""",
        encoding="utf-8",
    )
    return path


__all__ = [
    "APPS_DOCKER_ROOT",
    "ALL_SERVICES",
    "BASE_COMPOSE_FILE",
    "COMMON_COMPOSE_SERVICES_FILE",
    "CONTROLLABLE_MVP_SERVICES",
    "CUSTOM_SERVICES",
    "DOCKER_ROOT",
    "MvpComposeEnvironment",
    "ProviderConfiguration",
    "REPOSITORY_ROOT",
    "SIMULATORS_COMPOSE_FILE",
    "SIMULATORS_DOCKER_ROOT",
    "SIMULATORS_ENV_FILE",
    "SYNTHETIC_APP_COMMIT_SHA",
    "SYNTHETIC_APP_RELEASE",
    "STAGING_COMPOSE_FILE",
    "TRANSCRIPTION_COMPOSE_FILE",
    "TRANSCRIPTION_DOCKER_ROOT",
    "TRANSCRIPTION_ENV_FILE",
    "clean_environment",
    "create_mvp_compose_environment",
    "run_command",
]
