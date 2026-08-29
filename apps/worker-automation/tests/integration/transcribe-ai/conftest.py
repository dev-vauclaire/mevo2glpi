"""Session fixture for the disposable Transcribe AI integration stack."""

from __future__ import annotations

import io
import os
import platform
import re
import secrets
import tarfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr
from testcontainers.compose import DockerCompose
from testcontainers.core.docker_client import DockerClient

TRANSCRIBE_AI_TEST_DIRECTORY = Path(__file__).resolve().parent
TRANSCRIBE_AI_DEVICE_VARIABLE = "TRANSCRIBE_AI_TEST_DEVICE"
SUPPORTED_DEVICES = frozenset({"cpu", "gpu"})
SERVICE_READY_TIMEOUT_SECONDS = 10 * 60.0
API_READY_TIMEOUT_SECONDS = 2 * 60.0
POLL_INTERVAL_SECONDS = 1.0
SERVICE_STATE_TIMEOUT_SECONDS = 30.0
SERVICE_STOP_GRACE_SECONDS = 10
API_SERVICE_NAME = "api"
BATCH_WORKER_SERVICE_NAME = "mono-voice-worker"
BATCH_WORKER_READY_LOG_MARKER = b"Worker d\xc3\xa9marr\xc3\xa9 avec le contexte"
TRANSCRIBE_AI_NETWORK_LOGICAL_NAME = "transcribe_ai_test"
TRANSCRIBE_AI_INTERNAL_BASE_URL = "http://api:5000"
CONTROLLABLE_SERVICES = frozenset({API_SERVICE_NAME, BATCH_WORKER_SERVICE_NAME})
SYNTHETIC_AUDIO_CONTAINER_PATH = "/tmp/transcribe-ai-fixture.wav"
SYNTHETIC_AUDIO_FILTER = (
    "flite=text='Bonjour. Ceci est un court test automatique de "
    "transcription.':voice=slt"
)


@dataclass(slots=True)
class TranscribeAiTestEnvironment:
    """Session-scoped handle to the disposable Transcribe AI stack."""

    base_url: str
    api_key: SecretStr = field(repr=False)
    requested_device: str
    runtime_device: str
    audio_path: Path
    project_name: str
    network_name: str
    internal_base_url: str
    _compose: DockerCompose = field(repr=False, compare=False)
    _docker_client: DockerClient = field(repr=False, compare=False)
    _compose_environment: Mapping[str, str] = field(repr=False, compare=False)

    def stop_service(self, service_name: str) -> None:
        """Stop one allowlisted Transcribe AI service for an outage scenario."""
        _require_controllable_service(service_name)
        with patch.dict(os.environ, self._compose_environment, clear=False):
            _stop_service(self._compose, self._docker_client, service_name)

    def start_service(self, service_name: str) -> None:
        """Start an allowlisted service and wait for its specific readiness."""
        _require_controllable_service(service_name)
        with patch.dict(os.environ, self._compose_environment, clear=False):
            _start_service(self._compose, self._docker_client, service_name)
            if service_name == API_SERVICE_NAME:
                self.base_url = _get_loopback_base_url(
                    self._compose,
                    self._docker_client,
                )
                _wait_for_healthy_service(
                    self._compose,
                    self._docker_client,
                    service_name,
                )
                _wait_for_api(self.base_url, self.api_key)
            else:
                _wait_for_batch_worker(self._compose, self._docker_client)


def _requested_device() -> str:
    value = os.environ.get(TRANSCRIBE_AI_DEVICE_VARIABLE, "cpu").strip().casefold()
    if value not in SUPPORTED_DEVICES:
        choices = ", ".join(sorted(SUPPORTED_DEVICES))
        raise pytest.UsageError(
            f"{TRANSCRIBE_AI_DEVICE_VARIABLE} must be one of: {choices}"
        )
    return value


def _new_environment() -> dict[str, str]:
    suffix = secrets.token_hex(6)
    return {
        "COMPOSE_PROJECT_NAME": f"transcribeaitest{suffix}",
        "DB_USER": "transcribe_ai_test",
        "DB_PASSWORD": secrets.token_hex(32),
        "DB_NAME": "transcribe_ai_test",
        "API_SECRET_KEY": secrets.token_hex(32),
    }


