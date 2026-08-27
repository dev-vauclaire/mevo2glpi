"""Session fixture for the disposable runtime Compose smoke test."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "infra" / "docker" / "docker-compose.prod.yaml"
INTERNAL_CA_COMPOSE_FILE = (
    REPOSITORY_ROOT / "infra" / "docker" / "worker" / "docker-compose.internal-ca.yaml"
)
START_RUNTIME_SCRIPT = (
    REPOSITORY_ROOT / "infra" / "docker" / "worker" / "start-runtime.sh"
)
COMPOSE_VARIABLE_NAMES = frozenset(
    {
        "COMPOSE_FILE",
        "COMPOSE_PROJECT_NAME",
        "GTA_DATA_ROOT",
        "GTA_NGINX_BIND_IP",
        "GTA_NGINX_HTTPS_PORT",
        "GTA_NGINX_TLS_CERTIFICATE_FILE",
        "GTA_NGINX_TLS_KEY_FILE",
        "GTA_NGINX_HTPASSWD_FILE",
        "GTA_NGINX_SECRET_GID",
        "GTA_WEB_ENV_FILE",
        "GTA_WORKER_ENV_FILE",
        "GTA_IMAGE_TAG",
        "GTA_VCS_REF",
        "GTA_NGINX_IMAGE",
        "GTA_WEB_IMAGE",
        "GTA_WORKER_IMAGE",
    }
)
LIFECYCLE_DIRECTORIES = ("pending", "processing", "completed", "failed")
HTTP_READY_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class DeploymentEnvironment:
    """Own one isolated Compose project and its synthetic configuration."""

    project_name: str
    data_root: Path
    base_url: str
    nginx_certificate: Path
    nginx_image: str
    web_image: str
    worker_image: str
    nginx_username: str
    compose_environment_path: Path
    _environment: Mapping[str, str] = field(repr=False, compare=False)
    _secrets: Mapping[str, str] = field(repr=False, compare=False)
    _nginx_password: str = field(repr=False, compare=False)

    @property
    def nginx_basic_auth(self) -> tuple[str, str]:
        """Return the synthetic credentials without including them in repr."""
        return self.nginx_username, self._nginx_password

    def _secret_values(self) -> tuple[str, ...]:
        return (*self._secrets.values(), self._nginx_password)

    def compose(
        self,
        *arguments: str,
        check: bool = True,
        timeout_seconds: float = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        """Run Compose for this project without exposing synthetic secrets."""
        return _run_command(
            ["docker", "compose", *arguments],
            environment=self._environment,
            secrets_to_redact=self._secret_values(),
            check=check,
            timeout_seconds=timeout_seconds,
        )

    def compose_with_files(
        self,
        compose_files: Sequence[Path],
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run an isolated test overlay without changing the runtime contract."""
        command_environment = {
            **self._environment,
            "COMPOSE_FILE": os.pathsep.join(str(path) for path in compose_files),
        }
        return _run_command(
            ["docker", "compose", *arguments],
            environment=command_environment,
            secrets_to_redact=self._secret_values(),
            check=check,
        )

    def inspect_service(self, service_name: str) -> dict[str, object]:
        """Return Docker's inspect payload for one Compose service."""
        container_id = self.compose("ps", "--all", "--quiet", service_name)
        resolved_id = container_id.stdout.strip()
        if not resolved_id:
            raise RuntimeError(f"Compose service has no container: {service_name}")
        result = _run_command(
            ["docker", "inspect", resolved_id],
            environment=self._environment,
            secrets_to_redact=self._secret_values(),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker inspect returned an unexpected contract")
        return payload[0]

    def inspect_image(self, image_reference: str) -> dict[str, object]:
        """Return Docker's inspect payload for one image built by the fixture."""
        result = _run_command(
            ["docker", "image", "inspect", image_reference],
            environment=self._environment,
            secrets_to_redact=self._secret_values(),
        )
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != 1:
            raise RuntimeError("Docker image inspect returned an unexpected contract")
        return payload[0]

    def image_history(self, image_reference: str) -> str:
        """Return full build commands so tests can detect leaked credentials."""
        result = _run_command(
            [
                "docker",
                "history",
                "--no-trunc",
                "--format",
                "{{json .CreatedBy}}",
                image_reference,
            ],
            environment=self._environment,
            secrets_to_redact=self._secret_values(),
        )
        return result.stdout

    def assert_image_contains_no_runtime_secret(self, image_reference: str) -> None:
        """Check image metadata and history without exposing compared secrets."""
        image_metadata = json.dumps(
            self.inspect_image(image_reference),
            sort_keys=True,
        )
        image_history = self.image_history(image_reference)
        assert all(
            secret_value not in image_metadata and secret_value not in image_history
            for secret_value in self._secret_values()
        )

    def assert_worker_received_runtime_secrets(self) -> None:
        """Compare hashes in-container without rendering a credential."""
        for variable_name, secret_value in self._secrets.items():
            expected_digest = hashlib.sha256(secret_value.encode()).hexdigest()
            self.compose(
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                "import hashlib, os; "
                f"value = os.environ.get({variable_name!r}, '').encode(); "
                "actual = hashlib.sha256(value).hexdigest(); "
                f"raise SystemExit(0 if actual == {expected_digest!r} else 1)",
            )

    def wait_for_healthy(
        self,
        service_name: str,
        *,
        timeout_seconds: float = HTTP_READY_TIMEOUT_SECONDS,
    ) -> None:
        """Wait for Docker's image healthcheck, not only a running PID."""
        deadline = time.monotonic() + timeout_seconds
        last_health = "missing"
        while time.monotonic() < deadline:
            inspect_payload = self.inspect_service(service_name)
            state = inspect_payload.get("State")
            if not isinstance(state, dict):
                raise RuntimeError("Docker inspect returned no container state")
            if state.get("Status") in {"dead", "exited"}:
                break
            health = state.get("Health")
            if isinstance(health, dict):
                last_health = str(health.get("Status", "missing"))
                if last_health == "healthy":
                    return
                if last_health == "unhealthy":
                    break
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"The {service_name} healthcheck did not pass (last status: {last_health})"
        )

    def start_runtime(self, *, check: bool) -> subprocess.CompletedProcess[str]:
        """Invoke the same guarded startup used by the systemd example."""
        command_environment = {
            name: value
            for name, value in self._environment.items()
            if name not in COMPOSE_VARIABLE_NAMES
        }
        return _run_command(
            [str(START_RUNTIME_SCRIPT), str(self.compose_environment_path)],
            environment=command_environment,
            secrets_to_redact=self._secret_values(),
            check=check,
            timeout_seconds=120.0,
        )

    def replace_worker_with_exit_code(self, exit_code: int) -> None:
        """Create a deterministic previous worker state for startup policy tests."""
        if isinstance(exit_code, bool) or not 1 <= exit_code <= 255:
            raise ValueError("exit_code must be between 1 and 255")
        blocked_overlay = self.data_root.parent / "worker-exit.yml"
        blocked_overlay.write_text(
            "services:\n"
            "  worker:\n"
            f'    command: ["python", "-c", "raise SystemExit({exit_code})"]\n',
            encoding="utf-8",
        )
        self.compose_with_files(
            [COMPOSE_FILE, blocked_overlay],
            "config",
            "--quiet",
        )
        self.compose_with_files(
            [COMPOSE_FILE, blocked_overlay],
            "up",
            "--detach",
            "--no-deps",
            "--force-recreate",
            "worker",
        )

    def replace_worker_with_failing_healthcheck(self) -> None:
        """Keep the worker running while forcing Docker health to fail."""
        unhealthy_overlay = self.data_root.parent / "worker-unhealthy.yml"
        unhealthy_overlay.write_text(
            "services:\n"
            "  worker:\n"
            "    healthcheck:\n"
            '      test: ["CMD", "python", "-c", "raise SystemExit(1)"]\n'
            "      interval: 1s\n"
            "      timeout: 1s\n"
            "      retries: 1\n"
            "      start_period: 0s\n"
            "      start_interval: 1s\n",
            encoding="utf-8",
        )
        self.compose_with_files(
            [COMPOSE_FILE, unhealthy_overlay],
            "config",
            "--quiet",
        )
        self.compose_with_files(
            [COMPOSE_FILE, unhealthy_overlay],
            "up",
            "--detach",
            "--no-deps",
            "--force-recreate",
            "worker",
        )


