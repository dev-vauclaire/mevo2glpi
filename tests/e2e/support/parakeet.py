"""Disposable Parakeet/OpenAI-compatible stack for the nominal E2E path."""

from __future__ import annotations

import base64
import gzip
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from tests.support.mvp_compose import (
    REPOSITORY_ROOT,
    TRANSCRIPTION_COMPOSE_FILE,
    clean_environment,
    run_command,
)

SYNTHETIC_AUDIO_FIXTURE = (
    REPOSITORY_ROOT
    / "providers"
    / "simulators"
    / "microsoft-graph-simulator"
    / "src"
    / "mevo2glpi_microsoft_graph_simulator"
    / "fixtures"
    / "valid-speech.wav.gz.b64"
)


@dataclass(slots=True)
class ParakeetComposeEnvironment:
    """Own one isolated CPU transcription service and its private network."""

    project_name: str
    network_name: str
    network_alias: str
    api_key: str = field(repr=False)
    environment_file: Path
    override_file: Path
    image_reference: str

    @property
    def internal_base_url(self) -> str:
        """Return the OpenAI-compatible base URL reachable by the worker."""
        return f"http://{self.network_alias}:5092/v1"

    def compose(
        self,
        *arguments: str,
        check: bool = True,
        timeout_seconds: float = 180.0,
    ):
        """Run Compose with explicit, generated configuration."""
        return run_command(
            [
                "docker",
                "compose",
                "--project-name",
                self.project_name,
                "--env-file",
                str(self.environment_file),
                "-f",
                str(TRANSCRIPTION_COMPOSE_FILE),
                "-f",
                str(self.override_file),
                *arguments,
            ],
            environment=clean_environment(),
            check=check,
            timeout_seconds=timeout_seconds,
            secrets_to_redact=(self.api_key,),
        )

    def build(self) -> None:
        """Build the pinned model image used by the release."""
        self.compose("build", "transcription-api", timeout_seconds=20 * 60)

    def start(self) -> None:
        """Create the private network and wait for model readiness."""
        run_command(
            ["docker", "network", "create", self.network_name],
            environment=clean_environment(),
            secrets_to_redact=(self.api_key,),
        )
        self.compose(
            "up",
            "--detach",
            "--wait",
            "--wait-timeout",
            "300",
            "transcription-api",
            timeout_seconds=360,
        )

    def destroy(self) -> None:
        """Remove only resources owned by this disposable provider."""
        self.compose(
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "10",
            check=False,
        )
        run_command(
            ["docker", "image", "rm", "--force", self.image_reference],
            environment=clean_environment(),
            check=False,
            secrets_to_redact=(self.api_key,),
        )
        run_command(
            ["docker", "network", "rm", self.network_name],
            environment=clean_environment(),
            check=False,
            secrets_to_redact=(self.api_key,),
        )


def create_parakeet_compose_environment(
    runtime_root: Path,
) -> ParakeetComposeEnvironment:
    """Materialize ephemeral Compose configuration with restricted permissions."""
    suffix = secrets.token_hex(5)
    project_name = f"mevo2glpiparakeet{suffix}"
    network_name = f"{project_name}-network"
    network_alias = f"{project_name}-api"
    image_reference = f"{project_name}-transcription-api"
    api_key = secrets.token_hex(32)
    environment_file = runtime_root / "parakeet.env"
    environment_file.write_text(
        "\n".join(
            (
                f"COMPOSE_PROJECT_NAME={project_name}",
                "APP_RELEASE=v0.0.0-e2e",
                f"APP_COMMIT_SHA={'a' * 40}",
                f"TRANSCRIPTION_NETWORK_NAME={network_name}",
                f"TRANSCRIPTION_NETWORK_ALIAS={network_alias}",
                f"TRANSCRIPTION_API_KEY={api_key}",
                "TRANSCRIPTION_MODEL_PATH=/models/parakeet-ultra-Q8_0.gguf",
                "TRANSCRIPTION_MODEL_NAME=parakeet-ultra",
                "TRANSCRIPTION_CPUS=2.0",
                "TRANSCRIPTION_CPU_THREADS=2",
                "TRANSCRIPTION_MEMORY_LIMIT=4g",
                "TRANSCRIPTION_LOG_LEVEL=info",
                "TRANSCRIPTION_MAX_UPLOAD_BYTES=26214400",
                "TRANSCRIPTION_MAX_AUDIO_SECONDS=600",
                "TRANSCRIPTION_FFMPEG_TIMEOUT_SECONDS=60",
                f"MEVO2GLPI_E2E_TRANSCRIPTION_IMAGE={image_reference}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o600)
    override_file = runtime_root / "parakeet.override.yaml"
    override_file.write_text(
        """services:
  transcription-api:
    image: ${MEVO2GLPI_E2E_TRANSCRIPTION_IMAGE}
    healthcheck:
      interval: 2s
      timeout: 5s
      retries: 30
      start_period: 180s
""",
        encoding="utf-8",
    )
    return ParakeetComposeEnvironment(
        project_name=project_name,
        network_name=network_name,
        network_alias=network_alias,
        api_key=api_key,
        environment_file=environment_file,
        override_file=override_file,
        image_reference=image_reference,
    )


def load_synthetic_speech() -> bytes:
    """Decode the repository-owned, synthetic French speech fixture."""
    encoded = SYNTHETIC_AUDIO_FIXTURE.read_text(encoding="ascii")
    return gzip.decompress(base64.b64decode("".join(encoded.split()), validate=True))


__all__ = [
    "ParakeetComposeEnvironment",
    "create_parakeet_compose_environment",
    "load_synthetic_speech",
]