def _get_service_container(
    compose: DockerCompose,
    docker_client: DockerClient,
    service_name: str,
):
    compose_container = compose.get_container(
        service_name=service_name,
        include_all=True,
    )
    if compose_container.ID is None:
        raise RuntimeError(f"The {service_name} container has no Docker identifier")
    return docker_client.client.containers.get(compose_container.ID)


def _require_controllable_service(service_name: str) -> None:
    if service_name not in CONTROLLABLE_SERVICES:
        raise ValueError(
            "service_name is not controllable by the Transcribe AI fixture"
        )


def _stop_service(
    compose: DockerCompose,
    docker_client: DockerClient,
    service_name: str,
) -> None:
    container = _get_service_container(compose, docker_client, service_name)
    try:
        container.reload()
        status = container.attrs.get("State", {}).get("Status")
        if status not in {"dead", "exited"}:
            container.stop(timeout=SERVICE_STOP_GRACE_SECONDS)
    except Exception:
        raise RuntimeError("Unable to stop the Transcribe AI test service") from None
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
    container = _get_service_container(compose, docker_client, service_name)
    try:
        container.reload()
        if container.attrs.get("State", {}).get("Status") != "running":
            container.start()
    except Exception:
        raise RuntimeError("Unable to start the Transcribe AI test service") from None
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
    deadline = time.monotonic() + SERVICE_STATE_TIMEOUT_SECONDS
    last_state = "unknown"
    while time.monotonic() < deadline:
        try:
            container.reload()
            last_state = str(container.attrs.get("State", {}).get("Status", "missing"))
        except Exception:
            raise RuntimeError(
                f"Unable to observe the Transcribe AI service during {transition}"
            ) from None
        if last_state in expected_states:
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"The Transcribe AI service did not {transition} (last state: {last_state})"
    )


def _require_network_name(
    compose: DockerCompose,
    docker_client: DockerClient,
    project_name: str,
) -> str:
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project_name) is None:
        raise RuntimeError("The Transcribe AI Compose project name is unsafe")
    network_name = f"{project_name}_{TRANSCRIBE_AI_NETWORK_LOGICAL_NAME}"
    container = _get_service_container(compose, docker_client, API_SERVICE_NAME)
    container.reload()
    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    if not isinstance(networks, Mapping) or network_name not in networks:
        raise RuntimeError(
            "Transcribe AI is not attached to the expected private network"
        )
    return network_name


def _get_loopback_base_url(
    compose: DockerCompose,
    docker_client: DockerClient,
) -> str:
    container = _get_service_container(compose, docker_client, "api")
    bindings = (
        container.attrs.get("NetworkSettings", {}).get("Ports", {}).get("5000/tcp")
    )
    if not isinstance(bindings, list) or len(bindings) != 1:
        raise RuntimeError("Transcribe AI must expose exactly one API port")

    binding = bindings[0]
    if binding.get("HostIp") != "127.0.0.1":
        raise RuntimeError("Transcribe AI API must be bound to IPv4 loopback")

    try:
        port = int(binding["HostPort"])
    except (KeyError, TypeError, ValueError) as exception:
        raise RuntimeError("Transcribe AI has no usable random API port") from exception

    return f"http://127.0.0.1:{port}"


def _wait_for_healthy_service(
    compose: DockerCompose,
    docker_client: DockerClient,
    service_name: str,
    *,
    timeout_seconds: float = SERVICE_READY_TIMEOUT_SECONDS,
) -> None:
    container = _get_service_container(compose, docker_client, service_name)
    deadline = time.monotonic() + timeout_seconds
    last_health = "unknown"

    while time.monotonic() < deadline:
        container.reload()
        state = container.attrs.get("State", {})
        last_health = state.get("Health", {}).get("Status", "missing")
        if last_health == "healthy":
            return
        if state.get("Status") in {"dead", "exited"}:
            raise RuntimeError(f"The {service_name} service stopped before readiness")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        f"The {service_name} service did not become healthy (last state: {last_health})"
    )