def _run_command(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    secrets_to_redact: Sequence[str],
    check: bool = True,
    timeout_seconds: float = 120.0,
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
        raise RuntimeError(f"Unable to run command: {command[0]}") from error

    if check and result.returncode != 0:
        diagnostic = _redact(f"{result.stdout}\n{result.stderr}", secrets_to_redact)
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {diagnostic.strip()}"
        )
    return result


def _redact(value: str, secrets_to_redact: Sequence[str]) -> str:
    for secret_value in secrets_to_redact:
        value = value.replace(secret_value, "[REDACTED]")
    return value


def _write_runtime_environment(
    destination: Path,
) -> tuple[Path, Path, dict[str, str]]:
    runtime_secrets = {
        "LEGACY_TRANSCRIPTION_API_KEY": (
            f"synthetic-$transcription#-{secrets.token_hex(16)}"
        ),
        "GLPI_PASSWORD": f"synthetic-$password#-{secrets.token_hex(16)}",
        "GLPI_CLIENT_SECRET": f"synthetic-$client#-{secrets.token_hex(16)}",
    }

    web_environment = destination / "web.env"
    web_environment.write_text("MAX_UPLOAD_BYTES=26214400\n", encoding="utf-8")

    worker_environment = destination / "worker.env"
    worker_environment.write_text(
        "\n".join(
            (
                "PUBLIC_BASE_URL=https://audio.example.test",
                "LEGACY_TRANSCRIPTION_BASE_URL=https://transcribe.example.test",
                "LEGACY_TRANSCRIPTION_API_KEY="
                f"'{runtime_secrets['LEGACY_TRANSCRIPTION_API_KEY']}'",
                "TRANSCRIPTION_HTTP_TIMEOUT_SECONDS=1",
                "TRANSCRIPTION_POLL_INTERVAL_SECONDS=1",
                "TRANSCRIPTION_JOB_TIMEOUT_SECONDS=10",
                "GLPI_BASE_URL=https://glpi.example.test",
                "GLPI_USERNAME=synthetic-worker",
                f"GLPI_PASSWORD='{runtime_secrets['GLPI_PASSWORD']}'",
                "GLPI_CLIENT_ID=synthetic-client",
                f"GLPI_CLIENT_SECRET='{runtime_secrets['GLPI_CLIENT_SECRET']}'",
                "GLPI_HTTP_TIMEOUT_SECONDS=1",
                "GLPI_RETRY_INTERVAL_SECONDS=1",
                "WORKER_SCAN_INTERVAL_SECONDS=1",
                "GLPI_TICKET_TITLE=Demande audio synthetique",
                "GLPI_TICKET_TYPE=2",
                "GLPI_TICKET_URGENCY=3",
                "GLPI_TICKET_IMPACT=3",
                "",
            )
        ),
        encoding="utf-8",
    )
    web_environment.chmod(0o600)
    worker_environment.chmod(0o600)
    return web_environment, worker_environment, runtime_secrets


