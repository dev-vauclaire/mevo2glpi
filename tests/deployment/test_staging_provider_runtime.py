"""Exercise real clients against the two containerized provider simulators."""

from __future__ import annotations

import os
import re
import secrets
import socket
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from uuid import UUID

import httpx
import httpx2
import pytest
from pydantic import SecretStr

from glpi_ticketing_worker_automation.transcription import (
    JobStatus,
    TranscriptionClient,
)
from glpi_ticketing_worker_publisher.graph import (
    GraphClient,
    MoveOutcome,
    SyntheticGraphTokenProvider,
)

from .conftest import (
    PROVIDERS_COMPOSE_FILE,
    PROVIDERS_ENV_FILE,
    clean_environment,
    run_command,
)

pytestmark = [
    pytest.mark.deployment,
    pytest.mark.timeout(10 * 60),
]


@dataclass(slots=True)
class StagingProviderEnvironment:
    """Own two isolated simulator containers and synthetic credentials."""

    project_name: str
    environment_file: Path
    override_file: Path
    process_environment: Mapping[str, str] = field(repr=False)
    admin_token: str = field(repr=False)
    transcription_api_key: str = field(repr=False)
    graph_host_port: int
    transcription_host_port: int
    providers_network_name: str

    def compose(
        self,
        *arguments: str,
        check: bool = True,
        timeout_seconds: float = 600,
    ):
        return run_command(
            [
                "docker",
                "compose",
                "--project-name",
                self.project_name,
                "--env-file",
                str(self.environment_file),
                "--file",
                str(PROVIDERS_COMPOSE_FILE),
                "--file",
                str(self.override_file),
                *arguments,
            ],
            environment=self.process_environment,
            check=check,
            timeout_seconds=timeout_seconds,
        )


def _read_example_environment() -> dict[str, str]:
    source = PROVIDERS_ENV_FILE.read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", source, re.MULTILINE))


def _write_private_environment(path: Path, values: Mapping[str, str]) -> None:
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in sorted(values.items())),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _docker_network(command: str, network_name: str, *, check: bool = True) -> None:
    arguments = ["docker", "network", command]
    if command == "create":
        arguments.append("--internal")
    arguments.append(network_name)
    run_command(
        arguments,
        environment=clean_environment(),
        check=check,
    )