def _wait_for_api(base_url: str, api_key: SecretStr) -> None:
    deadline = time.monotonic() + API_READY_TIMEOUT_SECONDS
    last_status: int | None = None

    with httpx.Client(timeout=2.0, follow_redirects=False) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(
                    f"{base_url}/api/batchTranscription/result",
                    params={"job_uuid": "transcribe-ai-readiness"},
                    headers={"X-API-KEY": api_key.get_secret_value()},
                )
                last_status = response.status_code
                if response.status_code == httpx.codes.NOT_FOUND:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(POLL_INTERVAL_SECONDS)

    status = "unreachable" if last_status is None else str(last_status)
    raise RuntimeError(
        f"Transcribe AI API did not become ready (last HTTP status: {status})"
    )


def _wait_for_batch_worker(
    compose: DockerCompose,
    docker_client: DockerClient,
    *,
    timeout_seconds: float = SERVICE_READY_TIMEOUT_SECONDS,
) -> None:
    """Wait until the pinned batch worker has completed application startup."""
    container = _get_service_container(
        compose,
        docker_client,
        BATCH_WORKER_SERVICE_NAME,
    )
    deadline = time.monotonic() + timeout_seconds
    last_state = "unknown"

    while time.monotonic() < deadline:
        try:
            container.reload()
            state = container.attrs.get("State", {})
            last_state = str(state.get("Status", "missing"))
            if last_state in {"dead", "exited"}:
                raise RuntimeError(
                    "The Transcribe AI batch worker stopped before readiness"
                )
            started_at = state.get("StartedAt")
            if last_state == "running" and isinstance(started_at, str):
                logs = container.logs(since=_parse_docker_timestamp(started_at))
                if isinstance(logs, bytes) and BATCH_WORKER_READY_LOG_MARKER in logs:
                    return
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError(
                "Unable to observe the Transcribe AI batch worker readiness"
            ) from None
        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError(
        "The Transcribe AI batch worker did not become ready "
        f"(last state: {last_state})"
    )