def _write_nginx_runtime_files(
    destination: Path,
) -> tuple[Path, Path, Path, str, str]:
    certificate = destination / "nginx.crt"
    private_key = destination / "nginx.key"
    htpasswd = destination / "nginx.htpasswd"
    username = "synthetic-proxy-user"
    password = f"synthetic-$proxy#-{secrets.token_hex(16)}"

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
        environment=os.environ,
        secrets_to_redact=(password,),
    )
    try:
        password_result = subprocess.run(
            ["htpasswd", "-niB", username],
            cwd=REPOSITORY_ROOT,
            env=os.environ,
            input=f"{password}\n",
            text=True,
            capture_output=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Unable to generate synthetic proxy credentials") from error
    password_record = password_result.stdout.strip()
    expected_prefixes = (f"{username}:$2y$", f"{username}:$2b$")
    if password_result.returncode != 0 or not password_record.startswith(
        expected_prefixes
    ):
        raise RuntimeError("Unable to generate synthetic proxy credentials")

    htpasswd.write_text(f"{password_record}\n", encoding="utf-8")
    certificate.chmod(0o644)
    private_key.chmod(0o640)
    htpasswd.chmod(0o640)
    return certificate, private_key, htpasswd, username, password


def _published_loopback_port(
    environment: DeploymentEnvironment,
    service_name: str,
    target_port: str,
) -> str:
    inspect_payload = environment.inspect_service(service_name)
    network_settings = inspect_payload.get("NetworkSettings")
    if not isinstance(network_settings, dict):
        raise RuntimeError(f"The deployment {service_name} has no network settings")
    ports = network_settings.get("Ports")
    if not isinstance(ports, dict):
        raise RuntimeError(f"The deployment {service_name} has no port bindings")
    port_bindings = ports.get(target_port)
    if not isinstance(port_bindings, list) or len(port_bindings) != 1:
        raise RuntimeError(
            f"The deployment {service_name} has no unique {target_port} binding"
        )
    binding = port_bindings[0]
    if binding.get("HostIp") != "127.0.0.1":
        raise RuntimeError(
            f"The deployment {service_name} port is not bound to IPv4 loopback"
        )
    host_port = binding.get("HostPort")
    if not isinstance(host_port, str) or not host_port.isdecimal():
        raise RuntimeError(f"The deployment {service_name} port binding is invalid")
    return host_port


def _wait_for_nginx(environment: DeploymentEnvironment) -> str:
    https_port = _published_loopback_port(environment, "nginx", "8443/tcp")
    base_url = f"https://127.0.0.1:{https_port}"
    environment.wait_for_healthy("web")
    environment.wait_for_healthy("nginx")
    return base_url


def _assert_internal_ca_overlay_configuration(
    runtime_root: Path,
    command_environment: Mapping[str, str],
    runtime_secrets: Mapping[str, str],
) -> None:
    trust_bundle = runtime_root / "trust-bundle.pem"
    trust_bundle.write_text("synthetic test trust bundle\n", encoding="utf-8")
    trust_bundle.chmod(0o644)
    ca_environment = runtime_root / "compose-ca.env"
    ca_values = {
        **{name: command_environment[name] for name in COMPOSE_VARIABLE_NAMES},
        "COMPOSE_FILE": os.pathsep.join(
            (str(COMPOSE_FILE), str(INTERNAL_CA_COMPOSE_FILE))
        ),
        "GTA_CA_BUNDLE": str(trust_bundle),
    }
    ca_environment.write_text(
        "\n".join(f"{name}={value}" for name, value in ca_values.items()) + "\n",
        encoding="utf-8",
    )
    ca_environment.chmod(0o600)
    clean_environment = {
        name: value
        for name, value in os.environ.items()
        if name not in COMPOSE_VARIABLE_NAMES and name != "GTA_CA_BUNDLE"
    }
    result = _run_command(
        [
            "docker",
            "compose",
            "--env-file",
            str(ca_environment),
            "config",
            "--format",
            "json",
        ],
        environment=clean_environment,
        secrets_to_redact=tuple(runtime_secrets.values()),
    )
    payload = json.loads(result.stdout)
    try:
        worker = payload["services"]["worker"]
        ssl_cert_file = worker["environment"]["SSL_CERT_FILE"]
        volume_targets = {volume["target"] for volume in worker["volumes"]}
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "Internal CA overlay returned an invalid contract"
        ) from error
    expected_target = "/etc/ssl/certs/gta-trust-bundle.pem"
    if ssl_cert_file != expected_target or expected_target not in volume_targets:
        raise RuntimeError("Internal CA overlay is not present in resolved Compose")


