"""Executable lifecycle of the OpenAI-compatible transcription simulator."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import IntEnum
from typing import Protocol

import uvicorn
from pydantic import ValidationError

from .settings import OpenAiTranscriptionSimulatorSettings
from .web import create_app

LOGGER = logging.getLogger(__name__)


class OpenAiTranscriptionSimulatorExitCode(IntEnum):
    """Stable process exit codes for container orchestration."""

    SUCCESS = 0
    UNEXPECTED_ERROR = 1
    CONFIGURATION_ERROR = 2
    INTERRUPTED = 130


class _Server(Protocol):
    def run(self) -> None:
        """Serve until a standard shutdown signal is received."""
        ...


def build_server(settings: OpenAiTranscriptionSimulatorSettings) -> uvicorn.Server:
    """Compose one single-process server without HTTP access-body logging."""
    if not isinstance(settings, OpenAiTranscriptionSimulatorSettings):
        raise TypeError("settings must be an OpenAiTranscriptionSimulatorSettings")
    config = uvicorn.Config(
        create_app(settings),
        host=str(settings.openai_transcription_simulator_host),
        port=settings.openai_transcription_simulator_port,
        log_level="info",
        access_log=False,
        proxy_headers=False,
    )
    return uvicorn.Server(config)


def run_cli(
    *,
    settings_factory: Callable[[], OpenAiTranscriptionSimulatorSettings] = (
        OpenAiTranscriptionSimulatorSettings
    ),
    server_factory: Callable[[OpenAiTranscriptionSimulatorSettings], _Server] = (
        build_server
    ),
) -> OpenAiTranscriptionSimulatorExitCode:
    """Load sanitized settings and run the simulator server."""
    try:
        settings = settings_factory()
    except ValidationError:
        LOGGER.error("OpenAI transcription simulator configuration is invalid")
        return OpenAiTranscriptionSimulatorExitCode.CONFIGURATION_ERROR
    except Exception as error:  # pragma: no cover - defensive startup boundary
        LOGGER.error(
            "OpenAI transcription simulator configuration loading failed error_type=%s",
            type(error).__name__,
        )
        return OpenAiTranscriptionSimulatorExitCode.UNEXPECTED_ERROR

    LOGGER.info("OpenAI transcription simulator starting")
    try:
        server_factory(settings).run()
    except KeyboardInterrupt:
        LOGGER.info("OpenAI transcription simulator interrupted")
        return OpenAiTranscriptionSimulatorExitCode.INTERRUPTED
    except Exception as error:  # pragma: no cover - defensive runtime boundary
        LOGGER.error(
            "OpenAI transcription simulator stopped unexpectedly error_type=%s",
            type(error).__name__,
        )
        return OpenAiTranscriptionSimulatorExitCode.UNEXPECTED_ERROR
    LOGGER.info("OpenAI transcription simulator stopped")
    return OpenAiTranscriptionSimulatorExitCode.SUCCESS


def main() -> int:
    """Console-script entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return int(run_cli())


__all__ = [
    "OpenAiTranscriptionSimulatorExitCode",
    "build_server",
    "main",
    "run_cli",
]
