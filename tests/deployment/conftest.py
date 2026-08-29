"""Fixtures for the disposable multi-environment Compose deployment."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOCKER_ROOT = REPOSITORY_ROOT / "infra" / "docker"
BASE_COMPOSE_FILE = DOCKER_ROOT / "compose.yaml"
INTERNAL_CA_COMPOSE_FILE = DOCKER_ROOT / "compose.internal-ca.yaml"
ENVIRONMENTS_ROOT = DOCKER_ROOT / "environments"
CUSTOM_SERVICES = (
    "database-migrations",
    "web",
    "worker-automation",
    "worker-retention",
    "nginx",
)
ALL_SERVICES = ("postgres", *CUSTOM_SERVICES)
POLL_INTERVAL_SECONDS = 0.25


def _clean_environment() -> dict[str, str]:
    """Avoid letting a developer's active Compose environment leak into tests."""
    return {
        name: value
        for name, value in os.environ.items()
        if name != "COMPOSE_FILE"
        and name != "COMPOSE_PROJECT_NAME"
        and not name.startswith("GTA_")
    }


def _run_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    check: bool = True,
    timeout_seconds: float = 180.0,
    secrets_to_redact: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
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


def _parse_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError(f"Invalid environment example: {path.name}")
        values[name] = value
    return values


@pytest.fixture
def render_compose(tmp_path: Path) -> Callable[..., dict[str, object]]:
    """Render one environment without contacting the Docker daemon."""

    def render(
        environment_name: str,
        *,
        release_confirmed: bool = True,
        include_internal_ca: bool = False,
        check: bool = True,
    ) -> dict[str, object] | subprocess.CompletedProcess[str]:
        environment_root = ENVIRONMENTS_ROOT / environment_name
        compose_environment = _parse_environment_file(
            environment_root / "compose.env.example"
        )
        versions_file = environment_root / "versions.env"
        if versions_file.exists():
            compose_environment.update(_parse_environment_file(versions_file))

        service_env_names = (
            "database",
            "migrations",
            "web",
            "worker-automation",
            "worker-retention",
        )
        variable_names = (
            "GTA_DATABASE_ENV_FILE",
            "GTA_MIGRATIONS_ENV_FILE",
            "GTA_WEB_ENV_FILE",
            "GTA_AUTOMATION_ENV_FILE",
            "GTA_RETENTION_ENV_FILE",
        )
        for service_env_name, variable_name in zip(
            service_env_names,
            variable_names,
            strict=True,
        ):
            compose_environment[variable_name] = str(
                environment_root / "services" / f"{service_env_name}.env.example"
            )

        harmless_file = tmp_path / "compose-secret-placeholder"
        harmless_file.write_text("not-a-secret\n", encoding="utf-8")
        for variable_name in (
            "GTA_POSTGRES_PASSWORD_FILE",
            "GTA_NGINX_TLS_CERTIFICATE_FILE",
            "GTA_NGINX_TLS_KEY_FILE",
            "GTA_NGINX_HTPASSWD_FILE",
        ):
            compose_environment[variable_name] = str(harmless_file)
        compose_environment["GTA_NGINX_SECRET_GID"] = str(os.getgid())
        compose_environment["GTA_POSTGRES_SECRET_GID"] = str(os.getgid())
        if release_confirmed:
            compose_environment["GTA_RELEASE_DIGESTS_CONFIRMED"] = "1"

        compose_files = [
            BASE_COMPOSE_FILE,
            environment_root / "compose.override.yaml",
        ]
        if include_internal_ca:
            compose_files.append(INTERNAL_CA_COMPOSE_FILE)
            compose_environment["GTA_CA_BUNDLE"] = str(harmless_file)

        command = ["docker", "compose"]
        for compose_file in compose_files:
            command.extend(("-f", str(compose_file)))
        command.extend(("config", "--format", "json"))
        result = _run_command(
            command,
            environment={**_clean_environment(), **compose_environment},
            check=check,
        )
        if not check:
            return result
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("Compose returned a non-object configuration")
        return payload

    return render