def _cleanup(environment: DeploymentEnvironment) -> None:
    environment.compose(
        "down",
        "--remove-orphans",
        "--timeout",
        "10",
        check=False,
    )
    _run_command(
        [
            "docker",
            "image",
            "rm",
            environment.nginx_image,
            environment.web_image,
            environment.worker_image,
        ],
        environment=environment._environment,
        secrets_to_redact=environment._secret_values(),
        check=False,
    )


@pytest.fixture(scope="session")
def deployment_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[DeploymentEnvironment]:
    """Build and start the runtime stack once with an empty audio queue."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The deployment smoke test must not run with pytest-xdist")

    runtime_root = tmp_path_factory.mktemp("gta-deployment")
    data_root = runtime_root / "audio"
    data_root.mkdir()
    for directory_name in LIFECYCLE_DIRECTORIES:
        lifecycle_directory = data_root / directory_name
        lifecycle_directory.mkdir()
        # Synthetic test data only: fixed runtime UID 10001 must traverse/write it.
        lifecycle_directory.chmod(0o777)

    web_env, worker_env, runtime_secrets = _write_runtime_environment(runtime_root)
    (
        nginx_certificate,
        nginx_private_key,
        nginx_htpasswd,
        nginx_username,
        nginx_password,
    ) = _write_nginx_runtime_files(runtime_root)
    project_name = f"gtadeploy{secrets.token_hex(6)}"
    image_tag = "smoke"
    nginx_image = f"{project_name}-nginx:{image_tag}"
    web_image = f"{project_name}-web:{image_tag}"
    worker_image = f"{project_name}-worker:{image_tag}"
    command_environment: dict[str, str] = {
        **os.environ,
        "COMPOSE_FILE": str(COMPOSE_FILE),
        "COMPOSE_PROJECT_NAME": project_name,
        "GTA_DATA_ROOT": str(data_root),
        "GTA_NGINX_BIND_IP": "127.0.0.1",
        "GTA_NGINX_HTTPS_PORT": "0",
        "GTA_NGINX_TLS_CERTIFICATE_FILE": str(nginx_certificate),
        "GTA_NGINX_TLS_KEY_FILE": str(nginx_private_key),
        "GTA_NGINX_HTPASSWD_FILE": str(nginx_htpasswd),
        "GTA_NGINX_SECRET_GID": str(os.getgid()),
        "GTA_WEB_ENV_FILE": str(web_env),
        "GTA_WORKER_ENV_FILE": str(worker_env),
        "GTA_IMAGE_TAG": image_tag,
        "GTA_VCS_REF": "0" * 40,
        "GTA_NGINX_IMAGE": f"{project_name}-nginx",
        "GTA_WEB_IMAGE": f"{project_name}-web",
        "GTA_WORKER_IMAGE": f"{project_name}-worker",
    }
    compose_environment_path = runtime_root / "compose.env"
    compose_environment_path.write_text(
        "\n".join(
            f"{name}={command_environment[name]}"
            for name in (
                "COMPOSE_FILE",
                "COMPOSE_PROJECT_NAME",
                "GTA_DATA_ROOT",
                "GTA_NGINX_BIND_IP",
                "GTA_NGINX_HTTPS_PORT",
                "GTA_NGINX_TLS_CERTIFICATE_FILE",
                "GTA_NGINX_TLS_KEY_FILE",
                "GTA_NGINX_HTPASSWD_FILE",
                "GTA_NGINX_SECRET_GID",
                "GTA_WEB_ENV_FILE",
                "GTA_WORKER_ENV_FILE",
                "GTA_IMAGE_TAG",
                "GTA_VCS_REF",
                "GTA_NGINX_IMAGE",
                "GTA_WEB_IMAGE",
                "GTA_WORKER_IMAGE",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    compose_environment_path.chmod(0o600)
    environment = DeploymentEnvironment(
        project_name=project_name,
        data_root=data_root,
        base_url="",
        nginx_certificate=nginx_certificate,
        nginx_image=nginx_image,
        web_image=web_image,
        worker_image=worker_image,
        nginx_username=nginx_username,
        compose_environment_path=compose_environment_path,
        _environment=command_environment,
        _secrets=runtime_secrets,
        _nginx_password=nginx_password,
    )

    try:
        _run_command(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            environment=command_environment,
            secrets_to_redact=tuple(runtime_secrets.values()),
        )
        environment.compose("config", "--quiet")
        _assert_internal_ca_overlay_configuration(
            runtime_root,
            command_environment,
            runtime_secrets,
        )
        environment.compose("build", timeout_seconds=10 * 60.0)
        environment.compose("up", "--detach", "web", "nginx")

        base_url = _wait_for_nginx(environment)

        ready_environment = DeploymentEnvironment(
            project_name=environment.project_name,
            data_root=environment.data_root,
            base_url=base_url,
            nginx_certificate=environment.nginx_certificate,
            nginx_image=environment.nginx_image,
            web_image=environment.web_image,
            worker_image=environment.worker_image,
            nginx_username=environment.nginx_username,
            compose_environment_path=environment.compose_environment_path,
            _environment=environment._environment,
            _secrets=environment._secrets,
            _nginx_password=environment._nginx_password,
        )
        yield ready_environment
    finally:
        _cleanup(environment)