def _parse_docker_timestamp(value: str) -> datetime:
    """Parse Docker's RFC3339 start timestamp for a bounded log query."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError(
            "The Transcribe AI batch worker has an invalid start timestamp"
        ) from None


def _read_runtime_device(
    compose: DockerCompose,
    docker_client: DockerClient,
) -> str:
    container = _get_service_container(compose, docker_client, "mono-voice-stt")
    result = container.exec_run(
        [
            "python",
            "-c",
            "import torch; print('gpu' if torch.cuda.is_available() else 'cpu')",
        ],
        demux=True,
    )
    stdout_bytes, _ = result.output
    if result.exit_code != 0 or stdout_bytes is None:
        raise RuntimeError("Unable to determine the Transcribe AI runtime device")

    runtime_device = stdout_bytes.decode(errors="replace").strip()
    if runtime_device not in SUPPORTED_DEVICES:
        raise RuntimeError("Transcribe AI returned an unknown runtime device")
    return runtime_device


def _generate_synthetic_audio(
    compose: DockerCompose,
    docker_client: DockerClient,
    destination: Path,
) -> None:
    container = _get_service_container(compose, docker_client, "mono-voice-stt")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        SYNTHETIC_AUDIO_FILTER,
        "-ar",
        "16000",
        "-ac",
        "1",
        SYNTHETIC_AUDIO_CONTAINER_PATH,
    ]
    result = container.exec_run(command, demux=True)
    if result.exit_code != 0:
        raise RuntimeError("Unable to generate the synthetic transcription audio")

    archive_stream, _ = container.get_archive(SYNTHETIC_AUDIO_CONTAINER_PATH)
    archive_bytes = b"".join(archive_stream)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        if len(members) != 1:
            raise RuntimeError("The generated audio archive has an invalid structure")
        audio_stream = archive.extractfile(members[0])
        if audio_stream is None:
            raise RuntimeError("The generated audio could not be read")
        audio_bytes = audio_stream.read()

    if len(audio_bytes) <= 44:
        raise RuntimeError("The generated WAV audio is empty")
    destination.write_bytes(audio_bytes)


def _require_supported_runtime(
    docker_client: DockerClient,
    requested_device: str,
) -> None:
    docker_info = docker_client.client.info()
    docker_architecture = str(docker_info.get("Architecture", "")).casefold()
    host_architecture = platform.machine().casefold()
    supported_architectures = {"amd64", "x86_64"}
    if (
        docker_architecture not in supported_architectures
        or host_architecture not in supported_architectures
    ):
        pytest.skip("The Transcribe AI v1 images only support linux/amd64")

    runtimes = docker_info.get("Runtimes", {})
    if requested_device == "gpu" and "nvidia" not in runtimes:
        pytest.skip("GPU mode requires an NVIDIA GPU and the NVIDIA Container Toolkit")


def _assert_requested_device(requested_device: str, runtime_device: str) -> None:
    if runtime_device != requested_device:
        raise RuntimeError(
            "Transcribe AI did not start on the explicitly requested device"
        )


def _clean_test_resources(
    compose: DockerCompose,
    docker_client: DockerClient,
    environment: Mapping[str, str],
) -> None:
    project_name = environment["COMPOSE_PROJECT_NAME"]
    project_label = f"com.docker.compose.project={project_name}"
    cleanup_failed = False

    try:
        compose.stop()
    except Exception:
        cleanup_failed = True

    try:
        remaining_containers = docker_client.client.containers.list(
            all=True,
            filters={"label": project_label},
        )
        remaining_networks = docker_client.client.networks.list(
            filters={"label": project_label}
        )
        remaining_volumes = docker_client.client.volumes.list(
            filters={"label": project_label}
        )
        if remaining_containers or remaining_networks or remaining_volumes:
            cleanup_failed = True
    except Exception:
        cleanup_failed = True
    finally:
        docker_client.client.close()

    if cleanup_failed:
        raise RuntimeError(
            "The disposable Transcribe AI resources were not fully removed"
        )


@pytest.fixture(scope="session")
def transcribe_ai_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TranscribeAiTestEnvironment]:
    """Start one disposable Transcribe AI stack and remove all its resources."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The Transcribe AI integration suite must not use pytest-xdist")

    requested_device = _requested_device()
    environment = _new_environment()
    api_key = SecretStr(environment["API_SECRET_KEY"])
    docker_client = DockerClient()
    compose = DockerCompose(
        context=TRANSCRIBE_AI_TEST_DIRECTORY,
        compose_file_name=["compose.yml", f"compose.{requested_device}.yml"],
        pull=False,
        build=False,
        wait=False,
    )
    primary_error: BaseException | None = None

    try:
        with patch.dict(os.environ, environment, clear=False):
            _require_supported_runtime(docker_client, requested_device)
            compose.start()
            _wait_for_healthy_service(compose, docker_client, "postgres")
            _wait_for_healthy_service(compose, docker_client, "redis")
            _wait_for_healthy_service(compose, docker_client, "api")
            _wait_for_healthy_service(compose, docker_client, "mono-voice-stt")
            _wait_for_batch_worker(compose, docker_client)

            base_url = _get_loopback_base_url(compose, docker_client)
            _wait_for_api(base_url, api_key)
            runtime_device = _read_runtime_device(compose, docker_client)
            _assert_requested_device(requested_device, runtime_device)
            project_name = environment["COMPOSE_PROJECT_NAME"]
            network_name = _require_network_name(
                compose,
                docker_client,
                project_name,
            )

            audio_path = tmp_path_factory.mktemp("transcribe-ai-audio") / "fixture.wav"
            _generate_synthetic_audio(compose, docker_client, audio_path)

        yield TranscribeAiTestEnvironment(
            base_url=base_url,
            api_key=api_key,
            requested_device=requested_device,
            runtime_device=runtime_device,
            audio_path=audio_path,
            project_name=project_name,
            network_name=network_name,
            internal_base_url=TRANSCRIBE_AI_INTERNAL_BASE_URL,
            _compose=compose,
            _docker_client=docker_client,
            _compose_environment=dict(environment),
        )
    except BaseException as exception:
        primary_error = exception
        raise
    finally:
        try:
            with patch.dict(os.environ, environment, clear=False):
                _clean_test_resources(compose, docker_client, environment)
        except Exception:
            if primary_error is None:
                raise
            primary_error.add_note(
                "The Transcribe AI cleanup also failed; the primary error was kept"
            )