@dataclass(slots=True)
class DeploymentEnvironment:
    """Own one isolated disposable development stack."""

    project_name: str
    compose_environment_file: Path
    certificate: Path
    username: str
    _password: str = field(repr=False)
    _runtime_secrets: tuple[str, ...] = field(repr=False)
    _images: tuple[str, ...] = field(repr=False)

    @property
    def compose_files(self) -> tuple[Path, Path]:
        return (
            BASE_COMPOSE_FILE,
            ENVIRONMENTS_ROOT / "dev" / "compose.override.yaml",
        )

    def compose(
        self,
        *arguments: str,
        project_name: str | None = None,
        extra_files: Sequence[Path] = (),
        check: bool = True,
        timeout_seconds: float = 180.0,
    ) -> subprocess.CompletedProcess[str]:
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
        return _run_command(
            command,
            environment=_clean_environment(),
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
        container_id = self.compose(
            "ps",
            "--all",
            "--quiet",
            service_name,
            project_name=project_name,
        ).stdout.strip()
        if not container_id:
            raise RuntimeError(f"Service has no container: {service_name}")
        result = _run_command(
            ["docker", "inspect", container_id],
            environment=_clean_environment(),
            secrets_to_redact=(*self._runtime_secrets, self._password),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker inspect returned an invalid payload")
        return payload[0]

    def inspect_image_for_service(self, service_name: str) -> dict[str, object]:
        """Inspect the exact image used by one container in the running stack."""
        image_id = str(self.inspect_service(service_name)["Image"])
        result = _run_command(
            ["docker", "image", "inspect", image_id],
            environment=_clean_environment(),
            secrets_to_redact=(*self._runtime_secrets, self._password),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker image inspect returned an invalid payload")
        return payload[0]

    def assert_image_contains_no_runtime_secret(self, service_name: str) -> None:
        """Scan metadata and full history without rendering a leaked value."""
        image_id = str(self.inspect_service(service_name)["Image"])
        metadata = json.dumps(self.inspect_image_for_service(service_name))
        history = _run_command(
            ["docker", "history", "--no-trunc", image_id],
            environment=_clean_environment(),
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
        _run_command(
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
            environment=_clean_environment(),
            secrets_to_redact=(*self._runtime_secrets, self._password),
        )

    def wait_for_health(self, service_name: str, timeout_seconds: float = 60) -> None:
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

    @property
    def base_url(self) -> str:
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

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            verify=self.tls_context(),
            auth=(self.username, self._password),
            timeout=5,
        )

    def tls_context(self) -> ssl.SSLContext:
        """Trust only the disposable certificate generated for this stack."""
        return ssl.create_default_context(cafile=str(self.certificate))

    def start(self) -> None:
        self.compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "120",
            timeout_seconds=240,
        )
        self.wait_for_migration()
        services = (
            "postgres",
            "web",
            "worker-automation",
            "worker-retention",
            "nginx",
        )
        for service_name in services:
            self.wait_for_health(service_name)


def _write_service_environments(
    runtime_root: Path,
    database_password: str,
) -> tuple[dict[str, Path], tuple[str, ...]]:
    database_url = (
        "postgresql+psycopg://gta:"
        f"{database_password}@postgres:5432/glpi_ticketing_automation"
    )
    api_key = secrets.token_hex(24)
    glpi_password = secrets.token_hex(24)
    glpi_client_secret = secrets.token_hex(24)
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
            "PUBLIC_BASE_URL=https://localhost\n"
            "WORKER_IDLE_INTERVAL_SECONDS=0.2\n"
            "WORKER_HEARTBEAT_INTERVAL_SECONDS=1\n"
            "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS=8\n"
            "WORKER_RETRY_INITIAL_SECONDS=1\n"
            "WORKER_RETRY_MAX_SECONDS=2\n"
            "WORKER_SYSTEM_PROBE_INTERVAL_SECONDS=2\n"
            "COMPLETED_RETENTION_DAYS=90\n"
            "FAILED_RETENTION_DAYS=30\n"
            "BLOCKED_RETENTION_DAYS=90\n"
            "RETENTION_CHECK_INTERVAL_HOURS=6\n"
            "LEGACY_TRANSCRIPTION_BASE_URL=http://127.0.0.1:9\n"
            f"LEGACY_TRANSCRIPTION_API_KEY={api_key}\n"
            "TRANSCRIPTION_HTTP_TIMEOUT_SECONDS=1\n"
            "TRANSCRIPTION_POLL_INTERVAL_SECONDS=0.2\n"
            "TRANSCRIPTION_JOB_TIMEOUT_SECONDS=2\n"
            "GLPI_BASE_URL=http://127.0.0.1:9\n"
            "GLPI_USERNAME=synthetic-worker\n"
            f"GLPI_PASSWORD={glpi_password}\n"
            "GLPI_CLIENT_ID=synthetic-client\n"
            f"GLPI_CLIENT_SECRET={glpi_client_secret}\n"
            "GLPI_HTTP_TIMEOUT_SECONDS=1\n"
            "GLPI_TICKET_TITLE=Synthetic audio request\n"
            "GLPI_TICKET_TYPE=2\n"
            "GLPI_TICKET_URGENCY=3\n"
            "GLPI_TICKET_IMPACT=3\n"
        ),
        "worker-retention": (
            "WORKER_HEARTBEAT_INTERVAL_SECONDS=1\n"
            "WORKER_HEALTHCHECK_STALE_AFTER_SECONDS=8\n"
            "RETENTION_POLL_INTERVAL_SECONDS=0.2\n"
            "RETENTION_RETRY_INITIAL_SECONDS=1\n"
            "RETENTION_RETRY_MAX_SECONDS=2\n"
            "RETENTION_SYSTEM_PROBE_INTERVAL_SECONDS=2\n"
            "RETENTION_CHECK_INTERVAL_HOURS=6\n"
            "GLPI_BASE_URL=http://127.0.0.1:9\n"
            "GLPI_USERNAME=synthetic-worker\n"
            f"GLPI_PASSWORD={glpi_password}\n"
            "GLPI_CLIENT_ID=synthetic-client\n"
            f"GLPI_CLIENT_SECRET={glpi_client_secret}\n"
            "GLPI_HTTP_TIMEOUT_SECONDS=1\n"
        ),
    }
    paths: dict[str, Path] = {}
    for service_name, content in contents.items():
        path = runtime_root / f"{service_name}.env"
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        paths[service_name] = path
    return paths, (database_password, api_key, glpi_password, glpi_client_secret)