@pytest.fixture(scope="session")
def staging_provider_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[StagingProviderEnvironment]:
    """Build, start and remove two loopback-only provider simulators."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        pytest.fail("The staging provider suite must not run with pytest-xdist")

    suffix = secrets.token_hex(6)
    project_name = f"gta-staging-provider-test-{suffix}"
    runtime_root = tmp_path_factory.mktemp("gta-staging-provider")
    environment_file = runtime_root / "compose.env"
    override_file = runtime_root / "compose.override.yml"
    admin_token = secrets.token_urlsafe(32)
    transcription_api_key = secrets.token_urlsafe(32)
    graph_host_port = _reserve_loopback_port()
    transcription_host_port = _reserve_loopback_port()
    while transcription_host_port == graph_host_port:
        transcription_host_port = _reserve_loopback_port()
    providers_network_name = f"{project_name}-providers"
    values = _read_example_environment()
    values.update(
        {
            "COMPOSE_PROJECT_NAME": project_name,
            "PROVIDERS_NETWORK_NAME": providers_network_name,
            "MICROSOFT_GRAPH_SIMULATOR_ADMIN_TOKEN": admin_token,
            "TRANSCRIBE_AI_SIMULATOR_API_KEY": transcription_api_key,
        }
    )
    _write_private_environment(environment_file, values)
    override_file.write_text(
        "services:\n"
        "  microsoft-graph-simulator:\n"
        "    ports:\n"
        f'      - "127.0.0.1:{graph_host_port}:8080"\n'
        "  transcribe-ai-simulator:\n"
        "    ports:\n"
        f'      - "127.0.0.1:{transcription_host_port}:8080"\n',
        encoding="utf-8",
    )
    process_environment = clean_environment()
    environment = StagingProviderEnvironment(
        project_name=project_name,
        environment_file=environment_file,
        override_file=override_file,
        process_environment=process_environment,
        admin_token=admin_token,
        transcription_api_key=transcription_api_key,
        graph_host_port=graph_host_port,
        transcription_host_port=transcription_host_port,
        providers_network_name=providers_network_name,
    )

    try:
        _docker_network("create", providers_network_name)
        environment.compose(
            "build",
            "microsoft-graph-simulator",
            "transcribe-ai-simulator",
        )
        environment.compose(
            "up",
            "--detach",
            "--wait",
            "microsoft-graph-simulator",
            "transcribe-ai-simulator",
        )
        yield environment
    finally:
        environment.compose(
            "down",
            "--volumes",
            "--remove-orphans",
            check=False,
        )
        _docker_network("rm", providers_network_name, check=False)
        for image_name in (
            f"{project_name}-microsoft-graph-simulator:0.1.0",
            f"{project_name}-transcribe-ai-simulator:0.1.0",
        ):
            run_command(
                ["docker", "image", "rm", image_name],
                environment=clean_environment(),
                check=False,
            )


def _graph_base_url(environment: StagingProviderEnvironment) -> str:
    return f"http://127.0.0.1:{environment.graph_host_port}"


def _transcription_base_url(environment: StagingProviderEnvironment) -> str:
    return f"http://127.0.0.1:{environment.transcription_host_port}"


def test_real_graph_client_uses_the_microsoft_graph_simulator(
    staging_provider_environment: StagingProviderEnvironment,
) -> None:
    base_url = _graph_base_url(staging_provider_environment)
    mailbox_id = "00000000-0000-4000-8000-000000000003"
    with httpx.Client() as admin_client:
        seeded = admin_client.post(
            f"{base_url}/__admin/messages",
            headers={"X-Admin-Token": staging_provider_environment.admin_token},
            json={"extension": "ogg"},
            timeout=5,
        )
    assert seeded.status_code == httpx.codes.CREATED

    with httpx2.Client() as graph_http_client:
        graph = GraphClient(
            mailbox_id=UUID(mailbox_id),
            http_client=graph_http_client,
            token_provider=SyntheticGraphTokenProvider(),
            timeout=5,
            base_url=f"{base_url}/v1.0",
        )
        messages = graph.list_messages()
        assert len(messages) == 1
        attachments = graph.list_attachments(messages[0].message_id)
        assert len(attachments) == 1
        audio = BytesIO()
        download = graph.download_attachment(
            messages[0].message_id,
            attachments[0].attachment_id,
            audio,
        )
        processed = graph.ensure_mail_folder("Processed")
        move = graph.ensure_message_in_folder(
            messages[0].message_id,
            processed.folder_id,
        )
        assert graph.list_messages() == ()

    assert download.size_bytes == len(audio.getvalue())
    assert audio.getvalue().startswith(b"OggS")
    assert move.outcome is MoveOutcome.MOVED


def test_real_transcription_client_uses_the_transcribe_ai_simulator(
    staging_provider_environment: StagingProviderEnvironment,
    tmp_path: Path,
) -> None:
    base_url = _transcription_base_url(staging_provider_environment)

    audio_path = tmp_path / "synthetic.ogg"
    audio_path.write_bytes(b"OggS-synthetic-provider-contract")
    with httpx.Client() as transcription_http_client:
        transcription = TranscriptionClient(
            base_url=base_url,
            api_key=SecretStr(staging_provider_environment.transcription_api_key),
            http_client=transcription_http_client,
            timeout=5,
        )
        submission = transcription.submit(audio_path)
        pending = transcription.get_job(submission.job_uuid)
        completed = transcription.get_job(submission.job_uuid)

    assert pending.status is JobStatus.PENDING
    assert completed.status is JobStatus.COMPLETED
    assert completed.transcription == "Transcription synthétique de recette staging."
