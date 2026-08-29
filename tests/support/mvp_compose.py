"""Reusable orchestration for one disposable MVP Docker Compose project."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker"
BASE_COMPOSE_FILE = DOCKER_ROOT / "compose.yaml"
INTERNAL_CA_COMPOSE_FILE = DOCKER_ROOT / "compose.internal-ca.yaml"
ENVIRONMENTS_ROOT = DOCKER_ROOT / "environments"
DEV_COMPOSE_FILE = ENVIRONMENTS_ROOT / "dev" / "compose.override.yaml"
CUSTOM_SERVICES = (
    "database-migrations",
    "web",
    "worker-automation",
    "worker-retention",
    "nginx",
)
ALL_SERVICES = ("postgres", *CUSTOM_SERVICES)
CONTROLLABLE_MVP_SERVICES = frozenset({"worker-automation", "worker-retention"})
POLL_INTERVAL_SECONDS = 0.25
DEFAULT_TEST_NGINX_HTTP_PORT = 18080
DEFAULT_TEST_NGINX_HTTPS_PORT = 18443


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
        if name != "COMPOSE_FILE"
        and name != "COMPOSE_PROJECT_NAME"
        and not name.startswith("GTA_")
    }


def run_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
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
    """Own one isolated development Compose project and its runtime secrets."""

    project_name: str
    compose_environment_file: Path
    compose_files: tuple[Path, ...]
    certificate: Path
    username: str
    _password: str = field(repr=False)
    _database_password: str = field(repr=False)
    _runtime_secrets: tuple[str, ...] = field(repr=False)
    _images: tuple[str, ...] = field(repr=False)
    _postgres_port_reservation: socket.socket | None = field(repr=False)
    _nginx_port_reservations: tuple[socket.socket, ...] = field(repr=False)

    def compose(
        self,
        *arguments: str,
        project_name: str | None = None,
        extra_files: Sequence[Path] = (),
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
            check=check,
            timeout_seconds=timeout_seconds,
            secrets_to_redact=(*self._runtime_secrets, self._password),
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
            secrets_to_redact=(*self._runtime_secrets, self._password),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker inspect returned an invalid payload")
        return payload[0]

    def inspect_image_for_service(self, service_name: str) -> dict[str, object]:
        """Inspect the exact image used by one container in the running stack."""
        image_id = str(self.inspect_service(service_name)["Image"])
        result = run_command(
            ["docker", "image", "inspect", image_id],
            environment=clean_environment(),
            secrets_to_redact=(*self._runtime_secrets, self._password),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker image inspect returned an invalid payload")
        return payload[0]

    def assert_image_contains_no_runtime_secret(self, service_name: str) -> None:
        """Scan image metadata and history without rendering a leaked value."""
        image_id = str(self.inspect_service(service_name)["Image"])
        metadata = json.dumps(self.inspect_image_for_service(service_name))
        history = run_command(
            ["docker", "history", "--no-trunc", image_id],
            environment=clean_environment(),
            secrets_to_redact=(*self._runtime_secrets, self._password),
        ).stdout
        if any(
            secret in metadata or secret in history
            for secret in (*self._runtime_secrets, self._password)
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
            secrets_to_redact=(*self._runtime_secrets, self._password),
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
        """Return the explicitly configured loopback HTTPS endpoint."""
        payload = self.inspect_service("nginx")
        network = payload.get("NetworkSettings")
        if not isinstance(network, dict):
            raise RuntimeError("Nginx has no network settings")
        ports = network.get("Ports")
        if not isinstance(ports, dict):
            raise RuntimeError("Nginx has no published ports")
        bindings = ports.get("8443/tcp")
        if not isinstance(bindings, list) or len(bindings) != 1:
            raise RuntimeError("Nginx has no unique HTTPS binding")
        return f"https://127.0.0.1:{bindings[0]['HostPort']}"

    @property
    def http_base_url(self) -> str:
        """Return the configured loopback HTTP redirect endpoint."""
        payload = self.inspect_service("nginx")
        network = payload.get("NetworkSettings")
        if not isinstance(network, dict):
            raise RuntimeError("Nginx has no network settings")
        ports = network.get("Ports")
        if not isinstance(ports, dict):
            raise RuntimeError("Nginx has no published ports")
        bindings = ports.get("8080/tcp")
        if not isinstance(bindings, list) or len(bindings) != 1:
            raise RuntimeError("Nginx has no unique HTTP binding")
        return f"http://127.0.0.1:{bindings[0]['HostPort']}"

    def client(self) -> httpx.Client:
        """Build an authenticated client trusting only the disposable certificate."""
        return httpx.Client(
            base_url=self.base_url,
            verify=self.tls_context(),
            auth=(self.username, self._password),
            timeout=5,
        )

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

    def tls_context(self) -> ssl.SSLContext:
        """Trust only the disposable certificate generated for this stack."""
        return ssl.create_default_context(cafile=str(self.certificate))

    def build(self, timeout_seconds: float = 10 * 60) -> None:
        """Build the five local runtime images once for this test project."""
        self.compose("build", timeout_seconds=timeout_seconds)

    def start(self) -> None:
        """Start the complete stack and require all long-lived services healthy."""
        self._release_port_reservations()
        self.compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "120",
            timeout_seconds=240,
        )
        self.wait_for_migration()
        for service_name in (
            "postgres",
            "web",
            "worker-automation",
            "worker-retention",
            "nginx",
        ):
            self.wait_for_health(service_name)

    def _release_port_reservations(self) -> None:
        """Release explicit test ports immediately before Docker binds them."""
        if self._postgres_port_reservation is not None:
            self._postgres_port_reservation.close()
            self._postgres_port_reservation = None
        for reservation in self._nginx_port_reservations:
            reservation.close()
        self._nginx_port_reservations = ()

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
        run_command(
            ["docker", "image", "rm", "--force", *self._images],
            environment=clean_environment(),
            check=False,
            secrets_to_redact=(*self._runtime_secrets, self._password),
        )
        self.assert_project_removed()
        self.assert_images_removed()

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
                secrets_to_redact=(*self._runtime_secrets, self._password),
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
                secrets_to_redact=(*self._runtime_secrets, self._password),
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
    database_host: str = "postgres",
    public_base_url: str | None = None,
    publish_postgres_on_loopback: bool = False,
    nginx_http_port: int = DEFAULT_TEST_NGINX_HTTP_PORT,
    nginx_https_port: int = DEFAULT_TEST_NGINX_HTTPS_PORT,
) -> MvpComposeEnvironment:
    """Materialize private runtime configuration for one disposable MVP project."""
    _validate_explicit_port(nginx_http_port, "nginx_http_port")
    _validate_explicit_port(nginx_https_port, "nginx_https_port")
    if nginx_http_port == nginx_https_port:
        raise ValueError("Nginx HTTP and HTTPS ports must be distinct")
    effective_public_base_url = public_base_url or (
        f"https://127.0.0.1:{nginx_https_port}"
    )
    provider_configuration = providers or ProviderConfiguration.synthetic()
    database_password = secrets.token_hex(24)
    postgres_password = runtime_root / "postgres.password"
    postgres_password.write_text(f"{database_password}\n", encoding="utf-8")
    postgres_password.chmod(0o640)
    service_envs = _write_service_environments(
        runtime_root,
        database_password=database_password,
        database_host=database_host,
        providers=provider_configuration,
        public_base_url=effective_public_base_url,
    )
    certificate, private_key, htpasswd, username, password = _write_proxy_secrets(
        runtime_root
    )
    project_name = f"{project_prefix}{secrets.token_hex(5)}"
    image_variables = {
        "GTA_MIGRATIONS_IMAGE": f"{project_name}/database-migrations:smoke",
        "GTA_WEB_IMAGE": f"{project_name}/web:smoke",
        "GTA_AUTOMATION_IMAGE": f"{project_name}/worker-automation:smoke",
        "GTA_RETENTION_IMAGE": f"{project_name}/worker-retention:smoke",
        "GTA_NGINX_IMAGE": f"{project_name}/nginx:smoke",
    }
    nginx_port_reservations = _reserve_loopback_ports(
        nginx_http_port,
        nginx_https_port,
    )
    postgres_port_reservation: socket.socket | None = None
    try:
        if publish_postgres_on_loopback:
            postgres_port_reservation = socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
            )
            postgres_port_reservation.bind(("127.0.0.1", 0))
    except OSError:
        for reservation in nginx_port_reservations:
            reservation.close()
        raise

    values = {
        "COMPOSE_PROJECT_NAME": project_name,
        "GTA_POSTGRES_DB": "glpi_ticketing_automation",
        "GTA_POSTGRES_USER": "gta",
        "GTA_POSTGRES_PASSWORD_FILE": str(postgres_password),
        "GTA_POSTGRES_SECRET_GID": str(os.getgid()),
        **image_variables,
        "GTA_IMAGE_VERSION": "smoke",
        "GTA_VCS_REF": "0" * 40,
        "GTA_DATABASE_ENV_FILE": str(service_envs["database"]),
        "GTA_MIGRATIONS_ENV_FILE": str(service_envs["migrations"]),
        "GTA_WEB_ENV_FILE": str(service_envs["web"]),
        "GTA_AUTOMATION_ENV_FILE": str(service_envs["worker-automation"]),
        "GTA_RETENTION_ENV_FILE": str(service_envs["worker-retention"]),
        "GTA_NGINX_BIND_IP": "127.0.0.1",
        "GTA_NGINX_PUBLIC_HOST": "127.0.0.1",
        "GTA_NGINX_HTTP_PORT": str(nginx_http_port),
        "GTA_NGINX_HTTPS_PORT": str(nginx_https_port),
        "GTA_NGINX_TLS_CERTIFICATE_FILE": str(certificate),
        "GTA_NGINX_TLS_KEY_FILE": str(private_key),
        "GTA_NGINX_HTPASSWD_FILE": str(htpasswd),
        "GTA_NGINX_SECRET_GID": str(os.getgid()),
        **(extra_compose_environment or {}),
    }
    if postgres_port_reservation is not None:
        values["GTA_E2E_POSTGRES_PORT"] = str(
            postgres_port_reservation.getsockname()[1]
        )
    compose_environment_file = runtime_root / "compose.env"
    try:
        compose_environment_file.write_text(
            "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
            encoding="utf-8",
        )
        compose_environment_file.chmod(0o600)
    except OSError:
        if postgres_port_reservation is not None:
            postgres_port_reservation.close()
        for reservation in nginx_port_reservations:
            reservation.close()
        raise
    runtime_secrets = (
        database_password,
        *provider_configuration.secrets,
    )
    return MvpComposeEnvironment(
        project_name=project_name,
        compose_environment_file=compose_environment_file,
        compose_files=(BASE_COMPOSE_FILE, DEV_COMPOSE_FILE, *extra_compose_files),
        certificate=certificate,
        username=username,
        _password=password,
        _database_password=database_password,
        _runtime_secrets=runtime_secrets,
        _images=tuple(image_variables.values()),
        _postgres_port_reservation=postgres_port_reservation,
        _nginx_port_reservations=nginx_port_reservations,
    )


def _validate_explicit_port(port: object, field_name: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError(f"{field_name} must be an integer")
    if port < 1 or port > 65535:
        raise ValueError(f"{field_name} must be between 1 and 65535")


def _require_controllable_mvp_service(service_name: str) -> None:
    if service_name not in CONTROLLABLE_MVP_SERVICES:
        raise ValueError("service_name is not controllable by the MVP test harness")


def _reserve_loopback_ports(*ports: int) -> tuple[socket.socket, ...]:
    reservations: list[socket.socket] = []
    try:
        for port in ports:
            reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservation.bind(("127.0.0.1", port))
            reservations.append(reservation)
    except OSError:
        for reservation in reservations:
            reservation.close()
        raise
    return tuple(reservations)


def _write_service_environments(
    runtime_root: Path,
    *,
    database_password: str,
    database_host: str,
    providers: ProviderConfiguration,
    public_base_url: str,
) -> dict[str, Path]:
    database_url = (
        "postgresql+psycopg://gta:"
        f"{database_password}@{database_host}:5432/glpi_ticketing_automation"
    )
    contents = {
        "database": f"DATABASE_URL={database_url}\n",
        "migrations": (
            "DATABASE_CONNECT_TIMEOUT_SECONDS=2\n"
            "DATABASE_READY_TIMEOUT_SECONDS=60\n"
            "DATABASE_READY_INTERVAL_SECONDS=0.25\n"
        ),
        "web": (
            "MAX_UPLOAD_BYTES=1048576\n"
            "WORKER_HEARTBEAT_INTERVAL_SECONDS=1\n"
            "DASHBOARD_WORKER_STALE_AFTER_SECONDS=8\n"
        ),
        "worker-automation": (
            f"PUBLIC_BASE_URL={public_base_url}\n"
            "WORKER_IDLE_INTERVAL_SECONDS=0.2\n"
            "WORKER_HEARTBEAT_INTERVAL_SECONDS=1\n"
            "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS=8\n"
            "WORKER_RETRY_INITIAL_SECONDS=30\n"
            "WORKER_RETRY_MAX_SECONDS=30\n"
            "WORKER_SYSTEM_PROBE_INTERVAL_SECONDS=30\n"
            "COMPLETED_RETENTION_DAYS=90\n"
            "FAILED_RETENTION_DAYS=30\n"
            "BLOCKED_RETENTION_DAYS=90\n"
            "RETENTION_CHECK_INTERVAL_HOURS=6\n"
            f"LEGACY_TRANSCRIPTION_BASE_URL={providers.transcription_base_url}\n"
            f"LEGACY_TRANSCRIPTION_API_KEY={providers.transcription_api_key}\n"
            "TRANSCRIPTION_HTTP_TIMEOUT_SECONDS=30\n"
            "TRANSCRIPTION_POLL_INTERVAL_SECONDS=0.2\n"
            "TRANSCRIPTION_JOB_TIMEOUT_SECONDS=900\n"
            f"GLPI_BASE_URL={providers.glpi_base_url}\n"
            f"GLPI_USERNAME={providers.glpi_username}\n"
            f"GLPI_PASSWORD={providers.glpi_password}\n"
            f"GLPI_CLIENT_ID={providers.glpi_client_id}\n"
            f"GLPI_CLIENT_SECRET={providers.glpi_client_secret}\n"
            "GLPI_HTTP_TIMEOUT_SECONDS=30\n"
            "GLPI_TICKET_TITLE=Synthetic audio request\n"
            "GLPI_TICKET_TYPE=2\n"
            "GLPI_TICKET_URGENCY=3\n"
            "GLPI_TICKET_IMPACT=3\n"
        ),
        "worker-retention": (
            "WORKER_HEARTBEAT_INTERVAL_SECONDS=1\n"
            "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS=8\n"
            "RETENTION_POLL_INTERVAL_SECONDS=0.2\n"
            "RETENTION_RETRY_INITIAL_SECONDS=30\n"
            "RETENTION_RETRY_MAX_SECONDS=30\n"
            "RETENTION_SYSTEM_PROBE_INTERVAL_SECONDS=30\n"
            "RETENTION_CHECK_INTERVAL_HOURS=6\n"
            f"GLPI_BASE_URL={providers.glpi_base_url}\n"
            f"GLPI_USERNAME={providers.glpi_username}\n"
            f"GLPI_PASSWORD={providers.glpi_password}\n"
            f"GLPI_CLIENT_ID={providers.glpi_client_id}\n"
            f"GLPI_CLIENT_SECRET={providers.glpi_client_secret}\n"
            "GLPI_HTTP_TIMEOUT_SECONDS=30\n"
        ),
    }
    paths: dict[str, Path] = {}
    for service_name, content in contents.items():
        path = runtime_root / f"{service_name}.env"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        paths[service_name] = path
    return paths


def _write_proxy_secrets(runtime_root: Path) -> tuple[Path, Path, Path, str, str]:
    certificate = runtime_root / "nginx.crt"
    private_key = runtime_root / "nginx.key"
    htpasswd = runtime_root / "nginx.htpasswd"
    username = "synthetic-user"
    password = secrets.token_hex(24)
    run_command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        environment=clean_environment(),
    )
    sha_digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
    htpasswd.write_text(f"{username}:{{SHA}}{sha_digest}\n", encoding="utf-8")
    certificate.chmod(0o644)
    private_key.chmod(0o640)
    htpasswd.chmod(0o640)
    return certificate, private_key, htpasswd, username, password


__all__ = [
    "ALL_SERVICES",
    "BASE_COMPOSE_FILE",
    "CONTROLLABLE_MVP_SERVICES",
    "CUSTOM_SERVICES",
    "DEFAULT_TEST_NGINX_HTTP_PORT",
    "DEFAULT_TEST_NGINX_HTTPS_PORT",
    "DOCKER_ROOT",
    "ENVIRONMENTS_ROOT",
    "INTERNAL_CA_COMPOSE_FILE",
    "MvpComposeEnvironment",
    "ProviderConfiguration",
    "clean_environment",
    "create_mvp_compose_environment",
    "run_command",
]