def _write_proxy_secrets(runtime_root: Path) -> tuple[Path, Path, Path, str, str]:
    certificate = runtime_root / "nginx.crt"
    private_key = runtime_root / "nginx.key"
    htpasswd = runtime_root / "nginx.htpasswd"
    username = "synthetic-user"
    password = secrets.token_hex(24)
    _run_command(
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
        environment=_clean_environment(),
    )
    sha_digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
    htpasswd.write_text(f"{username}:{{SHA}}{sha_digest}\n", encoding="utf-8")
    certificate.chmod(0o644)
    private_key.chmod(0o640)
    htpasswd.chmod(0o640)
    return certificate, private_key, htpasswd, username, password


@pytest.fixture(scope="session")
def deployment_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DeploymentEnvironment]:
    """Build, start and finally destroy one complete disposable MVP stack."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("Deployment tests must not be parallelized")

    runtime_root = tmp_path_factory.mktemp("gta-mvp-deployment")
    database_password = secrets.token_hex(24)
    postgres_password = runtime_root / "postgres.password"
    postgres_password.write_text(f"{database_password}\n", encoding="utf-8")
    postgres_password.chmod(0o640)
    service_envs, runtime_secrets = _write_service_environments(
        runtime_root,
        database_password,
    )
    certificate, private_key, htpasswd, username, password = _write_proxy_secrets(
        runtime_root
    )
    project_name = f"gtamvp{secrets.token_hex(5)}"
    compose_environment_file = runtime_root / "compose.env"
    values = {
        "COMPOSE_PROJECT_NAME": project_name,
        "GTA_POSTGRES_DB": "glpi_ticketing_automation",
        "GTA_POSTGRES_USER": "gta",
        "GTA_POSTGRES_PASSWORD_FILE": str(postgres_password),
        "GTA_POSTGRES_SECRET_GID": str(os.getgid()),
        "GTA_MIGRATIONS_IMAGE": f"{project_name}/database-migrations:smoke",
        "GTA_WEB_IMAGE": f"{project_name}/web:smoke",
        "GTA_AUTOMATION_IMAGE": f"{project_name}/worker-automation:smoke",
        "GTA_RETENTION_IMAGE": f"{project_name}/worker-retention:smoke",
        "GTA_NGINX_IMAGE": f"{project_name}/nginx:smoke",
        "GTA_IMAGE_VERSION": "smoke",
        "GTA_VCS_REF": "0" * 40,
        "GTA_DATABASE_ENV_FILE": str(service_envs["database"]),
        "GTA_MIGRATIONS_ENV_FILE": str(service_envs["migrations"]),
        "GTA_WEB_ENV_FILE": str(service_envs["web"]),
        "GTA_AUTOMATION_ENV_FILE": str(service_envs["worker-automation"]),
        "GTA_RETENTION_ENV_FILE": str(service_envs["worker-retention"]),
        "GTA_NGINX_BIND_IP": "127.0.0.1",
        "GTA_NGINX_HTTPS_PORT": "0",
        "GTA_NGINX_TLS_CERTIFICATE_FILE": str(certificate),
        "GTA_NGINX_TLS_KEY_FILE": str(private_key),
        "GTA_NGINX_HTPASSWD_FILE": str(htpasswd),
        "GTA_NGINX_SECRET_GID": str(os.getgid()),
    }
    compose_environment_file.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )
    compose_environment_file.chmod(0o600)
    environment = DeploymentEnvironment(
        project_name=project_name,
        compose_environment_file=compose_environment_file,
        certificate=certificate,
        username=username,
        _password=password,
        _runtime_secrets=runtime_secrets,
        _images=tuple(
            values[variable_name]
            for variable_name in (
                "GTA_MIGRATIONS_IMAGE",
                "GTA_WEB_IMAGE",
                "GTA_AUTOMATION_IMAGE",
                "GTA_RETENTION_IMAGE",
                "GTA_NGINX_IMAGE",
            )
        ),
    )
    environment.compose("build", timeout_seconds=10 * 60)
    environment.start()
    try:
        yield environment
    finally:
        environment.compose(
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "10",
            check=False,
        )
        _run_command(
            ["docker", "image", "rm", "--force", *environment._images],
            environment=_clean_environment(),
            check=False,
            secrets_to_redact=(*runtime_secrets, password),
        )


__all__ = [
    "ALL_SERVICES",
    "BASE_COMPOSE_FILE",
    "CUSTOM_SERVICES",
    "DOCKER_ROOT",
    "DeploymentEnvironment",
    "ENVIRONMENTS_ROOT",
    "INTERNAL_CA_COMPOSE_FILE",
]
